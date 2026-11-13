"""Stage 6.5.2: wallet group roles, smart groups, split/move, after-mint target, USD + trades."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from nft_ops import groups, market
from nft_ops.opensea_api import OpenSeaAPI
from nft_ops.wallets import WalletError


def test_smart_groups_resolve_anywhere(env):
    w = env.wallets.generate(4)
    db = env.db
    db.execute("INSERT INTO balances VALUES(4663, ?, 'native', ?, ?)", (w[0], str(10**15), time.time()))
    db.execute("INSERT INTO balances VALUES(4663, ?, 'native', ?, ?)", (w[1], str(10**18), time.time()))
    db.execute("INSERT INTO wallet_readiness VALUES('d', 1, ?, 100, 'Ready', '[]', ?)", (w[2], time.time()))
    names = {g["name"]: g for g in groups.smart_list(db)}
    assert env.wallets.resolve_selection(None, "★ Low on gas") == [w[0]]
    assert env.wallets.resolve_selection(None, "★ Ready to mint (checked in 24 h)") == [w[2]]
    assert names["★ Low on gas"]["size"] == 1
    with pytest.raises(WalletError):
        env.wallets.create_group("★ mine")


def test_roles_vault_and_after_mint(env):
    w = env.wallets.generate(3)
    env.wallets.add_to_group(w[:2], "minters")
    env.wallets.add_to_group([w[2]], "cold")
    with pytest.raises(groups.GroupError):                    # no vault known yet
        groups.set_options(env.db, "minters", after_mint="vault")
    groups.set_options(env.db, "cold", role="vault")
    groups.set_options(env.db, "minters", role="minting", after_mint="vault")
    assert groups.after_mint_target(env.db, w[0]) == w[2]
    assert groups.after_mint_target(env.db, w[2]) is None      # the vault does not send to itself
    with pytest.raises(groups.GroupError):
        groups.set_options(env.db, "minters", vault="0x000000000000000000000000000000000000dEaD")
    h = {g["name"]: g for g in groups.health(env.db, 4663)}
    assert h["minters"]["role"] == "minting" and h["minters"]["size"] == 2 and h["minters"]["vault"] == w[2]


def test_split_and_move(env):
    w = env.wallets.generate(5)
    env.wallets.add_to_group(w, "all5")
    made = groups.split(env.db, "all5", 2, "batch", env.wallets.add_to_group)
    assert made == ["batch-1", "batch-2", "batch-3"]
    assert len(env.wallets.resolve_selection(None, "batch-3")) == 1
    first = env.wallets.resolve_selection(None, "batch-1")
    groups.move(env.db, first, "batch-1", "moved", env.wallets.add_to_group)
    assert env.wallets.resolve_selection(None, "batch-1") == []
    assert sorted(env.wallets.resolve_selection(None, "moved")) == sorted(first)


def _api(handler) -> OpenSeaAPI:
    return OpenSeaAPI(lambda: "k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                      requests_per_second=1000)


def test_usd_price_reads_camel_case_and_falls_back_to_eth(env):
    def handler(req: httpx.Request) -> httpx.Response:
        if "/chain/ethereum/" in req.url.path:
            return httpx.Response(200, json={"symbol": "ETH", "usdPrice": "2000.5", "ethPrice": "1.0"})
        if "/chain/hyperevm/" in req.url.path:
            return httpx.Response(200, json={"symbol": "HYPE", "usdPrice": "72.3"})
        return httpx.Response(400, json={"errors": ["Invalid API key"]})   # e.g. monad today
    api = _api(handler)
    market._USD.clear()
    assert asyncio.run(market.native_usd(env.db, api, 1)) == 2000.5
    assert asyncio.run(market.native_usd(env.db, api, 999)) == 72.3
    assert asyncio.run(market.native_usd(env.db, api, 8453)) == 2000.5      # Base: ETH → Ethereum price


def test_trades_count_as_spent_and_received(env):
    w = env.wallets.generate(1)[0]

    def handler(req: httpx.Request) -> httpx.Response:
        ev = lambda side, tx: {"event_type": "sale", "transaction": tx, "quantity": 1, "event_timestamp": 1,
                               "buyer": w if side == "buy" else "0x1", "seller": "0x1" if side == "buy" else w,
                               "nft": {"identifier": "7", "collection": "drop", "contract": "0xC0"},
                               "payment": {"quantity": str(2 * 10**17), "decimals": 18, "symbol": "ETH"}}
        return httpx.Response(200, json={"asset_events": [ev("buy", "0xaa"), ev("sell", "0xbb")], "next": None})
    out = asyncio.run(market.sync_trades(env.db, _api(handler), [w], [1]))
    assert out["trades"] == 2
    row = market.portfolio(env.db, w)[0]
    assert row["spent_buy"] == pytest.approx(0.2) and row["received"] == pytest.approx(0.2)


def test_after_mint_sends_new_nfts_to_vault(env):
    from types import SimpleNamespace

    from nft_ops import aftermint, nfts
    from nft_ops.notify import Notifier
    from nft_ops.txengine import TxEngine

    async def run():
        nft = "0x2222222222222222222222222222222222222222"
        w = env.wallets.generate(3)
        for a in w:
            env.chain.balances[a.lower()] = 10**18
        env.safety.set_limits({"max_transfer_per_operation": "10", "max_daily": "10"})
        env.wallets.add_to_group(w[:2], "minters")
        env.wallets.add_to_group([w[2]], "cold")
        groups.set_options(env.db, "cold", role="vault")
        groups.set_options(env.db, "minters", after_mint="vault")
        env.chain.erc721[nft.lower()] = {5: w[0].lower(), 6: w[1].lower()}
        nfts.record_mint(env.db, 4663, nft, w[0], [5])
        nfts.record_mint(env.db, 4663, nft, w[1], [6])
        tx = TxEngine(env.db, env.vault, env.pool, env.bus, env.safety)
        state = SimpleNamespace(db=env.db, pool=env.pool, tx=tx, notifier=Notifier(env.db, env.bus))
        job = SimpleNamespace(preview=SimpleNamespace(metadata=SimpleNamespace(network_id=4663, address=nft, slug="drop")),
                              runs={w[0]: SimpleNamespace(address=w[0], phase="confirmed"),
                                    w[1]: SimpleNamespace(address=w[1], phase="failed")})   # failed mint: untouched
        await aftermint.run(state, job)
        await asyncio.wait_for(next(iter(tx.jobs.values())).task, 30)
        assert env.chain.erc721[nft.lower()] == {5: w[2].lower(), 6: w[1].lower()}
        titles = [n["title"] for n in state.notifier.list()["notifications"]]
        assert "Sending minted NFTs to the vault" in titles
    asyncio.run(run())
