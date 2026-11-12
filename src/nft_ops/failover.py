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

    def set_backups(self, urls: list[str]) -> None:
        """Backup URLs, already verified to serve the same chain ID."""
        self._backups = list(dict.fromkeys(urls))

    def __repr__(self) -> str:
        return f"FailoverGateway(backups={len(self._backups)}, ...)"

    async def _post_json(self, endpoint: str, payload: object, *,
                         timeout_seconds: float | None = None) -> Any:
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


def make_gateway(timeout_seconds: float, max_connections: int) -> FailoverGateway:
    return FailoverGateway(timeout_seconds, max_connections=max_connections)
