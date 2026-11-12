"""Contract analyser: what can be minted, how, for how much, and with what restrictions.

Detection is kept separate from execution. A report only describes the contract; a custom
mint must still be chosen explicitly, simulated per wallet and confirmed by the user.

Sources, best first:
  1. Verified ABI from Sourcify, then the network's Blockscout explorer (if reachable).
  2. ABI pasted by the user.
  3. Bytecode scan: function selectors found in the deployed code, matched against a built-in
     list of common NFT mint / read functions (medium confidence: names are inferred).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
from eth_abi import decode, encode
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from .db import Database
from .networks import RpcError, RpcPool

EIP1967_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
MINT_WORDS = re.compile(r"mint|claim|purchase|buy", re.IGNORECASE)

# Common mint functions seen across NFT contracts (bytecode scan dictionary).
KNOWN_MINTS = [
    "mint()", "mint(uint256)", "mint(uint8)", "mint(uint32)", "mint(address,uint256)",
    "mint(uint256,address)", "mint(uint256,bytes32[])", "mint(address,uint256,bytes32[])",
    "mint(uint256,bytes)", "publicMint()", "publicMint(uint256)", "mintPublic()",
    "mintPublic(uint256)", "publicSaleMint(uint256)", "saleMint(uint256)", "mintNFT(uint256)",
    "mintNFTs(uint256)", "mintTo(address)", "mintTo(address,uint256)", "safeMint(address,uint256)",
    "presaleMint(uint256,bytes32[])", "mintPresale(uint256,bytes32[])",
    "whitelistMint(uint256,bytes32[])", "whitelistMint(bytes32[],uint256)",
    "allowlistMint(uint256,bytes32[])", "allowlistMint(bytes32[],uint256)",
    "fcfsMint(uint256,bytes32[])", "freeMint()", "freeMint(uint256)", "claim()", "claim(uint256)",
    "claim(address,uint256)", "purchase(uint256)", "buy(uint256)",
    # thirdweb DropERC721 / OpenEditionERC721
    "claim(address,uint256,address,uint256,(bytes32[],uint256,uint256,address),bytes)",
]
# No-argument read functions and how to decode their result.
KNOWN_READS: dict[str, str] = {
    "name()": "string", "symbol()": "string", "owner()": "address",
    "totalSupply()": "uint256", "maxSupply()": "uint256", "MAX_SUPPLY()": "uint256",
    "cost()": "uint256", "price()": "uint256", "mintPrice()": "uint256", "MINT_PRICE()": "uint256",
    "PRICE()": "uint256", "publicPrice()": "uint256", "publicSalePrice()": "uint256",
    "presalePrice()": "uint256", "whitelistPrice()": "uint256", "getPrice()": "uint256",
    "maxPerWallet()": "uint256", "MAX_PER_WALLET()": "uint256", "maxMintAmount()": "uint256",
    "maxPerTx()": "uint256", "MAX_PER_TX()": "uint256", "maxMintPerWallet()": "uint256",
    "walletLimit()": "uint256", "paused()": "bool", "saleActive()": "bool",
    "publicSaleActive()": "bool", "isPublicSaleActive()": "bool", "mintActive()": "bool",
    "saleIsActive()": "bool", "presaleActive()": "bool", "whitelistActive()": "bool",
    "saleState()": "uint256", "publicSaleStartTime()": "uint256", "merkleRoot()": "bytes32",
}
PRICE_READS = ["cost()", "price()", "mintPrice()", "MINT_PRICE()", "PRICE()", "publicPrice()",
               "publicSalePrice()", "getPrice()", "presalePrice()", "whitelistPrice()"]
SEADROP_MARKER = "mintSeaDrop(address,uint256)"


def selector(signature: str) -> str:
    return keccak(text=signature)[:4].hex()


_MINT_BY_SELECTOR = {selector(s): s for s in KNOWN_MINTS + [SEADROP_MARKER]}
_READ_BY_SELECTOR = {selector(s): s for s in KNOWN_READS}


class ContractError(Exception):
    pass


_BASIC = re.compile(r"(uint(8|16|32|64|128|256)?|int(8|16|32|64|128|256)?|address|bool|"
                    r"bytes([1-9]|[12][0-9]|3[0-2])?|string)")
_ARRAY_SUFFIX = re.compile(r"(\[\d*\])*")
NATIVE_PLACEHOLDER = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"   # "native coin" in thirdweb-style drops


def split_top(text: str) -> list[str]:
    """Split on commas that are not inside parentheses."""
    parts, depth, current = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise ContractError("unbalanced parentheses in function signature")
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    if depth:
        raise ContractError("unbalanced parentheses in function signature")
    if current:
        parts.append(current)
    return parts


def _tuple_parts(kind: str) -> tuple[list[str], str]:
    """'(a,b)[]' -> (['a', 'b'], '[]')."""
    depth = 0
    for i, ch in enumerate(kind):
        depth += ch == "("
        depth -= ch == ")"
        if depth == 0:
            return split_top(kind[1:i]), kind[i + 1:]
    raise ContractError("unbalanced parentheses in function signature")


def check_type(kind: str) -> None:
    if kind.startswith("("):
        parts, suffix = _tuple_parts(kind)
        if not parts or not _ARRAY_SUFFIX.fullmatch(suffix):
            raise ContractError(f"unsupported parameter type {kind}")
        for part in parts:
            check_type(part)
        return
    base = re.match(r"[a-z]+\d*", kind)
    if not base or not _BASIC.fullmatch(base.group()) or not _ARRAY_SUFFIX.fullmatch(kind[base.end():]):
        raise ContractError(f"unsupported parameter type {kind}")


def parse_signature(signature: str) -> tuple[str, list[str]]:
    """'claim(address,(bytes32[],uint256),bytes)' -> ('claim', ['address', '(bytes32[],uint256)', 'bytes'])."""
    match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*", signature)
    if not match:
        raise ContractError("function must look like name(type,type)")
    types = split_top(match.group(2).replace(" ", ""))
    for t in types:
        check_type(t)
    return match.group(1), types


def canonical(signature: str) -> str:
    name, types = parse_signature(signature)
    return f"{name}({','.join(types)})"


def selectors_in_bytecode(code: bytes) -> set[str]:
    """Collect PUSH4 operands (how Solidity dispatchers compare function selectors)."""
    found, i = set(), 0
    while i < len(code):
        op = code[i]
        if op == 0x63 and i + 4 < len(code):
            found.add(code[i + 1:i + 5].hex())
        elif op == 0x62 and i + 3 < len(code):       # optimiser drops a leading 00 byte
            found.add("00" + code[i + 1:i + 4].hex())
        i += 1 + (op - 0x5F if 0x60 <= op <= 0x7F else 0)
    return found


def abi_functions(abi: list[dict]) -> list[dict]:
    out = []
    for entry in abi:
        if entry.get("type") != "function":
            continue
        types = [_abi_type(i) for i in entry.get("inputs", [])]
        sig = f"{entry['name']}({','.join(types)})"
        out.append({"signature": sig, "selector": selector(sig), "name": entry["name"],
                    "inputs": [{"name": i.get("name", ""), "type": t}
                               for i, t in zip(entry.get("inputs", []), types, strict=True)],
                    "outputs": [_abi_type(o) for o in entry.get("outputs", [])],
                    "mutability": entry.get("stateMutability", "nonpayable")})
    return out


def _abi_type(param: dict) -> str:
    t = param["type"]
    if t.startswith("tuple"):
        return "(" + ",".join(_abi_type(c) for c in param.get("components", [])) + ")" + t[5:]
    return t


def _decode(kind: str, raw: Any) -> Any:
    if not isinstance(raw, str) or raw in ("0x", "") or len(raw) < 66:
        return None
    data = bytes.fromhex(raw[2:])
    try:
        value = decode([kind], data)[0]
    except Exception:  # noqa: BLE001 - not every contract returns what the name suggests
        return None
    if kind == "bytes32":
        return "0x" + value.hex()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value


async def fetch_verified_abi(chain_id: int, address: str, explorer: str | None,
                             client: httpx.AsyncClient) -> tuple[list[dict] | None, str]:
    try:
        r = await client.get(f"https://sourcify.dev/server/v2/contract/{chain_id}/{address}",
                             params={"fields": "abi"}, timeout=15)
        if r.status_code == 200 and isinstance(r.json().get("abi"), list):
            return r.json()["abi"], "Sourcify (verified source)"
    except (httpx.HTTPError, ValueError):
        pass
    if explorer and "blockscout" in explorer:
        try:
            r = await client.get(f"{explorer.rstrip('/')}/api/v2/smart-contracts/{address}", timeout=15)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 200 and isinstance(body.get("abi"), list) and body.get("is_verified", True):
                return body["abi"], "Blockscout explorer (verified source)"
        except (httpx.HTTPError, ValueError):
            pass
    return None, ""


@dataclass(slots=True)
class Probe:
    code: bytes
    implementation: str | None


async def _probe(pool: RpcPool, chain_id: int, address: str) -> Probe:
    code_hex, slot = await pool.batch(chain_id, [("eth_getCode", [address, "latest"]),
                                                 ("eth_getStorageAt", [address, EIP1967_IMPL_SLOT, "latest"])])
    if isinstance(code_hex, RpcError):
        raise ContractError(f"could not read contract code: {code_hex}")
    code = bytes.fromhex((code_hex or "0x")[2:])
    impl = None
    if isinstance(slot, str) and len(slot) >= 66 and int(slot, 16):
        impl = to_checksum_address(bytes.fromhex(slot[-40:]))
    return Probe(code, impl)


async def analyze(db: Database, pool: RpcPool, chain_id: int, address: str,
                  pasted_abi: str = "", http: httpx.AsyncClient | None = None) -> dict:
    address = to_checksum_address(address)
    probe = await _probe(pool, chain_id, address)
    if not probe.code:
        raise ContractError("no contract at this address on this network")
    code = probe.code
    notes: list[str] = []
    if probe.implementation:
        impl_probe = await _probe(pool, chain_id, probe.implementation)
        code = impl_probe.code or code
        notes.append(f"Proxy contract: code lives at {probe.implementation} (EIP-1967).")

    abi, abi_source = None, ""
    if pasted_abi.strip():
        try:
            parsed = json.loads(pasted_abi)
            abi = parsed["abi"] if isinstance(parsed, dict) else parsed
            if not isinstance(abi, list):
                raise ValueError
            abi_source = "ABI you pasted"
        except (ValueError, KeyError):
            raise ContractError("pasted ABI is not valid JSON (expected a list or {\"abi\": [...]})") from None
    else:
        net = db.one("SELECT explorer_url FROM networks WHERE chain_id = ?", (chain_id,))
        own_client = http is None
        http = http or httpx.AsyncClient()
        try:
            for target in filter(None, [address, probe.implementation]):
                abi, abi_source = await fetch_verified_abi(chain_id, target, net and net["explorer_url"], http)
                if abi:
                    break
        finally:
            if own_client:
                await http.aclose()

    present = selectors_in_bytecode(code)
    candidates: list[dict] = []
    reads_to_try: dict[str, str] = {}
    if abi:
        funcs = abi_functions(abi)
        for f in funcs:
            if f["mutability"] in ("view", "pure") and not f["inputs"] and len(f["outputs"]) == 1:
                if f["signature"] in KNOWN_READS or f["name"].lower() in {
                        k.split("(")[0].lower() for k in KNOWN_READS}:
                    reads_to_try[f["signature"]] = f["outputs"][0]
            elif f["mutability"] in ("payable", "nonpayable") and MINT_WORDS.search(f["name"]):
                if f["selector"] not in present and code:
                    continue  # ABI lists a function the deployed code does not have
                candidates.append({**f, "source": abi_source, "confidence": "high"})
        confidence = "high"
    else:
        for sel in sorted(present):
            if sel in _MINT_BY_SELECTOR and _MINT_BY_SELECTOR[sel] != SEADROP_MARKER:
                sig = _MINT_BY_SELECTOR[sel]
                _, types = parse_signature(sig)
                candidates.append({"signature": sig, "selector": sel, "name": sig.split("(")[0],
                                   "inputs": [{"name": "", "type": t} for t in types],
                                   "mutability": "unknown", "source": "bytecode scan",
                                   "confidence": "medium"})
        confidence = "medium" if candidates else "low"
        notes.append("No verified source found: functions were recognised from the deployed code "
                     "by their signatures. Paste the ABI for a precise report.")
    for sel in present:
        if sel in _READ_BY_SELECTOR:
            sig = _READ_BY_SELECTOR[sel]
            reads_to_try.setdefault(sig, KNOWN_READS[sig])

    sigs = sorted(reads_to_try)
    raw = await pool.batch(chain_id, [("eth_call", [{"to": address, "data": "0x" + selector(s)}, "latest"])
                                      for s in sigs]) if sigs else []
    reads = {s: _decode(reads_to_try[s], r) for s, r in zip(sigs, raw, strict=True)}
    reads = {s: v for s, v in reads.items() if v is not None}

    standard = await _standard(pool, chain_id, address)
    restrictions = []
    for f in candidates:
        types = [i["type"] for i in f["inputs"]]
        names = " ".join(i.get("name", "") for i in f["inputs"]).lower()
        if "sig" in names or "signature" in f["name"].lower():
            f["needs"] = "signature from the project per wallet (not supported for custom mints)"
        elif "bytes32[]" in f["signature"]:
            f["needs"] = "Merkle proof (allowlist) per wallet"
        elif any(t.startswith("(") for t in types):
            f["needs"] = "a bundled (struct) input — fill it as a JSON list"
        else:
            f["needs"] = ""
    if reads.get("paused()") is True:
        restrictions.append("Contract is paused.")
    for flag in ("saleActive()", "publicSaleActive()", "isPublicSaleActive()", "mintActive()",
                 "saleIsActive()"):
        if reads.get(flag) is False:
            restrictions.append(f"{flag[:-2]} is false: the sale is not open yet.")
    if reads.get("merkleRoot()") and int(reads["merkleRoot()"], 16):
        restrictions.append("A Merkle root is set: some mint functions need an allowlist proof.")
    total, maximum = reads.get("totalSupply()"), reads.get("maxSupply()") or reads.get("MAX_SUPPLY()")
    if total and maximum and int(total) >= int(maximum):
        restrictions.append("Sold out on-chain.")
    if selector(SEADROP_MARKER) in present:
        notes.insert(0, "This is an OpenSea SeaDrop drop: use Collections and the Mint page, which "
                        "handle OpenSea's allowlists and signatures.")
    price = next((reads[p] for p in PRICE_READS if p in reads), None)
    candidates.sort(key=lambda f: (f.get("mutability") != "payable", bool(f["needs"]), f["signature"]))
    report = {
        "chain_id": chain_id, "address": address, "implementation": probe.implementation,
        "code_size": len(code), "standard": standard, "abi_source": abi_source or "none",
        "verified": bool(abi and abi_source != "ABI you pasted"), "confidence": confidence,
        "name": reads.get("name()"), "symbol": reads.get("symbol()"),
        "price_wei": price, "reads": reads, "mint_functions": candidates,
        "restrictions": restrictions, "notes": notes, "analyzed_at": time.time(),
        "abi": abi or None,
    }
    db.execute("INSERT INTO contract_reports(chain_id, address, report, updated_at) VALUES(?, ?, ?, ?) "
               "ON CONFLICT(chain_id, address) DO UPDATE SET report = excluded.report, "
               "updated_at = excluded.updated_at", (chain_id, address, json.dumps(report), time.time()))
    return report


async def _standard(pool: RpcPool, chain_id: int, address: str) -> str:
    calls = [("eth_call", [{"to": address, "data": "0x01ffc9a7" + iface.ljust(64, "0")}, "latest"])
             for iface in ("80ac58cd", "d9b67a26")]
    try:
        r721, r1155 = await pool.batch(chain_id, calls)
    except RpcError:
        return "unknown"
    if isinstance(r721, str) and len(r721) >= 66 and int(r721[2:66], 16) == 1:
        return "erc721"
    if isinstance(r1155, str) and len(r1155) >= 66 and int(r1155[2:66], 16) == 1:
        return "erc1155"
    return "unknown"


def reports(db: Database) -> list[dict]:
    out = []
    for row in db.all("SELECT report FROM contract_reports ORDER BY updated_at DESC LIMIT 50"):
        r = json.loads(row["report"])
        r.pop("abi", None)
        out.append(r)
    return out


def encode_call(signature: str, args: list[Any]) -> bytes:
    _, types = parse_signature(signature)
    if len(args) != len(types):
        raise ContractError(f"{signature} takes {len(types)} argument(s), got {len(args)}")
    return keccak(text=canonical(signature))[:4] + encode(types, args)


def coerce(kind: str, value: Any) -> Any:
    """Turn a form value into the Python value eth_abi expects for `kind`."""
    if kind.startswith("(") and not kind.endswith("]"):
        parts, _ = _tuple_parts(kind)
        items = _json_list(value, kind)
        if len(items) != len(parts):
            raise ContractError(f"{kind} needs {len(parts)} values in a list, got {len(items)}")
        return tuple(coerce(p, v) for p, v in zip(parts, items, strict=True))
    if kind.endswith("]"):
        inner = kind[:kind.rindex("[")]
        if inner.startswith("(") or isinstance(value, list) or str(value).strip().startswith("["):
            items = _json_list(value, kind)
        else:
            items = [v for v in str(value).replace(",", " ").split() if v]
        size = kind[kind.rindex("[") + 1:-1]
        if size and len(items) != int(size):
            raise ContractError(f"{kind} needs exactly {size} values")
        return [coerce(inner, v) for v in items]
    text = str(value).strip()
    if kind.startswith(("uint", "int")):
        if not re.fullmatch(r"-?\d+", text):
            raise ContractError(f"{kind} needs a whole number, got {text!r}")
        number = int(text)
        if kind.startswith("uint") and number < 0:
            raise ContractError(f"{kind} cannot be negative")
        return number
    if kind == "address":
        return to_checksum_address(text)
    if kind == "bool":
        if text.lower() not in ("true", "false", "1", "0"):
            raise ContractError("bool must be true or false")
        return text.lower() in ("true", "1")
    if kind == "string":
        return text
    if kind.startswith("bytes"):
        raw = bytes.fromhex(text.removeprefix("0x"))
        size = kind[5:]
        if size and len(raw) != int(size):
            raise ContractError(f"{kind} needs exactly {size} bytes")
        return raw
    raise ContractError(f"unsupported type {kind}")


def _json_list(value: Any, kind: str) -> list:
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        parsed = json.loads(str(value))
    except ValueError:
        raise ContractError(f"{kind} needs a JSON list like [1, \"0x...\"]") from None
    if not isinstance(parsed, list):
        raise ContractError(f"{kind} needs a JSON list")
    return parsed


def fill_placeholders(value: Any, mapping: dict[str, Any]) -> Any:
    """Replace "{name}" strings anywhere inside a (JSON) value."""
    if isinstance(value, str) and value.strip() in mapping:
        return mapping[value.strip()]
    if isinstance(value, list):
        return [fill_placeholders(v, mapping) for v in value]
    return value
