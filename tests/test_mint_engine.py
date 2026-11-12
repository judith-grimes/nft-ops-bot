"""End-to-end mint operations against the fake chain and fake OpenSea.

These run the real vendored osnm-z engine: real signing, real receipt verification,
real timing code. Only the network is fake.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fakes import PRICE, FakeOpenSea, allowlist_metadata
from osnm_z.phase_selection import stage_window

from nft_ops import eligibility
from nft_ops.mint import MintEngine, MintEngineError, MintParams

ETH = 10**18


def _engine(env, metadata, allowlist, **kw) -> MintEngine:
    return MintEngine(env.db, env.vault, env.pool, env.bus, env.safety,
                      lambda: FakeOpenSea(metadata, allowlist, **kw), env.gateway_factory())


def _setup(env, start_offset=-60, n=3, eligible=2, funded=True):
    wallets = env.wallets.generate(n)
    metadata = allowlist_metadata(time.time() + start_offset, time.time() + 3600)
    if funded:
        for w in wallets:
            env.chain.balances[w.lower()] = ETH
    return wallets, metadata, set(wallets[:eligible])


async def _scan(env, engine, metadata, wallets):
    return await eligibility.scan(env.db, engine.opensea_factory, env.vault, metadata, wallets,
                                  spacing_seconds=0)


async def _wait(engine, job_id, timeout=30):
    await asyncio.wait_for(engine.jobs[job_id].task, timeout)


def test_eligibility_scan_separates_statuses(env):
    async def run():
        wallets, metadata, allow = _setup(env)
        watch = env.wallets.add_watch_only("0x000000000000000000000000000000000000dEaD")
        engine = _engine(env, metadata, allow, fail_auth={wallets[1]})
        counts = await _scan(env, engine, metadata, wallets + [watch])
        rows = {r["address"]: r for r in eligibility.table(env.db, "fake-drop", metadata)}
        assert rows[wallets[0]]["status"] == "ELIGIBLE"
        assert rows[wallets[1]]["status"] == "UNKNOWN"        # sign-in failed: not "ineligible"
        assert rows[wallets[2]]["status"] == "NOT_ELIGIBLE"
        assert rows[watch]["status"] == "UNVERIFIED"          # no key: could not ask
        assert counts == {"ELIGIBLE": 1, "NOT_ELIGIBLE": 1, "UNKNOWN": 1, "UNVERIFIED": 1}
    asyncio.run(run())


def test_parallel_mint_confirms_only_ready_wallets(env):
    async def run():
        wallets, metadata, allow = _setup(env)
        env.chain.balances[wallets[1].lower()] = PRICE  # enough for mint, not for gas
        engine = _engine(env, metadata, allow)
        env.db.execute("INSERT INTO wallets(address,label,has_key,created_at) VALUES(?,?,0,0)",
                       ("0x000000000000000000000000000000000000dEaD", "cold"))
        await _scan(env, engine, metadata, wallets)
        preview = await engine.preview(MintParams("fake-drop", 1, wallets))
        status = {r["address"]: r["status"] for r in preview["rows"]}
        assert status == {wallets[0]: "READY", wallets[1]: "INSUFFICIENT_GAS",
                          wallets[2]: "NOT_ELIGIBLE"}
        assert preview["token"] and not preview["blocks"]
        with pytest.raises(MintEngineError):
            await engine.start(preview["token"], "yes")      # must type MINT
        job_id = await engine.start(preview["token"], "MINT")
        await _wait(engine, job_id)
        runs = engine.jobs[job_id].summary()["runs"]
        assert [r["phase"] for r in runs] == ["confirmed"]
        assert len(env.chain.sent) == 1 and env.chain.sent[0]["from"] == wallets[0].lower()
        row = env.db.one("SELECT * FROM history WHERE job_id = ?", (job_id,))
        assert row["status"] == "confirmed" and row["tx_hash"] == env.chain.sent[0]["hash"]
        assert int(row["gas_cost_wei"]) == 150_000 * 11 * 10**8
        assert engine.active_wallets == {}
        with pytest.raises(MintEngineError):                  # previews are single use
            await engine.start(preview["token"], "MINT")
    asyncio.run(run())


def test_many_wallets_mint_in_parallel(env):
    async def run():
        wallets, metadata, allow = _setup(env, n=12, eligible=12)
        env.chain.send_delay = 0.2
        engine = _engine(env, metadata, allow)
        await _scan(env, engine, metadata, wallets)
        preview = await engine.preview(MintParams("fake-drop", 1, wallets))
        assert preview["totals"]["ready"] == 12
        started = time.perf_counter()
        job_id = await engine.start(preview["token"], "MINT")
        await _wait(engine, job_id)
        elapsed = time.perf_counter() - started
        assert len(env.chain.sent) == 12
        assert len({s["from"] for s in env.chain.sent}) == 12
        assert elapsed < 12 * 0.2  # sends overlapped instead of running one after another
    asyncio.run(run())


def test_stop_cancels_runs_waiting_for_launch(env):
    async def run():
        wallets, metadata, allow = _setup(env, start_offset=120)
        engine = _engine(env, metadata, allow)
        await _scan(env, engine, metadata, wallets)
        preview = await engine.preview(MintParams("fake-drop", 1, wallets[:2]))
        job_id = await engine.start(preview["token"], "MINT")
        for _ in range(100):
            await asyncio.sleep(0.05)
            if all(r.phase == "waiting" for r in engine.jobs[job_id].runs.values()):
                break
        env.safety.stop("test")
        await _wait(engine, job_id)
        assert [r.phase for r in engine.jobs[job_id].runs.values()] == ["cancelled", "cancelled"]
        assert env.chain.sent == []
        assert engine.jobs[job_id].status == "stopped"
        # STOP survives restarts and blocks new operations until RESUME.
        preview2 = await engine.preview(MintParams("fake-drop", 1, wallets[:1]))
        assert preview2["token"] is None and "STOP" in preview2["blocks"][0]
        with pytest.raises(ValueError):
            env.safety.resume("ok")
        env.safety.resume("RESUME")
    asyncio.run(run())


def test_spending_limit_blocks_at_preview_and_at_broadcast(env):
    async def run():
        wallets, metadata, allow = _setup(env, n=2, eligible=2)
        engine = _engine(env, metadata, allow)
        await _scan(env, engine, metadata, wallets)
        env.safety.set_limits({"max_value_per_tx": "0.0005"})        # below the 0.001 price
        preview = await engine.preview(MintParams("fake-drop", 1, wallets))
        assert {r["status"] for r in preview["rows"]} == {"LIMIT_EXCEEDED"}
        assert preview["token"] is None

        env.safety.set_limits({"max_value_per_tx": "1"})
        preview = await engine.preview(MintParams("fake-drop", 1, wallets))
        assert preview["totals"]["ready"] == 2
        # Lower the daily limit after the preview: the backend gate must still refuse.
        env.safety.set_limits({"max_daily": "0.0001"})
        job_id = await engine.start(preview["token"], "MINT")
        await _wait(engine, job_id)
        phases = [r.phase for r in engine.jobs[job_id].runs.values()]
        assert phases == ["blocked", "blocked"]
        assert env.chain.sent == []
        assert "daily limit" in engine.jobs[job_id].runs[wallets[0]].message
    asyncio.run(run())


def test_operation_cap_allows_only_what_fits(env):
    async def run():
        wallets, metadata, allow = _setup(env, n=3, eligible=3)
        engine = _engine(env, metadata, allow)
        await _scan(env, engine, metadata, wallets)
        preview = await engine.preview(MintParams("fake-drop", 1, wallets))
        per_wallet = float(preview["rows"][0]["worst_case"])
        # A cap below the total blocks the preview with an explanation.
        capped = await engine.preview(MintParams("fake-drop", 1, wallets,
                                                 operation_cap=str(per_wallet * 2)))
        assert capped["token"] is None and "operation limit" in capped["blocks"][0]
    asyncio.run(run())


def test_scheduled_allowlist_waits_for_launch(env):
    async def run():
        wallets, metadata, allow = _setup(env, start_offset=4, n=2, eligible=2)
        launch = stage_window(metadata.stages[0]).starts_at  # whole seconds, as OpenSea gives it
        sent_at: list[float] = []
        original = env.chain._dispatch

        async def spy(method, params):
            if method == "eth_sendRawTransaction":
                sent_at.append(time.time())
            return await original(method, params)
        env.chain._dispatch = spy
        engine = _engine(env, metadata, allow)
        await _scan(env, engine, metadata, wallets)
        preview = await engine.preview(MintParams("fake-drop", 1, wallets))
        assert preview["stage"]["state"] == "upcoming"
        job_id = await engine.start(preview["token"], "MINT")
        await _wait(engine, job_id, timeout=40)
        assert [r.phase for r in engine.jobs[job_id].runs.values()] == ["confirmed", "confirmed"]
        # osnm-z requests OpenSea's allowlist mint data 2 s before launch, then sends at once.
        assert all(t >= launch - 2.5 for t in sent_at)
    asyncio.run(run())
