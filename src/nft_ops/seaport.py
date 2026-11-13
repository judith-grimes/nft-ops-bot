"""Seaport helpers: encode OpenSea fulfilment data, validate it, resolve the conduit.

OpenSea's POST /offers/fulfillment_data returns the Seaport function signature (with full
ABI types) and its arguments as JSON. We encode exactly that, then validate the result
against the offer we evaluated before anything is signed.
"""

from __future__ import annotations

from eth_abi import decode, encode
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from .networks import RpcError, RpcPool

SUPPORTED = ("matchAdvancedOrders", "fulfillAdvancedOrder")
ZERO_KEY = "0x" + "00" * 32


class SeaportError(Exception):
    pass


def split_top(text: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in text:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    return out + ([cur] if cur else [])


def _norm(value: object) -> int:
    """OpenSea writes the same number as hex in one place and decimal in another."""
    text = str(value).strip().lower()
    return int(text, 16) if text.startswith("0x") else int(text)


def _hex(value: str | None) -> bytes:
    return bytes.fromhex((value or "0x")[2:])


def _item(i: dict, consideration: bool) -> tuple:
    base = (_norm(i["itemType"]), to_checksum_address(i["token"]), _norm(i["identifierOrCriteria"]),
            _norm(i["startAmount"]), _norm(i["endAmount"]))
    return base + ((to_checksum_address(i["recipient"]),) if consideration else ())


def _params(p: dict) -> tuple:
    return (to_checksum_address(p["offerer"]), to_checksum_address(p["zone"]),
            [_item(i, False) for i in p["offer"]], [_item(i, True) for i in p["consideration"]],
            _norm(p["orderType"]), _norm(p["startTime"]), _norm(p["endTime"]), _hex(p["zoneHash"]),
            _norm(p["salt"]), _hex(p["conduitKey"]), _norm(p["totalOriginalConsiderationItems"]))


def _advanced(o: dict) -> tuple:
    return (_params(o["parameters"]), _norm(o["numerator"]), _norm(o["denominator"]),
            _hex(o.get("signature")), _hex(o.get("extraData")))


def _resolvers(rs: list[dict]) -> list[tuple]:
    return [(_norm(r["orderIndex"]), _norm(r["side"]), _norm(r["index"]), _norm(r["identifier"]),
             [_hex(x) for x in r.get("criteriaProof") or []]) for r in rs]


def encode_fulfillment(tx: dict) -> bytes:
    fn = tx["function"]
    name = fn[:fn.index("(")]
    if name not in SUPPORTED:
        raise SeaportError(f"unsupported fulfilment function {name}")
    types = split_top(fn[fn.index("(") + 1:fn.rindex(")")])
    a = tx["input_data"]
    if name == "matchAdvancedOrders":
        values = [[_advanced(o) for o in a["orders"]], _resolvers(a.get("criteriaResolvers") or []),
                  [([(_norm(c["orderIndex"]), _norm(c["itemIndex"])) for c in f["offerComponents"]],
                    [(_norm(c["orderIndex"]), _norm(c["itemIndex"])) for c in f["considerationComponents"]])
                   for f in a["fulfillments"]], to_checksum_address(a["recipient"])]
    else:
        values = [_advanced(a["advancedOrder"]), _resolvers(a.get("criteriaResolvers") or []),
                  _hex(a["fulfillerConduitKey"]), to_checksum_address(a["recipient"])]
    if len(types) != len(values):
        raise SeaportError("fulfilment data does not match its function signature")
    return keccak(text=fn)[:4] + encode(types, values)


def orders_of(tx: dict) -> list[dict]:
    a = tx["input_data"]
    return a["orders"] if "orders" in a else [a["advancedOrder"]]


def validate(tx: dict, original_offer: dict, wallet: str, contract: str, token_id: str,
             currency_token: str, expected_proceeds: int) -> int:
    """Raise SeaportError unless the transaction does exactly what the evaluation assumed.

    Returns the proceeds (in the offer currency's base units) the wallet will receive.
    """
    wallet_l, contract_l = wallet.lower(), contract.lower()
    if tx["to"].lower() != original_offer["protocol_address"].lower():
        raise SeaportError("transaction is not addressed to the offer's Seaport contract")
    if _norm(tx.get("value") or 0) != 0:
        raise SeaportError("accepting an offer must not send any coin")
    a = tx["input_data"]
    if a["recipient"].lower() != wallet_l:
        raise SeaportError("proceeds would not go to your wallet")
    orders = orders_of(tx)
    original = original_offer["protocol_data"]["parameters"]
    first = orders[0]["parameters"]
    for key in ("offerer", "zone", "startTime", "endTime", "salt", "conduitKey"):
        if _norm(first[key]) != _norm(original[key]):
            raise SeaportError(f"order {key} differs from the offer that was evaluated")
    if [(_item(i, False)) for i in first["offer"]] != [(_item(i, False)) for i in original["offer"]]:
        raise SeaportError("offered payment differs from the offer that was evaluated")
    if [(_item(i, True)) for i in first["consideration"]] != [(_item(i, True)) for i in original["consideration"]]:
        raise SeaportError("order conditions differ from the offer that was evaluated")
    proceeds = 0
    for order in orders:
        p = order["parameters"]
        if p["offerer"].lower() == wallet_l:
            # Everything this wallet gives away in the transaction: only the one NFT.
            for i in p["offer"]:
                if (i["token"].lower() != contract_l or _norm(i["identifierOrCriteria"]) != int(token_id)
                        or _norm(i["itemType"]) not in (2, 3) or _norm(i["startAmount"]) > 1 and _norm(i["itemType"]) == 2):
                    raise SeaportError("the transaction would take something other than this NFT")
            for c in p["consideration"]:
                if c["recipient"].lower() == wallet_l and c["token"].lower() == currency_token.lower():
                    proceeds += _norm(c["startAmount"])
    if "orders" not in a:          # fulfillAdvancedOrder: wallet receives the offer items directly
        proceeds = expected_proceeds
    if proceeds < expected_proceeds:
        raise SeaportError(f"transaction pays {proceeds}, less than the expected {expected_proceeds}")
    return proceeds


async def operator_for(pool: RpcPool, chain_id: int, seaport: str, conduit_key: str) -> str:
    """The address that must be approved to move NFTs: the conduit for the key, or Seaport."""
    if not conduit_key or int(conduit_key, 16) == 0:
        return to_checksum_address(seaport)
    info = await pool.call(chain_id, "eth_call", [{"to": seaport, "data": "0x" + keccak(text="information()")[:4].hex()}, "latest"])
    try:
        _, _, controller = decode(["string", "bytes32", "address"], _hex(info))
    except Exception:  # noqa: BLE001
        raise SeaportError("could not read Seaport information()") from None
    data = keccak(text="getConduit(bytes32)")[:4] + encode(["bytes32"], [_hex(conduit_key)])
    res = await pool.call(chain_id, "eth_call", [{"to": controller, "data": "0x" + data.hex()}, "latest"])
    conduit, exists = decode(["address", "bool"], _hex(res))
    if not exists:
        raise SeaportError("the offer's conduit does not exist on this chain")
    return to_checksum_address(conduit)


async def is_approved(pool: RpcPool, chain_id: int, contract: str, owner: str, operator: str) -> bool:
    data = bytes.fromhex("e985e9c5") + encode(["address", "address"], [owner, operator])
    try:
        res = await pool.call(chain_id, "eth_call", [{"to": contract, "data": "0x" + data.hex()}, "latest"])
    except RpcError:
        return False
    return isinstance(res, str) and len(res) >= 66 and int(res[2:66], 16) == 1


def approval_calldata(operator: str, approved: bool = True) -> bytes:
    return bytes.fromhex("a22cb465") + encode(["address", "bool"], [operator, approved])


async def owns(pool: RpcPool, chain_id: int, contract: str, owner: str, token_id: str, standard: str) -> bool:
    try:
        if standard == "erc1155":
            data = bytes.fromhex("00fdd58e") + encode(["address", "uint256"], [owner, int(token_id)])
            res = await pool.call(chain_id, "eth_call", [{"to": contract, "data": "0x" + data.hex()}, "latest"])
            return int(res[2:66], 16) > 0
        data = bytes.fromhex("6352211e") + encode(["uint256"], [int(token_id)])
        res = await pool.call(chain_id, "eth_call", [{"to": contract, "data": "0x" + data.hex()}, "latest"])
        return isinstance(res, str) and res[-40:].lower() == owner[2:].lower()
    except (RpcError, ValueError):
        return False

