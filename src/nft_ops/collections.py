"""Load OpenSea drop collections (via the vendored osnm-z client) and describe their phases."""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Callable
from typing import Any

from osnm_z.config import OpenSeaConfig
from osnm_z.opensea import WalletOpenSeaClient
from osnm_z.opensea_protocol import (
    CollectionMetadata,
    ProtocolError,
    StageMetadata,
    parse_collection_locator,
)
from osnm_z.phase_selection import stage_name, stage_window

from .db import Database

OpenSeaFactory = Callable[[], Any]  # returns a WalletOpenSeaClient-compatible object


def default_opensea_factory(config: OpenSeaConfig | None = None) -> OpenSeaFactory:
    return lambda: WalletOpenSeaClient(config or OpenSeaConfig())


class CollectionError(Exception):
    pass


def to_json(metadata: CollectionMetadata) -> str:
    return json.dumps(dataclasses.asdict(metadata))


def from_json(text: str) -> CollectionMetadata:
    data = json.loads(text)
    data["stages"] = tuple(StageMetadata(**s) for s in data["stages"])
    return CollectionMetadata(**data)


def get(db: Database, slug: str) -> CollectionMetadata:
    row = db.one("SELECT data FROM collections WHERE slug = ?", (slug,))
    if row is None:
        raise CollectionError(f"collection {slug} is not loaded")
    return from_json(row["data"])


async def load(db: Database, factory: OpenSeaFactory, locator_text: str,
               chain_id: int | None) -> CollectionMetadata:
    try:
        locator = parse_collection_locator(locator_text)
    except ProtocolError:
        raise CollectionError("enter an OpenSea slug, opensea.io/collection/... link, "
                              "or 0x contract address") from None
    client = factory()
    try:
        if locator.slug is not None:
            metadata = await client.collection_metadata(locator.slug)
        else:
            if chain_id is None:
                raise CollectionError("choose the network for a contract address")
            metadata = await client.resolve_collection(locator, chain_id)
    except ProtocolError as error:
        raise CollectionError(f"OpenSea: {error}") from None
    finally:
        await client.aclose()
    network = db.one("SELECT name FROM networks WHERE chain_id = ?", (metadata.network_id,))
    if network is None:
        raise CollectionError(
            f"collection is on chain {metadata.network_id} ({metadata.chain_identifier}), "
            "which is not configured. Add it under Networks first."
        )
    if metadata.drop_kind != "Erc721SeaDropV1":
        raise CollectionError(f"unsupported drop type {metadata.drop_kind}; Stage 1 supports "
                              "OpenSea SeaDrop ERC-721 drops")
    db.execute(
        "INSERT INTO collections(slug, address, chain_id, chain_name, drop_kind, data, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT(slug) DO UPDATE SET address = excluded.address, "
        "chain_id = excluded.chain_id, chain_name = excluded.chain_name, "
        "drop_kind = excluded.drop_kind, data = excluded.data, updated_at = excluded.updated_at",
        (metadata.slug, metadata.address, metadata.network_id, network["name"],
         metadata.drop_kind, to_json(metadata), time.time()),
    )
    return metadata


def describe(metadata: CollectionMetadata, now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    stages = []
    for stage in metadata.stages:
        window = stage_window(stage)
        if window.ends_at is not None and now >= window.ends_at:
            state = "ended"
        elif now >= window.starts_at:
            state = "active"
        else:
            state = "upcoming"
        stages.append({
            "index": stage.stage_index,
            "name": stage_name(stage),
            "type": stage.stage_type,
            "starts_at": window.starts_at,
            "ends_at": window.ends_at,
            "state": state,
            "price_wei": None if stage.native_price_wei is None else str(stage.native_price_wei),
            "max_per_wallet": stage.max_total_mintable_by_wallet,
        })
    upcoming = [s for s in stages if s["state"] == "upcoming"]
    return {
        "slug": metadata.slug,
        "address": metadata.address,
        "chain_id": metadata.network_id,
        "chain": metadata.chain_identifier,
        "opensea_url": f"https://opensea.io/collection/{metadata.slug}",
        "disabled": metadata.is_disabled,
        "minted_out": metadata.is_minted_out,
        "stages": stages,
        "current": next((s["name"] for s in stages if s["state"] == "active"), None),
        "next": min(upcoming, key=lambda s: s["starts_at"])["name"] if upcoming else None,
    }


def list_all(db: Database) -> list[dict]:
    out = []
    for row in db.all("SELECT slug, data, chain_name, updated_at, watch, snapshot, next_check "
                      "FROM collections ORDER BY slug"):
        info = describe(from_json(row["data"]))
        info["chain_name"] = row["chain_name"]
        info["updated_at"] = row["updated_at"]
        info["watch"] = bool(row["watch"])
        snap = json.loads(row["snapshot"]) if row["snapshot"] else {}
        info["supply"] = snap.get("supply")
        info["checked_at"] = snap.get("checked_at")
        info["next_check"] = row["next_check"]
        counts = db.all("SELECT stage_index, COUNT(*) AS n FROM eligibility WHERE slug = ? AND "
                        "status = 'ELIGIBLE' GROUP BY stage_index", (row["slug"],))
        eligible = {c["stage_index"]: c["n"] for c in counts}
        for stage in info["stages"]:
            stage["eligible_wallets"] = eligible.get(stage["index"], 0)
        out.append(info)
    return out
