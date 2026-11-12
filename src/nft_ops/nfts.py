"""NFT standard detection, holdings discovery and on-chain ownership checks."""

from __future__ import annotations

import time

from eth_abi import decode, encode
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from .db import Database
from .networks import RpcError, RpcPool

SUPPORTS_INTERFACE = bytes.fromhex("01ffc9a7")
IFACE_ERC721 = bytes.fromhex("80ac58cd")
IFACE_ERC1155 = bytes.fromhex("d9b67a26")
OWNER_OF = bytes.fromhex("6352211e")
BALANCE_OF_721 = bytes.fromhex("70a08231")
TOKEN_OF_OWNER_BY_INDEX = bytes.fromhex("2f745c59")
BALANCE_OF_1155 = bytes.fromhex("00fdd58e")
TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
TRANSFER_SINGLE = "0x" + keccak(text="TransferSingle(address,address,address,uint256,uint256)").hex()
TRANSFER_BATCH = "0x" + keccak(text="TransferBatch(address,address,address,uint256[],uint256[])").hex()
MAX_ENUMERATE = 500
MAX_LOG_REQUESTS = 300


class NftError(Exception):
    pass


def _call(contract: str, data: bytes) -> tuple[str, list]:
    return "eth_call", [{"to": contract, "data": "0x" + data.hex()}, "latest"]


def _word(value: object) -> int | None:
    if isinstance(value, str) and value.startswith("0x") and len(value) >= 66:
        return int(value[2:66], 16)
    return None


def _topic(address: str) -> str:
    return "0x" + "00" * 12 + address[2:].lower()


async def detect_standard(pool: RpcPool, chain_id: int, contract: str) -> str:
    contract = to_checksum_address(contract)
    r721, r1155 = await pool.batch(chain_id, [
        _call(contract, SUPPORTS_INTERFACE + IFACE_ERC721.ljust(32, b"\0")),
        _call(contract, SUPPORTS_INTERFACE + IFACE_ERC1155.ljust(32, b"\0")),
    ])
    if _word(r721) == 1:
        return "erc721"
    if _word(r1155) == 1:
        return "erc1155"
    raise NftError("contract does not report ERC-721 or ERC-1155 support")


async def owners_of(pool: RpcPool, chain_id: int, contract: str, ids: list[int]) -> list[str | None]:
    results = await pool.batch(chain_id, [_call(contract, OWNER_OF + encode(["uint256"], [i]))
                                          for i in ids])
    out = []
    for r in results:
        word = _word(r)
        out.append(None if word is None else to_checksum_address(word.to_bytes(32, "big")[12:]))
    return out


async def balances_1155(pool: RpcPool, chain_id: int, contract: str,
                        pairs: list[tuple[str, int]]) -> list[int | None]:
    results = await pool.batch(chain_id, [
        _call(contract, BALANCE_OF_1155 + encode(["address", "uint256"], [o, i])) for o, i in pairs])
    return [_word(r) for r in results]


def _store(db: Database, chain_id: int, contract: str, standard: str,
           rows: list[tuple[str, int, int, str]]) -> None:
    """rows: (owner, token_id, amount, source)."""
    now = time.time()
    db.executemany(
        "INSERT INTO nft_tokens(chain_id, contract, token_id, owner, standard, amount, source, "
        "updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(chain_id, contract, token_id, owner) "
        "DO UPDATE SET amount = excluded.amount, updated_at = excluded.updated_at",
        [(chain_id, contract, str(tid), owner, standard, str(amount), source, now)
         for owner, tid, amount, source in rows])


def record_mint(db: Database, chain_id: int, contract: str, owner: str, ids: list[int]) -> None:
    _store(db, chain_id, to_checksum_address(contract), "erc721",
           [(to_checksum_address(owner), i, 1, "mint") for i in ids])


async def _logs(pool: RpcPool, chain_id: int, filter_: dict, lo: int, hi: int,
                budget: list[int]) -> list[dict]:
    if budget[0] <= 0:
        raise NftError("transfer-history scan needed too many requests; use a smaller range")
    budget[0] -= 1
    try:
        result = await pool.call(chain_id, "eth_getLogs",
                                 [{**filter_, "fromBlock": hex(lo), "toBlock": hex(hi)}])
        return result or []
    except RpcError:
        if hi - lo < 2_000:
            raise
        mid = (lo + hi) // 2
        return (await _logs(pool, chain_id, filter_, lo, mid, budget)
                + await _logs(pool, chain_id, filter_, mid + 1, hi, budget))


async def discover(db: Database, pool: RpcPool, chain_id: int, contract: str, standard: str,
                   owners: list[str], lookback_blocks: int = 0) -> dict:
    """Find NFTs held by `owners`, verify each on-chain, and store the current holdings."""
    contract = to_checksum_address(contract)
    owners = [to_checksum_address(o) for o in owners]
    owner_set = {o.lower() for o in owners}
    candidates: set[int] = {int(r["token_id"]) for r in db.all(
        "SELECT token_id FROM nft_tokens WHERE chain_id = ? AND contract = ?", (chain_id, contract))}
    notes: list[str] = []

    if standard == "erc721":
        balances = await pool.batch(chain_id, [_call(contract, BALANCE_OF_721 + encode(["address"], [o]))
                                               for o in owners])
        enum_calls, enum_ok = [], True
        for owner, bal in zip(owners, balances, strict=True):
            for index in range(min(_word(bal) or 0, MAX_ENUMERATE)):
                enum_calls.append(_call(contract, TOKEN_OF_OWNER_BY_INDEX
                                        + encode(["address", "uint256"], [owner, index])))
        if enum_calls:
            results = await pool.batch(chain_id, enum_calls)
            found = [_word(r) for r in results]
            if all(f is None for f in found):
                enum_ok = False
            candidates.update(f for f in found if f is not None)
        if not enum_ok:
            notes.append("This contract does not list tokens per owner; use the transfer-history "
                         "scan or add token IDs by hand.")

    if lookback_blocks > 0:
        latest = int(await pool.call(chain_id, "eth_blockNumber", []), 16)
        lo = max(0, latest - lookback_blocks)
        budget = [MAX_LOG_REQUESTS]
        for start in range(0, len(owners), 50):
            topics_to = [_topic(o) for o in owners[start:start + 50]]
            if standard == "erc721":
                logs = await _logs(pool, chain_id, {"address": contract,
                                                    "topics": [TRANSFER, None, topics_to]},
                                   lo, latest, budget)
                candidates.update(int(log["topics"][3], 16) for log in logs if len(log["topics"]) == 4)
            else:
                for topic0 in (TRANSFER_SINGLE, TRANSFER_BATCH):
                    logs = await _logs(pool, chain_id, {"address": contract,
                                                        "topics": [topic0, None, None, topics_to]},
                                       lo, latest, budget)
                    for log in logs:
                        data = bytes.fromhex(log["data"][2:])
                        if topic0 == TRANSFER_SINGLE:
                            candidates.add(decode(["uint256", "uint256"], data)[0])
                        else:
                            candidates.update(decode(["uint256[]", "uint256[]"], data)[0])

    ids = sorted(candidates)
    rows: list[tuple[str, int, int, str]] = []
    if standard == "erc721" and ids:
        for owner, token_id in zip(await owners_of(pool, chain_id, contract, ids), ids, strict=True):
            if owner and owner.lower() in owner_set:
                rows.append((owner, token_id, 1, "verified"))
    elif standard == "erc1155" and ids:
        pairs = [(o, i) for o in owners for i in ids]
        for (owner, token_id), amount in zip(pairs, await balances_1155(pool, chain_id, contract, pairs),
                                              strict=True):
            if amount:
                rows.append((owner, token_id, amount, "verified"))
    # Replace this collection's holdings for these owners with the verified view.
    marks = ",".join("?" for _ in owners)
    if owners:
        db.execute(f"DELETE FROM nft_tokens WHERE chain_id = ? AND contract = ? AND owner IN ({marks})",
                   (chain_id, contract, *owners))
    _store(db, chain_id, contract, standard, rows)
    return {"found": len(rows), "checked_ids": len(ids), "notes": notes}


async def add_manual(db: Database, pool: RpcPool, chain_id: int, contract: str, standard: str,
                     ids: list[int], wallets: set[str]) -> dict:
    contract = to_checksum_address(contract)
    rows, missing = [], []
    if standard == "erc721":
        for token_id, owner in zip(ids, await owners_of(pool, chain_id, contract, ids), strict=True):
            if owner and owner.lower() in wallets:
                rows.append((owner, token_id, 1, "manual"))
            else:
                missing.append(str(token_id))
    else:
        pairs = [(to_checksum_address(w), i) for w in wallets for i in ids]
        for (owner, token_id), amount in zip(pairs, await balances_1155(pool, chain_id, contract, pairs),
                                              strict=True):
            if amount:
                rows.append((owner, token_id, amount, "manual"))
    _store(db, chain_id, contract, standard, rows)
    return {"added": len(rows), "not_yours": missing}


def holdings(db: Database, chain_id: int, contract: str) -> list[dict]:
    return db.all(
        "SELECT n.*, w.label FROM nft_tokens n LEFT JOIN wallets w ON w.address = n.owner "
        "WHERE n.chain_id = ? AND n.contract = ? ORDER BY n.owner, CAST(n.token_id AS INTEGER)",
        (chain_id, to_checksum_address(contract)))


async def verify_owner(pool: RpcPool, chain_id: int, verify: tuple, recipient: str) -> bool:
    kind, contract, token_id = verify[0], verify[1], int(verify[2])
    if kind == "erc721":
        owner = (await owners_of(pool, chain_id, contract, [token_id]))[0]
        return bool(owner) and owner.lower() == recipient.lower()
    amount = (await balances_1155(pool, chain_id, contract, [(recipient, token_id)]))[0]
    return bool(amount) and amount >= int(verify[3])


def record_owner(db: Database, chain_id: int, verify: tuple, source: str, recipient: str) -> None:
    kind, contract, token_id = verify[0], to_checksum_address(verify[1]), str(int(verify[2]))
    if kind == "erc721":
        db.execute("DELETE FROM nft_tokens WHERE chain_id = ? AND contract = ? AND token_id = ?",
                   (chain_id, contract, token_id))
        if db.one("SELECT 1 FROM wallets WHERE address = ?", (to_checksum_address(recipient),)):
            _store(db, chain_id, contract, "erc721", [(to_checksum_address(recipient), int(token_id), 1, "transfer")])
        return
    amount = int(verify[3])
    row = db.one("SELECT amount FROM nft_tokens WHERE chain_id = ? AND contract = ? AND token_id = ? "
                 "AND owner = ?", (chain_id, contract, token_id, to_checksum_address(source)))
    if row:
        left = int(row["amount"]) - amount
        if left > 0:
            db.execute("UPDATE nft_tokens SET amount = ? WHERE chain_id = ? AND contract = ? AND "
                       "token_id = ? AND owner = ?", (str(left), chain_id, contract, token_id,
                                                      to_checksum_address(source)))
        else:
            db.execute("DELETE FROM nft_tokens WHERE chain_id = ? AND contract = ? AND token_id = ? "
                       "AND owner = ?", (chain_id, contract, token_id, to_checksum_address(source)))
