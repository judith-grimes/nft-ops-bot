"""Market data (Stage 5): holdings, floor history, recent-sales context, currency conversion, P&L.

The floor price alone is never treated as the market value: every snapshot also stores the
median of recent sales, and the floor is flagged as suspicious when it strays far from them.
"""

from __future__ import annotations

import json
import statistics
import time
from decimal import Decimal

from eth_utils.address import to_checksum_address

from .db import Database
from .opensea_api import CHAIN_IDS, CHAIN_SLUGS, OpenSeaAPI, OpenSeaError

COLLECTION_INFO_TTL = 600          # refresh fees / currency prices every 10 minutes
SALES_WINDOW_SECONDS = 7 * 86400


def native_symbol(db: Database, chain_id: int) -> str:
    row = db.one("SELECT native_symbol FROM networks WHERE chain_id = ?", (chain_id,))
    return row["native_symbol"] if row else "ETH"


def to_native(db: Database, chain_id: int, symbol: str, amount: int, decimals: int,
              info: dict | None) -> tuple[float | None, str]:
    """Convert a token amount to the chain's native coin. Returns (value, how)."""
    value = Decimal(amount) / (Decimal(10) ** decimals)
    native = native_symbol(db, chain_id).upper()
    sym = (symbol or "").upper()
    if sym in (native, "W" + native):
        return float(value), "1:1 wrapped native"
    for token in ((info or {}).get("pricing_currencies") or {}).values():
        if isinstance(token, dict) and (token.get("symbol") or "").upper() == sym and token.get("eth_price"):
            if native != "ETH":
                return None, f"no {sym}->{native} rate"
            return float(value * Decimal(str(token["eth_price"]))), f"OpenSea rate {token['eth_price']} ETH/{sym}"
    return None, f"no conversion rate for {sym or 'unknown currency'}"


async def collection_info(db: Database, api: OpenSeaAPI, slug: str, force: bool = False) -> dict:
    row = db.one("SELECT info, updated_at FROM market_collections WHERE slug = ?", (slug,))
    if row and not force and time.time() - row["updated_at"] < COLLECTION_INFO_TTL:
        return json.loads(row["info"])
    data = await api.collection(slug)
    contracts = data.get("contracts") or []
    chain_id = CHAIN_IDS.get(contracts[0]["chain"]) if contracts else None
    info = {"fees": data.get("fees") or [], "pricing_currencies": data.get("pricing_currencies") or {},
            "contracts": contracts, "name": data.get("name") or slug,
            "total_supply": data.get("total_supply"), "opensea_url": data.get("opensea_url")}
    db.execute("INSERT INTO market_collections(slug, chain_id, contract, name, info, updated_at) "
               "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(slug) DO UPDATE SET chain_id = excluded.chain_id, "
               "contract = excluded.contract, name = excluded.name, info = excluded.info, "
               "updated_at = excluded.updated_at",
               (slug, chain_id, contracts[0]["address"] if contracts else None, info["name"],
                json.dumps(info), time.time()))
    return info


async def sync_holdings(db: Database, api: OpenSeaAPI, wallets: list[str], chain_ids: list[int]) -> dict:
    """Ask OpenSea which NFTs every wallet holds on every enabled, supported chain."""
    found, errors = 0, []
    for chain_id in chain_ids:
        chain = CHAIN_SLUGS.get(chain_id)
        if not chain:
            continue
        for wallet in wallets:
            try:
                nfts = await api.nfts(chain, wallet)
            except OpenSeaError as error:
                errors.append(f"{chain}: {error}")
                continue
            now = time.time()
            rows = []
            for n in nfts:
                if n.get("is_disabled"):
                    continue
                traits = n.get("traits")
                rows.append((chain_id, to_checksum_address(wallet), to_checksum_address(n["contract"]),
                             str(n["identifier"]), n["collection"], n.get("name"),
                             n.get("token_standard"), str(n.get("quantity_string") or n.get("quantity") or 1),
                             json.dumps(traits) if traits else None, n.get("image_url"), now))
            db.execute("DELETE FROM os_holdings WHERE chain_id = ? AND address = ?",
                       (chain_id, to_checksum_address(wallet)))
            db.executemany("INSERT OR REPLACE INTO os_holdings VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            found += len(rows)
    return {"nfts": found, "errors": errors[:5]}


async def snapshot(db: Database, api: OpenSeaAPI, slug: str, chain_id: int) -> dict:
    """Record the floor and the recent-sales median for one collection."""
    info = await collection_info(db, api, slug)
    stats = await api.stats(slug)
    total = stats.get("total") or {}
    floor, symbol = total.get("floor_price"), total.get("floor_price_symbol") or ""
    floor_native = None
    if floor and symbol:
        decimals = 18
        floor_native, _ = to_native(db, chain_id, symbol, int(Decimal(str(floor)) * 10**decimals),
                                    decimals, info)
    prices = []
    try:
        for sale in await api.sales(slug, int(time.time()) - SALES_WINDOW_SECONDS):
            pay = sale.get("payment") or {}
            qty = int(sale.get("quantity") or 1) or 1
            value, _ = to_native(db, chain_id, pay.get("symbol", ""), int(pay.get("quantity") or 0),
                                 int(pay.get("decimals") or 18), info)
            if value:
                prices.append(value / qty)
    except OpenSeaError:
        pass
    median = statistics.median(prices) if prices else None
    db.execute("INSERT INTO floor_history(slug, ts, floor_native, floor_symbol, sales_median, sales_count) "
               "VALUES(?, ?, ?, ?, ?, ?)", (slug, time.time(), floor_native, symbol, median, len(prices)))
    return {"floor": floor_native, "symbol": symbol, "sales_median": median, "sales": len(prices)}


def latest(db: Database, slug: str) -> dict | None:
    return db.one("SELECT * FROM floor_history WHERE slug = ? ORDER BY ts DESC LIMIT 1", (slug,))


def floor_change(db: Database, slug: str, window_minutes: float) -> float | None:
    """Percent change from the highest floor in the window to the latest floor (negative = drop)."""
    rows = db.all("SELECT floor_native FROM floor_history WHERE slug = ? AND ts >= ? AND "
                  "floor_native IS NOT NULL ORDER BY ts", (slug, time.time() - window_minutes * 60))
    if len(rows) < 2:
        return None
    peak, now = max(r["floor_native"] for r in rows), rows[-1]["floor_native"]
    return (now - peak) / peak * 100 if peak else None


def floor_vs_sales(db: Database, slug: str) -> float | None:
    """Percent difference between the latest floor and the recent-sales median."""
    row = latest(db, slug)
    if not row or not row["floor_native"] or not row["sales_median"]:
        return None
    return (row["floor_native"] - row["sales_median"]) / row["sales_median"] * 100


def history(db: Database, slug: str, days: float = 7) -> list[dict]:
    return db.all("SELECT ts, floor_native, sales_median FROM floor_history WHERE slug = ? AND ts >= ? "
                  "ORDER BY ts", (slug, time.time() - days * 86400))


_USD: dict[int, tuple[float, float | None]] = {}     # chain_id -> (fetched_at, usd per native coin)
USD_TTL = 600
STABLES = {"USDC", "USDT", "USDG", "DAI", "USDC.E", "PYUSD"}


def _price(data: dict, *keys: str) -> float | None:
    for k in keys:                       # OpenSea answers usdPrice (camelCase); accept both
        v = data.get(k)
        if v not in (None, ""):
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f > 0:
                return f
    return None


async def native_usd(db: Database, api: OpenSeaAPI, chain_id: int) -> float | None:
    """USD price of one native coin.

    1. OpenSea's payment-token price for the chain's native coin (usdPrice).
    2. Native coin is ETH: the Ethereum ETH price.
    3. A pricing currency of a tracked collection on that chain, priced in ETH, times ETH/USD.
    """
    hit = _USD.get(chain_id)
    if hit and time.time() - hit[0] < (USD_TTL if hit[1] else 60):
        return hit[1]
    price = None
    chain = CHAIN_SLUGS.get(chain_id)
    eth_ratio = None
    if chain and api.configured:
        try:
            data = await api.payment_token(chain, "0x0000000000000000000000000000000000000000")
            price = _price(data, "usdPrice", "usd_price")
            eth_ratio = _price(data, "ethPrice", "eth_price")
        except OpenSeaError:
            price = None
    native = native_symbol(db, chain_id).upper()
    if price is None and chain_id != 1 and (native == "ETH" or eth_ratio):
        eth = await native_usd(db, api, 1)
        if eth:
            price = eth if native == "ETH" else eth * eth_ratio
    if price is None:
        for row in db.all("SELECT info FROM market_collections WHERE chain_id = ?", (chain_id,)):
            for token in (json.loads(row["info"]).get("pricing_currencies") or {}).values():
                if not isinstance(token, dict) or not token.get("eth_price"):
                    continue
                sym = (token.get("symbol") or "").upper()
                if sym in STABLES and native == "ETH":
                    price = 1 / float(token["eth_price"])
                elif sym in (native, "W" + native) and chain_id != 1:
                    eth = await native_usd(db, api, 1)
                    price = eth * float(token["eth_price"]) if eth else None
                if price:
                    break
            if price:
                break
    _USD[chain_id] = (time.time(), price)
    return price


async def sync_trades(db: Database, api: OpenSeaAPI, wallets: list[str], chain_ids: list[int]) -> dict:
    """Record what each wallet bought and sold on OpenSea (for Spent / Received)."""
    found, errors = 0, []
    for chain_id in chain_ids:
        chain = CHAIN_SLUGS.get(chain_id)
        if not chain:
            continue
        for wallet in wallets:
            try:
                events = await api.account_sales(wallet, chain)
            except OpenSeaError as error:
                errors.append(f"{chain}: {error}")
                continue
            rows = []
            for e in events:
                buyer, seller = (e.get("buyer") or "").lower(), (e.get("seller") or "").lower()
                side = "buy" if buyer == wallet.lower() else "sell" if seller == wallet.lower() else None
                nft = e.get("nft") or {}
                if not side or not e.get("transaction"):
                    continue
                pay = e.get("payment") or {}
                qty = int(e.get("quantity") or 1) or 1
                value, _ = to_native(db, chain_id, pay.get("symbol", ""), int(pay.get("quantity") or 0),
                                     int(pay.get("decimals") or 18), None)
                rows.append((chain_id, to_checksum_address(wallet), e["transaction"], str(nft.get("identifier", "")),
                             nft.get("collection"), side, None if value is None else value / qty * qty,
                             pay.get("symbol"), float(e.get("event_timestamp") or time.time())))
            db.executemany("INSERT OR REPLACE INTO os_trades VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            found += len(rows)
    return {"trades": found, "errors": errors[:5]}


def portfolio(db: Database, wallet: str | None = None) -> list[dict]:
    """Per collection: what you hold, its value at floor, what you spent and received.
    With ``wallet``, only that wallet's holdings, spending and sales are counted."""
    out = []
    hw, hargs = ("WHERE address = ?", (wallet,)) if wallet else ("", ())
    holdings = db.all("SELECT slug, chain_id, contract, COUNT(*) AS n, MAX(name) AS name FROM os_holdings "
                      f"{hw} GROUP BY slug, chain_id, contract", hargs)
    lw, largs = (" AND wallet = ?", (wallet,)) if wallet else ("", ())
    slugs = {h["slug"] for h in holdings} | {r["slug"] for r in db.all(
        f"SELECT DISTINCT slug FROM offer_log WHERE status = 'accepted' AND slug IS NOT NULL{lw}", largs)} | {
        r["slug"] for r in db.all("SELECT DISTINCT slug FROM os_trades WHERE slug IS NOT NULL"
                                  + (" AND address = ?" if wallet else ""), (wallet,) if wallet else ())}
    for slug in sorted(slugs):
        h = next((x for x in holdings if x["slug"] == slug), None)
        snap = latest(db, slug)
        floor = snap["floor_native"] if snap else None
        contract = h["contract"] if h else None
        spent = 0.0
        if contract:
            for r in db.all("SELECT value_wei, gas_cost_wei, operation, slug, asset FROM history "
                            "WHERE status = 'confirmed' AND operation IN ('mint', 'custom_mint')"
                            + (" AND address = ?" if wallet else ""), (wallet,) if wallet else ()):
                if r["operation"] == "mint" and r["slug"] != slug:
                    continue
                if r["operation"] == "custom_mint" and (r["asset"] or "").lower() != f"erc721:{contract}".lower():
                    continue
                spent += int(r["value_wei"] or 0) / 1e18 + int(r["gas_cost_wei"] or 0) / 1e18
        sold = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(net_native), 0) AS net FROM offer_log "
                      f"WHERE slug = ? AND status = 'accepted'{lw}", (slug, *largs))
        tw, targs = (" AND address = ?", (wallet,)) if wallet else ("", ())
        bought = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS v FROM os_trades "
                        f"WHERE slug = ? AND side = 'buy'{tw}", (slug, *targs))
        sales = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS v FROM os_trades "
                       f"WHERE slug = ? AND side = 'sell'{tw}", (slug, *targs))
        held = h["n"] if h else 0
        value = floor * held if floor is not None else None
        spent_mint = spent
        spent += bought["v"]
        # OpenSea sale events include sales made through the Offer Manager; use them when present
        received, sold_n = (sales["v"], sales["n"]) if sales["n"] else (sold["net"], sold["n"])
        out.append({
            "slug": slug, "name": (db.one("SELECT name FROM market_collections WHERE slug = ?", (slug,)) or {}).get("name") or slug,
            "chain_id": h["chain_id"] if h else (db.one("SELECT chain_id FROM os_trades WHERE slug = ? LIMIT 1",
                                                          (slug,)) or {}).get("chain_id"),
            "held": held, "floor": floor, "sales_median": snap["sales_median"] if snap else None,
            "value": value, "spent": spent, "spent_mint": spent_mint, "spent_buy": bought["v"],
            "bought": bought["n"], "sold": sold_n, "received": received,
            "pnl": None if value is None else received + value - spent,
            "change_24h": floor_change(db, slug, 24 * 60),
        })
    return sorted(out, key=lambda r: -(r["value"] or 0))
