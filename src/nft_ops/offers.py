"""NFT Offer Manager: monitor offers on every registered wallet, evaluate, and accept safely.

Execution modes (per collection, default from the '*' rules):
  safe  monitor and recommend only
  semi  qualifying offers wait for your confirmation (Accept button)
  full  accept automatically, but ONLY after the collection is armed with a typed confirmation,
        and never while the collection is paused (floor drop, suspicious floor, failures)

Before any acceptance (manual or automatic) every protection is re-checked with fresh data:
offer still listed by OpenSea, ownership, approval, fees and net proceeds, floor, the
Seaport transaction (validated against the evaluated offer), and an on-chain simulation.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Any

from eth_utils.address import to_checksum_address

from osnm_z.transaction import Eip1559Transaction, sign_eip1559_transaction

from . import market, seaport
from .db import Database
from .events import EventBus
from .networks import RpcError, RpcPool
from .offer_rules import evaluate, offer_applies, parse_offer, rules_for
from .opensea_api import CHAIN_SLUGS, OpenSeaAPI, OpenSeaError
from .safety import Safety
from .vault import Vault

DEFAULT_SETTINGS = {"enabled": True, "scan_seconds": 60, "holdings_minutes": 10, "floor_seconds": 120}
EST_GAS_UNITS = 250_000
GAS_BUFFER = 1.2
RECEIPT_SECONDS = 180
FAILURE_PAUSE = 3
MAX_TOKENS_PER_COLLECTION = 50   # per-NFT offer lookups per collection per scan
ARM_WORD = "AUTO-ACCEPT"
ACCEPT_WORD = "ACCEPT"


class OfferError(Exception):
    pass


class OfferEngine:
    def __init__(self, db: Database, vault: Vault, pool: RpcPool, bus: EventBus, safety: Safety,
                 tx_engine: Any, api: OpenSeaAPI) -> None:
        self.db, self.vault, self.pool, self.bus, self.safety = db, vault, pool, bus, safety
        self.tx, self.api = tx_engine, api
        self.locks: set[str] = set()
        self.reserved: dict[str, list[float]] = {}           # slug -> in-flight sale values
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._scan_lock = asyncio.Lock()
        self._last_holdings = 0.0
        self._operators: dict[tuple, str] = {}
        self._tasks: set[asyncio.Task] = set()
        self.last_scan: dict = {}

    # ----- settings, rules, state -------------------------------------------------------
    def settings(self) -> dict:
        return {**DEFAULT_SETTINGS, **(self.db.get_setting("offers", {}) or {})}

    def state(self, slug: str) -> dict:
        row = self.db.one("SELECT armed, armed_at, paused, paused_at, fail_streak FROM offer_rules WHERE slug = ?", (slug,))
        return dict(row) if row else {"armed": 0, "armed_at": None, "paused": None, "paused_at": None, "fail_streak": 0}

    def _ensure_row(self, slug: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO offer_rules(slug, rules) VALUES(?, '{}')", (slug,))

    def save_rules(self, slug: str, values: dict) -> dict:
        from .offer_rules import validate_rules
        clean = validate_rules(values)
        self._ensure_row(slug)
        current = json.loads(self.db.one("SELECT rules FROM offer_rules WHERE slug = ?", (slug,))["rules"])
        current.update(clean)
        self.db.execute("UPDATE offer_rules SET rules = ? WHERE slug = ?", (json.dumps(current), slug))
        return rules_for(self.db, slug)

    def arm(self, slug: str, confirmation: str) -> None:
        if slug == "*":
            raise OfferError("arm each collection separately")
        if confirmation.strip().upper() != ARM_WORD:
            raise OfferError(f"type {ARM_WORD} to switch on automatic acceptance")
        self._ensure_row(slug)
        self.db.execute("UPDATE offer_rules SET armed = 1, armed_at = ? WHERE slug = ?", (time.time(), slug))
        self.bus.publish("offers_state", slug=slug, armed=True)

    def disarm(self, slug: str) -> None:
        self._ensure_row(slug)
        self.db.execute("UPDATE offer_rules SET armed = 0 WHERE slug = ?", (slug,))
        self.bus.publish("offers_state", slug=slug, armed=False)

    def pause(self, slug: str, reason: str) -> None:
        self._ensure_row(slug)
        if self.state(slug)["paused"]:
            return
        self.db.execute("UPDATE offer_rules SET paused = ?, paused_at = ? WHERE slug = ?", (reason, time.time(), slug))
        self._alert(slug, "offers_paused", f"{slug}: automatic offer acceptance paused — {reason}. "
                    "Review it on the Offers page and resume when you are happy.", "warn")

    def resume(self, slug: str) -> None:
        self._ensure_row(slug)
        self.db.execute("UPDATE offer_rules SET paused = NULL, paused_at = NULL, fail_streak = 0 WHERE slug = ?", (slug,))
        self.bus.publish("offers_state", slug=slug, paused=False)

    def _alert(self, slug: str, kind: str, message: str, level: str = "info") -> None:
        cur = self.db.execute("INSERT INTO alerts(created_at, slug, kind, level, message, read) "
                              "VALUES(?, ?, ?, ?, ?, 0)", (time.time(), slug, kind, level, message))
        self.bus.publish("alert", id=int(cur.lastrowid), slug=slug, alert_kind=kind, level=level, message=message)

    # ----- monitoring loop ----------------------------------------------------------------
    async def run_forever(self) -> None:
        while True:
            s = self.settings()
            try:
                if s["enabled"] and self.vault.unlocked and self.api.configured:
                    await self.scan()
            except Exception as error:  # noqa: BLE001 - the loop must never die
                self.last_scan = {"at": time.time(), "error": str(error)[:200]}
            await asyncio.sleep(max(15, float(s["scan_seconds"])))

    def _chains(self) -> list[int]:
        return [r["chain_id"] for r in self.db.all("SELECT chain_id FROM networks WHERE enabled = 1")
                if r["chain_id"] in CHAIN_SLUGS]

    async def scan(self, sync_holdings: bool | None = None) -> dict:
        async with self._scan_lock:
            started = time.time()
            s = self.settings()
            wallets = [r["address"] for r in self.db.all("SELECT address FROM wallets")]
            own = {w.lower() for w in wallets}
            if sync_holdings or (sync_holdings is None and time.time() - self._last_holdings > s["holdings_minutes"] * 60):
                await market.sync_holdings(self.db, self.api, wallets, self._chains())
                self._last_holdings = time.time()
            holdings = self.db.all("SELECT * FROM os_holdings")
            for h in holdings:
                h["traits_list"] = ([f"{t.get('trait_type')}:{t.get('value')}" for t in json.loads(h["traits"])]
                                    if h["traits"] else None)
            by_slug: dict[str, list[dict]] = {}
            for h in holdings:
                by_slug.setdefault(h["slug"], []).append(h)
            totals = {"collections": 0, "offers": 0, "qualifying": 0, "accepted_now": 0}
            for slug, rows in by_slug.items():
                try:
                    found = await self._scan_collection(slug, rows, own, s)
                except (OpenSeaError, RpcError, seaport.SeaportError) as error:
                    self.last_scan.setdefault("errors", []).append(f"{slug}: {error}")
                    continue
                totals["collections"] += 1
                for k in ("offers", "qualifying", "accepted_now"):
                    totals[k] += found[k]
            self.db.execute("DELETE FROM offers_current WHERE slug NOT IN (SELECT DISTINCT slug FROM os_holdings)")
            self.last_scan = {"at": time.time(), "seconds": round(time.time() - started, 1), **totals}
            self.bus.publish("offers", **self.last_scan)
            return self.last_scan

    async def _market(self, slug: str, chain_id: int, rules: dict, force: bool = False) -> dict:
        info = await market.collection_info(self.db, self.api, slug)
        last = market.latest(self.db, slug)
        if force or not last or time.time() - last["ts"] > self.settings()["floor_seconds"]:
            await market.snapshot(self.db, self.api, slug, chain_id)
            last = market.latest(self.db, slug)
        return {"info": info, "floor": last["floor_native"] if last else None,
                "sales_median": last["sales_median"] if last else None,
                "change": market.floor_change(self.db, slug, float(rules["drop_window_minutes"])),
                "vs_sales": market.floor_vs_sales(self.db, slug)}

    async def _gas_native(self, chain_id: int) -> float:
        try:
            max_fee, _ = await self.tx.fees(chain_id)
        except Exception:  # noqa: BLE001
            return 0.0
        return EST_GAS_UNITS * max_fee / 1e18

    async def _operator(self, chain_id: int, protocol: str, conduit_key: str) -> str:
        key = (chain_id, protocol.lower(), (conduit_key or "").lower())
        if key not in self._operators:
            self._operators[key] = await seaport.operator_for(self.pool, chain_id, protocol, conduit_key)
        return self._operators[key]

    async def _scan_collection(self, slug: str, rows: list[dict], own: set[str], s: dict) -> dict:
        chain_id = rows[0]["chain_id"]
        rules = rules_for(self.db, slug)
        mkt = await self._market(slug, chain_id, rules)
        self._check_pause(slug, rules, mkt)
        offers = await self._fetch_offers(slug, rows)
        gas = await self._gas_native(chain_id)
        approvals: dict[tuple, bool] = {}
        results = []
        for raw in offers:
            try:
                parsed = parse_offer(raw)
            except (KeyError, ValueError, TypeError, IndexError):
                continue
            if CHAIN_SLUGS.get(chain_id) != parsed["chain"]:
                continue
            for h in rows:
                if not offer_applies(parsed, h):
                    continue
                operator = await self._operator(chain_id, parsed["protocol_address"], parsed["conduit_key"])
                akey = (h["address"], h["contract"].lower(), operator)
                if akey not in approvals:
                    approvals[akey] = await seaport.is_approved(self.pool, chain_id, h["contract"], h["address"], operator)
                ev = evaluate(self.db, rules, parsed, h, mkt, gas, own, self._cost_basis(slug, h),
                              approvals[akey])
                ev.update({"operator": operator, "wallet": h["address"], "contract": h["contract"],
                           "token_id": h["token_id"], "nft_name": h["name"], "slug": slug,
                           "chain_id": chain_id, "standard": h["standard"], "raw": raw,
                           "has_key": self.vault.has(h["address"])})
                results.append(ev)
        now = time.time()
        self.db.execute("DELETE FROM offers_current WHERE slug = ?", (slug,))
        self.db.executemany(
            "INSERT OR REPLACE INTO offers_current(order_hash, chain_id, contract, token_id, wallet, slug, "
            "evaluation, qualifies, net_native, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(e["order_hash"], chain_id, e["contract"], e["token_id"], e["wallet"], slug, json.dumps(e),
              int(e["qualifies"]), e["net"], now) for e in results])
        best: dict[tuple, dict] = {}
        for e in results:
            if e["qualifies"] and e["has_key"]:
                k = (e["contract"].lower(), e["token_id"], e["wallet"])
                if k not in best or (e["net"] or 0) > (best[k]["net"] or 0):
                    best[k] = e
        accepted_now = 0
        st = self.state(slug)
        if (rules["execution_mode"] == "full" and st["armed"] and not st["paused"]
                and not self.safety.stopped and best):
            used_orders: set[str] = set()
            for e in sorted(best.values(), key=lambda x: -(x["net"] or 0)):
                if e["order_hash"] in used_orders:     # one acceptance per offer per scan
                    continue
                used_orders.add(e["order_hash"])
                task = asyncio.create_task(self.accept(e["order_hash"], e["contract"], e["token_id"],
                                                       e["wallet"], how="auto"))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
                accepted_now += 1
        return {"offers": len(results), "qualifying": sum(1 for e in results if e["qualifies"]),
                "accepted_now": accepted_now}

    async def _fetch_offers(self, slug: str, rows: list[dict]) -> list[dict]:
        """Collection-wide offers plus the offers on each NFT the wallets hold (deduplicated)."""
        found: dict[str, dict] = {}
        for offer in await self.api.offers_collection(slug):
            found[offer.get("order_hash") or id(offer)] = offer
        for token_id in sorted({h["token_id"] for h in rows})[:MAX_TOKENS_PER_COLLECTION]:
            for offer in await self.api.offers_nft(slug, token_id, max_pages=1):
                found[offer.get("order_hash") or id(offer)] = offer
        return list(found.values())

    def _check_pause(self, slug: str, rules: dict, mkt: dict) -> None:
        if rules["mode"] == "fixed":
            return
        if mkt["change"] is not None and mkt["change"] <= -float(rules["pause_drop_pct"]):
            self.pause(slug, f"floor fell {abs(mkt['change']):.1f}% within {rules['drop_window_minutes']:g} min")
        elif mkt["vs_sales"] is not None and abs(mkt["vs_sales"]) > float(rules["max_floor_vs_sales_pct"]):
            self.pause(slug, f"floor is {mkt['vs_sales']:+.0f}% away from recent sales (suspicious)")

    def _cost_basis(self, slug: str, holding: dict) -> float | None:
        rows = self.db.all("SELECT value_wei, gas_cost_wei, quantity FROM history WHERE status = 'confirmed' "
                           "AND address = ? AND ((operation = 'mint' AND slug = ?) OR "
                           "(operation = 'custom_mint' AND lower(asset) = lower(?)))",
                           (holding["address"], slug, f"erc721:{holding['contract']}"))
        if not rows:
            return None
        units = sum(int(r["quantity"] or 1) for r in rows) or 1
        return sum(int(r["value_wei"] or 0) + int(r["gas_cost_wei"] or 0) for r in rows) / 1e18 / units

    # ----- acceptance ---------------------------------------------------------------------
    def _log(self, ev: dict, status: str, reason: str, how: str, tx_hash: str | None = None,
             gas_native: float | None = None, net: float | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO offer_log(ts, slug, nft_name, contract, token_id, wallet, offerer, order_hash, "
            "offer_type, currency, gross, gross_native, market_fee, creator_fee, gas_native, net_native, "
            "floor_native, tx_hash, status, reason, mode, chain_id) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), ev.get("slug"), ev.get("nft_name"), ev.get("contract"), ev.get("token_id"),
             ev.get("wallet"), ev.get("offerer"), ev.get("order_hash"), ev.get("type"), ev.get("currency"),
             ev.get("gross"), ev.get("gross_native"), ev.get("market_fee"), ev.get("creator_fee"),
             gas_native if gas_native is not None else ev.get("gas_native"),
             net if net is not None else ev.get("net"), ev.get("floor"), tx_hash, status, reason[:500], how,
             ev.get("chain_id")))
        self.bus.publish("offer_log", status=status, slug=ev.get("slug"), token_id=ev.get("token_id"), reason=reason[:200])
        return int(cur.lastrowid)

    def _daily(self, slug: str) -> tuple[int, float]:
        row = self.db.one("SELECT COUNT(*) AS n, COALESCE(SUM(gross_native), 0) AS v FROM offer_log WHERE slug = ? "
                          "AND status IN ('accepted', 'pending') AND ts >= ?", (slug, time.time() - 86400))
        inflight = self.reserved.get(slug, [])
        return row["n"] + len(inflight), row["v"] + sum(inflight)

    async def accept(self, order_hash: str, contract: str, token_id: str, wallet: str, how: str = "manual",
                     confirmation: str = "") -> dict:
        """Run every check with fresh data, then accept. how = manual | auto."""
        if how == "manual" and confirmation.strip().upper() != ACCEPT_WORD:
            raise OfferError(f"type {ACCEPT_WORD} to accept this offer")
        row = self.db.one("SELECT evaluation FROM offers_current WHERE order_hash = ? AND lower(contract) = lower(?) "
                          "AND token_id = ? AND lower(wallet) = lower(?)", (order_hash, contract, str(token_id), wallet))
        if row is None:
            raise OfferError("offer not found; refresh the Offers page")
        ev0 = json.loads(row["evaluation"])
        token_key, order_key = f"{ev0['chain_id']}:{contract.lower()}:{token_id}", f"order:{order_hash}"
        if token_key in self.locks or order_key in self.locks:
            return {"status": "skipped", "reason": "already being accepted"}
        self.locks.update({token_key, order_key})
        slug = ev0["slug"]
        sem = self._sems.setdefault(slug, asyncio.Semaphore(max(1, int(rules_for(self.db, slug)["max_concurrent"]))))
        try:
            async with sem:
                return await self._accept_locked(ev0, how)
        finally:
            self.locks.discard(token_key)
            self.locks.discard(order_key)

    async def _accept_locked(self, ev0: dict, how: str) -> dict:
        slug, chain_id, wallet = ev0["slug"], ev0["chain_id"], to_checksum_address(ev0["wallet"])
        contract, token_id, raw = to_checksum_address(ev0["contract"]), str(ev0["token_id"]), ev0["raw"]
        rules = rules_for(self.db, slug)

        def skip(reason: str, ev: dict | None = None, status: str = "skipped") -> dict:
            self._log(ev or ev0, status, reason, how)
            return {"status": status, "reason": reason}

        if self.safety.stopped:
            return skip("emergency STOP is active")
        st = self.state(slug)
        if how == "auto" and (rules["execution_mode"] != "full" or not st["armed"] or st["paused"]):
            return skip("automatic acceptance is not armed for this collection (or it is paused)")
        if not self.vault.has(wallet):
            return skip("no key for this wallet")
        # 1-4, 7: fresh ownership, approval, floor, and the offer re-parsed
        parsed = parse_offer(raw)
        owned = await seaport.owns(self.pool, chain_id, contract, wallet, token_id, ev0.get("standard") or "erc721")
        operator = await self._operator(chain_id, parsed["protocol_address"], parsed["conduit_key"])
        approved = await seaport.is_approved(self.pool, chain_id, contract, wallet, operator)
        mkt = await self._market(slug, chain_id, rules, force=True)
        if how == "auto":
            self._check_pause(slug, rules, mkt)
            if self.state(slug)["paused"]:
                return skip("paused: " + self.state(slug)["paused"])
        holding = {"chain_id": chain_id, "address": wallet, "contract": contract, "token_id": token_id,
                   "owned": owned, "traits_list": None}
        own = {r["address"].lower() for r in self.db.all("SELECT address FROM wallets")}
        gas_guess = await self._gas_native(chain_id)
        ev = evaluate(self.db, rules, parsed, holding, mkt, gas_guess, own, self._cost_basis(slug, holding), approved)
        ev.update({k: ev0[k] for k in ("slug", "nft_name", "contract", "token_id", "wallet", "chain_id")})
        if not ev["qualifies"]:
            return skip("no longer meets the rules: " + ev["reason"], ev)
        # daily limits (backend enforced)
        count, value = self._daily(slug)
        if count + 1 > int(rules["max_per_day"]):
            return skip(f"daily limit reached ({int(rules['max_per_day'])} sales per 24 h)", ev)
        if value + (ev["gross_native"] or 0) > float(rules["max_value_per_day"]):
            return skip(f"daily value limit {rules['max_value_per_day']} would be exceeded", ev)
        # 8: ask OpenSea for the fulfilment and validate it against what we evaluated
        try:
            ff = await self.api.fulfillment(ev["order_hash"], parsed["chain"], parsed["protocol_address"],
                                            wallet, contract, token_id)
            txd = ff["fulfillment_data"]["transaction"]
            if int(txd["chain"]) != chain_id:
                raise seaport.SeaportError("fulfilment is for a different chain")
            expected = parsed["gross"] - parsed["market_fee"] - parsed["creator_fee"]
            seaport.validate(txd, raw, wallet, contract, token_id, parsed["currency_token"], expected)
            data = seaport.encode_fulfillment(txd)
        except (OpenSeaError, seaport.SeaportError, KeyError, ValueError, TypeError) as error:
            return skip(f"offer could not be validated: {error}", ev)
        # 9: simulate
        try:
            gas_units = int(await self.pool.call(chain_id, "eth_estimateGas", [{
                "from": wallet, "to": txd["to"], "data": "0x" + data.hex(), "value": "0x0"}]), 16)
        except RpcError as error:
            return skip(f"simulation failed: {error}", ev)
        max_fee, prio = await self.tx.fees(chain_id)
        gas_limit = math.ceil(gas_units * GAS_BUFFER)
        gas_native = gas_limit * max_fee / 1e18
        # 10: rules again with the real gas cost
        ev2 = evaluate(self.db, rules, parsed, holding, mkt, gas_native, own, self._cost_basis(slug, holding), approved)
        if not ev2["qualifies"]:
            return skip("with the real gas cost it no longer meets the rules: " + ev2["reason"], ev)
        ev.update({"gas_native": gas_native, "net": ev2["net"]})
        # 11: final gate and send
        with self.safety.lock:
            if self.safety.stopped:
                return skip("emergency STOP is active", ev)
            limits = self.safety.limits_wei()
            if gas_limit * max_fee > limits["max_gas_cost_per_tx"]:
                return skip("gas over the per-transaction limit in Settings", ev)
            if self.safety.daily_spent_wei(chain_id) + gas_limit * max_fee > limits["max_daily"]:
                return skip("over the 24h spending limit (gas)", ev)
            count, value = self._daily(slug)
            if count + 1 > int(rules["max_per_day"]) or value + (ev["gross_native"] or 0) > float(rules["max_value_per_day"]):
                return skip("daily sale limit reached", ev)
            self.reserved.setdefault(slug, []).append(ev["gross_native"] or 0)
        try:
            nonce = int(await self.pool.call(chain_id, "eth_getTransactionCount", [wallet, "pending"]), 16)
            signed = sign_eip1559_transaction(Eip1559Transaction(
                chain_id=chain_id, nonce=nonce, max_priority_fee_per_gas=prio, max_fee_per_gas=max_fee,
                gas_limit=gas_limit, target=to_checksum_address(txd["to"]), value=0, calldata=data),
                self.vault.signer(wallet))
            log_id = self._log(ev, "pending", "sent", how, signed.hash_hex, gas_native, ev2["net"])
            hist = self.db.add_history(operation="offer_accept", chain_id=chain_id, address=wallet,
                                       slug=slug, asset=f"erc721:{contract}", token_id=token_id,
                                       amount=f"{ev['gross_native']:.6g}", tx_hash=signed.hash_hex, nonce=nonce,
                                       value_wei="0", spend_wei="0", max_cost_wei=str(gas_limit * max_fee),
                                       status="submitted", recipient=ev["offerer"])
            url = await self.pool.best_url(chain_id)
            broadcast = bool((self.db.get_setting("network", {}) or {}).get("broadcast_all", True))
            try:
                await self.pool.send_raw(chain_id, url, "0x" + signed.raw.hex(), broadcast)
            except RpcError as error:
                if "already known" not in str(error).lower():
                    self._fail(slug, log_id, hist, f"send rejected: {error}")
                    return {"status": "failed", "reason": str(error)}
            return await self._track(slug, chain_id, contract, token_id, wallet, signed.hash_hex, log_id, hist,
                                     ev.get("standard") or ev0.get("standard") or "erc721")
        finally:
            self.reserved[slug].remove(ev["gross_native"] or 0)

    async def _track(self, slug: str, chain_id: int, contract: str, token_id: str, wallet: str,
                     tx_hash: str, log_id: int, hist: int, standard: str) -> dict:
        deadline = time.time() + RECEIPT_SECONDS
        while time.time() < deadline:
            await asyncio.sleep(1.0)
            try:
                receipt = await self.pool.call(chain_id, "eth_getTransactionReceipt", [tx_hash])
            except RpcError:
                continue
            if not receipt or not receipt.get("blockHash"):
                continue
            gas_cost = int(receipt["gasUsed"], 16) * int(receipt.get("effectiveGasPrice") or "0x0", 16)
            if receipt.get("status") != "0x1":
                self.db.update_history(hist, status="failed", gas_cost_wei=str(gas_cost), error="reverted")
                self._fail(slug, log_id, None, "transaction reverted")
                return {"status": "failed", "reason": "transaction reverted", "tx_hash": tx_hash}
            still = await seaport.owns(self.pool, chain_id, contract, wallet, token_id, standard)
            self.db.update_history(hist, status="confirmed", gas_used=int(receipt["gasUsed"], 16),
                                   gas_cost_wei=str(gas_cost))
            self.db.execute("UPDATE offer_log SET status = 'accepted', gas_native = ?, reason = ? WHERE id = ?",
                            (gas_cost / 1e18, "sold; NFT left the wallet" if not still
                             else "confirmed, but the NFT still shows in the wallet — check the explorer", log_id))
            self.db.execute("UPDATE offer_rules SET fail_streak = 0 WHERE slug = ?", (slug,))
            self.db.execute("DELETE FROM os_holdings WHERE chain_id = ? AND lower(contract) = lower(?) AND token_id = ? "
                            "AND lower(address) = lower(?)", (chain_id, contract, token_id, wallet))
            self.db.execute("DELETE FROM offers_current WHERE lower(contract) = lower(?) AND token_id = ? AND "
                            "lower(wallet) = lower(?)", (contract, token_id, wallet))
            self.bus.publish("offer_log", status="accepted", slug=slug, token_id=token_id, tx_hash=tx_hash)
            return {"status": "accepted", "tx_hash": tx_hash}
        self.db.execute("UPDATE offer_log SET reason = 'sent; not confirmed yet — check the explorer' WHERE id = ?", (log_id,))
        return {"status": "pending", "tx_hash": tx_hash}

    def _fail(self, slug: str, log_id: int, hist: int | None, reason: str) -> None:
        self.db.execute("UPDATE offer_log SET status = 'failed', reason = ? WHERE id = ?", (reason, log_id))
        if hist:
            self.db.update_history(hist, status="failed", error=reason)
        self._ensure_row(slug)
        self.db.execute("UPDATE offer_rules SET fail_streak = fail_streak + 1 WHERE slug = ?", (slug,))
        if self.state(slug)["fail_streak"] >= FAILURE_PAUSE:
            self.pause(slug, f"{FAILURE_PAUSE} failed acceptances in a row")

    # ----- approvals (explicit, per wallet and collection) -----------------------------
    def approval_items(self, chain_id: int, wallet: str, contract: str, operator: str, approve: bool = True):
        from .txengine import TxItem
        slug_row = self.db.one("SELECT slug FROM os_holdings WHERE lower(contract) = lower(?) LIMIT 1", (contract,))
        label = f"{'Allow' if approve else 'Revoke'} OpenSea to transfer {slug_row['slug'] if slug_row else contract[:10]}"
        return [TxItem("approval", to_checksum_address(wallet), to_checksum_address(contract),
                       to_checksum_address(operator), f"erc721:{contract}", label, "—",
                       data=seaport.approval_calldata(to_checksum_address(operator), approve))]

    # ----- dashboard --------------------------------------------------------------------
    def dashboard(self, wallet: str | None = None) -> dict:
        """All offers, or only those on one wallet's NFTs (``wallet``)."""
        rows = []
        for r in self.db.all("SELECT evaluation FROM offers_current ORDER BY net_native DESC"):
            e = json.loads(r["evaluation"])
            e.pop("raw", None)
            if wallet and (e.get("wallet") or "").lower() != wallet.lower():
                continue
            rows.append(e)
        held = self.db.all("SELECT DISTINCT slug FROM os_holdings" + (" WHERE address = ?" if wallet else ""),
                           (wallet,) if wallet else ())
        slugs = sorted({r["slug"] for r in held} | (set() if wallet else {
            r["slug"] for r in self.db.all("SELECT slug FROM offer_rules WHERE slug != '*'")}))
        collections = []
        for slug in slugs:
            st = self.state(slug)
            rules = rules_for(self.db, slug)
            snap = market.latest(self.db, slug)
            count, value = self._daily(slug)
            collections.append({"slug": slug, "rules": rules, "armed": bool(st["armed"]), "paused": st["paused"],
                                "fail_streak": st["fail_streak"], "floor": snap["floor_native"] if snap else None,
                                "sales_median": snap["sales_median"] if snap else None,
                                "today_count": count, "today_value": value,
                                "held": self.db.one("SELECT COUNT(*) AS n FROM os_holdings WHERE slug = ?"
                                                    + (" AND address = ?" if wallet else ""),
                                                    (slug, wallet) if wallet else (slug,))["n"]})
        log = self.db.all("SELECT * FROM offer_log" + (" WHERE wallet = ?" if wallet else "")
                          + " ORDER BY id DESC LIMIT 200", (wallet,) if wallet else ())
        q = [r for r in rows if r["qualifies"]]
        best_per_nft: dict = {}
        for r in rows:
            k = (r["contract"].lower(), r["token_id"], r["wallet"])
            if k not in best_per_nft or (r["net"] or -1e9) > (best_per_nft[k]["net"] or -1e9):
                best_per_nft[k] = r
        return {
            "summary": {
                "active": len(rows), "qualifying": len(q), "below": len(rows) - len(q),
                "gross": sum(r["gross_native"] or 0 for r in q), "net": sum(r["net"] or 0 for r in q),
                "accepted": sum(1 for r in log if r["status"] == "accepted"),
                "failed": sum(1 for r in log if r["status"] == "failed"),
                "awaiting": sum(1 for r in q if r["has_key"]),
                "auto_collections": sum(1 for c in collections if c["armed"] and c["rules"]["execution_mode"] == "full"),
            },
            "offers": rows, "best": [r["order_hash"] + ":" + r["token_id"] for r in best_per_nft.values()],
            "collections": collections, "defaults": rules_for(self.db, "*"), "log": log,
            "last_scan": self.last_scan, "api_key": self.api.configured, "settings": self.settings(),
            "wallets": self.db.all("SELECT h.address, COALESCE(w.label, '') AS label, COUNT(*) AS nfts "
                                   "FROM os_holdings h LEFT JOIN wallets w ON w.address = h.address "
                                   "GROUP BY h.address ORDER BY w.label, h.address"),
            "wallet": wallet,
        }
