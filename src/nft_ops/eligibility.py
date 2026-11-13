"""Layer A — eligibility: does OpenSea say this wallet qualifies for a stage?

Statuses:
  ELIGIBLE      OpenSea's signed-in eligibility answer says yes (or the stage is public).
  NOT_ELIGIBLE  OpenSea answered and said no (or only a linked wallet qualifies).
  UNKNOWN       We asked but could not get an answer (sign-in failed, rate limit, ...).
  UNVERIFIED    We could not ask (watch-only wallet without a key).

This layer never looks at balances, gas or timing. That is Layer B (readiness, in mint.py).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from osnm_z.errors import MintError
from osnm_z.hooks import LOG_SINK
from osnm_z.opensea_protocol import (
    CollectionMetadata,
    EligibilitySnapshot,
    ProtocolError,
    effective_native_mint_price,
)
from osnm_z.phase_selection import (
    assess_wallet_eligibility,
    available_quantity,
    matching_eligibility_stage,
    stage_name,
    validate_snapshot_shape,
    wallet_mint_limit,
)

from .collections import OpenSeaFactory
from .db import Database
from .os_signin import is_rate_limited
from .vault import Vault

SOURCE_OPENSEA = "OpenSea eligibility (wallet signed in)"
SOURCE_PUBLIC = "Public stage (open to every wallet)"


@dataclass(slots=True)
class StageResult:
    stage_index: int
    status: str
    detail: str
    max_quantity: int | None
    minted: int | None
    price_wei: int | None
    source: str


def classify(metadata: CollectionMetadata, snapshot: EligibilitySnapshot | None,
             error: str | None, has_key: bool) -> list[StageResult]:
    results: list[StageResult] = []
    for stage in metadata.stages:
        public = stage.stage_type == "PUBLIC_SALE"
        if public:
            limit = stage.max_total_mintable_by_wallet
            minted = snapshot.minter_quantity_minted if snapshot else None
            remaining = None if limit is None or minted is None else max(0, limit - minted)
            results.append(StageResult(
                stage.stage_index, "ELIGIBLE",
                "public stage" + (f"; {minted}/{limit} already minted" if minted else ""),
                remaining if remaining is not None else limit, minted,
                stage.native_price_wei, SOURCE_PUBLIC))
            continue
        if not has_key:
            results.append(StageResult(stage.stage_index, "UNVERIFIED",
                                       "watch-only wallet: OpenSea sign-in needs its key",
                                       None, None, None, "not checked"))
            continue
        if snapshot is None:
            results.append(StageResult(stage.stage_index, "UNKNOWN", error or "no answer",
                                       None, None, None, SOURCE_OPENSEA))
            continue
        se = matching_eligibility_stage(snapshot, stage)
        assessment = assess_wallet_eligibility(se)
        price, _ = effective_native_mint_price(stage, se)
        limit = wallet_mint_limit(stage, se)
        if assessment.requires_wallet_switch:
            status, detail = "NOT_ELIGIBLE", ("another wallet linked to the same OpenSea "
                                              "account is eligible, not this address")
            remaining = 0
        elif assessment.is_eligible:
            remaining = available_quantity(stage, se, snapshot)
            status = "ELIGIBLE"
            detail = (f"{snapshot.minter_quantity_minted}/{limit} already minted"
                      if snapshot.minter_quantity_minted and limit else "on the list")
            if remaining == 0:
                detail = f"allocation used up ({snapshot.minter_quantity_minted}/{limit})"
        elif se.is_eligible is None:
            status, detail, remaining = "UNKNOWN", "OpenSea did not say", None
        else:
            status, detail, remaining = "NOT_ELIGIBLE", "not on this stage's list", 0
        results.append(StageResult(stage.stage_index, status, detail,
                                   remaining if remaining is not None else limit,
                                   snapshot.minter_quantity_minted, price, SOURCE_OPENSEA))
    return results


RATE_LIMITED_DETAIL = "OpenSea rate limited - try again later"
RATE_LIMIT_STOP_AFTER = 3     # consecutive rate-limited wallets before the scan stops


async def check_wallet(metadata: CollectionMetadata, factory: OpenSeaFactory, vault: Vault,
                       address: str) -> list[StageResult]:
    results, _ = await check_wallet_detail(metadata, factory, vault, address)
    return results


async def check_wallet_detail(metadata: CollectionMetadata, factory: OpenSeaFactory, vault: Vault,
                              address: str) -> tuple[list[StageResult], bool]:
    """Check one wallet. Returns (results, rate_limited)."""
    has_key = vault.unlocked and vault.has(address)
    needs_signin = any(s.stage_type != "PUBLIC_SALE" for s in metadata.stages)
    snapshot, error, limited = None, None, False
    if has_key and needs_signin:
        client = factory()
        token = LOG_SINK.set(lambda *_: None)   # retry chatter stays off the console
        step = "sign-in"
        try:
            await client.authenticate(vault.signer(address), address, metadata.network_id,
                                      metadata.slug)
            step = "eligibility answer"
            snapshot = await client.eligibility(metadata.slug, address)
            step = "phase check"
            validate_snapshot_shape(metadata, snapshot)
        except (ProtocolError, MintError) as exc:
            limited = is_rate_limited(exc)
            snapshot, error = None, RATE_LIMITED_DETAIL if limited else f"{step}: {exc}"
        finally:
            LOG_SINK.reset(token)
            await client.aclose()
    return classify(metadata, snapshot, error, has_key), limited


def clear(db: Database, slug: str) -> None:
    """Forget earlier results for this collection (a new scan replaces them)."""
    db.execute("DELETE FROM eligibility WHERE slug = ?", (slug,))


def save(db: Database, slug: str, address: str, results: list[StageResult]) -> None:
    now = time.time()
    db.executemany(
        "INSERT INTO eligibility(slug, address, stage_index, status, detail, max_quantity, minted, "
        "price_wei, source, checked_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(slug, address, stage_index) DO UPDATE SET status = excluded.status, "
        "detail = excluded.detail, max_quantity = excluded.max_quantity, minted = excluded.minted, "
        "price_wei = excluded.price_wei, source = excluded.source, checked_at = excluded.checked_at",
        [(slug, address, r.stage_index, r.status, r.detail, r.max_quantity, r.minted,
          None if r.price_wei is None else str(r.price_wei), r.source, now) for r in results],
    )


async def scan(db: Database, factory: OpenSeaFactory, vault: Vault, metadata: CollectionMetadata,
               addresses: list[str], *, concurrency: int = 2, spacing_seconds: float = 1.0,
               on_result: Callable[[str, list[StageResult]], None] | None = None,
               should_stop: Callable[[], bool] = lambda: False) -> dict:
    """Check many wallets with bounded concurrency so OpenSea is not flooded.

    Sign-ins also pass through the process-wide queue in os_signin. If OpenSea keeps
    answering "too many requests", the scan stops and the wallets it did not reach are
    marked UNKNOWN (rate limited) instead of being retried over and over.
    """
    semaphore = asyncio.Semaphore(max(1, concurrency))
    counts = {"ELIGIBLE": 0, "NOT_ELIGIBLE": 0, "UNKNOWN": 0, "UNVERIFIED": 0}
    start_gate = asyncio.Lock()
    state = {"streak": 0, "halted": False}

    def record(address: str, results: list[StageResult]) -> None:
        save(db, metadata.slug, address, results)
        for r in results:
            counts[r.status] = counts.get(r.status, 0) + 1
        if on_result:
            on_result(address, results)

    async def one(address: str) -> None:
        async with semaphore:
            async with start_gate:  # space out sign-ins
                if should_stop():
                    return
                if not state["halted"]:
                    await asyncio.sleep(spacing_seconds)
            if state["halted"]:
                has_key = vault.unlocked and vault.has(address)
                record(address, classify(metadata, None, RATE_LIMITED_DETAIL, has_key))
                return
            results, limited = await check_wallet_detail(metadata, factory, vault, address)
            state["streak"] = state["streak"] + 1 if limited else 0
            if state["streak"] >= RATE_LIMIT_STOP_AFTER:
                state["halted"] = True
            record(address, results)

    await asyncio.gather(*(one(a) for a in addresses))
    if state["halted"]:
        counts["rate_limited"] = True
    return counts


def table(db: Database, slug: str, metadata: CollectionMetadata) -> list[dict]:
    names = {s.stage_index: (stage_name(s), s.stage_type) for s in metadata.stages}
    rows = db.all("SELECT e.*, w.label FROM eligibility e LEFT JOIN wallets w "
                  "ON w.address = e.address WHERE e.slug = ? ORDER BY e.address, e.stage_index",
                  (slug,))
    for row in rows:
        row["stage_name"], row["stage_type"] = names.get(row["stage_index"], ("?", "?"))
    return rows
