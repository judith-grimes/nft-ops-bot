"""Sweeps, distributions and NFT transfers against the fake chain (real signing, real nonces)."""

from __future__ import annotations

import asyncio
import time

import pytest

from nft_ops import addressbook, nfts, transfers
from nft_ops.txengine import TxEngine, TxEngineError

ETH = 10**18
TOKEN = "0x1111111111111111111111111111111111111111"
NFT721 = "0x2222222222222222222222222222222222222222"
NFT1155 = "0x3333333333333333333333333333333333333333"
OUTSIDE = ["0x" + f"{i:040x}" for i in range(0xA1, 0xA6)]
CHAIN = 4663


def _engine(env) -> TxEngine:
    env.safety.set_limits({"max_transfer_per_operation": "10", "max_daily": "10"})
    return TxEngine(env.db, env.vault, env.pool, env.bus, env.safety)


def _fund(env, wallets, eth=1):
    for w in wallets:
        env.chain.balances[w.lower()] = eth * ETH


async def _run(engine, kind, items, notes=(), title="t", cap=""):
    preview = await engine.preview(kind, CHAIN, items, title, cap, list(notes))
    assert preview["token"], preview["blocks"]
    job_id = await engine.start(preview["token"], "SEND")
    await asyncio.wait_for(engine.jobs[job_id].task, 30)
    return preview, engine.jobs[job_id]


def test_native_sweep_keeps_reserve_and_counts_no_spend(env):
    async def run():
        engine = _engine(env)
        w = env.wallets.generate(3)
        _fund(env, w)
        items, notes = await transfers.plan_sweep(env.db, env.pool, CHAIN, w, w[0], "native",
                                                  reserve="0.01")
        assert {i.source for i in items} == {w[1], w[2]}
        preview, job = await _run(engine, "sweep", items, notes)
        assert [r.phase for r in job.runs] == ["confirmed", "confirmed"]
        for source in w[1:]:
            left = env.chain.balances[source.lower()]
            assert 0.01 * ETH <= left < 0.011 * ETH       # reserve kept, nothing more
        assert env.chain.balances[w[0].lower()] > 2.9 * ETH
        assert env.safety.daily_spent_wei(CHAIN) < 0.001 * ETH   # own wallets: only gas counts
    asyncio.run(run())


def test_sweep_destination_must_be_allowed(env):
    async def run():
        w = env.wallets.generate(2)
        _fund(env, w)
        with pytest.raises(TxEngineError, match="address book"):
            await transfers.plan_sweep(env.db, env.pool, CHAIN, w, OUTSIDE[0], "native")
        addressbook.add(env.db, OUTSIDE[0], "cold storage")
        items, _ = await transfers.plan_sweep(env.db, env.pool, CHAIN, w, OUTSIDE[0], "native")
        assert all(i.external for i in items)
    asyncio.run(run())


def test_erc20_sweep_moves_full_balance(env):
    async def run():
        engine = _engine(env)
        w = env.wallets.generate(3)
        _fund(env, w)
        env.chain.erc20[TOKEN.lower()] = {w[1].lower(): 5_000_000, w[2].lower(): 1_500_000}
        env.db.execute("INSERT INTO tokens VALUES(?, ?, 'USDX', 6)",
                       (CHAIN, "0x1111111111111111111111111111111111111111"))
        items, _ = await transfers.plan_sweep(env.db, env.pool, CHAIN, w, w[0], TOKEN)
        assert sorted(i.amount for i in items) == ["1.5", "5"]
        _, job = await _run(engine, "sweep", items)
        assert all(r.phase == "confirmed" for r in job.runs)
        assert env.chain.erc20[TOKEN.lower()][w[0].lower()] == 6_500_000
    asyncio.run(run())


def test_distribution_modes_are_exact(env):
    async def run():
        engine = _engine(env)
        (payer,) = env.wallets.generate(1)
        _fund(env, [payer])
        rows = [{"address": a} for a in OUTSIDE[:3]]
        items, notes = await transfers.plan_distribute(env.db, env.pool, CHAIN, "equal_split",
                                                       "native", rows, payer, total="0.1")
        assert sum(i.value for i in items) == ETH // 10 and all(i.external for i in items)
        assert "spending limit" in notes[0]
        pct = [{"address": OUTSIDE[0], "percent": "50"}, {"address": OUTSIDE[1], "percent": "30"},
               {"address": OUTSIDE[2], "percent": "20"}]
        items, _ = await transfers.plan_distribute(env.db, env.pool, CHAIN, "percent", "native",
                                                   pct, payer, total="0.2")
        assert [i.value for i in items] == [ETH // 10, 6 * ETH // 100, 4 * ETH // 100]
        with pytest.raises(TxEngineError, match="add up"):
            await transfers.plan_distribute(env.db, env.pool, CHAIN, "percent", "native",
                                            pct[:2], payer, total="1")
        with pytest.raises(TxEngineError, match="decimal places"):
            await transfers.plan_distribute(env.db, env.pool, CHAIN, "equal_each", "native",
                                            rows, payer, each="0.0000000000000000001")
        _, job = await _run(engine, "distribute", items)
        assert all(r.phase == "confirmed" for r in job.runs)
        assert env.chain.balances[OUTSIDE[0]] == ETH // 10
        sent_nonces = [s for s in env.chain.sent if s["from"] == payer.lower()]
        assert len(sent_nonces) == 3                      # three sequential nonces accepted
        assert env.safety.daily_spent_wei(CHAIN) >= 2 * ETH // 10  # external payments count
    asyncio.run(run())


def test_many_to_many_and_insufficient_funds(env):
    async def run():
        engine = _engine(env)
        a, b = env.wallets.generate(2)
        _fund(env, [a])
        env.chain.balances[b.lower()] = ETH // 1000
        rows = [{"source": a, "address": OUTSIDE[0], "amount": "0.2"},
                {"source": b, "address": OUTSIDE[1], "amount": "0.5"}]
        items, _ = await transfers.plan_distribute(env.db, env.pool, CHAIN, "pairs", "native", rows)
        preview = await engine.preview("distribute", CHAIN, items, "pairs")
        status = {r["source"]: r["status"] for r in preview["rows"]}
        assert status == {a: "READY", b: "INSUFFICIENT_FUNDS"}
        short_b = next(r["shortfall"] for r in preview["rows"] if r["source"] == b)
        assert 0.499 < float(short_b) < 0.5005           # 0.5 to send + gas, minus 0.001 held
        job_id = await engine.start(preview["token"], "SEND")
        await asyncio.wait_for(engine.jobs[job_id].task, 30)
        assert [r.item.source for r in engine.jobs[job_id].runs] == [a]   # only ready rows run
    asyncio.run(run())


def test_move_limit_blocks_preview(env):
    async def run():
        engine = _engine(env)
        env.safety.set_limits({"max_transfer_per_operation": "0.05"})
        (payer,) = env.wallets.generate(1)
        _fund(env, [payer])
        items, _ = await transfers.plan_distribute(env.db, env.pool, CHAIN, "equal_each", "native",
                                                   [{"address": OUTSIDE[0]}], payer, each="0.1")
        preview = await engine.preview("distribute", CHAIN, items, "t")
        assert preview["token"] is None and "per-operation" in preview["blocks"][0]
    asyncio.run(run())


def test_stop_blocks_remaining_transfers(env):
    async def run():
        engine = _engine(env)
        (payer,) = env.wallets.generate(1)
        _fund(env, [payer])
        env.chain.send_delay = 0.3
        items, _ = await transfers.plan_distribute(env.db, env.pool, CHAIN, "equal_each", "native",
                                                   [{"address": a} for a in OUTSIDE], payer,
                                                   each="0.01")
        preview = await engine.preview("distribute", CHAIN, items, "t")
        job_id = await engine.start(preview["token"], "SEND")
        for _ in range(200):                          # wait until the 2nd send is in flight
            if sum(env.chain.sends_by_port.values()) >= 2:
                break
            await asyncio.sleep(0.01)
        env.safety.stop("test")
        await asyncio.wait_for(engine.jobs[job_id].task, 30)
        phases = [r.phase for r in engine.jobs[job_id].runs]
        assert phases[:2] == ["confirmed", "confirmed"]          # in-flight one is still tracked
        assert set(phases[2:]) == {"blocked"} and len(env.chain.sent) == 2
        with pytest.raises(TxEngineError):
            preview2 = await engine.preview("distribute", CHAIN, items[:1], "t")
            await engine.start(preview2["token"] or "", "SEND")
    asyncio.run(run())


def test_nft_721_discovery_consolidation_and_verification(env):
    async def run():
        engine = _engine(env)
        w = env.wallets.generate(3)
        _fund(env, w)
        env.chain.erc721[NFT721.lower()] = {1: w[1].lower(), 2: w[1].lower(), 3: w[2].lower(),
                                            9: OUTSIDE[0]}
        assert await nfts.detect_standard(env.pool, CHAIN, NFT721) == "erc721"
        found = await nfts.discover(env.db, env.pool, CHAIN, NFT721, "erc721", w)
        assert found["found"] == 3
        held = nfts.holdings(env.db, CHAIN, NFT721)
        selection = [{"owner": h["owner"], "token_id": h["token_id"]} for h in held]
        selection.append({"owner": w[1], "token_id": 9})          # not ours on-chain
        items, _ = await transfers.plan_nft(env.db, env.pool, CHAIN, NFT721, "erc721", selection,
                                            "consolidate", destination=w[0])
        assert [i.status for i in items].count("NOT_OWNER") == 1
        _, job = await _run(engine, "nft_transfer", items)
        assert all(r.phase == "confirmed" for r in job.runs)
        assert all("verified" in r.message for r in job.runs)
        assert {i: o for i, o in env.chain.erc721[NFT721.lower()].items() if i != 9} == \
            {1: w[0].lower(), 2: w[0].lower(), 3: w[0].lower()}
        owners = {h["owner"] for h in nfts.holdings(env.db, CHAIN, NFT721)}
        assert owners == {w[0]}
    asyncio.run(run())


def test_nft_1155_distribution_batches_per_recipient(env):
    async def run():
        engine = _engine(env)
        (holder,) = env.wallets.generate(1)
        _fund(env, [holder])
        env.chain.erc1155[NFT1155.lower()] = {(holder.lower(), 7): 5, (holder.lower(), 8): 2}
        assert await nfts.detect_standard(env.pool, CHAIN, NFT1155) == "erc1155"
        selection = [{"owner": holder, "token_id": 7, "amount": 2, "recipient": OUTSIDE[0]},
                     {"owner": holder, "token_id": 8, "amount": 1, "recipient": OUTSIDE[0]},
                     {"owner": holder, "token_id": 7, "amount": 3, "recipient": OUTSIDE[1]}]
        items, _ = await transfers.plan_nft(env.db, env.pool, CHAIN, NFT1155, "erc1155", selection,
                                            "distribute")
        assert len(items) == 2 and items[0].data[:4].hex() == "2eb2c2d6"   # batch for 2 ids
        _, job = await _run(engine, "nft_transfer", items)
        assert all(r.phase == "confirmed" for r in job.runs)
        book = env.chain.erc1155[NFT1155.lower()]
        assert book[(OUTSIDE[0], 7)] == 2 and book[(OUTSIDE[0], 8)] == 1 and book[(OUTSIDE[1], 7)] == 3
    asyncio.run(run())


def test_mint_records_token_ids_for_transfers(env):
    from fakes import NFT, FakeOpenSea, allowlist_metadata

    from nft_ops import eligibility
    from nft_ops.mint import MintEngine, MintParams

    async def run():
        w = env.wallets.generate(1)
        _fund(env, w)
        meta = allowlist_metadata(time.time() - 60, time.time() + 3600)
        engine = MintEngine(env.db, env.vault, env.pool, env.bus, env.safety,
                            lambda: FakeOpenSea(meta, set(w)), env.gateway_factory())
        await eligibility.scan(env.db, engine.opensea_factory, env.vault, meta, w, spacing_seconds=0)
        preview = await engine.preview(MintParams("fake-drop", 1, w))
        job_id = await engine.start(preview["token"], "MINT")
        await asyncio.wait_for(engine.jobs[job_id].task, 30)
        held = nfts.holdings(env.db, 4663, NFT)
        assert [(h["owner"], h["source"]) for h in held] == [(w[0], "mint")]
    asyncio.run(run())


def test_recipient_list_parsing():
    ok = addressbook.parse_recipients(f"address,amount,label\n{OUTSIDE[0]},0.1,a\n{OUTSIDE[1]},2,b")
    assert ok["ok"] and [r["amount"] for r in ok["rows"]] == ["0.1", "2"]
    plain = addressbook.parse_recipients(f"{OUTSIDE[0]}\n{OUTSIDE[1]},0.5")
    assert plain["ok"] and len(plain["rows"]) == 2 and plain["rows"][1]["amount"] == "0.5"
    bad = addressbook.parse_recipients(f"address,amount\n{OUTSIDE[0]},-1\nnope,1\n{OUTSIDE[0]},1")
    assert not bad["ok"] and len(bad["problems"]) == 2
    secret = addressbook.parse_recipients("address\n0x" + "ab" * 32)
    assert not secret["ok"] and "private key" in secret["problems"][0]
