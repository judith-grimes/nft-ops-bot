"""RPC failover for the vendored osnm-z gateway, without modifying osnm-z.

FailoverGateway subclasses osnm_z.chain.ChainGateway and wraps its single low-level request
method. If the primary endpoint does not answer at all (connection error, timeout, rate
limit, HTTP error, unreadable body), the same request is sent to the next backup endpoint.

JSON-RPC answers, including errors such as "nonce too low", are never retried elsewhere:
they come back inside a normal response and are handled by osnm-z exactly as before.

Re-sending an eth_sendRawTransaction payload to a backup is safe: it is the identical signed
transaction, so a node that already has it answers "already known" (which osnm-z treats as
accepted) and it can never create a second transaction.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from osnm_z.chain import ChainGateway
from osnm_z.rpc_protocol import ChainError

DEAD_SECONDS = 30.0


class FailoverGateway(ChainGateway):
    def __init__(self, timeout_seconds: float, max_connections: int = 100) -> None:
        super().__init__(timeout_seconds, max_connections)
        self._backups: list[str] = []
        self._dead_until: dict[str, float] = {}
        self.failovers = 0
        self.on_failover = None  # optional callback(primary_url, used_url, error)
        self.broadcast = False   # send signed transactions to every endpoint at once
        self._background: set[asyncio.Task] = set()

    def set_backups(self, urls: list[str]) -> None:
        """Backup URLs, already verified to serve the same chain ID."""
        self._backups = list(dict.fromkeys(urls))

    def __repr__(self) -> str:
        return f"FailoverGateway(backups={len(self._backups)}, ...)"

    async def _post_json(self, endpoint: str, payload: object, *,
                         timeout_seconds: float | None = None) -> Any:
        if (self.broadcast and self._backups and isinstance(payload, dict)
                and payload.get("method") == "eth_sendRawTransaction"):
            return await self._broadcast(endpoint, payload, timeout_seconds)
        candidates = [endpoint] + [u for u in self._backups if u != endpoint]
        now = time.monotonic()
        alive = [u for u in candidates if self._dead_until.get(u, 0) <= now]
        order = alive + [u for u in candidates if u not in alive]  # dead ones as last resort
        last_error: ChainError | None = None
        for url in order:
            try:
                result = await super()._post_json(url, payload, timeout_seconds=timeout_seconds)
            except ChainError as error:
                last_error = error
                self._dead_until[url] = time.monotonic() + DEAD_SECONDS
                continue
            if url != endpoint:
                self.failovers += 1
                if self.on_failover is not None:
                    self.on_failover(endpoint, url, last_error)
            return result
        assert last_error is not None
        raise last_error


    async def _broadcast(self, endpoint: str, payload: dict,
                         timeout_seconds: float | None) -> Any:
        """Send the identical signed transaction to every endpoint at once.

        Returns the first answer that accepted it; the others keep going in the background
        (a node that already has it just answers "already known"). If no endpoint accepted it,
        the primary's JSON-RPC answer is returned so osnm-z sees the real rejection.
        """
        urls = [endpoint] + [u for u in self._backups if u != endpoint]
        tasks = {asyncio.create_task(ChainGateway._post_json(self, u, payload,
                                                             timeout_seconds=timeout_seconds)): u
                 for u in urls}
        answers: dict[str, Any] = {}
        last_error: ChainError | None = None
        pending = set(tasks)
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    url = tasks[task]
                    try:
                        envelope = task.result()
                    except ChainError as error:
                        last_error = error
                        continue
                    if isinstance(envelope, dict) and "result" in envelope:
                        return envelope
                    answers[url] = envelope
        finally:
            for task in pending:          # let the rest finish delivering in the background
                self._background.add(task)
                task.add_done_callback(self._background.discard)
        if answers:
            return answers.get(endpoint, next(iter(answers.values())))
        assert last_error is not None
        raise last_error


def make_gateway(timeout_seconds: float, max_connections: int) -> FailoverGateway:
    return FailoverGateway(timeout_seconds, max_connections=max_connections)
