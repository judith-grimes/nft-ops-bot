"""Collection monitor: watches OpenSea drop data and on-chain supply, raises alerts.

It never sends a transaction. When a phase opens it can re-check eligibility and report
how many wallets are ready, so you can start the mint yourself.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from . import collections
from .balances import format_units
from .collections import CollectionError, OpenSeaFactory
from .db import Database
from .events import EventBus
from .networks import RpcError, RpcPool

TOTAL_SUPPLY = "0x18160ddd"
MAX_SUPPLY = "0xd5abeb01"
DEFAULT_SETTINGS = {"interval_seconds": 120, "auto_rescan": True, "warn_minutes": [10, 1]}
NEAR_LAUNCH_SECONDS = 600          # check every 10 s in the last 10 minutes before a phase
NEAR_LAUNCH_INTERVAL = 10
FAILURE_ALERT_AFTER = 3
ROUGH_GAS_WEI = 10**15             # 0.001 native for "funded" estimates in alerts only


class Monitor:
    def __init__(self, db: Database, pool: RpcPool, bus: EventBus, factory: OpenSeaFactory,
                 rescan: Callable[[str], Awaitable[dict | None]] | None = None) -> None:
        self.db, self.pool, self.bus, self.factory = db, pool, bus, factory
        self.rescan = rescan
        self._lock = asyncio.Lock()

    # ----- settings & alerts -----------------------------------------------------------
    def settings(self) -> dict:
        return {**DEFAULT_SETTINGS, **(self.db.get_setting("monitor", {}) or {})}

    def set_settings(self, values: dict) -> dict:
        current = self.settings()
        if "interval_seconds" in values:
            interval = int(values["interval_seconds"])
            if not 20 <= interval <= 3600:
                raise ValueError("check interval must be 20-3600 seconds")
            current["interval_seconds"] = interval
        if "auto_rescan" in values:
            current["auto_rescan"] = bool(values["auto_rescan"])
        self.db.set_setting("monitor", current)
        return current

    def alert(self, slug: str | None, kind: str, message: str, level: str = "info") -> None:
        cur = self.db.execute(
            "INSERT INTO alerts(created_at, slug, kind, level, message, read) VALUES(?, ?, ?, ?, ?, 0)",
            (time.time(), slug, kind, level, message))
        self.bus.publish("alert", id=int(cur.lastrowid), slug=slug, alert_kind=kind, level=level,
                         message=message)

    def alerts(self, limit: int = 100) -> dict:
        rows = self.db.all("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,))
        unread = self.db.one("SELECT COUNT(*) AS n FROM alerts WHERE read = 0")["n"]
        return {"alerts": rows, "unread": unread}

    def mark_read(self) -> None:
        self.db.execute("UPDATE alerts SET read = 1 WHERE read = 0")

    def set_watch(self, slug: str, watch: bool) -> None:
        self.db.execute("UPDATE collections SET watch = ?, next_check = ? WHERE slug = ?",
                        (int(watch), time.time(), slug))

    # ----- loop ------------------------------------------------------------------------
    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - the monitor loop must never die
                pass
            await asyncio.sleep(2)

    async def tick(self, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        due = self.db.all("SELECT slug FROM collections WHERE watch = 1 AND "
                          "(next_check IS NULL OR next_check <= ?)", (now,))
        checked = []
        for row in due:
            await self.check(row["slug"])
            checked.append(row["slug"])
        return checked

    # ----- one collection --------------------------------------------------------------
    async def check(self, slug: str) -> dict:
        async with self._lock:
            row = self.db.one("SELECT snapshot, chain_id FROM collections WHERE slug = ?", (slug,))
            if row is None:
                return {}
            old = json.loads(row["snapshot"]) if row["snapshot"] else None
            failures = (old or {}).get("failures", 0)
            try:
                meta = await collections.load(self.db, self.factory, slug, None)
            except CollectionError as error:
                failures += 1
                snap = {**(old or {}), "failures": failures}
                self._save(slug, snap, time.time() + self.settings()["interval_seconds"])
                if failures == FAILURE_ALERT_AFTER:
                    self.alert(slug, "monitor_error", f"{slug}: monitoring failed {failures} times "
                               f"in a row ({error}). Still retrying.", "warn")
                return snap
            info = collections.describe(meta)
            supply = await self._supply(meta.network_id, meta.address)
            new = {
                "stages": {str(s["index"]): {k: s[k] for k in ("name", "type", "state", "starts_at",
                                                              "ends_at", "price_wei", "max_per_wallet")}
                           for s in info["stages"]},
                "minted_out": info["minted_out"], "disabled": info["disabled"], "supply": supply,
                "warned": (old or {}).get("warned", []), "failures": 0, "checked_at": time.time(),
            }
            symbol = (self.db.one("SELECT native_symbol FROM networks WHERE chain_id = ?",
                                  (meta.network_id,)) or {"native_symbol": ""})["native_symbol"]
            opened = self._diff(slug, old, new, symbol) if old else []
            self._warn_upcoming(slug, new)
            self._save(slug, new, self._next_check(new))
            for index in opened:
                await self._on_open(slug, index, new, meta.network_id)
            return new

    def _save(self, slug: str, snap: dict, next_check: float) -> None:
        self.db.execute("UPDATE collections SET snapshot = ?, next_check = ? WHERE slug = ?",
                        (json.dumps(snap), next_check, slug))

    def _next_check(self, snap: dict) -> float:
        now = time.time()
        interval = self.settings()["interval_seconds"]
        target = now + interval
        for stage in snap["stages"].values():
            until = stage["starts_at"] - now
            if 0 < until <= NEAR_LAUNCH_SECONDS:
                target = min(target, now + NEAR_LAUNCH_INTERVAL)
            if until > 0:
                target = min(target, stage["starts_at"] + 1)   # look right after it opens
            if stage.get("ends_at") and stage["ends_at"] > now:
                target = min(target, stage["ends_at"] + 1)
        return target

    async def _supply(self, chain_id: int, contract: str) -> dict | None:
        try:
            total, maximum = await self.pool.batch(chain_id, [
                ("eth_call", [{"to": contract, "data": TOTAL_SUPPLY}, "latest"]),
                ("eth_call", [{"to": contract, "data": MAX_SUPPLY}, "latest"])])
        except RpcError:
            return None
        def word(v: Any) -> int | None:
            return int(v[2:66], 16) if isinstance(v, str) and len(v) >= 66 else None
        t, m = word(total), word(maximum)
        return None if t is None else {"total": t, "max": m}

    # ----- comparisons -----------------------------------------------------------------
    def _diff(self, slug: str, old: dict, new: dict, symbol: str) -> list[str]:
        opened = []
        for index, stage in new["stages"].items():
            before = old.get("stages", {}).get(index)
            name = stage["name"]
            if before is None:
                self.alert(slug, "phase_added", f"{slug}: new phase “{name}” added "
                           f"(opens {_when(stage['starts_at'])}).")
                if stage["state"] == "active":
                    opened.append(index)
                continue
            if before["state"] != "active" and stage["state"] == "active":
                opened.append(index)
            if before["state"] == "active" and stage["state"] == "ended":
                self.alert(slug, "phase_closed", f"{slug}: phase “{name}” has closed.")
            if before["starts_at"] != stage["starts_at"] and stage["state"] == "upcoming":
                self.alert(slug, "time_changed", f"{slug}: “{name}” start moved to "
                           f"{_when(stage['starts_at'])}.", "warn")
            if before["price_wei"] != stage["price_wei"]:
                self.alert(slug, "price_changed", f"{slug}: “{name}” price changed from "
                           f"{_price(before['price_wei'], symbol)} to {_price(stage['price_wei'], symbol)}.",
                           "warn")
            if before["max_per_wallet"] != stage["max_per_wallet"]:
                self.alert(slug, "limit_changed", f"{slug}: “{name}” per-wallet limit changed from "
                           f"{before['max_per_wallet']} to {stage['max_per_wallet']}.", "warn")
        for index, stage in old.get("stages", {}).items():
            if index not in new["stages"]:
                self.alert(slug, "phase_removed", f"{slug}: phase “{stage['name']}” was removed.", "warn")
        if new["minted_out"] and not old.get("minted_out"):
            self.alert(slug, "sold_out", f"{slug}: OpenSea reports the drop is minted out.", "warn")
        supply, before = new.get("supply"), old.get("supply")
        if (supply and supply.get("max") and supply["total"] >= supply["max"]
                and not (before and before.get("max") and before["total"] >= before["max"])):
            self.alert(slug, "sold_out", f"{slug}: sold out on-chain ({supply['total']}/{supply['max']}).",
                       "warn")
        if new["disabled"] and not old.get("disabled"):
            self.alert(slug, "disabled", f"{slug}: OpenSea disabled this drop.", "warn")
        return opened

    def _warn_upcoming(self, slug: str, snap: dict) -> None:
        now = time.time()
        warned = set(snap.get("warned", []))
        for index, stage in snap["stages"].items():
            until = stage["starts_at"] - now
            if until <= 0:
                continue
            # All thresholds already passed count as one warning (no double alert when a
            # collection is first watched shortly before it opens).
            keys = [f"{index}:{stage['starts_at']}:{m}" for m in self.settings()["warn_minutes"]
                    if until <= m * 60]
            if keys and not set(keys) <= warned:
                warned.update(keys)
                self.alert(slug, "opening_soon", f"{slug}: “{stage['name']}” opens in "
                           f"{max(1, round(until / 60))} min.")
        snap["warned"] = sorted(warned)[-50:]

    async def _on_open(self, slug: str, index: str, snap: dict, chain_id: int) -> None:
        stage = snap["stages"][index]
        is_public = stage.get("type") == "PUBLIC_SALE"
        if self.settings()["auto_rescan"] and self.rescan is not None and not is_public:
            try:
                await self.rescan(slug)
            except Exception:  # noqa: BLE001 - an alert is still useful without the rescan
                pass
        rows = self.db.all("SELECT address, max_quantity, price_wei FROM eligibility WHERE slug = ? "
                           "AND stage_index = ? AND status = 'ELIGIBLE'", (slug, int(index)))
        keyed = {r["address"] for r in self.db.all("SELECT address FROM wallets WHERE has_key = 1")}
        candidates = [r for r in rows if r["address"] in keyed and r["max_quantity"] != 0]
        if is_public or not rows:
            candidates = [{"address": a, "price_wei": stage["price_wei"]} for a in keyed]
        funded = 0
        if candidates:
            try:
                balances = await self.pool.batch(chain_id, [("eth_getBalance", [c["address"], "latest"])
                                                            for c in candidates])
                for c, b in zip(candidates, balances, strict=True):
                    price = int(c.get("price_wei") or stage["price_wei"] or 0)
                    if isinstance(b, str) and int(b, 16) >= price + ROUGH_GAS_WEI:
                        funded += 1
            except RpcError:
                funded = -1
        funded_text = "balance check failed" if funded < 0 else f"{funded} funded"
        self.alert(slug, "phase_open", f"{slug}: “{stage['name']}” is OPEN — {len(candidates)} wallet(s) "
                   f"can mint, {funded_text}. Open the Mint page to preview and start.", "good")


def _when(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def _price(wei: str | None, symbol: str) -> str:
    return "unknown" if wei is None else f"{format_units(int(wei))} {symbol}"
