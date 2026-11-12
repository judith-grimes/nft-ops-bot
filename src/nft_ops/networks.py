"""Networks and a multi-endpoint JSON-RPC pool with chain-ID verification and fallback."""

from __future__ import annotations

import asyncio
import ipaddress
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from .db import Database

# chain_id, name, native symbol, explorer, enabled, rpc urls
DEFAULT_NETWORKS: list[tuple[int, str, str, str, bool, list[str]]] = [
    (4663, "Robinhood Chain", "ETH", "https://robinhoodchain.blockscout.com", True,
     ["https://rpc.mainnet.chain.robinhood.com"]),
    (1, "Ethereum", "ETH", "https://etherscan.io", True,
     ["https://ethereum-rpc.publicnode.com", "https://eth.llamarpc.com"]),
    (8453, "Base", "ETH", "https://basescan.org", True,
     ["https://mainnet.base.org", "https://base-rpc.publicnode.com"]),
    (42161, "Arbitrum One", "ETH", "https://arbiscan.io", False, ["https://arb1.arbitrum.io/rpc"]),
    (10, "Optimism", "ETH", "https://optimistic.etherscan.io", False,
     ["https://mainnet.optimism.io"]),
    (137, "Polygon", "POL", "https://polygonscan.com", False, ["https://polygon-rpc.com"]),
    (33139, "ApeChain", "APE", "https://apescan.io", False, ["https://rpc.apechain.com"]),
]
BLOCK_LAG_LIMIT = 30          # blocks behind the best endpoint before it counts as unhealthy
HEALTH_MAX_AGE_SECONDS = 60   # a mint job re-checks endpoints older than this
BATCH_CHUNK = 100


class RpcError(Exception):
    pass


def validate_rpc_url(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return url
    if parts.scheme == "http" and parts.hostname:
        host = parts.hostname
        try:
            if host == "localhost" or ipaddress.ip_address(host).is_loopback:
                return url
        except ValueError:
            pass
    raise RpcError("RPC URL must start with https:// (http:// is allowed only for localhost)")


def seed_networks(db: Database) -> None:
    for chain_id, name, symbol, explorer, enabled, urls in DEFAULT_NETWORKS:
        if db.one("SELECT 1 FROM networks WHERE chain_id = ?", (chain_id,)):
            continue
        db.execute(
            "INSERT INTO networks(chain_id, name, native_symbol, explorer_url, enabled) "
            "VALUES(?, ?, ?, ?, ?)",
            (chain_id, name, symbol, explorer, int(enabled)),
        )
        for priority, url in enumerate(urls):
            db.execute(
                "INSERT INTO rpc_endpoints(chain_id, url, priority) VALUES(?, ?, ?)",
                (chain_id, url, priority * 10),
            )


@dataclass(frozen=True, slots=True)
class Endpoint:
    id: int
    url: str


def _hex_int(value: Any) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise RpcError("invalid JSON-RPC quantity")
    return int(value, 16)


class RpcPool:
    """Sends reads to the best healthy endpoint of a chain and falls back on failure."""

    def __init__(self, db: Database, client: httpx.AsyncClient | None = None,
                 timeout: float = 10.0) -> None:
        self._db = db
        self._client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        self._verified: dict[str, int] = {}
        self._id = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    # ----- endpoint selection ----------------------------------------------------------
    def endpoints(self, chain_id: int) -> list[Endpoint]:
        rows = self._db.all(
            "SELECT id, url, healthy, latency_ms, priority FROM rpc_endpoints "
            "WHERE chain_id = ? AND enabled = 1",
            (chain_id,),
        )
        # Healthy first (by latency), then never-checked (by priority), unhealthy last.
        def rank(r: dict) -> tuple:
            health = {1: 0, None: 1, 0: 2}[r["healthy"]]
            return (health, r["latency_ms"] if r["latency_ms"] is not None else 1e9, r["priority"])
        return [Endpoint(r["id"], r["url"]) for r in sorted(rows, key=rank)]

    async def best_url(self, chain_id: int) -> str:
        """A verified, healthy endpoint for a mint job (re-checks stale health data)."""
        rows = self._db.all(
            "SELECT checked_at FROM rpc_endpoints WHERE chain_id = ? AND enabled = 1", (chain_id,)
        )
        if not rows:
            raise RpcError(f"no RPC endpoint configured for chain {chain_id}")
        if any(r["checked_at"] is None or time.time() - r["checked_at"] > HEALTH_MAX_AGE_SECONDS
               for r in rows):
            await self.check(chain_id)
        healthy = self._db.all(
            "SELECT url FROM rpc_endpoints WHERE chain_id = ? AND enabled = 1 AND healthy = 1 "
            "ORDER BY latency_ms",
            (chain_id,),
        )
        if not healthy:
            raise RpcError(f"no healthy RPC endpoint for chain {chain_id}")
        return healthy[0]["url"]

    # ----- calls -----------------------------------------------------------------------
    async def call(self, chain_id: int, method: str, params: list) -> Any:
        errors: list[str] = []
        for endpoint in self.endpoints(chain_id):
            try:
                await self._ensure_chain(endpoint.url, chain_id)
                return await self._single(endpoint.url, method, params)
            except (RpcError, httpx.HTTPError, ValueError) as error:
                errors.append(f"{_host(endpoint.url)}: {error}")
        raise RpcError("all RPC endpoints failed: " + "; ".join(errors) if errors
                       else f"no RPC endpoint configured for chain {chain_id}")

    async def batch(self, chain_id: int, calls: list[tuple[str, list]]) -> list[Any]:
        """Run many reads. Each result is the value or an RpcError for that call."""
        results: list[Any] = []
        for start in range(0, len(calls), BATCH_CHUNK):
            results.extend(await self._batch_chunk(chain_id, calls[start:start + BATCH_CHUNK]))
        return results

    async def _batch_chunk(self, chain_id: int, calls: list[tuple[str, list]]) -> list[Any]:
        errors: list[str] = []
        for endpoint in self.endpoints(chain_id):
            try:
                await self._ensure_chain(endpoint.url, chain_id)
                payload = [self._envelope(m, p) for m, p in calls]
                response = await self._client.post(endpoint.url, json=payload)
                if response.status_code == 429:
                    raise RpcError("rate limited")
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, list):
                    # Endpoint does not support batches: fall back to individual calls.
                    return [await self._safe_single(endpoint.url, m, p) for m, p in calls]
                by_id = {item.get("id"): item for item in body if isinstance(item, dict)}
                out: list[Any] = []
                for envelope in payload:
                    item = by_id.get(envelope["id"])
                    if item is None:
                        out.append(RpcError("missing response"))
                    elif "error" in item:
                        out.append(RpcError(str(item["error"].get("message", "RPC error"))))
                    else:
                        out.append(item.get("result"))
                return out
            except (RpcError, httpx.HTTPError, ValueError) as error:
                errors.append(f"{_host(endpoint.url)}: {error}")
        raise RpcError("all RPC endpoints failed: " + "; ".join(errors) if errors
                       else f"no RPC endpoint configured for chain {chain_id}")

    async def _safe_single(self, url: str, method: str, params: list) -> Any:
        try:
            return await self._single(url, method, params)
        except (RpcError, httpx.HTTPError, ValueError) as error:
            return RpcError(str(error))

    def _envelope(self, method: str, params: list) -> dict:
        self._id += 1
        return {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}

    async def _single(self, url: str, method: str, params: list) -> Any:
        envelope = self._envelope(method, params)
        response = await self._client.post(url, json=envelope)
        if response.status_code == 429:
            raise RpcError("rate limited")
        if response.status_code >= 400:
            try:
                message = response.json().get("error", {}).get("message")
            except ValueError:
                message = None
            raise RpcError(message or f"HTTP {response.status_code}")
        body = response.json()
        if not isinstance(body, dict) or body.get("id") != envelope["id"]:
            raise RpcError("invalid JSON-RPC response")
        if "error" in body:
            raise RpcError(str(body["error"].get("message", "RPC error")))
        return body.get("result")

    async def _ensure_chain(self, url: str, chain_id: int) -> None:
        if self._verified.get(url) == chain_id:
            return
        actual = _hex_int(await self._single(url, "eth_chainId", []))
        if actual != chain_id:
            raise RpcError(f"endpoint is on chain {actual}, expected {chain_id}")
        self._verified[url] = chain_id

    # ----- health ----------------------------------------------------------------------
    async def check(self, chain_id: int) -> list[dict]:
        rows = self._db.all(
            "SELECT id, url FROM rpc_endpoints WHERE chain_id = ? AND enabled = 1", (chain_id,)
        )
        results = await asyncio.gather(*(self._probe(r["url"], chain_id) for r in rows))
        best_block = max((r["block"] for r in results if r["block"] is not None), default=None)
        now = time.time()
        for row, result in zip(rows, results, strict=True):
            healthy, error = result["ok"], result["error"]
            if healthy and best_block is not None and best_block - result["block"] > BLOCK_LAG_LIMIT:
                healthy, error = False, f"lagging {best_block - result['block']} blocks"
            self._db.execute(
                "UPDATE rpc_endpoints SET healthy = ?, latency_ms = ?, block_number = ?, "
                "last_error = ?, checked_at = ? WHERE id = ?",
                (int(healthy), result["latency_ms"], result["block"], error, now, row["id"]),
            )
            if not healthy:
                self._verified.pop(row["url"], None)
        return self._db.all("SELECT * FROM rpc_endpoints WHERE chain_id = ?", (chain_id,))

    async def _probe(self, url: str, chain_id: int) -> dict:
        started = time.perf_counter()
        try:
            actual = _hex_int(await self._single(url, "eth_chainId", []))
            if actual != chain_id:
                return {"ok": False, "error": f"wrong chain {actual}", "latency_ms": None,
                        "block": None}
            block = _hex_int(await self._single(url, "eth_blockNumber", []))
            latency = (time.perf_counter() - started) * 500  # average of the two requests
            self._verified[url] = chain_id
            return {"ok": True, "error": None, "latency_ms": round(latency, 1), "block": block}
        except (RpcError, httpx.HTTPError, ValueError) as error:
            return {"ok": False, "error": str(error)[:200], "latency_ms": None, "block": None}


def _host(url: str) -> str:
    return urlsplit(url).hostname or "endpoint"
