"""Stage 6.5.1: per-wallet portfolio, USD conversion, timezone data available everywhere."""

from __future__ import annotations

import asyncio
import json
import time

from nft_ops import market
from nft_ops.opensea_api import OpenSeaAPI


def test_timezones_resolve_without_os_database():
    import importlib.util
    from zoneinfo import ZoneInfo
    assert importlib.util.find_spec("tzdata") is not None      # bundled for Windows
    assert ZoneInfo("Asia/Jakarta").key == "Asia/Jakarta"


def test_portfolio_per_wallet_and_usd_fallback(env):
    db = env.db
    now = time.time()
    for addr, tid in (("0xA", "1"), ("0xA", "2"), ("0xB", "3")):
        db.execute("INSERT INTO os_holdings VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (4663, addr, "0xC0", tid, "drop", "Drop", "erc721", "1", None, None, now))
    db.execute("INSERT INTO floor_history(slug, ts, floor_native, floor_symbol, sales_median, sales_count) "
               "VALUES('drop', ?, 0.1, 'ETH', 0.1, 3)", (now,))
    db.execute("INSERT INTO market_collections(slug, chain_id, contract, name, info, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
               ("drop", 4663, "0xC0", "Drop", json.dumps({"pricing_currencies": {"offer_currency": {
                   "symbol": "USDG", "eth_price": "0.0005", "decimals": 6}}}), now))
    all_rows = market.portfolio(db)
    a_rows = market.portfolio(db, "0xA")
    assert all_rows[0]["held"] == 3 and a_rows[0]["held"] == 2
    api = OpenSeaAPI(lambda: None)                      # no key: falls back to the stablecoin rate
    market._USD.clear()
    price = asyncio.run(market.native_usd(db, api, 4663))
    assert round(price) == 2000
