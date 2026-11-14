"""Scatter (scatter.art) drops: Archetype contracts (github.com/guminc/archetype).

Everything is read from the contract itself — no Scatter website or private API is used:

* phases ("invites") come from the ``Invited(bytes32 key, bytes32 cid)`` events plus
  ``invites(key)``; public keys 0-3 are also probed directly;
* a key ``<= 0xff`` (or ``keccak256(tokenAddress)``) is a **public** phase; otherwise the key is
  the **Merkle root** of the allowlist;
* the exact cost comes from ``computeMintPayment(key, qty, false)`` (newer versions) or
  ``computePrice(key, qty, false)`` (older ones), protocol fee included;
* allowlist proofs are built locally from an allowlist you paste (several tree layouts are
  tried and the one whose root equals the on-chain key is used) or taken from proofs you paste,
  and every proof is verified against the on-chain root before use.

Phases paid in an ERC-20 token are shown but not minted (they need a token approval first).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from eth_abi import decode, encode
from eth_utils.address import is_address, to_checksum_address
from eth_utils.crypto import keccak

from .db import Database
from .networks import RpcError, RpcPool
from .txengine import TxItem

ZERO = "0x" + "00" * 20
SIG_MINT_V2 = "mint((bytes32,bytes32[]),uint256,address,bytes,(address,uint128,uint256,uint256))"
SIG_MINT_V1 = "mint((bytes32,bytes32[]),uint256,address,bytes)"
SIG_INVITES = "invites(bytes32)"
SIG_MINTED = "minted(address,bytes32)"
SIG_PAYMENT = "computeMintPayment(bytes32,uint256,bool)"
SIG_PRICE = "computePrice(bytes32,uint256,bool)"
SIG_TOTAL = "totalSupply()"
SIG_NAME = "name()"
INVITED_TOPIC = "0x" + keccak(text="Invited(bytes32,bytes32)").hex()
INVITE_TYPES = ["uint128", "uint32", "uint32", "uint32", "uint32", "uint32", "address", "bool"]
DEFAULT_GAS = 300_000
EIP1167 = re.compile(r"^0x363d3d373d3d3d363d73([0-9a-f]{40})5af43d82803e903d91602b57fd5bf3$")


class ScatterError(Exception):
    pass


def sel(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def _call(contract: str, data: bytes) -> tuple[str, list]:
    return ("eth_call", [{"to": contract, "data": "0x" + data.hex()}, "latest"])


def _hex(result: Any) -> bytes | None:
    if isinstance(result, RpcError) or not isinstance(result, str) or not result.startswith("0x"):
        return None
    raw = bytes.fromhex(result[2:])
    return raw or None


async def _code(pool: RpcPool, chain_id: int, address: str) -> bytes:
    code = (await pool.call(chain_id, "eth_getCode", [address, "latest"]) or "0x").lower()
    m = EIP1167.match(code)
    if m:                                               # Scatter collections are minimal-proxy clones
        code = (await pool.call(chain_id, "eth_getCode", ["0x" + m.group(1), "latest"]) or "0x").lower()
    return bytes.fromhex(code[2:]) if code.startswith("0x") else b""


def _has(code: bytes, signature: str) -> bool:
    return b"\x63" + sel(signature) in code


def is_public_key(key: bytes, token: str) -> bool:
    return int.from_bytes(key, "big") <= 0xFF or key == keccak(bytes.fromhex(token[2:].rjust(40, "0")))


# ===== Merkle (Solady MerkleProofLib: sorted pairs) =======================================
def leaf(address: str) -> bytes:
    return keccak(bytes.fromhex(address[2:].lower()))


def _pair(a: bytes, b: bytes) -> bytes:
    return keccak(a + b) if a <= b else keccak(b + a)


def verify(proof: list[bytes], root: bytes, address: str) -> bool:
    h = leaf(address)
    for p in proof:
        h = _pair(h, p)
    return h == root


def _tree(leaves: list[bytes], duplicate_odd: bool) -> list[list[bytes]]:
    layers = [leaves]
    while len(layers[-1]) > 1:
        cur, nxt = layers[-1], []
        for i in range(0, len(cur), 2):
            if i + 1 < len(cur):
                nxt.append(_pair(cur[i], cur[i + 1]))
            else:
                nxt.append(_pair(cur[i], cur[i]) if duplicate_odd else cur[i])
        layers.append(nxt)
    return layers


def _proof(layers: list[list[bytes]], index: int) -> list[bytes]:
    out = []
    for layer in layers[:-1]:
        sib = index ^ 1
        if sib < len(layer):
            out.append(layer[sib])
        index //= 2
    return out


def proofs_from_list(addresses: list[str], root: bytes, wanted: list[str]) -> dict[str, list[str]] | None:
    """Rebuild the allowlist tree; return proofs for ``wanted`` if a layout matches ``root``."""
    uniq = list(dict.fromkeys(a.lower() for a in addresses))
    leaf_sets = [[leaf(a) for a in uniq]]
    leaf_sets.append(sorted(leaf_sets[0]))                       # merkletreejs sortLeaves
    leaf_sets.append([leaf(a) for a in sorted(uniq)])            # addresses sorted first
    for leaves in leaf_sets:
        for dup in (False, True):
            layers = _tree(leaves, dup)
            if layers[-1] and layers[-1][0] == root:
                index = {lf: i for i, lf in enumerate(leaves)}
                out = {}
                for w in wanted:
                    i = index.get(leaf(w))
                    if i is not None:
                        out[w.lower()] = ["0x" + p.hex() for p in _proof(layers, i)]
                return out
    return None


def parse_addresses(text: str) -> list[str]:
    return list(dict.fromkeys(m.lower() for m in re.findall(r"0x[0-9a-fA-F]{40}", text)))


# ===== reading the drop ===================================================================
async def inspect(db: Database, pool: RpcPool, chain_id: int, address: str, lookback: int = 3_000_000) -> dict:
    if not is_address(address):
        raise ScatterError("enter a 0x… contract address")
    contract = to_checksum_address(address)
    code = await _code(pool, chain_id, contract)
    if not code:
        raise ScatterError("no contract at this address on the selected network")
    v2, v1 = _has(code, SIG_MINT_V2), _has(code, SIG_MINT_V1)
    if not (_has(code, SIG_INVITES) and (v2 or v1)):
        return {"is_scatter": False}
    keys: set[bytes] = {i.to_bytes(32, "big") for i in range(4)}
    events_ok = True
    try:
        latest = int(await pool.call(chain_id, "eth_blockNumber", []), 16)
        logs = await _logs(pool, chain_id, contract, max(0, latest - lookback), latest)
        keys |= {bytes.fromhex(lg["topics"][1][2:]) for lg in logs if len(lg.get("topics", [])) > 1}
    except (RpcError, ScatterError):
        events_ok = False
    keys |= {bytes.fromhex(r["key"][2:]) for r in db.all(
        "SELECT DISTINCT key FROM scatter_proofs WHERE chain_id = ? AND contract = ?", (chain_id, contract))}
    keys_list = sorted(keys)
    res = await pool.batch(chain_id, [_call(contract, sel(SIG_INVITES) + encode(["bytes32"], [k])) for k in keys_list]
                           + [_call(contract, sel(SIG_TOTAL)), _call(contract, sel(SIG_NAME))])
    total = _hex(res[-2])
    name_raw = _hex(res[-1])
    phases = []
    now = time.time()
    for k, r in zip(keys_list, res[:-2], strict=True):
        raw = _hex(r)
        if not raw or len(raw) < 32 * 8:
            continue
        price, start, end, limit, max_supply, unit, token, blacklist = decode(INVITE_TYPES, raw[:256])
        if not (price or start or end or limit or max_supply):
            continue                                       # empty slot: no phase under this key
        public = is_public_key(k, token)
        loaded = db.one("SELECT COUNT(*) AS n FROM scatter_proofs WHERE chain_id = ? AND contract = ? AND key = ?",
                        (chain_id, contract, "0x" + k.hex()))["n"]
        state = ("ended" if end and now >= end else "upcoming" if start and now < start else "active")
        phases.append({
            "key": "0x" + k.hex(), "public": public, "price_wei": str(price), "start": start or None,
            "end": end or None, "limit": limit, "max_supply": max_supply, "unit_size": unit or 1,
            "currency": token, "native": int(token, 16) == 0, "blacklist": blacklist, "state": state,
            "name": "Public" if public else "Allowlist", "proofs_loaded": loaded})
    phases.sort(key=lambda p: (p["start"] or 0, not p["public"]))
    for i, p in enumerate(phases):
        if not p["public"]:
            p["name"] = f"Allowlist {i + 1}" if sum(1 for x in phases if not x["public"]) > 1 else "Allowlist"
    name = None
    if name_raw:
        try:
            name = decode(["string"], name_raw)[0]
        except Exception:  # noqa: BLE001 - cosmetic
            name = None
    return {"is_scatter": True, "version": "v2" if v2 else "v1", "contract": contract, "name": name,
            "total_supply": int.from_bytes(total[:32], "big") if total else None,
            "phases": phases, "events_ok": events_ok}


async def _logs(pool: RpcPool, chain_id: int, contract: str, lo: int, hi: int, budget: list[int] | None = None) -> list:
    budget = budget if budget is not None else [40]
    if budget[0] <= 0:
        raise ScatterError("phase history too long to read with this RPC")
    budget[0] -= 1
    try:
        return await pool.call(chain_id, "eth_getLogs", [{"address": contract, "topics": [INVITED_TOPIC],
                                                          "fromBlock": hex(lo), "toBlock": hex(hi)}]) or []
    except RpcError:
        if hi - lo < 5_000:
            raise
        mid = (lo + hi) // 2
        return await _logs(pool, chain_id, contract, lo, mid, budget) + await _logs(pool, chain_id, contract, mid + 1, hi, budget)


# ===== allowlist proofs ===================================================================
def load_allowlist(db: Database, chain_id: int, contract: str, key: str, text: str, wallets: list[str]) -> dict:
    """Store verified proofs for your wallets, from a full allowlist or from pasted proofs."""
    from .custommint import parse_proofs

    contract = to_checksum_address(contract)
    root = bytes.fromhex(key[2:])
    stripped = text.strip()
    proofs: dict[str, list[str]] = {}
    looks_like_proofs = stripped.startswith("{") or any(
        len(re.findall(r"0x[0-9a-fA-F]{64}", line)) for line in stripped.splitlines())
    if looks_like_proofs:
        for addr, proof in parse_proofs(stripped).items():
            if verify([bytes.fromhex(p[2:]) for p in proof], root, addr):
                proofs[addr] = proof
        if not proofs:
            raise ScatterError("none of the pasted proofs match this phase's on-chain allowlist")
        source = "pasted proofs"
    else:
        listed = parse_addresses(stripped)
        if not listed:
            raise ScatterError("paste the allowlist addresses (or proofs) for this phase")
        built = proofs_from_list(listed, root, wallets)
        if built is None:
            raise ScatterError(f"these {len(listed)} addresses do not produce this phase's on-chain "
                               "allowlist root — the list is incomplete or for another phase")
        proofs = built
        source = f"allowlist of {len(listed)} addresses"
    now = time.time()
    db.executemany("INSERT OR REPLACE INTO scatter_proofs VALUES(?, ?, ?, ?, ?, ?)",
                   [(chain_id, contract, key.lower(), to_checksum_address(a), json.dumps(p), now)
                    for a, p in proofs.items()])
    mine = {w.lower() for w in wallets}
    return {"verified": len(proofs), "yours": sum(1 for a in proofs if a in mine), "source": source}


# ===== planning the mint ==================================================================
async def plan(db: Database, pool: RpcPool, chain_id: int, contract: str, key: str, quantity: int,
               wallets: list[str], schedule: bool, gas_limit: int = 0) -> tuple[list[TxItem], list[str], dict]:
    if not 1 <= quantity <= 1000:
        raise ScatterError("quantity must be 1-1000")
    info = await inspect(db, pool, chain_id, contract)
    if not info.get("is_scatter"):
        raise ScatterError("this is not a Scatter (Archetype) drop")
    phase = next((p for p in info["phases"] if p["key"] == key.lower()), None)
    if phase is None:
        raise ScatterError("that phase was not found on the contract")
    if not phase["native"]:
        raise ScatterError("this phase is paid in an ERC-20 token, which is not supported yet")
    if phase["state"] == "ended":
        raise ScatterError("this phase has ended")
    contract = info["contract"]
    k = bytes.fromhex(key[2:])
    calls = [_call(contract, sel(SIG_PAYMENT) + encode(["bytes32", "uint256", "bool"], [k, quantity, False])),
             _call(contract, sel(SIG_PRICE) + encode(["bytes32", "uint256", "bool"], [k, quantity, False]))]
    calls += [_call(contract, sel(SIG_MINTED) + encode(["address", "bytes32"], [w, k])) for w in wallets]
    res = await pool.batch(chain_id, calls)
    pay, price = _hex(res[0]), _hex(res[1])
    total_mints = quantity * phase["unit_size"]
    if pay and len(pay) >= 128:
        _cost, value, _cur, total_mints = decode(["uint256", "uint256", "address", "uint256"], pay[:128])
    elif price:
        value = int.from_bytes(price[:32], "big")
    else:
        raise ScatterError("the contract did not return a price for this phase")
    proofs = {r["address"].lower(): json.loads(r["proof"]) for r in db.all(
        "SELECT address, proof FROM scatter_proofs WHERE chain_id = ? AND contract = ? AND key = ?",
        (chain_id, contract, key.lower()))}
    future = bool(phase["start"] and phase["start"] > time.time())
    if future and not schedule:
        raise ScatterError("this phase has not opened yet — choose 'Send at the start time'")
    items, missing = [], 0
    for wallet, minted_raw in zip(wallets, res[2:], strict=True):
        w = to_checksum_address(wallet)
        status, reason = "READY", ""
        proof = [] if phase["public"] else proofs.get(w.lower())
        if proof is None:
            status, reason, missing = "NO_PROOF", "not on this phase's allowlist (no verified proof)", missing + 1
        minted = _hex(minted_raw)
        already = int.from_bytes(minted[:32], "big") if minted else 0
        if status == "READY" and phase["limit"] and already + total_mints > phase["limit"]:
            status, reason = "ALLOCATION_EXCEEDED", f"limit {phase['limit']} per wallet, already minted {already}"
        args = [(k, [bytes.fromhex(p[2:]) for p in (proof or [])]), quantity, ZERO, b""]
        if info["version"] == "v2":
            args.append((ZERO, 0, value, 0))
            data = sel(SIG_MINT_V2) + encode(["(bytes32,bytes32[])", "uint256", "address", "bytes",
                                              "(address,uint128,uint256,uint256)"], args)
        else:
            data = sel(SIG_MINT_V1) + encode(["(bytes32,bytes32[])", "uint256", "address", "bytes"], args)
        item = TxItem("custom_mint", w, contract, w, f"erc721:{contract}",
                      f"{info.get('name') or 'Scatter'} {phase['name']} ×{total_mints}", str(total_mints),
                      value=value, data=data if status == "READY" else b"", external=True,
                      verify=("minted", contract, total_mints),
                      scheduled_at=float(phase["start"]) if future else None,
                      fixed_gas=gas_limit or (DEFAULT_GAS if future else 0))
        item.status, item.reason = status, reason
        items.append(item)
    notes = [(f"Scatter {phase['name']}: each wallet calls mint() and pays {value / 1e18:g} "
              f"(price + Scatter fee) for {total_mints} NFT(s).")]
    if missing:
        notes.append(f"{missing} wallet(s) have no verified allowlist proof and are skipped.")
    if future:
        notes.append("Scheduled: wallets wait and send at the phase start. Keep NFT Ops running.")
    return items, notes, {"phase": phase, "value": str(value), "total_mints": total_mints}
