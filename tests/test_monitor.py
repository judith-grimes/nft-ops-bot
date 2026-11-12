"""Collection monitor: change detection, phase-open handling and supply alerts."""

from __future__ import annotations

import asyncio
import dataclasses
import time

from fakes import NFT, PRICE, FakeOpenSea, allowlist_metadata

from nft_ops import collections, eligibility
from nft_ops.monitor import Monitor


class Drop:
    def __init__(self, meta):
        self.meta = meta

    def factory(self, allow=frozenset()):
        return lambda: FakeOpenSea(self.meta, set(allow))

    def set_stage(self, **changes):
        stage = dataclasses.replace(self.meta.stages[0], **changes)
        self.meta = dataclasses.replace(self.meta, stages=(stage,))


def _kinds(mon):
    return [a["kind"] for a in mon.alerts()["alerts"]]


def test_changes_raise_alerts_once(env):
    async def run():
        drop = Drop(allowlist_metadata(time.time() + 7200, time.time() + 9000))
        mon = Monitor(env.db, env.pool, env.bus, lambda: drop.factory()())
        await collections.load(env.db, mon.factory, "fake-drop", None)
        await mon.check("fake-drop")
        assert _kinds(mon) == []                                   # first look: nothing changed
        drop.set_stage(native_price_wei=2 * PRICE, max_total_mintable_by_wallet=2)
        await mon.check("fake-drop")
        assert sorted(_kinds(mon)) == ["limit_changed", "price_changed"]
        await mon.check("fake-drop")
        assert len(_kinds(mon)) == 2                               # no duplicate alerts
        assert "0.002" in mon.alerts()["alerts"][-1]["message"] or "0.002" in \
            mon.alerts()["alerts"][0]["message"]
        assert mon.alerts()["unread"] == 2
        mon.mark_read()
        assert mon.alerts()["unread"] == 0
    asyncio.run(run())


def test_phase_open_rescans_and_counts_ready_wallets(env):
    async def run():
        wallets = env.wallets.generate(3)
        env.chain.balances[wallets[0].lower()] = 10**18             # only one wallet funded
        drop = Drop(allowlist_metadata(time.time() + 45, time.time() + 9000))   # 10- and 1-min both due
        rescans = []

        async def rescan(slug):
            rescans.append(slug)
            meta = collections.get(env.db, slug)
            return await eligibility.scan(env.db, drop.factory(wallets[:2]), env.vault, meta,
                                          wallets, spacing_seconds=0)
        mon = Monitor(env.db, env.pool, env.bus, lambda: drop.factory()(), rescan)
        await collections.load(env.db, mon.factory, "fake-drop", None)
        await mon.check("fake-drop")
        assert _kinds(mon).count("opening_soon") == 1              # opens within 10 minutes
        await mon.check("fake-drop")
        assert _kinds(mon).count("opening_soon") == 1              # not repeated
        assert mon.db.one("SELECT next_check FROM collections")["next_check"] - time.time() <= 11
        start = time.time() - 5
        drop.set_stage(start_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)))
        await mon.check("fake-drop")
        assert rescans == ["fake-drop"]
        opened = [a for a in mon.alerts()["alerts"] if a["kind"] == "phase_open"]
        assert len(opened) == 1 and "2 wallet(s) can mint, 1 funded" in opened[0]["message"]
        assert env.chain.sent == []                                # monitoring never sends
    asyncio.run(run())


def test_sold_out_on_chain_and_failure_alert(env):
    async def run():
        drop = Drop(allowlist_metadata(time.time() - 60, time.time() + 9000))
        env.chain.supply[NFT.lower()] = (90, 100)
        fail = {"on": False}

        def factory():
            client = drop.factory()()
            if fail["on"]:
                async def broken(slug):
                    from osnm_z.opensea_protocol import ErrorKind, ProtocolError
                    raise ProtocolError(ErrorKind.TRANSPORT)
                client.collection_metadata = broken
            return client
        mon = Monitor(env.db, env.pool, env.bus, factory)
        await collections.load(env.db, mon.factory, "fake-drop", None)
        snap = await mon.check("fake-drop")
        assert snap["supply"] == {"total": 90, "max": 100}
        env.chain.supply[NFT.lower()] = (100, 100)
        await mon.check("fake-drop")
        assert "sold_out" in _kinds(mon)
        fail["on"] = True
        for _ in range(4):
            await mon.check("fake-drop")
        assert _kinds(mon).count("monitor_error") == 1
    asyncio.run(run())


def test_tick_only_checks_watched_and_due(env):
    async def run():
        drop = Drop(allowlist_metadata(time.time() + 7200, time.time() + 9000))
        mon = Monitor(env.db, env.pool, env.bus, lambda: drop.factory()())
        await collections.load(env.db, mon.factory, "fake-drop", None)
        assert await mon.tick() == ["fake-drop"]
        assert await mon.tick() == []                              # not due again yet
        mon.set_watch("fake-drop", False)
        assert await mon.tick(now=time.time() + 10_000) == []
    asyncio.run(run())
