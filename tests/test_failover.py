"""The primary RPC dies mid-operation; mints and transfers finish through the backup."""

from __future__ import annotations

import asyncio
import time

from fakes import FakeOpenSea, allowlist_metadata

from nft_ops import eligibility, transfers
from nft_ops.mint import MintEngine, MintParams
from nft_ops.txengine import TxEngine

BACKUP = "http://127.0.0.1:8546"


def _add_backup(env):
    env.db.execute("INSERT INTO rpc_endpoints(chain_id, url, priority) VALUES(4663, ?, 50)", (BACKUP,))


def test_mint_survives_primary_rpc_outage(env):
    async def run():
        _add_backup(env)
        wallets = env.wallets.generate(2)
        for w in wallets:
            env.chain.balances[w.lower()] = 10**18
        meta = allowlist_metadata(time.time() + 4, time.time() + 3600)
        events = []
        original = env.bus.publish
        env.bus.publish = lambda kind, **d: (events.append(kind), original(kind, **d))
        engine = MintEngine(env.db, env.vault, env.pool, env.bus, env.safety,
                            lambda: FakeOpenSea(meta, set(wallets)), env.gateway_factory())
        await env.pool.check(4663)                       # both endpoints healthy
        await eligibility.scan(env.db, engine.opensea_factory, env.vault, meta, wallets,
                               spacing_seconds=0)
        preview = await engine.preview(MintParams("fake-drop", 1, wallets))
        primary = preview_url = engine.previews[preview["token"]].rpc_url
        job_id = await engine.start(preview["token"], "MINT")
        await asyncio.sleep(1.0)
        env.chain.down_ports.add(int(primary.rsplit(":", 1)[1]))     # primary dies before launch
        await asyncio.wait_for(engine.jobs[job_id].task, 40)
        assert [r.phase for r in engine.jobs[job_id].runs.values()] == ["confirmed", "confirmed"]
        assert len(env.chain.sent) == 2                   # each wallet sent exactly once
        assert "failover" in events
        assert preview_url
    asyncio.run(run())


def test_transfer_send_falls_back_but_rpc_errors_do_not(env):
    async def run():
        _add_backup(env)
        env.safety.set_limits({"max_transfer_per_operation": "10", "max_daily": "10"})
        (payer,) = env.wallets.generate(1)
        env.chain.balances[payer.lower()] = 10**18
        engine = TxEngine(env.db, env.vault, env.pool, env.bus, env.safety)
        await env.pool.check(4663)
        items, _ = await transfers.plan_distribute(
            env.db, env.pool, 4663, "equal_each", "native",
            [{"address": "0x00000000000000000000000000000000000000a1"}], payer, each="0.01")
        preview = await engine.preview("distribute", 4663, items, "t")
        primary = engine.previews[preview["token"]].rpc_url
        env.chain.down_ports.add(int(primary.rsplit(":", 1)[1]))
        job_id = await engine.start(preview["token"], "SEND")
        await asyncio.wait_for(engine.jobs[job_id].task, 30)
        assert [r.phase for r in engine.jobs[job_id].runs] == ["confirmed"]
        assert len(env.chain.sent) == 1
    asyncio.run(run())
