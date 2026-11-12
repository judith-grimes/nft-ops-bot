"""Shared engine for sweeps, distributions and NFT transfers.

Flow: a planner produces TxItems -> preview() simulates every item with eth_estimateGas,
prices gas, checks balances, destinations and limits, and returns a single-use token ->
start(token, "SEND") executes exactly the previewed items.

Execution: wallets run in parallel; each wallet's items are sent in order with consecutive
nonces. Immediately before each send, the gate re-checks the emergency STOP and the spending
limits under the safety lock. Signing uses the reviewed osnm-z transaction code.
"""

from __future__ import annotations

import asyncio
import json
import math
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from eth_utils.address import to_checksum_address

from osnm_z.rpc_protocol import decode_fee_inputs
from osnm_z.transaction import Eip1559Transaction, sign_eip1559_transaction

from .db import Database
from .events import EventBus
from .networks import RpcError, RpcPool
from .safety import GateBlocked, Safety, from_wei, to_wei
from .vault import Vault

PREVIEW_TTL_SECONDS = 300
GAS_BUFFER = 1.2
FEE_MULTIPLIER = 1.25          # same headroom osnm-z uses for an immediate send
RECEIPT_TIMEOUT_SECONDS = 180
CONFIRM_WORD = "SEND"
CONFIRM_WORDS = {"custom_mint": "MINT"}
KIND_LABELS = {"sweep": "Fund sweep", "distribute": "Fund distribution",
               "nft_transfer": "NFT transfer", "custom_mint": "Custom mint"}
PREFETCH_SECONDS = 3.0
SALE_POLL_SECONDS = 0.25       # how often to look for a new block while waiting for a sale         # scheduled sends refresh nonce and balance this long before launch


class TxEngineError(Exception):
    pass


@dataclass(slots=True)
class TxItem:
    kind: str                 # sweep | distribute | nft_transfer
    source: str
    target: str               # transaction "to" (recipient, or token contract)
    recipient: str            # who receives the asset
    asset: str                # 'native' | token address | 'erc721:<c>' | 'erc1155:<c>'
    asset_label: str          # e.g. "ETH", "USDC", "Crumbrot #12"
    amount: str               # human-readable amount
    value: int = 0            # native wei sent with the tx
    data: bytes = b""
    token_id: str | None = None
    sweep_max: bool = False   # native sweep: value = balance - reserve - gas, set in preview
    reserve_wei: int = 0
    external: bool = False    # recipient is not one of your wallets
    verify: tuple | None = None   # ('erc721', c, id) | ('erc1155', c, id, n) | ('minted', c, qty)
    scheduled_at: float | None = None   # unix time to send at (custom mints)
    resimulate: bool = True             # re-run eth_estimateGas right before a scheduled send
    fixed_gas: int = 0                  # user gas limit; used when the sale is not open yet
    shortfall: int = 0                  # native coin the wallet is missing (set in preview)
    wait_for_open: float = 0.0          # >0: send when the sale opens; max wait in seconds
    gas_limit: int = 0
    status: str = "READY"
    reason: str = ""

    def native_out(self) -> int:
        return self.value

    def spend(self) -> int:
        return self.value if self.external else 0

    def public(self, max_fee: int) -> dict:
        return {
            "source": self.source, "recipient": self.recipient, "asset": self.asset_label,
            "amount": self.amount, "token_id": self.token_id, "external": self.external,
            "gas_limit": self.gas_limit, "max_gas_cost": from_wei(self.gas_limit * max_fee),
            "status": self.status, "reason": self.reason,
            "shortfall": from_wei(self.shortfall) if self.shortfall else None,
        }


@dataclass(slots=True)
class TxPreview:
    token: str
    kind: str
    chain_id: int
    rpc_url: str
    items: list[TxItem]
    max_fee: int
    priority_fee: int
    move_cap: int
    expires_at: float
    title: str


@dataclass(slots=True)
class TxRun:
    item: TxItem
    history_id: int
    phase: str = "queued"
    tx_hash: str | None = None
    message: str = ""

    def summary(self) -> dict:
        return {**self.item.public(0), "phase": self.phase, "tx_hash": self.tx_hash,
                "message": self.message}


@dataclass(slots=True)
class TxJob:
    id: int
    preview: TxPreview
    runs: list[TxRun]
    moved_wei: int = 0
    status: str = "running"
    created_at: float = field(default_factory=time.time)
    task: asyncio.Task | None = None
    tasks: list[asyncio.Task] = field(default_factory=list)
    open_event: asyncio.Event | None = None     # set when the sale-open watcher finishes
    open_state: str = ""                        # opened | timeout | stopped

    def summary(self) -> dict:
        return {"id": self.id, "kind": self.preview.kind, "title": self.preview.title,
                "open_state": self.open_state,
                "status": self.status, "chain_id": self.preview.chain_id,
                "created_at": self.created_at, "runs": [r.summary() for r in self.runs]}


class TxEngine:
    def __init__(self, db: Database, vault: Vault, pool: RpcPool, bus: EventBus,
                 safety: Safety) -> None:
        self.db, self.vault, self.pool, self.bus, self.safety = db, vault, pool, bus, safety
        self.previews: dict[str, TxPreview] = {}
        self.jobs: dict[int, TxJob] = {}
        self.busy_sources: dict[str, int] = {}
        self.external_busy = lambda address: None   # set by the app: is it minting right now?
        # STOP needs no task cancellation here: every send passes _gate(), which checks STOP
        # under the safety lock, so nothing new goes out and nothing in flight is orphaned.

    # ===== fees ===========================================================================
    async def fees(self, chain_id: int) -> tuple[int, int]:
        block, priority = await self.pool.batch(chain_id, [
            ("eth_getBlockByNumber", ["latest", False]), ("eth_maxPriorityFeePerGas", [])])
        if isinstance(block, RpcError) or isinstance(priority, RpcError):
            raise TxEngineError("could not read network fees")
        estimate = decode_fee_inputs(block, priority)
        max_fee = math.ceil(estimate.max_fee_per_gas * FEE_MULTIPLIER)
        prio = math.ceil(estimate.max_priority_fee_per_gas * FEE_MULTIPLIER)
        return max_fee, min(prio, max_fee)

    # ===== preview ========================================================================
    async def preview(self, kind: str, chain_id: int, items: list[TxItem], title: str,
                      move_cap: str = "", notes: list[str] | None = None) -> dict:
        if not items:
            raise TxEngineError("nothing to do: no transfers matched your selection")
        if len(items) > 2000:
            raise TxEngineError("too many transfers in one operation (max 2000)")
        rpc_url = await self.pool.best_url(chain_id)
        max_fee, prio = await self.fees(chain_id)
        sources = sorted({i.source for i in items})
        raw_balances = await self.pool.batch(chain_id, [("eth_getBalance", [s, "pending"])
                                                        for s in sources])
        balances = {s: (None if isinstance(b, RpcError) else int(b, 16))
                     for s, b in zip(sources, raw_balances, strict=True)}
        limits = self.safety.limits_wei()

        for item in items:
            if not self.vault.has(item.source):
                item.status, item.reason = "NO_KEY", "no key stored for this wallet"
            elif item.source in self.busy_sources:
                item.status, item.reason = "BUSY", f"wallet is busy in operation #{self.busy_sources[item.source]}"
            elif self.external_busy(item.source) is not None:
                item.status, item.reason = "BUSY", f"wallet is minting in operation #{self.external_busy(item.source)}"
            elif item.kind != "custom_mint" and item.source.lower() == item.recipient.lower():
                item.status, item.reason = "SAME_ADDRESS", "source and recipient are the same"
        await self._simulate(chain_id, items)

        # Per wallet: native needed = sum(value + gas*max_fee); compute max sweeps last.
        for source in sources:
            mine = [i for i in items if i.source == source and i.status == "READY"]
            balance = balances[source]
            if balance is None:
                for i in mine:
                    i.status, i.reason = "RPC_ERROR", "could not read balance"
                continue
            gas_total = sum(i.gas_limit * max_fee for i in mine)
            fixed = sum(i.value for i in mine if not i.sweep_max)
            for i in mine:
                if i.sweep_max:
                    i.value = balance - i.reserve_wei - gas_total - fixed
                    if i.value <= 0:
                        i.status = "NOTHING_TO_SWEEP"
                        i.reason = (f"balance {from_wei(balance)} does not cover the "
                                    f"{from_wei(i.reserve_wei)} reserve plus gas")
                        i.value = 0
                    else:
                        i.amount = from_wei(i.value)
            needed = sum(i.value + i.gas_limit * max_fee for i in mine if i.status == "READY")
            if needed > balance:
                for i in mine:
                    if i.status == "READY":
                        i.shortfall = needed - balance
                        i.status = "INSUFFICIENT_FUNDS"
                        i.reason = (f"wallet needs {from_wei(needed)} incl. max gas, "
                                    f"has {from_wei(balance)}")
            for i in items:
                # Shortfall for wallets whose test run already failed for lack of funds.
                if i.source == source and i.status == "INSUFFICIENT_FUNDS" and not i.shortfall:
                    gas = i.gas_limit or i.fixed_gas or 150_000
                    i.shortfall = max(0, i.value + gas * max_fee - balance)
            for i in mine:
                gas_cost = i.gas_limit * max_fee
                if i.status == "READY" and gas_cost > limits["max_gas_cost_per_tx"]:
                    i.status, i.reason = "LIMIT_EXCEEDED", (
                        f"max gas cost {from_wei(gas_cost)} is over the per-transaction limit")

        if kind == "custom_mint":
            for i in items:
                if i.status == "READY" and i.value > limits["max_value_per_tx"]:
                    i.status, i.reason = "LIMIT_EXCEEDED", (
                        f"mint value {from_wei(i.value)} is over the per-transaction limit")
        ready = [i for i in items if i.status == "READY"]
        # Mints use the mint limits; moving funds uses the transfer limit.
        cap = limits["max_total_per_operation" if kind == "custom_mint" else "max_transfer_per_operation"]
        if move_cap.strip():
            cap = min(cap, to_wei(move_cap))
        moved = sum(i.native_out() for i in ready)
        spend = sum(i.spend() + i.gas_limit * max_fee for i in ready)
        daily_used = self.safety.daily_spent_wei(chain_id)
        blocks = []
        if self.safety.stopped:
            blocks.append("Emergency STOP is active. Resume first.")
        if not ready:
            blocks.append("No transfer is ready.")
        if moved > cap:
            blocks.append(f"This {'spends' if kind == 'custom_mint' else 'moves'} {from_wei(moved)} "
                          f"native coin, over the per-operation limit {from_wei(cap)}. Raise the "
                          "limit in Settings or select less.")
        if daily_used + spend > limits["max_daily"]:
            blocks.append(f"24h spend would reach {from_wei(daily_used + spend)}, over the daily "
                          f"limit {from_wei(limits['max_daily'])}.")
        token = secrets.token_urlsafe(16)
        self._expire()
        if not blocks:
            self.previews[token] = TxPreview(token, kind, chain_id, rpc_url, ready, max_fee, prio,
                                             cap, time.time() + PREVIEW_TTL_SECONDS, title)
        return {
            "token": None if blocks else token, "expires_in": PREVIEW_TTL_SECONDS,
            "kind": kind, "title": title, "chain_id": chain_id,
            "confirm_word": CONFIRM_WORDS.get(kind, CONFIRM_WORD),
            "scheduled_at": min((i.scheduled_at for i in ready if i.scheduled_at), default=None),
            "fees": {"max_fee_gwei": f"{max_fee / 1e9:.4f}", "priority_fee_gwei": f"{prio / 1e9:.4f}"},
            "rows": [i.public(max_fee) for i in items],
            "totals": {"ready": len(ready), "total": len(items), "native_moved": from_wei(moved),
                       "external_spend": from_wei(sum(i.spend() for i in ready)),
                       "max_gas": from_wei(sum(i.gas_limit * max_fee for i in ready)),
                       "move_cap": from_wei(cap), "daily_used": from_wei(daily_used),
                       "daily_limit": from_wei(limits["max_daily"]),
                       "wallets": len({i.source for i in ready}),
                       "external_recipients": len({i.recipient for i in ready if i.external})},
            "blocks": blocks, "notes": notes or [],
        }

    async def _simulate(self, chain_id: int, items: list[TxItem]) -> None:
        semaphore = asyncio.Semaphore(8)

        async def one(item: TxItem) -> None:
            if item.status != "READY":
                return
            async with semaphore:
                # A plain coin transfer costs the same gas whatever the amount, so it is
                # estimated without the amount; the balance check below reports shortfalls.
                plain = not item.data
                call = {"from": item.source, "to": item.target, "data": "0x" + item.data.hex(),
                        "value": hex(0 if (item.sweep_max or plain) else item.value)}
                try:
                    gas = int(await self.pool.call(chain_id, "eth_estimateGas", [call]), 16)
                    item.gas_limit = max(21_000, math.ceil(gas * GAS_BUFFER), item.fixed_gas)
                except (RpcError, ValueError, TypeError) as error:
                    text = str(error)[:200]
                    if "insufficient funds" in text.lower():
                        item.status, item.reason = "INSUFFICIENT_FUNDS", text
                    elif item.fixed_gas and (item.wait_for_open or
                                             (item.scheduled_at and item.scheduled_at > time.time())):
                        # Before launch most mints revert ("sale not active"). Use the user's gas
                        # limit and decide at launch.
                        item.gas_limit = item.fixed_gas
                        item.reason = (f"test run fails now ({text[:80]}); "
                                       + ("it is re-run right before sending and the mint is "
                                          "skipped if it still fails" if item.resimulate else
                                          "FAST MODE: sent at launch without a re-check"))
                    else:
                        item.status, item.reason = "SIMULATION_FAILED", text
        await asyncio.gather(*(one(i) for i in items))

    def _expire(self) -> None:
        now = time.time()
        for token in [t for t, p in self.previews.items() if p.expires_at < now]:
            del self.previews[token]

    # ===== start ==========================================================================
    async def start(self, token: str, confirmation: str) -> int:
        self._expire()
        preview = self.previews.get(token)
        if preview is None:
            raise TxEngineError("preview expired or already used; preview again")
        word = CONFIRM_WORDS.get(preview.kind, CONFIRM_WORD)
        if confirmation.strip().upper() != word:
            raise TxEngineError(f"type {word} to confirm")
        self.previews.pop(token, None)
        with self.safety.lock:
            if self.safety.stopped:
                raise TxEngineError("emergency STOP is active")
            busy = [i.source for i in preview.items if i.source in self.busy_sources
                    or self.external_busy(i.source) is not None]
            if busy:
                raise TxEngineError(f"{busy[0]} is busy in another operation")
            cur = self.db.execute(
                "INSERT INTO jobs(kind, chain_id, params, status, created_at) "
                "VALUES(?, ?, ?, 'running', ?)",
                (preview.kind, preview.chain_id,
                 json.dumps({"title": preview.title, "items": len(preview.items)}), time.time()))
            job_id = int(cur.lastrowid)
            runs = []
            for item in preview.items:
                hid = self.db.add_history(
                    job_id=job_id, operation=preview.kind, chain_id=preview.chain_id,
                    address=item.source, recipient=item.recipient, asset=item.asset,
                    token_id=item.token_id, amount=item.amount, status="queued")
                runs.append(TxRun(item, hid))
                self.busy_sources[item.source] = job_id
            job = TxJob(job_id, preview, runs)
            self.jobs[job_id] = job
        job.task = asyncio.create_task(self._run(job))
        self.bus.publish("txjob", job=job.summary())
        return job_id

    # ===== execution ======================================================================
    async def _run(self, job: TxJob) -> None:
        try:
            by_source: dict[str, list[TxRun]] = {}
            for run in job.runs:
                by_source.setdefault(run.item.source, []).append(run)
            watcher = None
            probes = [r.item for r in job.runs if r.item.wait_for_open]
            if probes:
                job.open_event = asyncio.Event()
                watcher = asyncio.create_task(self._watch_sale_open(job, probes))
            job.tasks = [asyncio.create_task(self._run_source(job, runs))
                         for runs in by_source.values()]
            await asyncio.gather(*job.tasks, return_exceptions=True)
            if watcher is not None and not watcher.done():
                watcher.cancel()
        finally:
            for run in job.runs:
                if run.phase == "queued":
                    self._finish(job, run, "cancelled", "not sent")
            job.status = "stopped" if self.safety.stopped else "finished"
            self.db.execute("UPDATE jobs SET status = ?, finished_at = ? WHERE id = ?",
                            (job.status, time.time(), job.id))
            for run in job.runs:
                if self.busy_sources.get(run.item.source) == job.id:
                    self.busy_sources.pop(run.item.source, None)
            self.bus.publish("txjob", job=job.summary())

    async def _run_source(self, job: TxJob, runs: list[TxRun]) -> None:
        preview = job.preview
        source = runs[0].item.source
        sent: list[TxRun] = []
        try:
            signer = self.vault.signer(source)
            if any(r.item.wait_for_open for r in runs) and job.open_event is not None:
                for run in runs:
                    run.phase, run.message = "waiting", "waiting for the sale to open (test run every block)"
                    self._publish(job, run)
                await job.open_event.wait()
                if job.open_state != "opened":
                    why = ("STOP pressed while waiting" if job.open_state == "stopped"
                           else "the sale did not open within the maximum wait")
                    for run in runs:
                        self._finish(job, run, "cancelled", f"{why}; nothing sent")
                    return
            launch = min((r.item.scheduled_at for r in runs if r.item.scheduled_at), default=None)
            if launch and launch > time.time():
                for run in runs:
                    run.phase, run.message = "waiting", f"sends at {time.strftime('%H:%M:%S', time.localtime(launch))}"
                    self._publish(job, run)
                if not await self._wait_until(launch - PREFETCH_SECONDS):
                    for run in runs:
                        self._finish(job, run, "cancelled", "STOP pressed while waiting; nothing sent")
                    return
            nonce = int(await self.pool.call(preview.chain_id, "eth_getTransactionCount",
                                             [source, "pending"]), 16)
            balance = int(await self.pool.call(preview.chain_id, "eth_getBalance",
                                               [source, "pending"]), 16)
            if launch and not await self._wait_until(launch):
                for run in runs:
                    self._finish(job, run, "cancelled", "STOP pressed while waiting; nothing sent")
                return
            for index, run in enumerate(runs):
                item = run.item
                if (item.scheduled_at or item.wait_for_open) and item.resimulate and item.data:
                    problem = await self._recheck(preview.chain_id, item)
                    if problem:
                        self._finish(job, run, "not_submitted", f"skipped, test run at launch failed: {problem}")
                        for rest in runs[index + 1:]:
                            self._finish(job, rest, "not_submitted", "skipped: an earlier item failed its test run")
                        return
                if item.sweep_max:
                    # Recompute from the live balance so the gas reserve is never touched.
                    other_gas = sum(r.item.gas_limit for r in runs) * preview.max_fee
                    other_value = sum(r.item.value for r in runs if not r.item.sweep_max)
                    live = balance - item.reserve_wei - other_gas - other_value
                    if live <= 0:
                        self._finish(job, run, "not_submitted", "balance no longer covers reserve and gas")
                        continue
                    item.value = min(item.value, live)
                    item.amount = from_wei(item.value)
                try:
                    self._gate(job, item)
                except GateBlocked as error:
                    for rest in runs[index:]:
                        self._finish(job, rest, "blocked", str(error))
                    return
                signed = sign_eip1559_transaction(Eip1559Transaction(
                    chain_id=preview.chain_id, nonce=nonce,
                    max_priority_fee_per_gas=preview.priority_fee,
                    max_fee_per_gas=preview.max_fee, gas_limit=item.gas_limit,
                    target=to_checksum_address(item.target), value=item.value,
                    calldata=item.data), signer)
                run.tx_hash, run.phase = signed.hash_hex, "submitted"
                self.db.update_history(
                    run.history_id, status="submitted", tx_hash=signed.hash_hex, nonce=nonce,
                    value_wei=str(item.value), spend_wei=str(item.spend()), amount=item.amount,
                    max_cost_wei=str(item.spend() + item.gas_limit * preview.max_fee))
                sent.append(run)  # track from here on, even if the send result is unclear
                try:
                    await self._send(preview.chain_id, preview.rpc_url, "0x" + signed.raw.hex(),
                                     signed.hash_hex)
                except (RpcError, ValueError) as error:
                    sent.remove(run)
                    self._finish(job, run, "failed", f"send rejected: {error}")
                    for rest in runs[index + 1:]:
                        self._finish(job, rest, "not_submitted", "skipped: an earlier transfer from this wallet failed")
                    return
                self._publish(job, run)
                nonce += 1
        except Exception as error:  # noqa: BLE001 - report, never crash the job
            for run in runs:
                if run.phase == "queued":
                    self._finish(job, run, "not_submitted", str(error)[:300])
        finally:
            await self._track(job, sent)

    async def _watch_sale_open(self, job: TxJob, probes: list[TxItem]) -> None:
        """Test-run a mint on every new block; open the gate for all wallets once it passes."""
        deadline = time.time() + max(p.wait_for_open for p in probes)
        chain_id, last_block, index = job.preview.chain_id, None, 0
        try:
            while True:
                if self.safety.stopped:
                    job.open_state = "stopped"
                    return
                if time.time() > deadline:
                    job.open_state = "timeout"
                    return
                try:
                    block = await self.pool.call(chain_id, "eth_blockNumber", [])
                except RpcError:
                    block = None
                if block is not None and block != last_block:
                    last_block = block
                    probe = probes[index % len(probes)]
                    problem = await self._recheck(chain_id, probe)
                    if problem is None:
                        job.open_state = "opened"
                        self.bus.publish("sale_open", job=job.id, block=block)
                        return
                    if "insufficient funds" in problem.lower():
                        index += 1      # this wallet cannot prove it; try the next one
                await asyncio.sleep(SALE_POLL_SECONDS)
        finally:
            if not job.open_state:
                job.open_state = "stopped"
            if job.open_event is not None:
                job.open_event.set()

    async def _wait_until(self, target: float) -> bool:
        """Sleep until `target` (unix time). Returns False if STOP is pressed meanwhile."""
        while True:
            if self.safety.stopped:
                return False
            left = target - time.time()
            if left <= 0:
                return True
            await asyncio.sleep(min(0.25, left) if left > 0.3 else max(0.0, left - 0.002))

    async def _recheck(self, chain_id: int, item: TxItem) -> str | None:
        call = {"from": item.source, "to": item.target, "data": "0x" + item.data.hex(),
                "value": hex(item.value), "gas": hex(item.gas_limit)}
        try:
            await self.pool.call(chain_id, "eth_estimateGas", [call])
        except (RpcError, ValueError, TypeError) as error:
            return str(error)[:200]
        return None

    def _gate(self, job: TxJob, item: TxItem) -> None:
        with self.safety.lock:
            if self.safety.stopped:
                raise GateBlocked("emergency STOP is active; not sent")
            limits = self.safety.limits_wei()
            gas_cost = item.gas_limit * job.preview.max_fee
            if gas_cost > limits["max_gas_cost_per_tx"]:
                raise GateBlocked("blocked: gas cost over the per-transaction limit")
            if job.preview.kind == "custom_mint" and item.value > limits["max_value_per_tx"]:
                raise GateBlocked("blocked: mint value over the per-transaction limit")
            if job.moved_wei + item.native_out() > job.preview.move_cap:
                raise GateBlocked("blocked: operation would move more than its limit")
            daily = self.safety.daily_spent_wei(job.preview.chain_id)
            if daily + item.spend() + gas_cost > limits["max_daily"]:
                raise GateBlocked("blocked: over the 24h spending limit")
            job.moved_wei += item.native_out()

    async def _send(self, chain_id: int, url: str, raw: str, local_hash: str) -> None:
        try:
            broadcast = bool((self.db.get_setting("network", {}) or {}).get("broadcast_all", True))
            returned, used = await self.pool.send_raw(chain_id, url, raw, broadcast)
            if returned is None:   # a node said "already known": it has our transaction
                return
        except RpcError as error:
            if "already known" in str(error).lower():
                return
            raise
        if used != url and not broadcast:
            self.bus.publish("failover", chain_id=chain_id, operation="transfer")
        if not isinstance(returned, str) or returned.lower() != local_hash.lower():
            raise RpcError("RPC returned a different transaction hash")

    async def _track(self, job: TxJob, runs: list[TxRun]) -> None:
        deadline = time.time() + RECEIPT_TIMEOUT_SECONDS
        waiting = list(runs)
        while waiting and time.time() < deadline:
            await asyncio.sleep(1.0)
            results = await self.pool.batch(job.preview.chain_id, [
                ("eth_getTransactionReceipt", [r.tx_hash]) for r in waiting])
            still = []
            for run, receipt in zip(waiting, results, strict=True):
                if isinstance(receipt, RpcError) or not receipt or not receipt.get("blockHash"):
                    still.append(run)
                    continue
                gas_used = int(receipt["gasUsed"], 16)
                cost = gas_used * int(receipt.get("effectiveGasPrice") or "0x0", 16)
                if receipt.get("status") != "0x1":
                    self._finish(job, run, "failed", "transaction reverted",
                                 gas_used=gas_used, gas_cost_wei=str(cost))
                    continue
                note = await self._verify(job.preview.chain_id, run.item, receipt)
                self._finish(job, run, "confirmed", note, gas_used=gas_used, gas_cost_wei=str(cost))
            waiting = still
        for run in waiting:
            self._finish(job, run, "pending", "sent; not confirmed yet — check the explorer")

    async def _verify(self, chain_id: int, item: TxItem, receipt: dict | None = None) -> str:
        if not item.verify:
            return "confirmed"
        if item.verify[0] == "minted":
            got = minted_to(receipt or {}, item.verify[1], item.source)
            if got >= int(item.verify[2]):
                return f"minted {got} NFT(s); proven by the receipt"
            return (f"confirmed, but the receipt shows {got} of {item.verify[2]} NFT(s) minted to "
                    "this wallet — check the explorer")
        from . import nfts  # local import: nfts imports this module's types
        try:
            ok = await nfts.verify_owner(self.pool, chain_id, item.verify, item.recipient)
        except RpcError:
            return "confirmed; ownership check failed to load"
        if ok:
            nfts.record_owner(self.db, chain_id, item.verify, item.source, item.recipient)
            return "confirmed; new owner verified on-chain"
        return "confirmed, but on-chain ownership does not show the recipient yet"

    def _finish(self, job: TxJob, run: TxRun, status: str, message: str, **fields: Any) -> None:
        run.phase, run.message = status, message
        self.db.update_history(run.history_id, status=status,
                               error=None if status in ("confirmed", "pending") else message,
                               **fields)
        self._publish(job, run)

    def _publish(self, job: TxJob, run: TxRun) -> None:
        self.bus.publish("txrun", job=job.id, run=run.summary())

    def list_jobs(self) -> list[dict]:
        return [j.summary() for j in sorted(self.jobs.values(), key=lambda j: -j.id)]



TRANSFER_TOPIC = "0x" + __import__("eth_utils").crypto.keccak(text="Transfer(address,address,uint256)").hex()
TRANSFER_SINGLE_TOPIC = "0x" + __import__("eth_utils").crypto.keccak(
    text="TransferSingle(address,address,address,uint256,uint256)").hex()


def minted_to(receipt: dict, contract: str, wallet: str) -> int:
    """Count NFTs minted (from the zero address) to `wallet` by `contract` in a receipt."""
    zero = "0x" + "00" * 32
    me = "0x" + "00" * 12 + wallet[2:].lower()
    count = 0
    for log in receipt.get("logs") or []:
        if str(log.get("address", "")).lower() != contract.lower():
            continue
        topics = [t.lower() for t in log.get("topics") or []]
        if len(topics) == 4 and topics[0] == TRANSFER_TOPIC and topics[1] == zero and topics[2] == me:
            count += 1
        elif (len(topics) == 4 and topics[0] == TRANSFER_SINGLE_TOPIC and topics[2] == zero
              and topics[3] == me):
            count += int(log.get("data", "0x")[66:130] or "0", 16)
    return count
