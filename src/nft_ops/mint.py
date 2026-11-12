"""Layer B (execution readiness), previews, and parallel mint operations.

Each selected wallet runs the vendored osnm-z engine (execute_mint_phase) in its own asyncio
task, with its own OpenSea session, sharing one verified RPC gateway per operation. The
pre-broadcast gate from safety.py runs inside every task right before its transaction is sent.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from osnm_z.chain import ChainGateway
from osnm_z.config import (
    GWEI_IN_WEI,
    AppConfig,
    FeesConfig,
    OpenSeaConfig,
    RetryConfig,
    SchedulingConfig,
    parse_gwei,
)
from osnm_z.errors import MintError
from osnm_z.execution import MintExecutionContext, execute_mint_phase
from osnm_z.fee import configured_fee_estimate, initial_transaction_fees
from osnm_z.hooks import LOG_SINK, PRE_BROADCAST_GATE, PreBroadcastCheck
from osnm_z.launch_timing import ensure_mint_not_expired, is_future_phase
from osnm_z.opensea_protocol import CollectionMetadata, ProtocolError, StageMetadata
from osnm_z.outcomes import ExecutionState
from osnm_z.phase_selection import (
    public_only_eligibility,
    stage_name,
    stage_window,
    validate_mint_limits,
    validate_snapshot_shape,
)
from osnm_z.public_mint import (
    PublicMintError,
    build_public_mint_action,
    discover_seadrop_address,
    resolve_public_mint_context_and_stats,
)
from osnm_z.rpc_protocol import ChainError
from osnm_z.scheduling import SystemUtcClock

from . import collections
from .collections import OpenSeaFactory
from .db import Database
from .events import EventBus
from .networks import RpcError, RpcPool
from .safety import GateBlocked, OperationBudget, Safety, from_wei, to_wei
from .vault import Vault

PREVIEW_TTL_SECONDS = 180
PRE_BROADCAST = {"queued", "preparing", "waiting"}
GatewayFactory = Callable[[float, int], Any]
DEFAULT_MINT_SETTINGS = {
    "gas_limit": 300000,
    "fee_mode": "auto",
    "max_fee_gwei": "",
    "priority_fee_gwei": "",
    "public_offset_ms": 0,
    "setup_concurrency": 5,
    "pending_timeout_seconds": 60,
}


class MintEngineError(Exception):
    pass


@dataclass(slots=True)
class MintParams:
    slug: str
    stage_index: int
    addresses: list[str]
    quantity: int = 1
    gas_limit: int = 300000
    fee_mode: str = "auto"
    max_fee_gwei: str = ""
    priority_fee_gwei: str = ""
    public_offset_ms: int = 0
    operation_cap: str = ""            # optional per-operation cap in native token
    pending_timeout_seconds: int = 60

    def fees(self) -> FeesConfig:
        if self.fee_mode == "auto":
            return FeesConfig.automatic_mode()
        if self.fee_mode != "manual":
            raise MintEngineError("fee mode must be auto or manual")
        max_fee = parse_gwei(self.max_fee_gwei, "max fee")
        priority = parse_gwei(self.priority_fee_gwei, "priority fee")
        if max_fee == 0 or max_fee < priority:
            raise MintEngineError("manual max fee must be above zero and cover the priority fee")
        return FeesConfig.manual(max_fee, priority)

    def validate(self) -> None:
        if not self.addresses:
            raise MintEngineError("select at least one wallet")
        if not 1 <= self.quantity <= 1000:
            raise MintEngineError("quantity must be between 1 and 1000")
        if not 21_000 <= self.gas_limit <= 30_000_000:
            raise MintEngineError("gas limit must be between 21000 and 30000000")
        if not 0 <= self.public_offset_ms <= 60_000:
            raise MintEngineError("public broadcast offset must be 0-60000 ms")
        if not 1 <= self.pending_timeout_seconds <= 3600:
            raise MintEngineError("receipt timeout must be 1-3600 seconds")
        self.fees()

    def app_config(self, rpc_url: str) -> AppConfig:
        config = AppConfig(
            opensea=OpenSeaConfig(),
            scheduling=SchedulingConfig(public_mint_broadcast_offset_ms=self.public_offset_ms),
            retry=RetryConfig(pending_timeout_seconds=self.pending_timeout_seconds),
            fees=self.fees(),
            rpc_url=rpc_url,
            gas_limit=self.gas_limit,
        )
        config.validate()
        return config


@dataclass(slots=True)
class Preview:
    token: str
    params: MintParams
    metadata: CollectionMetadata
    stage: StageMetadata
    rpc_url: str
    ready: list[str]
    cap_wei: int
    expires_at: float


@dataclass(slots=True)
class WalletRun:
    address: str
    label: str
    history_id: int
    job_id: int
    phase: str = "queued"
    tx_hash: str | None = None
    message: str = ""
    cancelled: bool = False
    task: asyncio.Task | None = None
    logs: deque = field(default_factory=lambda: deque(maxlen=200))

    def summary(self) -> dict:
        return {"address": self.address, "label": self.label, "phase": self.phase,
                "tx_hash": self.tx_hash, "message": self.message, "logs": list(self.logs)[-30:]}


@dataclass(slots=True)
class MintJob:
    id: int
    preview: Preview
    budget: OperationBudget
    runs: dict[str, WalletRun]
    status: str = "running"
    created_at: float = field(default_factory=time.time)
    task: asyncio.Task | None = None

    def summary(self) -> dict:
        stage = self.preview.stage
        return {
            "id": self.id, "status": self.status, "slug": self.preview.metadata.slug,
            "stage": stage_name(stage), "stage_type": stage.stage_type,
            "chain_id": self.preview.metadata.network_id,
            "starts_at": stage_window(stage).starts_at, "quantity": self.preview.params.quantity,
            "created_at": self.created_at,
            "reserved": from_wei(self.budget.reserved_wei), "cap": from_wei(self.budget.cap_wei),
            "runs": [r.summary() for r in self.runs.values()],
        }


class MintEngine:
    def __init__(self, db: Database, vault: Vault, pool: RpcPool, bus: EventBus, safety: Safety,
                 opensea_factory: OpenSeaFactory, gateway_factory: GatewayFactory | None = None
                 ) -> None:
        self.db, self.vault, self.pool, self.bus, self.safety = db, vault, pool, bus, safety
        self.opensea_factory = opensea_factory
        self.gateway_factory = gateway_factory or (
            lambda timeout, conns: ChainGateway(timeout, max_connections=conns))
        self.jobs: dict[int, MintJob] = {}
        self.previews: dict[str, Preview] = {}
        self.active_wallets: dict[str, int] = {}
        self._pending_trackers: set[asyncio.Task] = set()
        safety.on_stop(self._cancel_pre_broadcast)

    # ===== preview (Layer B: execution readiness) =======================================
    async def preview(self, params: MintParams) -> dict:
        params.validate()
        metadata = await collections.load(self.db, self.opensea_factory, params.slug, None)
        stage = next((s for s in metadata.stages if s.stage_index == params.stage_index), None)
        if stage is None:
            raise MintEngineError("that phase no longer exists on OpenSea")
        chain_id = metadata.network_id
        rpc_url = await self.pool.best_url(chain_id)
        is_public = stage.stage_type == "PUBLIC_SALE"
        window = stage_window(stage)
        now = int(time.time())
        ended = window.ends_at is not None and now >= window.ends_at
        scheduled = window.starts_at > now
        fees_cfg = params.fees()
        notes: list[str] = []

        async with self.gateway_factory(10.0, 16) as gateway:
            chain = await gateway.prepare_chain(rpc_url)
            if chain.chain_id != chain_id:
                raise MintEngineError(f"RPC is on chain {chain.chain_id}, collection is on {chain_id}")
            snapshot = await gateway.wallet_snapshot(chain, params.addresses[0],
                                                     configured_fee_estimate(fees_cfg))
            fees = initial_transaction_fees(fees_cfg, snapshot.submission_inputs.fee_estimate,
                                            scheduled=scheduled)
            balances = await self.pool.batch(chain_id, [
                ("eth_getBalance", [a, "pending"]) for a in params.addresses])
            eligibility = {r["address"]: r for r in self.db.all(
                "SELECT * FROM eligibility WHERE slug = ? AND stage_index = ?",
                (metadata.slug, stage.stage_index))}
            labels = {r["address"]: r["label"] for r in self.db.all("SELECT address, label FROM wallets")}
            gas_cost_max = params.gas_limit * fees.max_fee_per_gas

            rows = []
            for address, balance_raw in zip(params.addresses, balances, strict=True):
                row = eligibility.get(address)
                price = int(row["price_wei"]) if row and row["price_wei"] is not None else (
                    stage.native_price_wei or 0)
                value = price * params.quantity
                status, reason = self._readiness(
                    address, stage, metadata, row, params.quantity, value, gas_cost_max,
                    balance_raw, ended)
                rows.append({
                    "address": address, "label": labels.get(address, ""),
                    "eligibility": "ELIGIBLE" if is_public else (row["status"] if row else "NOT SCANNED"),
                    "max_quantity": row["max_quantity"] if row else stage.max_total_mintable_by_wallet,
                    "price": from_wei(price), "value": from_wei(value),
                    "max_gas_cost": from_wei(gas_cost_max), "worst_case": from_wei(value + gas_cost_max),
                    "balance": None if isinstance(balance_raw, RpcError) else from_wei(int(balance_raw, 16)),
                    "status": status, "reason": reason, "simulation": "",
                    "_worst": value + gas_cost_max,
                })

            if is_public and not scheduled and not ended:
                await self._simulate_public(gateway, chain, metadata, stage, params, rows)
            elif not is_public:
                notes.append("Allowlist mint data is issued by OpenSea at mint time, so these "
                             "wallets are simulated by OpenSea's signed response at launch, not now.")
            elif scheduled:
                notes.append("Phase has not opened yet; on-chain simulation runs only once it is active.")

        ready = [r for r in rows if r["status"] == "READY"]
        total_worst = sum(r["_worst"] for r in ready)
        limits = self.safety.limits_wei()
        cap = limits["max_total_per_operation"]
        if params.operation_cap.strip():
            cap = min(cap, to_wei(params.operation_cap))
        daily_used = self.safety.daily_spent_wei(chain_id)
        blocks = []
        if self.safety.stopped:
            blocks.append("Emergency STOP is active. Resume first.")
        if not ready:
            blocks.append("No wallet is ready to mint.")
        if total_worst > cap:
            blocks.append(f"Worst-case total {from_wei(total_worst)} is over the operation limit "
                          f"{from_wei(cap)}. Raise the limit or select fewer wallets.")
        if daily_used + total_worst > limits["max_daily"]:
            blocks.append(f"24h spend would reach {from_wei(daily_used + total_worst)}, over the "
                          f"daily limit {from_wei(limits['max_daily'])}.")
        token = secrets.token_urlsafe(16)
        self._expire_previews()
        if not blocks:
            self.previews[token] = Preview(token, params, metadata, stage, rpc_url,
                                          [r["address"] for r in ready], cap,
                                          time.time() + PREVIEW_TTL_SECONDS)
        for r in rows:
            r.pop("_worst")
        return {
            "token": None if blocks else token,
            "expires_in": PREVIEW_TTL_SECONDS,
            "collection": collections.describe(metadata),
            "stage": {"index": stage.stage_index, "name": stage_name(stage), "type": stage.stage_type,
                      "starts_at": window.starts_at, "ends_at": window.ends_at,
                      "state": "ended" if ended else "upcoming" if scheduled else "active"},
            "fees": {"max_fee_gwei": f"{fees.max_fee_per_gas / GWEI_IN_WEI:.4f}",
                     "priority_fee_gwei": f"{fees.max_priority_fee_per_gas / GWEI_IN_WEI:.4f}",
                     "mode": params.fee_mode, "gas_limit": params.gas_limit},
            "rows": rows,
            "totals": {"ready": len(ready), "selected": len(rows),
                       "value": from_wei(sum(to_wei(r["value"]) for r in ready)),
                       "max_gas": from_wei(sum(to_wei(r["max_gas_cost"]) for r in ready)),
                       "worst_case": from_wei(total_worst), "operation_cap": from_wei(cap),
                       "daily_used": from_wei(daily_used),
                       "daily_limit": from_wei(limits["max_daily"])},
            "blocks": blocks,
            "notes": notes,
        }

    def _readiness(self, address: str, stage: StageMetadata, metadata: CollectionMetadata,
                   row: dict | None, quantity: int, value: int, gas_cost_max: int,
                   balance_raw: Any, ended: bool) -> tuple[str, str]:
        is_public = stage.stage_type == "PUBLIC_SALE"
        if not self.vault.has(address):
            return "NO_KEY", "watch-only wallet; import its key to mint"
        if address in self.active_wallets:
            return "ALREADY_RUNNING", f"already in operation #{self.active_wallets[address]}"
        if ended:
            return "PHASE_NOT_ACTIVE", "phase has ended"
        if not is_public and (metadata.is_minted_out or metadata.is_disabled):
            return "SOLD_OUT", "OpenSea reports the drop is minted out or disabled"
        if not is_public:
            if row is None:
                return "ELIGIBILITY_UNKNOWN", "run the eligibility scan for this collection first"
            if row["status"] == "NOT_ELIGIBLE":
                return "NOT_ELIGIBLE", row["detail"]
            if row["status"] != "ELIGIBLE":
                return "ELIGIBILITY_UNKNOWN", row["detail"] or row["status"]
        limit = row["max_quantity"] if row else stage.max_total_mintable_by_wallet
        if limit is not None and quantity > limit:
            return "ALLOCATION_EXCEEDED", (f"wallet can mint {limit} more" if limit
                                           else "allocation used up")
        if isinstance(balance_raw, RpcError):
            return "RPC_ERROR", f"balance check failed: {balance_raw}"
        balance = int(balance_raw, 16)
        if balance < value:
            return "INSUFFICIENT_BALANCE", f"needs {from_wei(value)} for the mint, has {from_wei(balance)}"
        if balance < value + gas_cost_max:
            return "INSUFFICIENT_GAS", (f"needs {from_wei(value + gas_cost_max)} incl. max gas, "
                                        f"has {from_wei(balance)}")
        problems = self.safety.per_tx_problems(value, gas_cost_max)
        if problems:
            return "LIMIT_EXCEEDED", "; ".join(problems)
        return "READY", "ready to mint"

    async def _simulate_public(self, gateway: Any, chain: Any, metadata: CollectionMetadata,
                               stage: StageMetadata, params: MintParams, rows: list[dict]) -> None:
        client = self.opensea_factory()
        try:
            token = LOG_SINK.set(lambda *_: None)
            try:
                target = await discover_seadrop_address(client, gateway, chain, metadata)
            finally:
                LOG_SINK.reset(token)
        finally:
            await client.aclose()
        semaphore = asyncio.Semaphore(8)

        async def simulate(row: dict) -> None:
            async with semaphore:
                try:
                    context, stats = await resolve_public_mint_context_and_stats(
                        gateway, chain, metadata, row["address"], target, time.time_ns())
                    action = build_public_mint_action(metadata, stage, context, stats,
                                                      row["address"], params.quantity)
                    estimate = await self.pool.call(metadata.network_id, "eth_estimateGas", [{
                        "from": row["address"], "to": action.target, "value": hex(action.value),
                        "data": "0x" + action.calldata.hex()}])
                    gas = int(estimate, 16)
                    row["simulation"] = f"ok, ~{gas:,} gas"
                    if gas > params.gas_limit:
                        row["status"], row["reason"] = "GAS_LIMIT_TOO_LOW", (
                            f"simulation needs ~{gas:,} gas; gas limit is {params.gas_limit:,}")
                except (PublicMintError, ChainError, MintError, RpcError) as error:
                    row["status"], row["reason"] = "SIMULATION_FAILED", str(error)[:200]
                    row["simulation"] = "failed"

        await asyncio.gather(*(simulate(r) for r in rows if r["status"] == "READY"))

    def _expire_previews(self) -> None:
        now = time.time()
        for token in [t for t, p in self.previews.items() if p.expires_at < now]:
            del self.previews[token]

    # ===== start ==========================================================================
    async def start(self, token: str, confirmation: str) -> int:
        if confirmation.strip().upper() != "MINT":
            raise MintEngineError("type MINT to confirm")
        self._expire_previews()
        preview = self.previews.pop(token, None)
        if preview is None:
            raise MintEngineError("preview expired or already used; preview again")
        with self.safety.lock:
            if self.safety.stopped:
                raise MintEngineError("emergency STOP is active")
            busy = [a for a in preview.ready if a in self.active_wallets]
            if busy:
                raise MintEngineError(f"{busy[0]} is already running in another operation")
            cur = self.db.execute(
                "INSERT INTO jobs(kind, slug, stage_index, stage_name, chain_id, params, status, "
                "created_at) VALUES('mint', ?, ?, ?, ?, ?, 'running', ?)",
                (preview.metadata.slug, preview.stage.stage_index, stage_name(preview.stage),
                 preview.metadata.network_id,
                 json.dumps({"quantity": preview.params.quantity, "gas_limit": preview.params.gas_limit,
                             "fee_mode": preview.params.fee_mode, "wallets": len(preview.ready)}),
                 time.time()))
            job_id = int(cur.lastrowid)
            labels = {r["address"]: r["label"] for r in self.db.all("SELECT address, label FROM wallets")}
            runs = {}
            for address in preview.ready:
                hid = self.db.add_history(
                    job_id=job_id, operation="mint", chain_id=preview.metadata.network_id,
                    address=address, slug=preview.metadata.slug, stage_name=stage_name(preview.stage),
                    quantity=preview.params.quantity, status="queued", recipient=address)
                runs[address] = WalletRun(address, labels.get(address, ""), hid, job_id)
                self.active_wallets[address] = job_id
            job = MintJob(job_id, preview,
                          OperationBudget(job_id, preview.metadata.network_id, preview.cap_wei), runs)
            self.jobs[job_id] = job
        job.task = asyncio.create_task(self._run_job(job))
        self.bus.publish("job", job=job.summary())
        return job_id

    # ===== execution ======================================================================
    async def _run_job(self, job: MintJob) -> None:
        preview = job.preview
        try:
            async with self.gateway_factory(10.0, min(128, max(8, 2 * len(job.runs)))) as gateway:
                chain = await gateway.prepare_chain(preview.rpc_url)
                if chain.chain_id != preview.metadata.network_id:
                    raise MintEngineError("RPC chain changed since the preview")
                public_target = None
                if preview.stage.stage_type == "PUBLIC_SALE":
                    client = self.opensea_factory()
                    try:
                        public_target = await discover_seadrop_address(
                            client, gateway, chain, preview.metadata)
                    finally:
                        await client.aclose()
                setup = asyncio.Semaphore(int(self.db.get_setting("mint", {}).get(
                    "setup_concurrency", DEFAULT_MINT_SETTINGS["setup_concurrency"])))
                for run in job.runs.values():
                    run.task = asyncio.create_task(
                        self._run_wallet(job, run, gateway, chain, public_target, setup))
                await asyncio.gather(*(r.task for r in job.runs.values()), return_exceptions=True)
        except Exception as error:  # noqa: BLE001 - surface any setup failure on every run
            for run in job.runs.values():
                if run.phase in PRE_BROADCAST:
                    self._finish(run, "failed", f"operation setup failed: {error}")
        finally:
            job.status = "stopped" if self.safety.stopped else "finished"
            self.db.execute("UPDATE jobs SET status = ?, finished_at = ? WHERE id = ?",
                            (job.status, time.time(), job.id))
            for address in job.runs:
                if self.active_wallets.get(address) == job.id:
                    self.active_wallets.pop(address, None)
            self.bus.publish("job", job=job.summary())

    async def _run_wallet(self, job: MintJob, run: WalletRun, gateway: Any, chain: Any,
                          public_target: str | None, setup: asyncio.Semaphore) -> None:
        preview = job.preview
        metadata, stage, params = preview.metadata, preview.stage, preview.params
        is_public = stage.stage_type == "PUBLIC_SALE"

        def sink(level: str, message: str, countdown: int | None) -> None:
            if level != "progress":
                run.logs.append({"t": time.time(), "level": level, "msg": message})
            if countdown is not None and countdown > time.time_ns():
                self._set_phase(run, "waiting", message)
            self.bus.publish("mint_log", job=job.id, address=run.address, level=level,
                             msg=message, countdown=countdown)

        def on_pass(check: PreBroadcastCheck) -> None:
            run.phase, run.tx_hash = "broadcasting", check.transaction_hash
            self.db.update_history(run.history_id, status="submitted", tx_hash=check.transaction_hash,
                                   nonce=check.nonce, value_wei=str(check.value_wei),
                                   max_cost_wei=str(check.worst_case_cost_wei))
            self.bus.publish("run", job=job.id, run=run.summary())

        LOG_SINK.set(sink)
        PRE_BROADCAST_GATE.set(self.safety.make_gate(job.budget, run.address, on_pass,
                                                     lambda: run.cancelled))
        client = self.opensea_factory()
        try:
            self._set_phase(run, "preparing", "signing in and checking eligibility")
            signer = self.vault.signer(run.address)
            async with setup:
                if is_public:
                    eligibility = public_only_eligibility(metadata)
                else:
                    await client.authenticate(signer, run.address, chain.chain_id, metadata.slug)
                    eligibility = await client.eligibility(metadata.slug, run.address)
                    validate_snapshot_shape(metadata, eligibility)
                validate_mint_limits(metadata, stage, eligibility, params.quantity)
            phase = stage_window(stage)
            if not is_public:
                ensure_mint_not_expired(None, phase.ends_at)
            clock = SystemUtcClock()
            self._set_phase(run, "waiting", "prepared; waiting for launch")
            result = await execute_mint_phase(MintExecutionContext(
                config=params.app_config(preview.rpc_url), chain=chain, gateway=gateway,
                client=client, signer=signer, metadata=metadata, eligibility=eligibility,
                selected_stage=stage, phase=phase,
                is_scheduled=is_future_phase(phase.starts_at, clock), quantity=params.quantity,
                gas_limit=params.gas_limit, public_mint_target=public_target, system_clock=clock))
            if result.state is ExecutionState.COMPLETED:
                gas_cost, gas_used = await self._receipt_cost(chain.chain_id, run.tx_hash)
                self._finish(run, "confirmed", "minted", gas_used=gas_used, gas_cost_wei=gas_cost)
            else:
                self._finish(run, "pending", "submitted; waiting for confirmation (not resent)")
                self._track_later(run, chain.chain_id)
        except asyncio.CancelledError:
            if run.tx_hash:
                self._finish(run, "pending", "STOP pressed after broadcast; still tracking")
                self._track_later(run, chain.chain_id)
            else:
                self._finish(run, "cancelled", "cancelled before anything was sent")
        except GateBlocked as error:
            self._finish(run, "blocked", str(error))
        except (MintError, ProtocolError, ChainError, Exception) as error:  # noqa: BLE001
            if run.tx_hash:
                gas_cost, gas_used = await self._receipt_cost(chain.chain_id, run.tx_hash)
                self._finish(run, "failed", str(error)[:300], gas_used=gas_used,
                             gas_cost_wei=gas_cost)
            else:
                self._finish(run, "not_submitted", str(error)[:300])
        finally:
            await client.aclose()

    def _set_phase(self, run: WalletRun, phase: str, message: str) -> None:
        if run.phase in PRE_BROADCAST and phase in PRE_BROADCAST:
            if run.phase == phase and run.message == message:
                return
            run.phase, run.message = phase, message
            self.db.update_history(run.history_id, status=phase)
            self.bus.publish("run", job=run.job_id, run=run.summary())

    def _finish(self, run: WalletRun, status: str, message: str, **fields: Any) -> None:
        run.phase, run.message = status, message
        self.db.update_history(run.history_id, status=status, error=None if status in (
            "confirmed", "pending") else message, **{k: v for k, v in fields.items() if v is not None})
        self.bus.publish("run", job=run.job_id, run=run.summary())

    async def _receipt_cost(self, chain_id: int, tx_hash: str | None) -> tuple[str | None, int | None]:
        if not tx_hash:
            return None, None
        try:
            receipt = await self.pool.call(chain_id, "eth_getTransactionReceipt", [tx_hash])
        except RpcError:
            return None, None
        if not receipt:
            return None, None
        gas_used = int(receipt["gasUsed"], 16)
        price = int(receipt.get("effectiveGasPrice") or "0x0", 16)
        return str(gas_used * price), gas_used

    def _track_later(self, run: WalletRun, chain_id: int) -> None:
        async def track() -> None:
            deadline = time.time() + 900
            while time.time() < deadline:
                await asyncio.sleep(5)
                try:
                    receipt = await self.pool.call(chain_id, "eth_getTransactionReceipt", [run.tx_hash])
                except RpcError:
                    continue
                if receipt:
                    gas_used = int(receipt["gasUsed"], 16)
                    cost = str(gas_used * int(receipt.get("effectiveGasPrice") or "0x0", 16))
                    ok = receipt.get("status") == "0x1"
                    self._finish(run, "confirmed" if ok else "failed",
                                 "confirmed later" if ok else "transaction reverted",
                                 gas_used=gas_used, gas_cost_wei=cost)
                    return
            run.message = "still unconfirmed after 15 minutes; check the explorer"
        task = asyncio.create_task(track())
        self._pending_trackers.add(task)
        task.add_done_callback(self._pending_trackers.discard)

    # ===== STOP ===========================================================================
    def _cancel_pre_broadcast(self) -> None:
        """Called by Safety.stop() while holding the safety lock."""
        for job in self.jobs.values():
            for run in job.runs.values():
                if run.phase in PRE_BROADCAST and run.task and not run.task.done():
                    run.cancelled = True
                    run.task.cancel()

    def list_jobs(self) -> list[dict]:
        return [j.summary() for j in sorted(self.jobs.values(), key=lambda j: -j.id)]
