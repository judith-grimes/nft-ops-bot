"""Offer Manager with simulated OpenSea offers and a simulated Seaport (no real network)."""

from __future__ import annotations

import asyncio
import json

import pytest
from fake_opensea_api import CONDUIT, OS_FEE, SEAPORT, WETH, FakeOffer, FakeOpenSeaAPI

from nft_ops import offer_rules
from nft_ops.offers import OfferEngine, OfferError
from nft_ops.opensea_api import OpenSeaAPI
from nft_ops.txengine import TxEngine

NFT = "0x7777777777777777777777777777777777777777"
BUYER = "0x00000000000000000000000000000000000000b1"
ETH = 10**18


def _setup(env, approve=True):
    wallets = env.wallets.generate(2)
    for w in wallets:
        env.chain.balances[w.lower()] = ETH
    env.chain.erc721[NFT.lower()] = {1: wallets[0].lower(), 2: wallets[0].lower(), 3: wallets[1].lower()}
    env.chain.erc20[WETH] = {BUYER: 100 * ETH}
    fake = FakeOpenSeaAPI(env.chain, NFT)
    api = OpenSeaAPI(lambda: "test-key", client=fake.client(), requests_per_second=1000)
    tx = TxEngine(env.db, env.vault, env.pool, env.bus, env.safety)
    engine = OfferEngine(env.db, env.vault, env.pool, env.bus, env.safety, tx, api)
    if approve:
        for w in wallets:
            env.chain.approvals[(NFT.lower(), w.lower(), CONDUIT)] = True
    return wallets, fake, engine, tx


def _offer(i, price, token=None, **kw):
    return FakeOffer(f"0x{i:064x}", BUYER, int(price * ETH), NFT.lower(), token, **kw)


def _rows(engine, token=None):
    d = engine.dashboard()
    return [o for o in d["offers"] if token is None or o["token_id"] == str(token)]


def test_net_proceeds_and_thresholds(env):
    async def run():
        wallets, fake, engine, _ = _setup(env)
        fake.offers = [_offer(1, 0.12, 1), _offer(2, 0.09), _offer(3, 0.0955, 2)]
        await engine.scan(sync_holdings=True)
        by = {(o["order_hash"][-1], o["token_id"]): o for o in _rows(engine)}
        good = by[("1", "1")]
        assert good["qualifies"] and good["market_fee"] == pytest.approx(0.0012)
        assert good["net"] == pytest.approx(0.12 - 0.0012 - good["gas_native"])
        assert good["min_price"] == pytest.approx(0.095)                      # 95% of floor 0.1
        coll = by[("2", "3")]                                                # collection offer applies to #3
        assert not coll["qualifies"] and "minimum" in coll["reason"]
        near = by[("3", "2")]                                                # gross >= 0.095, net < 0.095
        assert not near["qualifies"] and "you receive" in near["reason"]
        assert ("1", "2") not in by                                          # item offer for #1 only
        assert engine.dashboard()["summary"]["qualifying"] == 1
    asyncio.run(run())


def test_approval_then_manual_accept_pays_exact_net(env):
    async def run():
        wallets, fake, engine, tx = _setup(env, approve=False)
        fake.offers = [_offer(1, 0.12, 1, creator_bps=200)]
        await engine.scan(sync_holdings=True)
        (row,) = _rows(engine, 1)
        assert not row["qualifies"] and "approval" in row["reason"]
        assert row["operator"].lower() == CONDUIT                               # resolved on-chain
        items = engine.approval_items(4663, wallets[0], NFT, row["operator"])
        preview = await tx.preview("approval", 4663, items, "Approval")
        assert preview["confirm_word"] == "APPROVE"
        job = await tx.start(preview["token"], "APPROVE")
        await asyncio.wait_for(tx.jobs[job].task, 30)
        assert env.chain.approvals[(NFT.lower(), wallets[0].lower(), CONDUIT)] is True
        await engine.scan()
        (row,) = _rows(engine, 1)
        assert row["qualifies"] and row["creator_fee"] == pytest.approx(0.0024)
        with pytest.raises(OfferError, match="ACCEPT"):
            await engine.accept(row["order_hash"], NFT, "1", wallets[0], "manual", "yes")
        result = await engine.accept(row["order_hash"], NFT, "1", wallets[0], "manual", "ACCEPT")
        assert result["status"] == "accepted", result
        assert env.chain.erc721[NFT.lower()][1] == BUYER                        # NFT went to the buyer
        assert env.chain.erc20[WETH][wallets[0].lower()] == int(0.12 * ETH) - int(0.0012 * ETH) - int(0.0024 * ETH)
        assert env.chain.erc20[WETH][OS_FEE] == int(0.0012 * ETH)
        log = env.db.one("SELECT * FROM offer_log WHERE status = 'accepted'")
        assert log["tx_hash"] and log["market_fee"] == pytest.approx(0.0012) and log["mode"] == "manual"
        hist = env.db.one("SELECT * FROM history WHERE operation = 'offer_accept'")
        assert hist["status"] == "confirmed"
        assert _rows(engine, 1) == []                                           # sold: no longer listed
    asyncio.run(run())


def test_full_auto_needs_arming_and_takes_best_offer_once(env):
    async def run():
        wallets, fake, engine, _ = _setup(env)
        fake.offers = [_offer(1, 0.12, 1), _offer(2, 0.13, 1), _offer(3, 0.125)]
        assert offer_rules.rules_for(env.db, "fake-drop")["execution_mode"] == "full"
        await engine.scan(sync_holdings=True)
        await asyncio.sleep(0.2)
        assert env.chain.sent == []                                             # not armed: nothing sent
        with pytest.raises(OfferError):
            engine.arm("fake-drop", "yes")
        engine.arm("fake-drop", "AUTO-ACCEPT")
        await engine.scan()
        for _ in range(100):
            if not engine._tasks:
                break
            await asyncio.sleep(0.05)
        await asyncio.gather(*list(engine._tasks), return_exceptions=True)
        accepted = env.db.all("SELECT token_id, order_hash FROM offer_log WHERE status = 'accepted'")
        tokens = [a["token_id"] for a in accepted]
        assert len(tokens) == len(set(tokens))                                  # never two sales of one NFT
        best_for_1 = next(a for a in accepted if a["token_id"] == "1")
        assert best_for_1["order_hash"].endswith("2")                           # the 0.13 offer won
        assert env.chain.erc721[NFT.lower()][1] == BUYER
    asyncio.run(run())


def test_floor_drop_pauses_auto_accept(env):
    async def run():
        wallets, fake, engine, _ = _setup(env)
        engine.arm("fake-drop", "AUTO-ACCEPT")
        engine.save_rules("*", {"floor_pct": 50})
        fake.floor = 0.1
        await engine.scan(sync_holdings=True)                                   # no offers yet
        env.db.execute("UPDATE floor_history SET ts = ts - 300")                # make it an older snapshot
        fake.floor, fake.offers = 0.085, [_offer(1, 0.2, 1)]
        engine._market.__func__  # noqa: B018
        env.db.execute("UPDATE market_collections SET updated_at = 0")
        await engine._market("fake-drop", 4663, offer_rules.rules_for(env.db, "fake-drop"), force=True)
        await engine.scan()
        await asyncio.gather(*list(engine._tasks), return_exceptions=True)
        st = engine.state("fake-drop")
        assert st["paused"] and "15.0%" in st["paused"]
        assert env.chain.sent == []
        assert any(a["kind"] == "offers_paused" for a in env.db.all("SELECT kind FROM alerts"))
        (row,) = _rows(engine, 1)
        assert not row["qualifies"] and "floor changed" in row["reason"]
        engine.resume("fake-drop")
        assert not engine.state("fake-drop")["paused"]
    asyncio.run(run())


def test_rechecks_before_sending(env):
    async def run():
        wallets, fake, engine, _ = _setup(env)
        fake.offers = [_offer(1, 0.12, 1), _offer(2, 0.12, 2)]
        await engine.scan(sync_holdings=True)
        row1, row2 = _rows(engine, 1)[0], _rows(engine, 2)[0]
        env.chain.erc721[NFT.lower()][1] = "0x" + "99" * 20                     # NFT moved elsewhere
        r = await engine.accept(row1["order_hash"], NFT, "1", wallets[0], "manual", "ACCEPT")
        assert r["status"] == "skipped" and "no longer held" in r["reason"]
        env.safety.stop("test")
        r = await engine.accept(row2["order_hash"], NFT, "2", wallets[0], "manual", "ACCEPT")
        assert r["status"] == "skipped" and "STOP" in r["reason"]
        env.safety.resume("RESUME")
        original = fake._handle

        def tampered(request):                                                   # OpenSea returns a bad tx
            response = original(request)
            if request.url.path.endswith("/offers/fulfillment_data"):
                data = response.json()
                data["fulfillment_data"]["transaction"]["input_data"]["orders"][1]["parameters"]["offer"].append(
                    {"itemType": 1, "token": WETH, "identifierOrCriteria": "0", "startAmount": "5", "endAmount": "5"})
                return type(response)(200, json=data)
            return response
        fake._handle = tampered
        engine.api._client = fake.client()
        r = await engine.accept(row2["order_hash"], NFT, "2", wallets[0], "manual", "ACCEPT")
        assert r["status"] == "skipped" and "something other than this NFT" in r["reason"]
        assert env.chain.sent == []
    asyncio.run(run())


def test_daily_limit_and_cancelled_offer(env):
    async def run():
        wallets, fake, engine, _ = _setup(env)
        engine.save_rules("fake-drop", {"max_per_day": 1})
        fake.offers = [_offer(1, 0.12, 1), _offer(2, 0.12, 2), _offer(3, 0.12, 3)]
        await engine.scan(sync_holdings=True)
        a = await engine.accept(_rows(engine, 1)[0]["order_hash"], NFT, "1", wallets[0], "manual", "ACCEPT")
        b = await engine.accept(_rows(engine, 2)[0]["order_hash"], NFT, "2", wallets[0], "manual", "ACCEPT")
        assert a["status"] == "accepted" and b["status"] == "skipped" and "daily limit" in b["reason"]
        engine.save_rules("fake-drop", {"max_per_day": 10})
        fake.filled.add(_rows(engine, 3)[0]["order_hash"])                      # buyer cancelled/filled it
        c = await engine.accept(_rows(engine, 3)[0]["order_hash"], NFT, "3", wallets[1], "manual", "ACCEPT")
        assert c["status"] == "skipped" and "validated" in c["reason"]
    asyncio.run(run())


def test_stablecoin_offer_conversion(env):
    async def run():
        wallets, fake, engine, _ = _setup(env)
        offer = _offer(1, 0, 1)
        offer.price_wei = 400 * 10**6                       # 400 USDG at 0.0005 ETH = 0.2 ETH
        raw = offer.api()
        raw["price"] = {"currency": "USDG", "decimals": 6, "value": str(400 * 10**6)}
        parsed = offer_rules.parse_offer(raw)
        info = json.loads(json.dumps({"pricing_currencies": {"offer_currency": {"symbol": "USDG", "eth_price": "0.0005"}}}))
        hold = {"chain_id": 4663, "address": wallets[0], "contract": NFT, "token_id": "1"}
        mkt = {"floor": 0.1, "info": info, "change": None, "vs_sales": None}
        rules = offer_rules.rules_for(env.db, "fake-drop")
        ev = offer_rules.evaluate(env.db, rules, parsed, hold, mkt, 0.0005, set(), approved=True)
        assert ev["gross_native"] == pytest.approx(0.2 * 0.99)                 # 1% safety haircut
        assert ev["qualifies"]
        ev = offer_rules.evaluate(env.db, {**rules, "allow_stablecoins": False}, parsed, hold, mkt, 0.0005, set(),
                                  approved=True)
        assert not ev["qualifies"] and "not allowed" in ev["reason"]
    asyncio.run(run())


def test_protections_cannot_be_made_optional(env):
    with pytest.raises(ValueError, match="never be optional"):
        offer_rules.validate_rules({"optional": ["net_min"]})
    assert offer_rules.validate_rules({"optional": ["fee_pct"]}) == {"optional": ["fee_pct"]}
    with pytest.raises(ValueError):
        offer_rules.validate_rules({"execution_mode": "yolo"})
    assert SEAPORT  # imported constants are used by the fakes


def test_trait_offers_only_apply_when_traits_known_and_matching():
    raw = FakeOffer("0x" + "ab" * 32, BUYER, ETH, NFT.lower(), None).api()
    raw["criteria"] = {"traits": [{"type": "Hat", "value": "Crown"}]}
    parsed = offer_rules.parse_offer(raw)
    assert parsed["type"] == "trait"
    base = {"contract": NFT, "token_id": "1"}
    assert not offer_rules.offer_applies(parsed, {**base, "traits_list": None})          # unknown traits
    assert not offer_rules.offer_applies(parsed, {**base, "traits_list": []})            # empty: unknown
    assert not offer_rules.offer_applies(parsed, {**base, "traits_list": ["Hat:Cap"]})
    assert offer_rules.offer_applies(parsed, {**base, "traits_list": ["Hat:Crown", "Eyes:Blue"]})
    raw["criteria"] = {"traits": None, "numeric_traits": [{"type": "Level", "min": 5}]}
    numeric = offer_rules.parse_offer(raw)
    assert not offer_rules.offer_applies(numeric, {**base, "traits_list": ["Level:7"]})  # not supported: skip
