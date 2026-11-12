"""Contract analyser and custom mints against a fake non-SeaDrop contract."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from fakes import CustomDrop

from nft_ops import contracts, custommint
from nft_ops.contracts import ContractError
from nft_ops.txengine import TxEngine, TxEngineError

DROP = "0x4444444444444444444444444444444444444444"
CHAIN = 4663
NO_EXPLORER = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404, json={})))


def _setup(env, sale_start=0.0, n=2):
    drop = CustomDrop(DROP, sale_start=sale_start)
    env.chain.custom[DROP.lower()] = drop
    wallets = env.wallets.generate(n)
    for w in wallets:
        env.chain.balances[w.lower()] = 10**18
    engine = TxEngine(env.db, env.vault, env.pool, env.bus, env.safety)
    return drop, wallets, engine


def test_bytecode_scan_report(env):
    async def run():
        _setup(env, sale_start=time.time() + 3600)
        report = await contracts.analyze(env.db, env.pool, CHAIN, DROP, http=NO_EXPLORER)
        assert report["confidence"] == "medium" and report["abi_source"] == "none"
        sigs = {f["signature"]: f for f in report["mint_functions"]}
        assert set(sigs) == {"mint(uint256)", "presaleMint(uint256,bytes32[])"}
        assert "Merkle" in sigs["presaleMint(uint256,bytes32[])"]["needs"]
        assert report["price_wei"] == str(2 * 10**15)
        assert report["reads"]["maxSupply()"] == "100" and report["name"] == "Custom Drop"
        assert any("not open yet" in r for r in report["restrictions"])
        assert contracts.reports(env.db)[0]["address"] == DROP.replace("0x4", "0x4")
    asyncio.run(run())


def test_pasted_abi_gives_high_confidence_and_drops_absent_functions(env):
    async def run():
        _setup(env)
        abi = [{"type": "function", "name": "mint", "stateMutability": "payable",
                "inputs": [{"name": "quantity", "type": "uint256"}], "outputs": []},
               {"type": "function", "name": "mintGhost", "stateMutability": "payable",
                "inputs": [], "outputs": []},
               {"type": "function", "name": "cost", "stateMutability": "view", "inputs": [],
                "outputs": [{"name": "", "type": "uint256"}]}]
        report = await contracts.analyze(env.db, env.pool, CHAIN, DROP, json.dumps(abi))
        assert report["confidence"] == "high" and report["abi_source"] == "ABI you pasted"
        assert [f["signature"] for f in report["mint_functions"]] == ["mint(uint256)"]
        assert report["mint_functions"][0]["inputs"][0]["name"] == "quantity"
        with pytest.raises(ContractError):
            await contracts.analyze(env.db, env.pool, CHAIN, DROP, "not json")
        with pytest.raises(ContractError, match="no contract"):
            await contracts.analyze(env.db, env.pool, CHAIN,
                                    "0x5555555555555555555555555555555555555555", http=NO_EXPLORER)
    asyncio.run(run())


def test_open_sale_custom_mint_is_simulated_confirmed_and_proven(env):
    async def run():
        drop, wallets, engine = _setup(env)
        items, notes = custommint.plan(env.db, CHAIN, DROP, "mint(uint256)", ["{quantity}"], 2,
                                       "0.002", wallets)
        preview = await engine.preview("custom_mint", CHAIN, items, "Custom mint")
        assert preview["confirm_word"] == "MINT" and preview["totals"]["ready"] == 2
        with pytest.raises(TxEngineError, match="MINT"):
            await engine.start(preview["token"], "SEND")
        job_id = await engine.start(preview["token"], "MINT")
        await asyncio.wait_for(engine.jobs[job_id].task, 30)
        runs = engine.jobs[job_id].runs
        assert [r.phase for r in runs] == ["confirmed", "confirmed"]
        assert all("minted 2 NFT(s)" in r.message for r in runs)
        assert drop.total == 4
        # Mint value counts toward the daily limit (it leaves your wallets).
        assert env.safety.daily_spent_wei(CHAIN) >= 4 * 2 * 10**15
    asyncio.run(run())


def test_wrong_price_fails_simulation_and_sends_nothing(env):
    async def run():
        _, wallets, engine = _setup(env)
        items, _ = custommint.plan(env.db, CHAIN, DROP, "mint(uint256)", ["{quantity}"], 1,
                                   "0.001", wallets)
        preview = await engine.preview("custom_mint", CHAIN, items, "t")
        assert {r["status"] for r in preview["rows"]} == {"SIMULATION_FAILED"}
        assert "wrong payment" in preview["rows"][0]["reason"] and preview["token"] is None
        assert env.chain.sent == []
    asyncio.run(run())


def test_scheduled_mint_sends_at_launch(env):
    async def run():
        launch = time.time() + 3
        drop, wallets, engine = _setup(env, sale_start=launch)
        sent_at = []
        original = env.chain._dispatch

        async def spy(method, params):
            if method == "eth_sendRawTransaction":
                sent_at.append(time.time())
            return await original(method, params)
        env.chain._dispatch = spy
        items, _ = custommint.plan(env.db, CHAIN, DROP, "mint(uint256)", ["{quantity}"], 1, "0.002",
                                   wallets, start_at=launch, gas_limit=200_000)
        preview = await engine.preview("custom_mint", CHAIN, items, "t")
        assert preview["totals"]["ready"] == 2 and "fails now" in preview["rows"][0]["reason"]
        job_id = await engine.start(preview["token"], "MINT")
        await asyncio.sleep(1)
        assert {r.phase for r in engine.jobs[job_id].runs} == {"waiting"}
        await asyncio.wait_for(engine.jobs[job_id].task, 30)
        assert [r.phase for r in engine.jobs[job_id].runs] == ["confirmed", "confirmed"]
        assert all(launch <= t < launch + 1.5 for t in sent_at)
    asyncio.run(run())


def test_scheduled_mint_skips_if_still_closed_and_stop_cancels_waiting(env):
    async def run():
        drop, wallets, engine = _setup(env, sale_start=time.time() + 3600)   # never opens in time
        items, _ = custommint.plan(env.db, CHAIN, DROP, "mint(uint256)", ["{quantity}"], 1, "0.002",
                                   wallets[:1], start_at=time.time() + 2, gas_limit=200_000)
        preview = await engine.preview("custom_mint", CHAIN, items, "t")
        job_id = await engine.start(preview["token"], "MINT")
        await asyncio.wait_for(engine.jobs[job_id].task, 30)
        run_ = engine.jobs[job_id].runs[0]
        assert run_.phase == "not_submitted" and "sale not active" in run_.message
        assert env.chain.sent == []                                     # no gas wasted
        items, _ = custommint.plan(env.db, CHAIN, DROP, "mint(uint256)", ["{quantity}"], 1, "0.002",
                                   wallets[1:], start_at=time.time() + 60, gas_limit=200_000)
        preview = await engine.preview("custom_mint", CHAIN, items, "t")
        job_id = await engine.start(preview["token"], "MINT")
        await asyncio.sleep(0.5)
        env.safety.stop("test")
        await asyncio.wait_for(engine.jobs[job_id].task, 5)
        assert engine.jobs[job_id].runs[0].phase == "cancelled" and env.chain.sent == []
    asyncio.run(run())


def test_presale_with_proofs(env):
    async def run():
        drop, wallets, engine = _setup(env, n=2)
        proof = "0x" + "ab" * 32
        drop.proofs[wallets[0].lower()] = proof
        proofs = json.dumps({wallets[0]: [proof]})
        items, notes = custommint.plan(env.db, CHAIN, DROP, "presaleMint(uint256,bytes32[])",
                                       ["{quantity}", "{proof}"], 1, "0.002", wallets, proofs)
        assert [i.status for i in items] == ["READY", "NO_PROOF"] and "skipped" in notes[1]
        preview = await engine.preview("custom_mint", CHAIN, items, "t")
        job_id = await engine.start(preview["token"], "MINT")
        await asyncio.wait_for(engine.jobs[job_id].task, 30)
        assert [r.phase for r in engine.jobs[job_id].runs] == ["confirmed"]
        with pytest.raises(ContractError, match="proofs"):
            custommint.plan(env.db, CHAIN, DROP, "presaleMint(uint256,bytes32[])",
                            ["{quantity}", "{proof}"], 1, "0.002", wallets, "")
    asyncio.run(run())


def test_signature_and_argument_validation():
    assert contracts.canonical("mint( uint256 , bytes32[] )") == "mint(uint256,bytes32[])"
    with pytest.raises(ContractError):
        contracts.parse_signature("mint(uint256")
    with pytest.raises(ContractError, match="struct"):
        contracts.parse_signature("claim((bytes32[],uint256))")
    with pytest.raises(ContractError):
        contracts.coerce("uint256", "-1")
    with pytest.raises(ContractError):
        contracts.coerce("bytes32", "0x1234")
    assert contracts.coerce("bytes32[]", "0x" + "11" * 32 + " 0x" + "22" * 32)[1] == b"\x22" * 32
