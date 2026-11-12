"""Speed up and cancel stuck transactions (same nonce, higher fees)."""

from __future__ import annotations

import asyncio

import pytest

from nft_ops import replace, transfers, txengine
from nft_ops.replace import ReplaceError, Replacer
from nft_ops.txengine import TxEngine

OUT = "0x00000000000000000000000000000000000000a1"


async def _stuck(env, monkeypatch):
    monkeypatch.setattr(txengine, "RECEIPT_TIMEOUT_SECONDS", 1.5)
    monkeypatch.setattr(replace, "POLL_SECONDS", 0.2)
    env.safety.set_limits({"max_transfer_per_operation": "10", "max_daily": "10"})
    (payer,) = env.wallets.generate(1)
    env.chain.balances[payer.lower()] = 10**18
    engine = TxEngine(env.db, env.vault, env.pool, env.bus, env.safety)
    env.chain.mining = False                          # congested: nothing gets mined
    items, _ = await transfers.plan_distribute(env.db, env.pool, 4663, "equal_each", "native",
                                               [{"address": OUT}], payer, each="0.01")
    preview = await engine.preview("distribute", 4663, items, "t")
    job_id = await engine.start(preview["token"], "SEND")
    await asyncio.wait_for(engine.jobs[job_id].task, 30)
    run = engine.jobs[job_id].runs[0]
    assert run.phase == "pending"
    return engine, run.history_id, payer


async def _wait_status(env, history_id, statuses, timeout=10):
    for _ in range(int(timeout / 0.1)):
        row = env.db.one("SELECT status FROM history WHERE id = ?", (history_id,))
        if row["status"] in statuses:
            return row["status"]
        await asyncio.sleep(0.1)
    return row["status"]


def test_speed_up_replaces_stuck_transfer(env, monkeypatch):
    async def run():
        engine, hid, payer = await _stuck(env, monkeypatch)
        r = Replacer(engine)
        p = await r.preview(hid, "speedup")
        assert p["token"] and float(p["new_max_fee_gwei"]) >= float(p["old_max_fee_gwei"]) * 1.1
        with pytest.raises(ReplaceError, match="REPLACE"):
            await r.execute(p["token"], "yes")
        res = await r.execute(p["token"], "REPLACE")
        await env.chain.mine()
        assert await _wait_status(env, hid, {"replaced"}) == "replaced"
        assert await _wait_status(env, res["history_id"], {"confirmed"}) == "confirmed"
        assert env.chain.balances[OUT] == 10**16                 # paid exactly once
        assert len(env.chain.sent) == 1
    asyncio.run(run())


def test_cancel_sends_nothing_to_recipient(env, monkeypatch):
    async def run():
        engine, hid, payer = await _stuck(env, monkeypatch)
        r = Replacer(engine)
        p = await r.preview(hid, "cancel")
        res = await r.execute(p["token"], "REPLACE")
        await env.chain.mine()
        assert await _wait_status(env, hid, {"cancelled"}) == "cancelled"
        assert await _wait_status(env, res["history_id"], {"confirmed"}) == "confirmed"
        assert env.chain.balances.get(OUT, 0) == 0               # the payment never happened
        # Nothing left to replace once it is settled.
        with pytest.raises(ReplaceError):
            await r.preview(hid, "speedup")
    asyncio.run(run())


def test_replace_respects_stop(env, monkeypatch):
    async def run():
        engine, hid, _ = await _stuck(env, monkeypatch)
        r = Replacer(engine)
        p = await r.preview(hid, "speedup")
        env.safety.stop("test")
        with pytest.raises(Exception, match="STOP"):
            await r.execute(p["token"], "REPLACE")
        assert env.db.one("SELECT COUNT(*) AS n FROM history")["n"] == 1   # no replacement row
    asyncio.run(run())
