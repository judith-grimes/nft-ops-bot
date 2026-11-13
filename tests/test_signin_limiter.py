"""OpenSea sign-in pacing: shared queue, back-off on HTTP 429, early stop of the scan."""

from __future__ import annotations

import asyncio
import time

from fakes import FakeOpenSea, allowlist_metadata

from nft_ops import eligibility
from nft_ops.os_signin import PacedOpenSeaClient, SignInLimiter, is_rate_limited
from osnm_z.config import OpenSeaConfig
from osnm_z.opensea_protocol import ErrorKind, ProtocolError


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        self.now += seconds


def test_limiter_spaces_requests_and_backs_off():
    async def run():
        clock = Clock()
        lim = SignInLimiter(spacing=1.2, base_cooldown=15, max_cooldown=60, clock=clock, sleep=clock.sleep)
        await lim.acquire()
        await lim.acquire()
        assert clock.slept == [1.2]                            # second sign-in waits its turn
        assert [lim.rate_limited() for _ in range(4)] == [15, 30, 60, 60]   # doubles, capped
        await lim.acquire()
        assert clock.slept[-1] >= 59                           # everyone waits out the pause
        lim.succeeded()
        assert lim.rate_limited() == 15                        # a success resets the back-off
    asyncio.run(run())


def test_rate_limit_detection():
    assert is_rate_limited(ProtocolError(ErrorKind.AUTHENTICATION, 429))
    assert is_rate_limited(ProtocolError(ErrorKind.RATE_LIMITED))
    assert not is_rate_limited(ProtocolError(ErrorKind.AUTHENTICATION, 403))


def test_paced_client_retries_429_once_quietly(capsys):
    async def run():
        clock = Clock()
        lim = SignInLimiter(spacing=0, base_cooldown=15, clock=clock, sleep=clock.sleep)
        client = PacedOpenSeaClient(OpenSeaConfig(retry_interval_ms=2000), lim)
        calls = 0

        async def always_429():
            nonlocal calls
            calls += 1
            raise ProtocolError(ErrorKind.AUTHENTICATION, 429)
        try:
            try:
                await client._retry_request("wallet authentication", always_429)
                raise AssertionError("should have raised")
            except ProtocolError as error:
                assert error.status == 429
        finally:
            await client.aclose()
        assert calls == 2                      # not 6 rapid-fire attempts
        assert 15 in clock.slept               # second try waited for the pause
        assert lim.status()["rate_limited_total"] == 2
    asyncio.run(run())
    assert "429" not in capsys.readouterr().out   # no console spam


def test_scan_stops_after_repeated_rate_limits(env):
    class Limited(FakeOpenSea):
        async def authenticate(self, signer, wallet, chain_id, slug):
            raise ProtocolError(ErrorKind.AUTHENTICATION, 429)

    async def run():
        wallets = env.wallets.generate(6)
        meta = allowlist_metadata(time.time() - 60, time.time() + 3600)
        made = []

        def factory():
            made.append(1)
            return Limited(meta, set())
        counts = await eligibility.scan(env.db, factory, env.vault, meta, wallets,
                                        concurrency=1, spacing_seconds=0)
        rows = eligibility.table(env.db, "fake-drop", meta)
        assert len(made) == 3                              # stopped asking after 3 refusals
        assert counts["rate_limited"] is True
        assert len(rows) == 6 and all(r["status"] == "UNKNOWN" for r in rows)
        assert all(r["detail"] == eligibility.RATE_LIMITED_DETAIL for r in rows)
    asyncio.run(run())


def test_scan_keeps_going_when_rate_limits_are_not_consecutive(env):
    async def run():
        wallets = env.wallets.generate(4)
        meta = allowlist_metadata(time.time() - 60, time.time() + 3600)
        n = {"i": 0}

        class Sometimes(FakeOpenSea):
            async def authenticate(self, signer, wallet, chain_id, slug):
                n["i"] += 1
                if n["i"] % 2:
                    raise ProtocolError(ErrorKind.RATE_LIMITED)
                await super().authenticate(signer, wallet, chain_id, slug)
        counts = await eligibility.scan(env.db, lambda: Sometimes(meta, set(wallets)), env.vault,
                                        meta, wallets, concurrency=1, spacing_seconds=0)
        assert "rate_limited" not in counts
        assert counts["ELIGIBLE"] == 2 and counts["UNKNOWN"] == 2
    asyncio.run(run())
