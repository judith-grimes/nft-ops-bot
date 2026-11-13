"""Readiness scores, scan history, calendar and health come from real checks and data."""

from __future__ import annotations

import asyncio
import time

from fakes import FakeOpenSea, allowlist_metadata

from nft_ops import collections, eligibility, insights
from nft_ops.mint import MintEngine

ETH = 10**18


def _setup(env, n=3, eligible=2, start=-60):
    wallets = env.wallets.generate(n)
    meta = allowlist_metadata(time.time() + start, time.time() + start + 7200)
    allow = set(wallets[:eligible])
    engine = MintEngine(env.db, env.vault, env.pool, env.bus, env.safety,
                        lambda: FakeOpenSea(meta, allow), env.gateway_factory())
    return wallets, meta, engine


def test_readiness_scores_reflect_real_checks(env):
    async def run():
        wallets, meta, engine = _setup(env)
        env.chain.balances[wallets[0].lower()] = ETH            # eligible + funded
        env.chain.balances[wallets[1].lower()] = 10**15         # eligible, too little for fees
        env.chain.balances[wallets[2].lower()] = ETH            # not eligible
        await collections.load(env.db, engine.opensea_factory, "fake-drop", None)
        await eligibility.scan(env.db, engine.opensea_factory, env.vault, meta, wallets, spacing_seconds=0)
        r = await insights.readiness(env.db, engine, env.pool, env.safety, "fake-drop", 1, wallets)
        by = {w["address"]: w for w in r["wallets"]}
        assert by[wallets[0]]["status"] == "Ready" and by[wallets[0]]["score"] == 100
        poor = by[wallets[1]]
        assert poor["status"] == "Not Ready" and poor["score"] < 100
        gas = next(c for c in poor["checks"] if c["key"] == "gas")
        assert gas["state"] == "fail" and gas["fix"]
        assert by[wallets[2]]["status"] == "Not Ready"
        assert next(c for c in by[wallets[2]]["checks"] if c["key"] == "eligibility")["state"] == "fail"
        saved = insights.last_readiness(env.db, "fake-drop", 1)
        assert len(saved) == 3 and {s["status"] for s in saved} == {"Ready", "Not Ready"}
    asyncio.run(run())


def test_unscanned_wallet_needs_attention_not_ineligible(env):
    async def run():
        wallets, _meta, engine = _setup(env, n=1)
        env.chain.balances[wallets[0].lower()] = ETH
        await collections.load(env.db, engine.opensea_factory, "fake-drop", None)
        r = await insights.readiness(env.db, engine, env.pool, env.safety, "fake-drop", 1, wallets)
        w = r["wallets"][0]
        assert w["status"] == "Needs Attention"
        assert next(c for c in w["checks"] if c["key"] == "eligibility")["state"] == "unverified"
    asyncio.run(run())


def test_scan_history_is_kept(env):
    async def run():
        wallets, meta, engine = _setup(env)
        await collections.load(env.db, engine.opensea_factory, "fake-drop", None)
        counts = await eligibility.scan(env.db, engine.opensea_factory, env.vault, meta, wallets[:1],
                                        spacing_seconds=0)
        first = insights.record_scan(env.db, "fake-drop", "group a", counts)
        eligibility.clear(env.db, "fake-drop")
        counts = await eligibility.scan(env.db, engine.opensea_factory, env.vault, meta, wallets[1:],
                                        spacing_seconds=0)
        insights.record_scan(env.db, "fake-drop", "group b", counts)
        history = insights.scans(env.db, "fake-drop")
        assert [h["label"] for h in history] == ["group b", "group a"]
        old = insights.scan_results(env.db, first)
        assert {r["address"] for r in old["results"]} == {wallets[0]}   # older scan still readable
    asyncio.run(run())


def test_calendar_and_health(env):
    async def run():
        _wallets, _meta, engine = _setup(env, start=3600)
        await collections.load(env.db, engine.opensea_factory, "fake-drop", None)
        events = insights.calendar(env.db)
        assert events and events[0]["kind"] == "phase" and events[0]["slug"] == "fake-drop"
        h = insights.health(env.db, {}, {}, False)
        assert h["scheduler"]["ok"] is False and h["scheduler"]["text"] == "Not running"
    asyncio.run(run())
