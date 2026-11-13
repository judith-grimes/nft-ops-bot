"""One shared, polite queue for OpenSea wallet sign-ins and eligibility look-ups.

OpenSea limits wallet sign-ins per IP address and answers HTTP 429 ("too many requests")
when they come too fast. Everything in this process that signs a wallet in (eligibility
scans, the monitor's automatic re-check, mint preparation) goes through one SignInLimiter:

* requests leave the queue at least ``spacing`` seconds apart;
* a 429 pauses the whole queue (15 s, then 30 s, 60 s, ... up to 5 minutes);
* a success resets the pause back to the start.

We never try to get around OpenSea's limit; we only wait for it.

``PacedOpenSeaClient`` is the osnm-z client with its retry loop replaced (by subclassing,
the vendored file is unchanged) so that every attempt waits its turn in the queue, a 429 is
retried at most once, and retry warnings go to the caller's log sink instead of the console.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from osnm_z.config import DEFAULT_OPENSEA_ATTEMPTS, OpenSeaConfig
from osnm_z.hooks import emit
from osnm_z.opensea import WalletOpenSeaClient
from osnm_z.opensea_protocol import ErrorKind, ProtocolError, is_retryable_request_error

T = TypeVar("T")

# Operations that count against OpenSea's sign-in / profile limits. The time-critical mint
# calldata request does not use this retry loop and is never queued here.
PACED_OPERATIONS = {"wallet authentication", "wallet eligibility", "collection metadata",
                    "collection search"}
RATE_LIMIT_TRIES = 2          # one retry after the pause, then give up on that wallet


def is_rate_limited(error: BaseException) -> bool:
    return isinstance(error, ProtocolError) and (
        error.kind is ErrorKind.RATE_LIMITED or error.status == 429)


class SignInLimiter:
    def __init__(self, spacing: float = 1.2, base_cooldown: float = 15.0,
                 max_cooldown: float = 300.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.spacing, self.base_cooldown, self.max_cooldown = spacing, base_cooldown, max_cooldown
        self._clock, self._sleep = clock, sleep
        self._lock: asyncio.Lock | None = None
        self._next = 0.0                 # earliest time the next request may leave
        self._cooldown = 0.0             # length of the current pause (0 = none)
        self._cooldown_until = 0.0
        self.rate_limited_total = 0
        self.on_pause: Callable[[float], None] | None = None   # e.g. tell the dashboard

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def acquire(self) -> None:
        async with self._get_lock():
            wait = max(self._next, self._cooldown_until) - self._clock()
            if wait > 0:
                await self._sleep(wait)
            self._next = self._clock() + self.spacing

    def rate_limited(self) -> float:
        """OpenSea said 429: pause everyone. Returns the pause length in seconds."""
        self.rate_limited_total += 1
        self._cooldown = min(self.max_cooldown,
                             self._cooldown * 2 if self._cooldown else self.base_cooldown)
        self._cooldown_until = max(self._cooldown_until, self._clock() + self._cooldown)
        if self.on_pause:
            try:
                self.on_pause(self._cooldown)
            except Exception:  # noqa: BLE001, S110 - a UI notification must never break a request
                pass
        return self._cooldown

    def succeeded(self) -> None:
        self._cooldown = 0.0

    def status(self) -> dict:
        remaining = max(0.0, self._cooldown_until - self._clock())
        return {"cooling_down_seconds": round(remaining), "pause_seconds": round(self._cooldown),
                "rate_limited_total": self.rate_limited_total}


SHARED_LIMITER = SignInLimiter()


class PacedOpenSeaClient(WalletOpenSeaClient):
    def __init__(self, config: OpenSeaConfig, limiter: SignInLimiter) -> None:
        super().__init__(config)
        self._limiter = limiter

    async def _retry_request(self, operation_name: str,
                             operation: Callable[[], Awaitable[T]]) -> T:  # type: ignore[override]
        paced = operation_name in PACED_OPERATIONS
        attempt_limit = max(self._attempts, DEFAULT_OPENSEA_ATTEMPTS)
        limited_tries = 0
        for attempt in range(1, attempt_limit + 1):
            if paced:
                await self._limiter.acquire()
            try:
                result = await operation()
            except ProtocolError as error:
                limited = is_rate_limited(error)
                if limited:
                    limited_tries += 1
                    pause = self._limiter.rate_limited()
                if (not is_retryable_request_error(error) or attempt == attempt_limit
                        or (limited and limited_tries >= RATE_LIMIT_TRIES)):
                    raise
                if limited:
                    emit("warn", f"OpenSea is rate limiting; pausing {pause:.0f}s before retrying")
                    if not paced:
                        await asyncio.sleep(min(pause, 5.0))
                else:
                    emit("warn", f"OpenSea {operation_name} attempt {attempt} failed: {error}")
                    await asyncio.sleep(self._retry_interval_ms / 1000)
                continue
            if paced:
                self._limiter.succeeded()
            return result
        raise AssertionError("bounded OpenSea retry loop did not return")
