"""Simulated OpenSea REST API and Seaport contract for offer-manager tests.

The API answers in the same shapes as the real api.opensea.io v2 responses captured during
development (offers with Seaport protocol_data, fulfillment_data with matchAdvancedOrders and
a mirror order). The fake Seaport on the fake chain checks ownership and approval, then moves
the NFT to the buyer and pays the seller and fee recipients from the buyer's WETH.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import httpx
from eth_abi import decode, encode
from eth_utils.crypto import keccak

SEAPORT = "0x0000000000000068f116a894984e2db1123eb395"
CONTROLLER = "0x00000000f9490004c11cef243f5400493c00ad63"
CONDUIT = "0x1e0049783f008a0085193e00003d00cd54003c71"
CONDUIT_KEY = "0x0000007b02230091a7ed01230072f7006a004d60a8d4e71d599b8104250f0000"
WETH = "0x5555555555555555555555555555555555555555"
OS_FEE = "0x0000a26b00c1f0df003000390027140000faa719"
MATCH = ("matchAdvancedOrders(((address,address,(uint8,address,uint256,uint256,uint256)[],(uint8,address,uint256,"
         "uint256,uint256,address)[],uint8,uint256,uint256,bytes32,uint256,bytes32,uint256),uint120,uint120,bytes,"
         "bytes)[],(uint256,uint8,uint256,uint256,bytes32[])[],((uint256,uint256)[],(uint256,uint256)[])[],address)")
MATCH_TYPES = ["((address,address,(uint8,address,uint256,uint256,uint256)[],(uint8,address,uint256,uint256,uint256,"
               "address)[],uint8,uint256,uint256,bytes32,uint256,bytes32,uint256),uint120,uint120,bytes,bytes)[]",
               "(uint256,uint8,uint256,uint256,bytes32[])[]", "((uint256,uint256)[],(uint256,uint256)[])[]", "address"]


@dataclass
class FakeOffer:
    order_hash: str
    offerer: str
    price_wei: int
    contract: str
    token_id: int | None = None          # None = collection offer
    fee_bps: int = 100                   # OpenSea fee (1%)
    creator_bps: int = 0
    currency: str = "WETH"
    status: str = "ACTIVE"
    end: float = field(default_factory=lambda: time.time() + 86400)

    def protocol(self) -> dict:
        fee = self.price_wei * self.fee_bps // 10000
        creator = self.price_wei * self.creator_bps // 10000
        cons = [{"itemType": 4 if self.token_id is None else 2, "token": self.contract,
                 "identifierOrCriteria": "0" if self.token_id is None else str(self.token_id),
                 "startAmount": "1", "endAmount": "1", "recipient": self.offerer},
                {"itemType": 1, "token": WETH, "identifierOrCriteria": "0", "startAmount": str(fee),
                 "endAmount": str(fee), "recipient": OS_FEE}]
        if creator:
            cons.append({"itemType": 1, "token": WETH, "identifierOrCriteria": "0", "startAmount": str(creator),
                         "endAmount": str(creator), "recipient": "0x" + "c7" * 20})
        return {"parameters": {
            "offerer": self.offerer, "zone": "0x000056f7000000ece9003ca63978907a00ffd100",
            "offer": [{"itemType": 1, "token": WETH, "identifierOrCriteria": "0",
                       "startAmount": str(self.price_wei), "endAmount": str(self.price_wei)}],
            "consideration": cons, "orderType": 2, "startTime": str(int(time.time()) - 600),
            "endTime": str(int(self.end)), "zoneHash": "0x" + "00" * 32, "salt": hex(int(self.order_hash[2:10], 16)),
            "conduitKey": CONDUIT_KEY, "totalOriginalConsiderationItems": str(len(cons)), "counter": "0"},
            "signature": "0x" + "11" * 65}

    def api(self) -> dict:
        return {"order_hash": self.order_hash, "chain": "robinhood", "protocol_address": SEAPORT,
                "protocol_data": self.protocol(), "status": self.status, "remaining_quantity": 1,
                "price": {"currency": self.currency, "decimals": 18, "value": str(self.price_wei)},
                "criteria": {"collection": {"slug": "fake-drop"}, "contract": {"address": self.contract},
                             "traits": None, "numeric_traits": None, "encoded_token_ids": "*"}
                if self.token_id is None else None}


class FakeOpenSeaAPI:
    def __init__(self, chain, contract: str) -> None:
        self.chain, self.contract = chain, contract.lower()
        self.offers: list[FakeOffer] = []
        self.floor = 0.1
        self.sales = [0.1, 0.1, 0.1]
        self.filled: set[str] = set()
        self.requests: list[str] = []
        chain.custom_seaport = self                  # let the fake chain call back into us

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/api/v2", "")
        self.requests.append(f"{request.method} {path}")
        if request.headers.get("x-api-key") != "test-key":
            return httpx.Response(401, json={"errors": ["bad key"]})
        if path == "/collections/fake-drop":
            return httpx.Response(200, json={"collection": "fake-drop", "name": "Fake Drop", "fees": [
                {"fee": 1.0, "recipient": OS_FEE, "required": True}],
                "contracts": [{"address": self.contract, "chain": "robinhood"}],
                "pricing_currencies": {"offer_currency": {"symbol": "USDG", "eth_price": "0.0005", "decimals": 6}}})
        if path == "/collections/fake-drop/stats":
            return httpx.Response(200, json={"total": {"floor_price": self.floor, "floor_price_symbol": "ETH"},
                                             "intervals": []})
        if path == "/events/collection/fake-drop":
            return httpx.Response(200, json={"asset_events": [{"payment": {"quantity": str(int(p * 1e18)),
                                             "decimals": 18, "symbol": "WETH"}, "quantity": 1} for p in self.sales],
                                             "next": None})
        if path == "/offers/collection/fake-drop":
            return httpx.Response(200, json={"offers": [o.api() for o in self.offers if o.token_id is None
                                                        and o.order_hash not in self.filled], "next": None})
        if path.startswith("/offers/collection/fake-drop/nfts/"):
            token = int(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"offers": [o.api() for o in self.offers if o.token_id == token
                                                        and o.order_hash not in self.filled], "next": None})
        if path == "/offers/collection/fake-drop/all":
            return httpx.Response(200, json={"offers": [o.api() for o in self.offers
                                                        if o.order_hash not in self.filled], "next": None})
        if path.startswith("/chain/robinhood/account/"):
            owner = path.split("/")[4].lower()
            nfts = [{"identifier": str(i), "collection": "fake-drop", "contract": self.contract,
                     "token_standard": "erc721", "name": f"Fake #{i}", "is_disabled": False, "quantity": 1,
                     "traits": []}
                    for i, o in self.chain.erc721.get(self.contract, {}).items() if o == owner]
            return httpx.Response(200, json={"nfts": nfts, "next": None})
        if path == "/offers/fulfillment_data" and request.method == "POST":
            body = json.loads(request.content)
            offer = next((o for o in self.offers if o.order_hash == body["offer"]["hash"]), None)
            if offer is None or offer.order_hash in self.filled:
                return httpx.Response(400, json={"errors": ["The order_hash does not exist"]})
            seller = body["fulfiller"]["address"]
            token = int(body["consideration"]["token_id"])
            proto = offer.protocol()
            fees = sum(int(c["startAmount"]) for c in proto["parameters"]["consideration"] if c["token"] == WETH)
            mirror = {"parameters": {"offerer": seller, "zone": "0x" + "00" * 20,
                      "offer": [{"itemType": 2, "token": self.contract, "identifierOrCriteria": str(token),
                                 "startAmount": "1", "endAmount": "1"}],
                      "consideration": [{"itemType": 1, "token": WETH, "identifierOrCriteria": "0",
                                         "startAmount": str(offer.price_wei - fees),
                                         "endAmount": str(offer.price_wei - fees), "recipient": seller}],
                      "orderType": 0, "startTime": proto["parameters"]["startTime"],
                      "endTime": proto["parameters"]["endTime"], "zoneHash": "0x" + "00" * 32, "salt": "7",
                      "conduitKey": CONDUIT_KEY, "totalOriginalConsiderationItems": "1"},
                      "numerator": 1, "denominator": 1, "signature": "0x", "extraData": "0x"}
            first = {**proto, "numerator": 1, "denominator": 1, "extraData": "0x00" + seller[2:]}
            return httpx.Response(200, json={"protocol": "seaport1.6", "fulfillment_data": {"orders": [proto],
                "transaction": {"function": MATCH, "chain": 4663, "to": SEAPORT, "value": "0",
                                "input_data": {"orders": [first, mirror],
                                               "criteriaResolvers": [] if offer.token_id is not None else [
                                                   {"orderIndex": 0, "side": 1, "index": 0, "identifier": str(token),
                                                    "criteriaProof": []}],
                                               "fulfillments": [], "recipient": seller}}}})
        return httpx.Response(404, json={"errors": [f"no fake for {path}"]})

    # ----- fake Seaport behaviour, called by FakeChain -----------------------------------
    def seaport_call(self, data: bytes) -> str | None:
        sel = data[:4].hex()
        if sel == keccak(text="information()")[:4].hex():
            return "0x" + encode(["string", "bytes32", "address"], ["1.6", b"\x00" * 32, CONTROLLER]).hex()
        return None

    def controller_call(self, data: bytes) -> str:
        return "0x" + encode(["address", "bool"], [CONDUIT, True]).hex()

    def execute(self, sender: str, data: bytes, dry: bool) -> str | None:
        if data[:4] != keccak(text=MATCH)[:4]:
            return "unknown function"
        orders, _resolvers, _fulfil, recipient = decode(MATCH_TYPES, data[4:])
        (buy_params, *_), (mirror, *_) = orders[0], orders[1]
        offerer, price = buy_params[0].lower(), buy_params[2][0][3]
        token = mirror[2][0][2]
        order_hash = next((o.order_hash for o in self.offers if o.offerer.lower() == offerer
                           and o.price_wei == price), None)
        if order_hash is None or order_hash in self.filled:
            return "order not fillable"
        if self.chain.erc721.get(self.contract, {}).get(token) != sender:
            return "ERC721: transfer caller is not owner nor approved"
        if not self.chain.approvals.get((self.contract, sender, CONDUIT)):
            return "ERC721: transfer caller is not owner nor approved"
        weth = self.chain.erc20.setdefault(WETH, {})
        if weth.get(offerer, 0) < price:
            return "WETH: insufficient balance"
        if dry:
            return None
        self.filled.add(order_hash)
        self.chain.erc721[self.contract][token] = offerer
        weth[offerer] -= price
        fees = 0
        for item in buy_params[3]:
            if item[1].lower() == WETH:
                weth[item[5].lower()] = weth.get(item[5].lower(), 0) + item[3]
                fees += item[3]
        weth[recipient.lower()] = weth.get(recipient.lower(), 0) + price - fees
        return None
