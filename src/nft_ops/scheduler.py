"""Scheduled mints: configure once, the app monitors and runs it (audit C1, H2, H3, H5, H6).

A task lives in the ``scheduled_mints`` table, so it survives restarts and page refreshes.
A background loop (``tick`` every few seconds) moves each task through its life:

  scheduled ──(30 min before)──► monitoring ──(run_at)──► running ──► done / partial / failed
                                                    │
                                                    ├─ monitor mode ──► done (no transaction)
                                                    └─ confirm mode ──► awaiting_confirmation
                                                                         └─(you click)─► running
  any time: paused / cancelled; too late after a restart ──► missed (never silently skipped)

Safety rules:
* Execution goes through the normal mint preview and start, so every backend check still
  applies: readiness, single-use token, spending limits, STOP and the pre-broadcast gate.
* A task is claimed with one conditional UPDATE (``WHERE status IN ...``), so it can never run
  twice, even if two loops race.
* Automatic and Notify & Confirm tasks need a maximum total spend; it becomes the operation cap.
* If the official phase time changes, the task is paused and you are asked to confirm.
* Readiness is checked again at execution; an old readiness result is never trusted.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from osnm_z.phase_selection import stage_name, stage_window

from . import collections
from .db import Database
from .mint import MintEngine, MintJob, params_from
from .notify import Notifier
from .safety import Safety, from_wei, to_wei
from .vault import Vault

MODES = {"monitor": "Monitor only", "confirm": "Notify & confirm", "auto": "Automatic mint"}
WAITING = ("scheduled", "monitoring")
OPEN = ("scheduled", "monitoring", "awaiting_confirmation", "running", "paused")
PRECHECK_BEFORE = 30 * 60        # start checking readiness 30 minutes before
RECHECK_EVERY = 10 * 60
MISSED_GRACE = 120               # how late (after the official start) a task may still start
CONFIRM_WINDOW = 3600            # how long a Notify & Confirm task waits if the phase has no end
PHASE_MOVED = 60                 # seconds of change in the official start that pause a task

READINESS_TEXT = {
    "READY": "Ready",
    "NOT_ELIGIBLE": "Not eligible for this phase",
    "ELIGIBILITY_UNKNOWN": "Could not verify eligibility",
    "INSUFFICIENT_BALANCE": "Not enough funds for the mint price",
    "INSUFFICIENT_GAS": "Not enough funds for gas",
    "ALLOCATION_EXCEEDED": "Allocation used up or too small",
    "LIMIT_EXCEEDED": "Over a spending limit",
    "NO_KEY": "Watch-only wallet (no key)",
    "ALREADY_RUNNING": "Busy in another operation",
    "PHASE_NOT_ACTIVE": "Phase has ended",
    "SOLD_OUT": "Sold out",
    "RPC_ERROR": "Could not read the balance (RPC problem)",
    "SIMULATION_FAILED": "Test run failed",
}
RESULT_TEXT = {
    "confirmed": "Minted",
    "failed": "Transaction failed",
    "pending": "Sent, waiting for confirmation",
    "not_submitted": "Not sent",
    "blocked": "Blocked by a safety limit",
    "cancelled": "Cancelled before sending",
}


class ScheduleError(Exception):
    pass


def tz_ok(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError(f"unknown timezone {name!r}") from None
    return name


class Scheduler:
    def __init__(self, db: Database, mint: MintEngine, notifier: Notifier, vault: Vault,
                 safety: Safety, resolve: Callable[[dict], list[str]],
                 rescan: Callable[[str, list[str]], Awaitable[Any]] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.db, self.mint, self.notifier, self.vault, self.safety = db, mint, notifier, vault, safety
        self.resolve = resolve          # task -> wallet addresses
        self.rescan = rescan            # (slug, addresses) -> eligibility scan
        self.clock = clock
        self._lock: asyncio.Lock | None = None     # one readiness check / execution at a time

    # ===== create / edit =================================================================
    def create(self, data: dict) -> int:
        slug = str(data.get("slug") or "")
        meta = collections.get(self.db, slug)
        stage = next((s for s in meta.stages if s.stage_index == int(data.get("stage_index", -1))), None)
        if stage is None:
            raise ScheduleError("choose a phase of this collection")
        mode = str(data.get("mode") or "monitor")
        if mode not in MODES:
            raise ScheduleError("mode must be monitor, confirm or auto")
        source = str(data.get("wallet_source") or "all")
        if source not in ("all", "group", "addresses"):
            raise ScheduleError("choose which wallets to use")
        group = str(data.get("group") or "") or None
        addresses = [str(a) for a in data.get("addresses") or []]
        if source == "group" and not group:
            raise ScheduleError("choose a wallet group")
        if source == "addresses" and not addresses:
            raise ScheduleError("select at least one wallet")
        quantity = int(data.get("quantity") or 1)
        if not 1 <= quantity <= 1000:
            raise ScheduleError("quantity per wallet must be 1-1000")
        max_total = str(data.get("max_total") or "").strip()
        if mode != "monitor":
            if not max_total or to_wei(max_total) <= 0:
                raise ScheduleError("set a maximum total spend for this task")
        elif max_total:
            to_wei(max_total)
        lead = int(data.get("lead_seconds", 120))
        if not 30 <= lead <= 3600:
            raise ScheduleError("start-early time must be 30-3600 seconds")
        window = stage_window(stage)
        now = self.clock()
        if data.get("run_at"):
            run_at = float(data["run_at"])
        else:
            run_at = window.starts_at - lead
        if window.ends_at is not None and now >= window.ends_at:
            raise ScheduleError("this phase has already ended")
        if run_at < now - 5:
            run_at = now            # phase already open: run now
        timezone = tz_ok(str(data.get("timezone") or "Asia/Jakarta"))
        group_key = group if source == "group" else source
        dup = self.db.one(
            "SELECT id FROM scheduled_mints WHERE slug = ? AND stage_index = ? AND wallet_source = ? "
            "AND COALESCE(group_name, '') = ? AND status IN ('scheduled','monitoring',"
            "'awaiting_confirmation','running','paused')",
            (slug, stage.stage_index, source, group or ""))
        if dup:
            raise ScheduleError(f"task #{dup['id']} already covers this phase and wallets "
                                f"({group_key}); edit or cancel it instead")
        name = str(data.get("name") or "").strip() or f"{meta.slug} · {stage_name(stage)}"
        cur = self.db.execute(
            "INSERT INTO scheduled_mints(name, slug, stage_index, stage_name, stage_type, chain_id, "
            "wallet_source, group_name, addresses, quantity, max_total, mode, phase_starts_at, "
            "phase_ends_at, run_at, lead_seconds, timezone, status, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?)",
            (name, slug, stage.stage_index, stage_name(stage), stage.stage_type, meta.network_id,
             source, group, json.dumps(addresses), quantity, max_total, mode, window.starts_at,
             window.ends_at, run_at, lead, timezone, now, now))
        tid = int(cur.lastrowid)
        self.notifier.send("schedule", "info", "Mint scheduled",
                           f"{name}: {MODES[mode]}, starts {self._when(run_at, timezone)}.",
                           link=f"schedules:{tid}", slug=slug)
        return tid

    def update(self, tid: int, data: dict) -> None:
        task = self._get(tid)
        if task["status"] not in ("scheduled", "monitoring", "paused"):
            raise ScheduleError("only waiting or paused tasks can be edited")
        fields: dict[str, Any] = {}
        if "mode" in data:
            if data["mode"] not in MODES:
                raise ScheduleError("mode must be monitor, confirm or auto")
            fields["mode"] = data["mode"]
        if "quantity" in data:
            q = int(data["quantity"])
            if not 1 <= q <= 1000:
                raise ScheduleError("quantity per wallet must be 1-1000")
            fields["quantity"] = q
        if "max_total" in data:
            fields["max_total"] = str(data["max_total"]).strip()
            if fields["max_total"]:
                to_wei(fields["max_total"])
        if "timezone" in data:
            fields["timezone"] = tz_ok(str(data["timezone"]))
        if "lead_seconds" in data:
            lead = int(data["lead_seconds"])
            if not 30 <= lead <= 3600:
                raise ScheduleError("start-early time must be 30-3600 seconds")
            fields["lead_seconds"] = lead
            fields["run_at"] = (task["phase_starts_at"] or task["run_at"] + task["lead_seconds"]) - lead
        mode = fields.get("mode", task["mode"])
        cap = fields.get("max_total", task["max_total"])
        if mode != "monitor" and (not cap or to_wei(cap) <= 0):
            raise ScheduleError("set a maximum total spend for this task")
        if fields:
            fields["updated_at"] = self.clock()
            sets = ", ".join(f"{k} = ?" for k in fields)
            self.db.execute(f"UPDATE scheduled_mints SET {sets} WHERE id = ?",
                            (*fields.values(), tid))

    def pause(self, tid: int) -> None:
        task = self._get(tid)
        if task["status"] not in ("scheduled", "monitoring", "awaiting_confirmation"):
            raise ScheduleError("only waiting tasks can be paused")
        self._set(tid, "paused", "paused by you", paused_from=task["status"])

    def resume(self, tid: int) -> None:
        task = self._get(tid)
        if task["status"] != "paused":
            raise ScheduleError("task is not paused")
        meta = collections.get(self.db, task["slug"])
        stage = next((s for s in meta.stages if s.stage_index == task["stage_index"]), None)
        if stage is None:
            raise ScheduleError("this phase no longer exists; cancel the task")
        window = stage_window(stage)
        if window.ends_at is not None and self.clock() >= window.ends_at:
            raise ScheduleError("this phase has ended; cancel the task")
        run_at = max(self.clock(), window.starts_at - task["lead_seconds"])
        self.db.execute("UPDATE scheduled_mints SET status = 'scheduled', status_detail = ?, "
                        "phase_starts_at = ?, phase_ends_at = ?, run_at = ?, paused_from = NULL, "
                        "updated_at = ? WHERE id = ?",
                        ("resumed", window.starts_at, window.ends_at, run_at, self.clock(), tid))

    def cancel(self, tid: int) -> None:
        task = self._get(tid)
        if task["status"] == "running":
            raise ScheduleError("the mint is already running; use STOP to halt unsent wallets")
        if task["status"] not in OPEN:
            raise ScheduleError("task already finished")
        self._set(tid, "cancelled", "cancelled by you")

    # ===== reading =======================================================================
    def list(self) -> list[dict]:
        return [self._public(r) for r in self.db.all(
            "SELECT * FROM scheduled_mints ORDER BY CASE WHEN status IN ('scheduled','monitoring',"
            "'awaiting_confirmation','running','paused') THEN 0 ELSE 1 END, run_at")]

    def get(self, tid: int) -> dict:
        return self._public(self._get(tid))

    def has_armed(self) -> bool:
        """A task that may need to sign later (keeps the vault from auto-locking)."""
        return bool(self.db.one(
            "SELECT 1 FROM scheduled_mints WHERE mode != 'monitor' AND status IN "
            "('scheduled','monitoring','awaiting_confirmation','running')"))

    # ===== background loop ===============================================================
    def recover(self) -> int:
        """After a restart: tasks that were mid-run are reported honestly."""
        n = 0
        for task in self.db.all("SELECT * FROM scheduled_mints WHERE status = 'executing'"):
            self._not_attempted(task, "NFT Ops was closed during the final check, before anything was sent")
            n += 1
        for task in self.db.all("SELECT * FROM scheduled_mints WHERE status = 'running'"):
            if task["job_id"] and task["job_id"] in self.mint.jobs:
                continue
            report = self._build_report(task, interrupted=True)
            self._set(task["id"], "interrupted", "NFT Ops was closed while this mint was running",
                      report=json.dumps(report))
            self.notifier.send("schedule", "bad", "Scheduled mint interrupted",
                               f"{task['name']}: NFT Ops was closed while it was running. "
                               f"{report['summary']}", link=f"schedules:{task['id']}", slug=task["slug"])
            n += 1
        return n

    async def tick(self) -> None:
        for task in self.db.all("SELECT * FROM scheduled_mints WHERE status IN "
                                "('scheduled','monitoring','awaiting_confirmation','running')"):
            try:
                await self._advance(task)
            except Exception as error:  # noqa: BLE001 - one task must never stop the others
                self._set(task["id"], task["status"], f"check failed: {str(error)[:200]}")

    async def _advance(self, task: dict) -> None:
        now = self.clock()
        official = task["phase_starts_at"] or task["run_at"] + task["lead_seconds"]
        status = task["status"]
        if status in WAITING:
            if now > max(task["run_at"], official) + MISSED_GRACE:
                self._missed(task, "NFT Ops was not running (or the PC was off) when this mint "
                                   "was due, so nothing was checked or sent")
            elif now >= task["run_at"]:
                await self._execute(task)
            elif now >= task["run_at"] - PRECHECK_BEFORE and (
                    not task["checked_at"] or now - task["checked_at"] >= RECHECK_EVERY):
                await self.precheck(task["id"])
        elif status == "awaiting_confirmation":
            end = task["phase_ends_at"] or (official + CONFIRM_WINDOW)
            if now >= end:
                self._missed(task, "the phase ended before you confirmed")
        elif status == "running":
            job = self.mint.jobs.get(task["job_id"])
            if job is None or job.status != "running":
                self._complete(task)

    # ===== steps =========================================================================
    def _serial(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def precheck(self, tid: int) -> dict:
        """Readiness check ahead of time (also the 'Check now' button)."""
        async with self._serial():
            return await self._precheck(tid)

    async def _precheck(self, tid: int) -> dict:
        task = self._get(tid)
        addresses = await self._ensure_eligibility(task)
        preview = await self.mint.preview(self._params(task, addresses))
        moved = abs((preview["stage"]["starts_at"] or 0) - (task["phase_starts_at"] or 0)) > PHASE_MOVED
        readiness = self._readiness(preview)
        old = json.loads(task["readiness"]) if task["readiness"] else None
        status = "monitoring" if task["status"] in WAITING else task["status"]
        detail = f"{readiness['ready']}/{readiness['total']} wallets ready"
        self.db.execute("UPDATE scheduled_mints SET readiness = ?, checked_at = ?, status = ?, "
                        "status_detail = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(readiness), self.clock(), status, detail, self.clock(), tid))
        if moved and task["status"] in WAITING:
            new = preview["stage"]["starts_at"]
            self._set(tid, "paused", "the official phase time changed; check it and press Resume",
                      paused_from=status)
            self.notifier.send("schedule", "warn", "Mint time changed",
                               f"{task['name']}: the phase now starts {self._when(new, task['timezone'])} "
                               f"(was {self._when(task['phase_starts_at'], task['timezone'])}). The task is "
                               "paused until you confirm with Resume.", link=f"schedules:{tid}",
                               slug=task["slug"])
        elif old is None or old.get("ready") != readiness["ready"]:
            not_ready = readiness["total"] - readiness["ready"]
            if not_ready:
                self.notifier.send(
                    "readiness", "warn", "Wallets need attention",
                    f"{task['name']}: {readiness['ready']} of {readiness['total']} wallets are ready. "
                    f"{self._top_reasons(readiness)}", link=f"schedules:{tid}", slug=task["slug"])
        return readiness

    async def _ensure_eligibility(self, task: dict) -> list[str]:
        """Allowlist phases: ask OpenSea about this task's own wallets once before minting."""
        addresses = self._wallets(task)
        if (self.rescan and task["stage_type"] != "PUBLIC_SALE" and not task["eligibility_checked"]
                and self.vault.unlocked and addresses):
            await self.rescan(task["slug"], addresses)
            self.db.execute("UPDATE scheduled_mints SET eligibility_checked = 1 WHERE id = ?",
                            (task["id"],))
        return addresses

    async def _execute(self, task: dict) -> None:
        tid = task["id"]
        target = "awaiting_confirmation" if task["mode"] == "confirm" else "running"
        if not self._claim(tid, WAITING, "executing"):
            return                                   # someone else took it: never twice
        task = self._get(tid)
        if self.safety.stopped:
            return self._not_attempted(task, "Emergency STOP was active, so nothing was sent")
        if task["mode"] != "monitor" and not self.vault.unlocked:
            return self._not_attempted(task, "the vault was locked, so no wallet could sign. "
                                             "Keep NFT Ops unlocked while automatic tasks are waiting")
        try:
            await self._ensure_eligibility(task)
            preview = await self.mint.preview(self._params(task, self._wallets(task)))
        except Exception as error:  # noqa: BLE001 - reported to the user below
            return self._not_attempted(task, f"the final readiness check failed: {str(error)[:200]}")
        readiness = self._readiness(preview)
        self.db.execute("UPDATE scheduled_mints SET readiness = ?, checked_at = ? WHERE id = ?",
                        (json.dumps(readiness), self.clock(), tid))
        if task["mode"] == "monitor":
            report = self._build_report({**task, "readiness": json.dumps(readiness)}, monitor=True)
            self._set(tid, "done", "monitor only: no transaction was sent", report=json.dumps(report))
            self.notifier.send("schedule", "info", "Monitor report",
                               f"{task['name']}: {report['summary']}", link=f"schedules:{tid}",
                               slug=task["slug"])
            return None
        if not preview.get("token"):
            reason = "; ".join(preview.get("blocks") or ["no wallet was ready"])
            return self._not_attempted(task, reason)
        if target == "awaiting_confirmation":
            self._set(tid, "awaiting_confirmation", f"{readiness['ready']} wallets ready; waiting for you")
            self.notifier.send("schedule", "warn", "Mint ready — confirm to send",
                               f"{task['name']}: {readiness['ready']} wallets are ready, worst case "
                               f"{preview['totals']['worst_case']}. Open the task and click Mint now.",
                               link=f"schedules:{tid}", slug=task["slug"])
            return None
        await self._start(task, preview)
        return None

    async def confirm(self, tid: int) -> int:
        """Notify & Confirm: the user clicked 'Mint now'. Readiness is checked again first."""
        if not self._claim(tid, ("awaiting_confirmation",), "executing"):
            raise ScheduleError("this task is not waiting for confirmation")
        task = self._get(tid)
        preview = await self.mint.preview(self._params(task, self._wallets(task)))
        self.db.execute("UPDATE scheduled_mints SET readiness = ?, checked_at = ? WHERE id = ?",
                        (json.dumps(self._readiness(preview)), self.clock(), tid))
        if not preview.get("token"):
            self._set(tid, "awaiting_confirmation", "; ".join(preview.get("blocks") or []))
            raise ScheduleError("; ".join(preview.get("blocks") or ["no wallet is ready"]))
        return await self._start(self._get(tid), preview)

    async def _start(self, task: dict, preview: dict) -> int:
        try:
            job_id = await self.mint.start(preview["token"], "MINT")
        except Exception as error:
            self._not_attempted(task, f"the mint could not start: {str(error)[:200]}")
            raise
        self.db.execute("UPDATE scheduled_mints SET status = 'running', job_id = ?, status_detail = ?, "
                        "updated_at = ? WHERE id = ?",
                        (job_id, f"operation #{job_id} running", self.clock(), task["id"]))
        self.notifier.send("schedule", "info", "Scheduled mint started",
                           f"{task['name']}: {preview['totals']['ready']} wallets, operation #{job_id}.",
                           link=f"schedules:{task['id']}", slug=task["slug"])
        return job_id

    def _complete(self, task: dict) -> None:
        report = self._build_report(task)
        status = {"Successful": "done", "Partially successful": "partial"}.get(report["status"], "failed")
        self._set(task["id"], status, report["summary"], report=json.dumps(report))
        level = {"done": "good", "partial": "warn"}.get(status, "bad")
        self.notifier.send("mint", level, f"Scheduled mint report — {report['status']}",
                           f"{task['name']}: {report['summary']}", link=f"schedules:{task['id']}",
                           slug=task["slug"])

    def _missed(self, task: dict, why: str) -> None:
        if not self._claim(task["id"], (task["status"],), "missed"):
            return
        report = self._build_report(task, never=why)
        self._set(task["id"], "missed", why, report=json.dumps(report))
        self.notifier.send("schedule", "bad", "Scheduled mint missed", f"{task['name']}: {why}.",
                           link=f"schedules:{task['id']}", slug=task["slug"])

    def _not_attempted(self, task: dict, why: str) -> None:
        report = self._build_report(self._get(task["id"]), never=why)
        self._set(task["id"], "failed", f"not attempted: {why}", report=json.dumps(report))
        self.notifier.send("mint", "bad", "Scheduled mint not attempted", f"{task['name']}: {why}.",
                           link=f"schedules:{task['id']}", slug=task["slug"])

    # ===== report ========================================================================
    def _build_report(self, task: dict, *, monitor: bool = False, interrupted: bool = False,
                      never: str | None = None) -> dict:
        readiness = json.loads(task["readiness"]) if task.get("readiness") else {"wallets": []}
        rows = {w["address"]: w for w in readiness.get("wallets", [])}
        wallets = []
        minted = success = failed = skipped = 0
        spent = gas = 0
        sent = {}
        if task.get("job_id") and not never:
            for h in self.db.all("SELECT * FROM history WHERE job_id = ?", (task["job_id"],)):
                sent[h["address"]] = h
        labels = {r["address"]: r["label"] for r in self.db.all("SELECT address, label FROM wallets")}
        for address in dict.fromkeys([*rows, *sent]):
            h, r = sent.get(address), rows.get(address, {})
            entry = {"address": address, "label": labels.get(address, ""), "tx_hash": None,
                     "value": None, "gas_cost": None}
            if h:
                ok = h["status"] == "confirmed"
                entry.update(tx_hash=h["tx_hash"], result=RESULT_TEXT.get(h["status"], h["status"]),
                             reason=(f"{h['quantity']} NFT{'s' if (h['quantity'] or 1) != 1 else ''}"
                                     if ok else (h["error"] or "")),
                             value=from_wei(int(h["value_wei"])) if h["value_wei"] else None,
                             gas_cost=from_wei(int(h["gas_cost_wei"])) if h["gas_cost_wei"] else None,
                             outcome="success" if ok else "pending" if h["status"] == "pending" else "failed")
                if ok:
                    success += 1
                    minted += int(h["quantity"] or 0)
                    spent += int(h["value_wei"] or 0)
                elif h["status"] != "pending":
                    failed += 1
                gas += int(h["gas_cost_wei"] or 0)
            else:
                ready = r.get("status") == "READY"
                why = (never if ready and never else
                       "monitor only" if monitor and ready else
                       READINESS_TEXT.get(r.get("status", ""), r.get("status") or "not checked"))
                detail = r.get("reason") if not ready else ""
                entry.update(result="Not attempted", reason=f"{why}" + (f" — {detail}" if detail else ""),
                             outcome="skipped")
                skipped += 1
            wallets.append(entry)
        pending = sum(1 for w in wallets if w.get("outcome") == "pending")
        if monitor:
            status = "Monitor only"
        elif never:
            status = "Not attempted"
        elif interrupted:
            status = "Interrupted"
        elif success and not failed and not pending:
            status = "Successful" if not skipped else "Partially successful"
        elif success:
            status = "Partially successful"
        elif pending:
            status = "Waiting for confirmation"
        else:
            status = "Failed"
        ready_count = readiness.get("ready", 0)
        summary = (f"{ready_count} of {readiness.get('total', len(wallets))} wallets ready; nothing sent"
                   if monitor else
                   f"not attempted — {never}" if never else
                   f"{success} succeeded, {failed} failed, {skipped} not attempted"
                   + (f", {pending} still confirming" if pending else "") + f"; {minted} NFT minted")
        return {"status": status, "summary": summary, "mode": MODES.get(task["mode"], task["mode"]),
                "collection": task["slug"], "phase": task["stage_name"], "chain_id": task["chain_id"],
                "ran_at": self.clock(), "timezone": task["timezone"], "wallets": wallets,
                "totals": {"success": success, "failed": failed, "skipped": skipped, "pending": pending,
                           "minted": minted, "spent": from_wei(spent), "gas": from_wei(gas)}}

    # ===== helpers =======================================================================
    def _claim(self, tid: int, from_states: tuple[str, ...], to_state: str) -> bool:
        marks = ",".join("?" * len(from_states))
        cur = self.db.execute(
            f"UPDATE scheduled_mints SET status = ?, updated_at = ? WHERE id = ? AND status IN ({marks})",
            (to_state, self.clock(), tid, *from_states))
        return cur.rowcount == 1

    def _set(self, tid: int, status: str, detail: str, **extra: Any) -> None:
        fields = {"status": status, "status_detail": detail, "updated_at": self.clock(), **extra}
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE scheduled_mints SET {sets} WHERE id = ?", (*fields.values(), tid))

    def _get(self, tid: int) -> dict:
        row = self.db.one("SELECT * FROM scheduled_mints WHERE id = ?", (tid,))
        if row is None:
            raise ScheduleError(f"task #{tid} not found")
        return row

    def _wallets(self, task: dict) -> list[str]:
        return [a for a in self.resolve(task) if self.vault.has(a)] if self.vault.unlocked else self.resolve(task)

    def _params(self, task: dict, addresses: list[str]):
        if not addresses:
            raise ScheduleError("no wallets with keys in this selection")
        return params_from({"slug": task["slug"], "stage_index": task["stage_index"],
                            "quantity": task["quantity"], "operation_cap": task["max_total"]},
                           addresses, self.db.get_setting("mint", {}) or {})

    @staticmethod
    def _readiness(preview: dict) -> dict:
        wallets = [{"address": r["address"], "label": r.get("label", ""), "status": r["status"],
                    "reason": r["reason"], "eligibility": r.get("eligibility"),
                    "balance": r.get("balance"), "worst_case": r.get("worst_case")}
                   for r in preview["rows"]]
        return {"ready": preview["totals"]["ready"], "total": preview["totals"]["selected"],
                "worst_case": preview["totals"]["worst_case"], "blocks": preview.get("blocks", []),
                "wallets": wallets}

    @staticmethod
    def _top_reasons(readiness: dict) -> str:
        counts: dict[str, int] = {}
        for w in readiness["wallets"]:
            if w["status"] != "READY":
                key = READINESS_TEXT.get(w["status"], w["status"])
                counts[key] = counts.get(key, 0) + 1
        return ", ".join(f"{n} × {k}" for k, n in sorted(counts.items(), key=lambda x: -x[1])[:3]) + "."

    @staticmethod
    def _when(ts: float | None, tz: str) -> str:
        if not ts:
            return "unknown"
        import datetime as dt
        return dt.datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%d %b %Y %H:%M ") + tz

    def _public(self, row: dict) -> dict:
        out = dict(row)
        out["addresses"] = json.loads(row["addresses"] or "[]")
        out["readiness"] = json.loads(row["readiness"]) if row["readiness"] else None
        out["report"] = json.loads(row["report"]) if row["report"] else None
        out["mode_label"] = MODES.get(row["mode"], row["mode"])
        out["official_label"] = self._when(row["phase_starts_at"], row["timezone"])
        out["run_label"] = self._when(row["run_at"], row["timezone"])
        return out

    def job_finished(self, job: MintJob) -> None:
        """Manual (unscheduled) mints also get a notification when they finish."""
        if self.db.one("SELECT 1 FROM scheduled_mints WHERE job_id = ?", (job.id,)):
            return                                        # the task report covers it
        runs = list(job.runs.values())
        ok = sum(1 for r in runs if r.phase == "confirmed")
        bad = [r for r in runs if r.phase in ("failed", "not_submitted", "blocked")]
        level = "good" if ok and not bad else "warn" if ok else "bad"
        reasons = "; ".join(f"{r.label or r.address[:10]}: {r.message}" for r in bad[:3])
        self.notifier.send("mint", level, f"Mint #{job.id} finished",
                           f"{job.preview.metadata.slug}: {ok} of {len(runs)} wallets minted."
                           + (f" Problems — {reasons}" if reasons else ""), link="mint",
                           slug=job.preview.metadata.slug)
