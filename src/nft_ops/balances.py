"""Native, ERC-20 and NFT (ERC-721 balanceOf) balance scanning through the RPC pool."""

from __future__ import annotations

import time
from decimal import Decimal

from eth_abi import decode, encode
from eth_utils.address import to_checksum_address

from .db import Database
from .networks import RpcError, RpcPool

BALANCE_OF = bytes.fromhex("70a08231")
SYMBOL = bytes.fromhex("95d89b41")
DECIMALS = bytes.fromhex("313ce567")


def format_units(amount: int | str | None, decimals: int = 18, places: int = 6) -> str:
    if amount is None:
        return "—"
    value = Decimal(int(amount)) / (Decimal(10) ** decimals)
    text = f"{value:.{places}f}".rstrip("0").rstrip(".")
    return text or "0"


def _balance_of_call(token: str, owner: str) -> tuple[str, list]:
    data = BALANCE_OF + encode(["address"], [to_checksum_address(owner)])
    return "eth_call", [{"to": to_checksum_address(token), "data": "0x" + data.hex()}, "latest"]


def _decode_uint(value: object) -> int | None:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) < 66:
        return None
    return int(value[2:66], 16)


async def scan(db: Database, pool: RpcPool, chain_id: int, addresses: list[str],
               nft_contracts: list[str] | None = None) -> dict:
    """Refresh native + token + NFT balances for the given wallets on one chain."""
    tokens = db.all("SELECT address FROM tokens WHERE chain_id = ?", (chain_id,))
    assets: list[tuple[str, str, tuple[str, list]]] = []
    for owner in addresses:
        assets.append((owner, "native", ("eth_getBalance", [owner, "latest"])))
        for token in tokens:
            assets.append((owner, token["address"], _balance_of_call(token["address"], owner)))
        for nft in nft_contracts or []:
            assets.append((owner, f"nft:{to_checksum_address(nft)}", _balance_of_call(nft, owner)))
    results = await pool.batch(chain_id, [call for _, _, call in assets])
    now = time.time()
    rows, failed = [], 0
    for (owner, asset, _), result in zip(assets, results, strict=True):
        if isinstance(result, RpcError):
            failed += 1
            continue
        amount = int(result, 16) if asset == "native" and isinstance(result, str) \
            else _decode_uint(result)
        if amount is None:
            failed += 1
            continue
        rows.append((chain_id, owner, asset, str(amount), now))
    db.executemany(
        "INSERT INTO balances(chain_id, address, asset, amount, updated_at) VALUES(?, ?, ?, ?, ?) "
        "ON CONFLICT(chain_id, address, asset) DO UPDATE SET amount = excluded.amount, "
        "updated_at = excluded.updated_at",
        rows,
    )
    return {"updated": len(rows), "failed": failed}


async def add_token(db: Database, pool: RpcPool, chain_id: int, address: str) -> dict:
    token = to_checksum_address(address)
    symbol_raw, decimals_raw = await pool.batch(chain_id, [
        ("eth_call", [{"to": token, "data": "0x" + SYMBOL.hex()}, "latest"]),
        ("eth_call", [{"to": token, "data": "0x" + DECIMALS.hex()}, "latest"]),
    ])
    if isinstance(decimals_raw, RpcError) or _decode_uint(decimals_raw) is None:
        raise RpcError("contract did not return decimals(); is it an ERC-20 token?")
    decimals = _decode_uint(decimals_raw)
    if decimals is None or decimals > 36:
        raise RpcError("token decimals look invalid")
    try:
        symbol = decode(["string"], bytes.fromhex(symbol_raw[2:]))[0][:12]
    except Exception:  # noqa: BLE001 - some tokens return bytes32 or nothing
        symbol = "TOKEN"
    db.execute("INSERT OR REPLACE INTO tokens(chain_id, address, symbol, decimals) "
               "VALUES(?, ?, ?, ?)", (chain_id, token, symbol, decimals))
    return {"address": token, "symbol": symbol, "decimals": decimals}


def balance_table(db: Database, chain_id: int) -> dict[str, dict[str, str]]:
    """address -> asset -> amount string."""
    table: dict[str, dict[str, str]] = {}
    for row in db.all("SELECT address, asset, amount FROM balances WHERE chain_id = ?",
                      (chain_id,)):
        table.setdefault(row["address"], {})[row["asset"]] = row["amount"]
    return table
