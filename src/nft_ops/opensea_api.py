"""Client for OpenSea's official REST API v2 (https://docs.opensea.io/reference/api-overview).

Every request needs an API key in the `x-api-key` header. Requests are paced (OpenSea rate
limits) and retried with back-off on HTTP 429. Only documented endpoints are used:

  GET  /collections/{slug}                       fees, contracts, pricing currencies
  GET  /collections/{slug}/stats                 floor price, volume, sales
  GET  /events/collection/{slug}?event_type=sale recent sales (market context)
  GET  /offers/collection/{slug}/all             all offers on a collection
  GET  /offers/collection/{slug}/nfts/{id}       offers on one NFT
  GET  /chain/{chain}/account/{address}/nfts     NFTs a wallet holds
  GET  /chain/{chain}/payment_token/{address}    token price in USD (native coin = 0x0)
  GET  /events/accounts/{address}?event_type=sale purchases and sales of one wallet
  POST /offers/fulfillment_data                  Seaport transaction to accept an offer
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import httpx

BASE_URL = "https://api.opensea.io/api/v2"
# OpenSea chain slug -> EVM chain ID (slugs from GET /api/v2/chains).
CHAIN_IDS: dict[str, int] = {
    "ethereum": 1, "base": 8453, "arbitrum": 42161, "optimism": 10, "polygon": 137,
    "ape_chain": 33139, "robinhood": 4663, "hyperevm": 999, "monad": 143, "arc": 5042,
    "zora": 7777777, "abstract": 2741, "unichain": 130, "soneium": 1868, "shape": 360,
    "bera_chain": 80094, "sei": 1329, "avalanche": 43114, "ronin": 2020, "ink": 57073,
    "elysium": 99901,   # Kinetiq Elysium; OpenSea does not publish chain IDs, verify on use
}
CHAIN_SLUGS = {v: k for k, v in CHAIN_IDS.items()}


class OpenSeaError(Exception):
    pass


class OpenSeaAPI:
    def __init__(self, key_provider: Callable[[], str | None], client: httpx.AsyncClient | None = None,
                 base_url: str = BASE_URL, requests_per_second: float = 4.0) -> None:
        self._key = key_provider
        self._client = client or httpx.AsyncClient(timeout=20)
        self._base = base_url.rstrip("/")
        self._spacing = 1.0 / requests_per_second
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.calls = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def configured(self) -> bool:
        return bool(self._key())

    async def _request(self, method: str, path: str, params: dict | None = None,
                       body: dict | None = None) -> Any:
        key = self._key()
        if not key:
            raise OpenSeaError("add your OpenSea API key in Settings first")
        for attempt in range(4):
            async with self._lock:                     # pace requests
                wait = self._last + self._spacing - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
            self.calls += 1
            try:
                response = await self._client.request(
                    method, f"{self._base}{path}", params=params, json=body,
                    headers={"x-api-key": key, "accept": "application/json"})
            except httpx.HTTPError as error:
                if attempt == 3:
                    raise OpenSeaError(f"OpenSea unreachable: {type(error).__name__}") from None
                await asyncio.sleep(1 + attempt)
                continue
            if response.status_code == 429:
                retry = float(response.headers.get("retry-after", 2 ** attempt))
                await asyncio.sleep(min(retry, 30))
                continue
            if response.status_code in (401, 403):
                raise OpenSeaError("OpenSea refused the API key (missing, wrong or expired)")
            try:
                data = response.json()
            except ValueError:
                raise OpenSeaError(f"OpenSea returned HTTP {response.status_code}") from None
            if response.status_code >= 400:
                errors = data.get("errors") if isinstance(data, dict) else None
                raise OpenSeaError(f"OpenSea: {'; '.join(map(str, errors)) if errors else response.status_code}")
            return data
        raise OpenSeaError("OpenSea kept rate limiting; try again shortly")

    async def _pages(self, path: str, key: str, params: dict, max_pages: int) -> list[dict]:
        out, cursor = [], None
        for _ in range(max_pages):
            data = await self._request("GET", path, {**params, **({"next": cursor} if cursor else {})})
            out.extend(data.get(key) or [])
            cursor = data.get("next")
            if not cursor:
                break
        return out

    # ----- endpoints -------------------------------------------------------------------
    async def collection(self, slug: str) -> dict:
        return await self._request("GET", f"/collections/{slug}")

    async def stats(self, slug: str) -> dict:
        return await self._request("GET", f"/collections/{slug}/stats")

    async def sales(self, slug: str, after: int, max_pages: int = 2) -> list[dict]:
        return await self._pages(f"/events/collection/{slug}", "asset_events",
                                 {"event_type": "sale", "after": after, "limit": 200}, max_pages)

    async def offers_all(self, slug: str, max_pages: int = 5) -> list[dict]:
        return await self._pages(f"/offers/collection/{slug}/all", "offers", {"limit": 200}, max_pages)

    async def offers_collection(self, slug: str, max_pages: int = 1) -> list[dict]:
        """Collection-wide offers (any NFT of the collection qualifies)."""
        return await self._pages(f"/offers/collection/{slug}", "offers", {"limit": 100}, max_pages)

    async def offers_nft(self, slug: str, token_id: str, max_pages: int = 2) -> list[dict]:
        return await self._pages(f"/offers/collection/{slug}/nfts/{token_id}", "offers",
                                 {"limit": 200}, max_pages)

    async def nfts(self, chain: str, address: str, max_pages: int = 10) -> list[dict]:
        return await self._pages(f"/chain/{chain}/account/{address}/nfts", "nfts",
                                 {"limit": 200}, max_pages)

    async def payment_token(self, chain: str, address: str) -> dict:
        """GET /chain/{chain}/payment_token/{address}: symbol, decimals, usd_price, eth_price."""
        return await self._request("GET", f"/chain/{chain}/payment_token/{address}")

    async def account_sales(self, address: str, chain: str, max_pages: int = 3) -> list[dict]:
        """GET /events/accounts/{address}?event_type=sale: NFTs this wallet bought or sold."""
        return await self._pages(f"/events/accounts/{address}", "asset_events",
                                 {"event_type": "sale", "chain": chain, "limit": 50}, max_pages)

    async def fulfillment(self, order_hash: str, chain: str, protocol_address: str, fulfiller: str,
                          contract: str, token_id: str) -> dict:
        body = {"offer": {"hash": order_hash, "chain": chain, "protocol_address": protocol_address},
                "fulfiller": {"address": fulfiller},
                "consideration": {"asset_contract_address": contract, "token_id": str(token_id)}}
        return await self._request("POST", "/offers/fulfillment_data", body=body)
