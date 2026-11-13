"""Scheduled mints: persistence, single execution, modes, missed windows, reports, recovery."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fakes import FakeOpenSea, allowlist_metadata

from nft_ops import collections, eligibility
from nft_ops.db import Database
from nft_ops.mint import MintEngine
from nft_ops.notify import Notifier
from nft_ops.recovery import recover_interrupted
from nft_ops.scheduler import ScheduleError, Scheduler

ETH = 10**18


def _make(env, start_offset=-60, n=3, eligible=1, clock=time.time):
    wallets = env.wallets.generate(n)
    for w in wallets:
        env.chain.balances[w.lower()] = ETH
    meta = allowlist_metadata(time.time() + start_offset, time.time() + start_offset + 7200)
    allow = set(wallets[:eligible])
    engine = MintEngine(env.db, env.vault, env.pool, env.bus, env.safety,
                        lambda: FakeOpenSea(meta, allow), env.gateway_factory())
    notifier = Notifier(env.db, env.bus)

    async def rescan(slug, addresses):
        await eligibility.scan(env.db, engine.opensea_factory, env.vault,
                               collections.get(env.db, slug), addresses, spacing_seconds=0)

    sched = Scheduler(env.db, engine, notifier, env.vault, env.safety,
                      lambda task: env.wallets.resolve_selection(json.loads(task["addresses"])),
                      rescan, clock=clock)
    return wallets, meta, engine, sched, notifier


async def _load(env, engine):
    await collections.load(env.db, engine.opensea_factory, "fake-drop", None)


async def _finish(sched, engine, tid):
    task = sched.get(tid)
    if task["job_id"]:
        await asyncio.wait_for(engine.jobs[task["job_id"]].task, 30)
    await sched.tick()
    return sched.get(tid)


def _task(wallets, **kw):
    return {"slug": "fake-drop", "stage_index": 1, "wallet_source": "addresses",
            "addresses": wallets, "quantity": 1, "max_total": "1", "mode": "auto", **kw}


def test_auto_mint_runs_once_and_reports_every_wallet(env):
    async def run():
        wallets, _meta, engine, sched, notifier = _make(env)
        env.chain.balances[wallets[1].lower()] = 0          # eligible-but-broke is not tested here
        await _load(env, engine)
        tid = sched.create(_task(wallets))
        await asyncio.gather(sched.tick(), sched.tick())    # two loops racing: runs once
        task = await _finish(sched, engine, tid)
        assert len(env.chain.sent) == 1 and env.chain.sent[0]["from"] == wallets[0].lower()
        assert task["status"] == "partial"
        rep = task["report"]
        by = {w["address"]: w for w in rep["wallets"]}
        assert by[wallets[0]]["result"] == "Minted" and by[wallets[0]]["tx_hash"]
        assert by[wallets[1]]["result"] == "Not attempted"
        assert "Not eligible" in by[wallets[2]]["reason"]
        assert rep["totals"]["success"] == 1 and rep["totals"]["minted"] == 1
        titles = [n["title"] for n in notifier.list()["notifications"]]
        assert any("report" in t for t in titles)
        await sched.tick()                                  # finished tasks never run again
        assert len(env.chain.sent) == 1
    asyncio.run(run())


def test_insufficient_gas_is_reported_not_sent(env):
    async def run():
        wallets, _meta, engine, sched, _n = _make(env, n=1, eligible=1)
        env.chain.balances[wallets[0].lower()] = 10**15     # far below price + gas
        await _load(env, engine)
        tid = sched.create(_task(wallets))
        task = await _finish(sched, engine, tid)
        assert env.chain.sent == []
        assert task["status"] == "failed" and task["report"]["status"] == "Not attempted"
        assert "Not enough funds" in task["report"]["wallets"][0]["reason"]
    asyncio.run(run())


def test_spending_cap_is_enforced_by_backend(env):
    async def run():
        wallets, _meta, engine, sched, _n = _make(env, n=1, eligible=1)
        await _load(env, engine)
        tid = sched.create(_task(wallets, max_total="0.0000001"))
        task = await _finish(sched, engine, tid)
        assert env.chain.sent == []
        assert "operation limit" in task["status_detail"]
    asyncio.run(run())


def test_auto_and_confirm_need_a_spending_cap(env):
    async def run():
        wallets, _meta, engine, sched, _n = _make(env)
        await _load(env, engine)
        for mode in ("auto", "confirm"):
            with pytest.raises(ScheduleError):
                sched.create(_task(wallets, mode=mode, max_total=""))
        sched.create(_task(wallets, mode="monitor", max_total=""))
        with pytest.raises(ScheduleError):                  # duplicate task for same wallets
            sched.create(_task(wallets, mode="monitor", max_total=""))
    asyncio.run(run())


def test_monitor_only_never_sends(env):
    async def run():
        wallets, _meta, engine, sched, _n = _make(env)
        await _load(env, engine)
        tid = sched.create(_task(wallets, mode="monitor"))
        task = await _finish(sched, engine, tid)
        assert env.chain.sent == [] and task["status"] == "done"
        assert task["report"]["status"] == "Monitor only"
    asyncio.run(run())


def test_confirm_mode_waits_for_click(env):
    async def run():
        wallets, _meta, engine, sched, _n = _make(env)
        await _load(env, engine)
        tid = sched.create(_task(wallets, mode="confirm"))
        await sched.tick()
        assert sched.get(tid)["status"] == "awaiting_confirmation" and env.chain.sent == []
        await sched.confirm(tid)
        with pytest.raises(ScheduleError):                  # second click does nothing
            await sched.confirm(tid)
        task = await _finish(sched, engine, tid)
        assert len(env.chain.sent) == 1 and task["status"] == "partial"
    asyncio.run(run())


def test_locked_vault_is_reported(env):
    async def run():
        wallets, _meta, engine, sched, _n = _make(env)
        await _load(env, engine)
        tid = sched.create(_task(wallets))
        env.vault.lock()
        await sched.tick()
        task = sched.get(tid)
        assert env.chain.sent == [] and "vault was locked" in task["status_detail"]
    asyncio.run(run())


def test_task_survives_restart_and_missed_window_is_recorded(env, tmp_path):
    async def run():
        now = [time.time()]
        wallets, _meta, engine, sched, _n = _make(env, start_offset=3600, clock=lambda: now[0])
        await _load(env, engine)
        tid = sched.create(_task(wallets))
        assert sched.get(tid)["status"] == "scheduled"
        # "restart": a fresh database connection and a fresh scheduler see the same task
        db2 = Database(tmp_path / "nft-ops.db")
        notifier2 = Notifier(db2, env.bus)
        sched2 = Scheduler(db2, engine, notifier2, env.vault, env.safety,
                           lambda task: json.loads(task["addresses"]), None, clock=lambda: now[0])
        assert sched2.get(tid)["status"] == "scheduled"
        now[0] += 3600 + 600                                 # PC was off past the start
        await sched2.tick()
        task = sched2.get(tid)
        assert task["status"] == "missed" and env.chain.sent == []
        assert "not running" in task["status_detail"]
        assert any(n["title"] == "Scheduled mint missed" for n in notifier2.list()["notifications"])
    asyncio.run(run())


def test_restart_recovery_marks_interrupted_operations(env):
    db = env.db
    job = db.execute("INSERT INTO jobs(kind, status, created_at) VALUES('mint', 'running', 0)").lastrowid
    waiting = db.add_history(job_id=job, operation="mint", chain_id=4663, address="0x1", status="waiting")
    sent = db.add_history(job_id=job, operation="mint", chain_id=4663, address="0x2", status="submitted",
                          tx_hash="0xabc")
    out = recover_interrupted(db)
    assert out == {"jobs": 1, "not_sent": 1, "in_flight": 1}
    assert db.one("SELECT status FROM jobs WHERE id = ?", (job,))["status"] == "interrupted"
    assert db.one("SELECT status, error FROM history WHERE id = ?", (waiting,))["status"] == "not_submitted"
    assert db.one("SELECT status FROM history WHERE id = ?", (sent,))["status"] == "pending"
