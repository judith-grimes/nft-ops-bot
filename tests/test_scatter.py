"""Scatter (Archetype) adapter against a fake Archetype contract on the fake chain."""

from __future__ import annotations

import asyncio
import time

import pytest
from fakes import FakeScatter

from nft_ops import scatter
from nft_ops.txengine import TxEngine

DROP = "0x5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c5c"
ETH = 10**18


def _root_and_list(addresses):
    leaves = [scatter.leaf(a) for a in addresses]
    layers = scatter._tree(leaves, False)
    return layers[-1][0]


def _setup(env, allow_n=2, public_start=0):
    w = env.wallets.generate(3)
    for a in w:
        env.chain.balances[a.lower()] = ETH
    outsiders = ["0x" + f"{i:040x}" for i in range(1, 6)]
    allowlist = [w[0], *outsiders, w[1]][: allow_n + 5]
    root = _root_and_list(allowlist)
    drop = FakeScatter(DROP)
    drop.invites[(0).to_bytes(32, "big")] = {"price": 2 * 10**15, "start": public_start, "end": 0, "limit": 3, "max_supply": 100}
    drop.invites[root] = {"price": 10**15, "start": 0, "end": 0, "limit": 1, "max_supply": 50}
    env.chain.custom[DROP] = drop
    env.safety.set_limits({"max_total_per_operation": "1", "max_daily": "1"})
    return w, drop, root, allowlist


def test_detects_phases_and_prices(env):
    async def run():
        _w, _drop, root, _ = _setup(env)
        info = await scatter.inspect(env.db, env.pool, 4663, DROP)
        assert info["is_scatter"] and info["version"] == "v2" and info["name"] == "Scatter Drop"
        kinds = {p["key"]: p["public"] for p in info["phases"]}
        assert kinds["0x" + root.hex()] is False and kinds["0x" + "00" * 32] is True
    asyncio.run(run())


def test_allowlist_proofs_from_list_and_wrong_list_rejected(env):
    w, _drop, root, allowlist = _setup(env)
    key = "0x" + root.hex()
    out = scatter.load_allowlist(env.db, 4663, DROP, key, "\n".join(allowlist), w)
    assert out["yours"] == 2
    with pytest.raises(scatter.ScatterError):
        scatter.load_allowlist(env.db, 4663, DROP, key, "\n".join(allowlist[:-1]), w)   # incomplete list


def test_public_and_allowlist_mint_end_to_end(env):
    async def run():
        w, drop, root, allowlist = _setup(env)
        key = "0x" + root.hex()
        scatter.load_allowlist(env.db, 4663, DROP, key, ",".join(allowlist), w)
        tx = TxEngine(env.db, env.vault, env.pool, env.bus, env.safety)
        items, notes, _ = await scatter.plan(env.db, env.pool, 4663, DROP, key, 1, w, False)
        status = {i.source: i.status for i in items}
        assert status[w[0]] == "READY" and status[w[1]] == "READY" and status[w[2]] == "NO_PROOF"
        preview = await tx.preview("custom_mint", 4663, items, "Scatter", "", notes)
        assert preview["token"], preview["blocks"]
        job = await tx.start(preview["token"], preview["confirm_word"])
        await asyncio.wait_for(tx.jobs[job].task, 30)
        assert drop.minted_by[(w[0].lower(), root)] == 1 and drop.minted_by[(w[1].lower(), root)] == 1
        # second allowlist mint: limit 1 per wallet is respected before sending
        items, _, _ = await scatter.plan(env.db, env.pool, 4663, DROP, key, 1, w[:1], False)
        assert items[0].status == "ALLOCATION_EXCEEDED"
        # public phase: no proof, price includes the protocol fee
        items, _, info = await scatter.plan(env.db, env.pool, 4663, DROP, "0x" + "00" * 32, 2, w[2:], False)
        assert items[0].status == "READY" and int(info["value"]) == 2 * (2 * 10**15 + 10**14)
    asyncio.run(run())


def test_future_phase_needs_schedule(env):
    async def run():
        w, _drop, _root, _ = _setup(env, public_start=int(time.time()) + 3600)
        with pytest.raises(scatter.ScatterError):
            await scatter.plan(env.db, env.pool, 4663, DROP, "0x" + "00" * 32, 1, w, False)
        items, _notes, _ = await scatter.plan(env.db, env.pool, 4663, DROP, "0x" + "00" * 32, 1, w, True)
        assert items[0].scheduled_at and items[0].fixed_gas == scatter.DEFAULT_GAS
    asyncio.run(run())
