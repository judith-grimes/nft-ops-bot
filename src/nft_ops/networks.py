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
# Every URL below was checked live with eth_chainId before it was added.
DEFAULT_NETWORKS: list[tuple[int, str, str, str, bool, list[str]]] = [
    (4663, "Robinhood Chain", "ETH", "https://robinhoodchain.blockscout.com", True,
     ["https://rpc.mainnet.chain.robinhood.com"]),
    (1, "Ethereum", "ETH", "https://etherscan.io", True,
     ["https://ethereum-rpc.publicnode.com", "https://eth.drpc.org"]),
    (8453, "Base", "ETH", "https://basescan.org", True,
     ["https://mainnet.base.org", "https://base-rpc.publicnode.com"]),
    (42161, "Arbitrum One", "ETH", "https://arbiscan.io", False, ["https://arb1.arbitrum.io/rpc"]),
    (10, "Optimism", "ETH", "https://optimistic.etherscan.io", False,
     ["https://mainnet.optimism.io"]),
    (137, "Polygon", "POL", "https://polygonscan.com", False,
     ["https://polygon-bor-rpc.publicnode.com", "https://polygon.drpc.org"]),
    (33139, "ApeChain", "APE", "https://apescan.io", False, ["https://rpc.apechain.com"]),
    (999, "HyperEVM", "HYPE", "https://hyperevmscan.io", True,
     ["https://rpc.hyperliquid.xyz/evm", "https://hyperliquid.drpc.org"]),
    (143, "Monad", "MON", "https://monadscan.com", True,
     ["https://rpc.monad.xyz", "https://monad-mainnet.drpc.org"]),
    (9745, "Plasma", "XPL", "https://plasmascan.to", True, ["https://rpc.plasma.to"]),
    (5042, "Arc", "USDC", "https://explorer.arc.io", True, ["https://rpc.mainnet.arc.io"]),
    (1440000, "XRPL EVM", "XRP", "https://explorer.xrplevm.org", True,
     ["https://rpc.xrplevm.org"]),
    # Kinetiq's Elysium (HyperEVM L2). The only free RPC found is run by the
    # independent Elyscan explorer; add an official one when Kinetiq publishes it.
    (99901, "Elysium (Kinetiq)", "HYPE", "https://elyscan.live", True,
     ["https://rpc.elyscan.live"]),
    (1339, "Elysium (Vulcan Forged)", "LAVA", "https://blockscout.elysiumchain.tech", False,
     ["https://rpc.elysiumchain.tech"]),
]
# One-time fixes for databases created by older versions (applied once per version).
# version -> (dead default URLs to remove, (chain_id, url) defaults to add to existing networks)
SEED_UPGRADES: dict[int, tuple[list[str], list[tuple[int, str]]]] = {
    2: (["https://polygon-rpc.com", "https://eth.llamarpc.com"],
        [(137, "https://polygon-bor-rpc.publicnode.com"), (137, "https://polygon.drpc.org"),
         (1, "https://eth.drpc.org")]),
}
BLOCK_LAG_LIMIT = 30          # blocks behind the best endpoint before it counts as unhealthy
HEALTH_MAX_AGE_SECONDS = 60   # a mint job re-checks endpoints older than this
BATCH_CHUNK = 100


class RpcError(Exception):
    pass


class RpcTransportError(RpcError):
    """The endpoint did not give a usable answer (network, HTTP status, not JSON).

    Only these are safe to retry on another endpoint; a JSON-RPC error is a real answer.
    """


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
    """Add any built-in network that is missing, then apply one-time upgrades."""
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
    version = int(db.get_setting("seed_version", 1))
    for target in sorted(v for v in SEED_UPGRADES if v > version):
        dead, additions = SEED_UPGRADES[target]
        for url in dead:
            db.execute("DELETE FROM rpc_endpoints WHERE url = ?", (url,))
        for chain_id, url in additions:
            if db.one("SELECT 1 FROM networks WHERE chain_id = ?", (chain_id,)):
                db.execute("INSERT OR IGNORE INTO rpc_endpoints(chain_id, url, priority) "
                           "VALUES(?, ?, 20)", (chain_id, url))
        db.set_setting("seed_version", target)


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

    def healthy_urls(self, chain_id: int) -> list[str]:
        """Endpoints whose last health check confirmed the right chain, fastest first."""
        return [r["url"] for r in self._db.all(
            "SELECT url FROM rpc_endpoints WHERE chain_id = ? AND enabled = 1 AND healthy = 1 "
            "ORDER BY latency_ms", (chain_id,))]

    async def send_raw(self, chain_id: int, primary: str, raw: str,
                       broadcast: bool = False) -> tuple[Any, str]:
        """eth_sendRawTransaction. Returns (result, url that accepted it).

        Without broadcast: the primary, then the next endpoint only if it gave no answer.
        With broadcast: every healthy endpoint at once; the first acceptance wins. Identical
        signed bytes can never create a second transaction.
        """
        urls = [primary] + [u for u in self.healthy_urls(chain_id) if u != primary]
        if not broadcast or len(urls) == 1:
            last: RpcTransportError | None = None
            for url in urls:
                try:
                    return await self._single(url, "eth_sendRawTransaction", [raw]), url
                except RpcTransportError as error:
                    last = error
            assert last is not None
            raise last
        tasks = {asyncio.create_task(self._single(u, "eth_sendRawTransaction", [raw])): u for u in urls}
        rejections: dict[str, RpcError] = {}
        transport: RpcTransportError | None = None
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    result = task.result()
                except RpcTransportError as error:
                    transport = error
                    continue
                except RpcError as error:
                    if "already known" in str(error).lower():
                        for rest in pending:
                            self._keep(rest)
                        return None, tasks[task]
                    rejections[tasks[task]] = error
                    continue
                for rest in pending:
                    self._keep(rest)
                return result, tasks[task]
        if rejections:
            raise rejections.get(primary, next(iter(rejections.values())))
        assert transport is not None
        raise transport

    def _keep(self, task: asyncio.Task) -> None:
        """Let a still-running broadcast finish in the background."""
        if not hasattr(self, "_background"):
            self._background: set[asyncio.Task] = set()
        self._background.add(task)
        task.add_done_callback(self._background.discard)

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
                        out.append(RpcError(_error_text(item["error"])))
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
        try:
            response = await self._client.post(url, json=envelope)
        except httpx.HTTPError as error:
            raise RpcTransportError(f"connection failed: {type(error).__name__}") from None
        if response.status_code == 429:
            raise RpcTransportError("rate limited")
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = None
            message = _error_text(body.get("error")) if isinstance(body, dict) and body.get("error") else None
            raise RpcTransportError(f"HTTP {response.status_code}: {message}" if message
                                    else f"HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise RpcTransportError("endpoint did not return JSON") from None
        if isinstance(body, dict) and body.get("error") and "result" not in body:
            # Provider errors often lack a matching id; show the provider's own message.
            raise RpcError(_error_text(body["error"]))
        if not isinstance(body, dict) or body.get("id") != envelope["id"]:
            raise RpcError("invalid JSON-RPC response")
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
        except Exception as error:  # noqa: BLE001 - a broken endpoint must show as down, not crash
            return {"ok": False, "error": (str(error) or type(error).__name__)[:200],
                    "latency_ms": None, "block": None}


def _error_text(error: object) -> str:
    """RPC providers send errors as {"message": ...}, a plain string, or something else."""
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(error)[:300] or "RPC error"


def _host(url: str) -> str:
    return urlsplit(url).hostname or "endpoint"
