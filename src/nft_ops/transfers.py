"""Planners: turn a sweep / distribution / NFT-transfer request into exact TxItems."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from eth_abi import encode
from eth_utils.address import to_checksum_address

from . import addressbook, nfts
from .balances import format_units
from .db import Database
from .networks import RpcError, RpcPool
from .safety import to_wei
from .txengine import TxEngineError, TxItem
from .wallets import checksum

ERC20_TRANSFER = bytes.fromhex("a9059cbb")
ERC20_BALANCE_OF = bytes.fromhex("70a08231")
ERC721_SAFE_TRANSFER = bytes.fromhex("42842e0e")    # safeTransferFrom(address,address,uint256)
ERC1155_SAFE_TRANSFER = bytes.fromhex("f242432a")   # safeTransferFrom(address,address,uint256,uint256,bytes)
ERC1155_BATCH_TRANSFER = bytes.fromhex("2eb2c2d6")  # safeBatchTransferFrom(...)


def parse_units(text: str | float, decimals: int, what: str = "amount") -> int:
    try:
        value = Decimal(str(text).strip())
    except InvalidOperation:
        raise TxEngineError(f"{what} is not a number: {text!r}") from None
    if value.is_nan() or value <= 0:
        raise TxEngineError(f"{what} must be above zero")
    scaled = value * (Decimal(10) ** decimals)
    if scaled != scaled.to_integral_value():
        raise TxEngineError(f"{what} {text} has more than {decimals} decimal places")
    return int(scaled)


def _asset(db: Database, chain_id: int, asset: str) -> tuple[str, str, int]:
    """Returns (asset key, symbol, decimals)."""
    if asset == "native":
        net = db.one("SELECT native_symbol FROM networks WHERE chain_id = ?", (chain_id,))
        if net is None:
            raise TxEngineError("unknown network")
        return "native", net["native_symbol"], 18
    token = db.one("SELECT * FROM tokens WHERE chain_id = ? AND address = ?",
                   (chain_id, checksum(asset)))
    if token is None:
        raise TxEngineError("track this token on the Wallets page first (it sets its decimals)")
    return token["address"], token["symbol"], int(token["decimals"])


def _item(db: Database, kind: str, source: str, recipient: str, asset: str, symbol: str,
          decimals: int, units: int) -> TxItem:
    external = not addressbook.is_own_wallet(db, recipient)
    if asset == "native":
        return TxItem(kind, source, recipient, recipient, "native", symbol,
                      format_units(units, 18, 18), value=units, external=external)
    data = ERC20_TRANSFER + encode(["address", "uint256"], [recipient, units])
    return TxItem(kind, source, asset, recipient, asset, symbol, format_units(units, decimals, decimals),
                  data=data, external=external)


async def _token_balances(pool: RpcPool, chain_id: int, token: str, owners: list[str]) -> list[int | None]:
    results = await pool.batch(chain_id, [
        ("eth_call", [{"to": token, "data": "0x" + (ERC20_BALANCE_OF + encode(["address"], [o])).hex()},
                      "latest"]) for o in owners])
    return [None if isinstance(r, RpcError) or not isinstance(r, str) or len(r) < 66
            else int(r[2:66], 16) for r in results]


# ===== sweep ===============================================================================
async def plan_sweep(db: Database, pool: RpcPool, chain_id: int, sources: list[str],
                     destination: str, asset: str, reserve: str = "0",
                     min_balance: str = "0") -> tuple[list[TxItem], list[str]]:
    destination = checksum(destination)
    if not addressbook.is_allowed_destination(db, destination):
        raise TxEngineError("sweep destination must be one of your wallets or in the address book")
    key, symbol, decimals = _asset(db, chain_id, asset)
    sources = [s for s in (checksum(x) for x in sources) if s != destination]
    if not sources:
        raise TxEngineError("select at least one source wallet other than the destination")
    notes = []
    reserve_wei = to_wei(reserve or "0")
    threshold = to_wei(min_balance or "0") if key == "native" else (
        parse_units(min_balance, decimals, "minimum balance") if min_balance not in ("", "0") else 0)
    items: list[TxItem] = []
    if key == "native":
        balances = await pool.batch(chain_id, [("eth_getBalance", [s, "pending"]) for s in sources])
        skipped = 0
        for source, raw in zip(sources, balances, strict=True):
            bal = None if isinstance(raw, RpcError) else int(raw, 16)
            if bal is not None and bal < max(threshold, 1):
                skipped += 1
                continue
            item = TxItem("sweep", source, destination, destination, "native", symbol, "max",
                          sweep_max=True, reserve_wei=reserve_wei,
                          external=not addressbook.is_own_wallet(db, destination))
            items.append(item)
        if skipped:
            notes.append(f"{skipped} wallet(s) skipped: balance below the minimum.")
        notes.append(f"Each wallet keeps {format_units(reserve_wei)} {symbol} plus the gas for "
                     "this transfer; only the rest is swept.")
    else:
        balances = await _token_balances(pool, chain_id, key, sources)
        for source, bal in zip(sources, balances, strict=True):
            if not bal or bal < threshold:
                continue
            items.append(_item(db, "sweep", source, destination, key, symbol, decimals, bal))
        notes.append(f"Sweeping {symbol} needs native coin in each wallet for gas.")
    return items, notes


# ===== distribution ========================================================================
async def plan_distribute(db: Database, pool: RpcPool, chain_id: int, mode: str, asset: str,
                          rows: list[dict], source: str | None = None, total: str = "",
                          each: str = "") -> tuple[list[TxItem], list[str]]:
    key, symbol, decimals = _asset(db, chain_id, asset)
    if not rows:
        raise TxEngineError("add at least one recipient")
    recipients = [checksum(r["address"]) for r in rows]
    items: list[TxItem] = []
    if mode == "pairs":
        for r in rows:
            if not r.get("source") or not r.get("amount"):
                raise TxEngineError("many-to-many rows need source, address and amount")
            items.append(_item(db, "distribute", checksum(r["source"]), checksum(r["address"]),
                               key, symbol, decimals, parse_units(r["amount"], decimals)))
    else:
        if not source:
            raise TxEngineError("choose the wallet that pays")
        source = checksum(source)
        if mode == "equal_each":
            units = [parse_units(each, decimals, "amount per recipient")] * len(recipients)
        elif mode == "equal_split":
            whole = parse_units(total, decimals, "total")
            base, extra = divmod(whole, len(recipients))
            if base == 0:
                raise TxEngineError("total is too small to split between that many recipients")
            units = [base + (1 if i < extra else 0) for i in range(len(recipients))]
        elif mode == "percent":
            whole = parse_units(total, decimals, "total")
            pcts = [Decimal(str(r.get("percent", "0"))) for r in rows]
            if sum(pcts) != 100:
                raise TxEngineError(f"percentages add up to {sum(pcts)}, not 100")
            units = [int(whole * p / 100) for p in pcts]
            units[0] += whole - sum(units)  # rounding remainder, so the total is exact
        elif mode == "custom":
            units = [parse_units(r.get("amount", ""), decimals, f"amount for {r['address']}")
                     for r in rows]
        else:
            raise TxEngineError("unknown distribution mode")
        for recipient, amount in zip(recipients, units, strict=True):
            if amount <= 0:
                raise TxEngineError(f"amount for {recipient} rounds to zero")
            items.append(_item(db, "distribute", source, recipient, key, symbol, decimals, amount))
    notes = []
    if key != "native":
        by_source: dict[str, int] = {}
        for i in items:
            by_source[i.source] = by_source.get(i.source, 0) + int(
                Decimal(i.amount) * (Decimal(10) ** decimals))
        sources = list(by_source)
        for s, bal in zip(sources, await _token_balances(pool, chain_id, key, sources), strict=True):
            if bal is None or bal < by_source[s]:
                for i in items:
                    if i.source == s:
                        i.status = "INSUFFICIENT_TOKENS"
                        i.reason = (f"needs {format_units(by_source[s], decimals)} {symbol}, has "
                                    f"{'?' if bal is None else format_units(bal, decimals)}")
    external = sum(1 for i in items if i.external)
    if external:
        notes.append(f"{external} recipient(s) are not your wallets; their amounts count toward "
                     "the 24h spending limit.")
    return items, notes


# ===== NFT transfers =======================================================================
async def plan_nft(db: Database, pool: RpcPool, chain_id: int, contract: str, standard: str,
                   selection: list[dict], mode: str, destination: str = "",
                   recipients: list[str] | None = None) -> tuple[list[TxItem], list[str]]:
    """selection: [{owner, token_id, amount?, recipient?}]"""
    contract = to_checksum_address(contract)
    if not selection:
        raise TxEngineError("select at least one NFT")
    picks = [{"owner": checksum(s["owner"]), "token_id": int(s["token_id"]),
              "amount": int(s.get("amount") or 1), "recipient": s.get("recipient")} for s in selection]
    already = 0
    if mode == "consolidate":
        destination = checksum(destination)
        if not addressbook.is_allowed_destination(db, destination):
            raise TxEngineError("consolidation destination must be one of your wallets or in the "
                                "address book")
        already = sum(1 for p in picks if p["owner"] == destination)
        picks = [p for p in picks if p["owner"] != destination]
        if not picks:
            raise TxEngineError("every selected NFT is already in the destination wallet")
        for p in picks:
            p["recipient"] = destination
    elif mode == "distribute":
        targets = [checksum(r) for r in (recipients or [])]
        if not targets and not all(p["recipient"] for p in picks):
            raise TxEngineError("add recipient addresses")
        for index, p in enumerate(picks):
            p["recipient"] = checksum(p["recipient"]) if p["recipient"] else targets[index % len(targets)]
    else:
        raise TxEngineError("unknown NFT transfer mode")

    label = db.one("SELECT slug FROM collections WHERE address = ? AND chain_id = ?",
                   (contract, chain_id))
    name = label["slug"] if label else contract[:10]
    items: list[TxItem] = []
    if standard == "erc721":
        owners = await nfts.owners_of(pool, chain_id, contract, [p["token_id"] for p in picks])
        for p, owner in zip(picks, owners, strict=True):
            data = ERC721_SAFE_TRANSFER + encode(["address", "address", "uint256"],
                                                 [p["owner"], p["recipient"], p["token_id"]])
            item = TxItem("nft_transfer", p["owner"], contract, p["recipient"], f"erc721:{contract}",
                          f"{name} #{p['token_id']}", "1", data=data, token_id=str(p["token_id"]),
                          external=not addressbook.is_own_wallet(db, p["recipient"]),
                          verify=("erc721", contract, p["token_id"]))
            if not owner or owner.lower() != p["owner"].lower():
                item.status, item.reason = "NOT_OWNER", f"on-chain owner is {owner or 'unknown'}"
            items.append(item)
    elif standard == "erc1155":
        held = await nfts.balances_1155(pool, chain_id, contract,
                                        [(p["owner"], p["token_id"]) for p in picks])
        groups: dict[tuple[str, str], list[dict]] = {}
        for p, have in zip(picks, held, strict=True):
            p["have"] = have or 0
            groups.setdefault((p["owner"], p["recipient"]), []).append(p)
        for (owner, recipient), group in groups.items():
            ids = [p["token_id"] for p in group]
            amounts = [p["amount"] for p in group]
            if len(group) == 1:
                data = ERC1155_SAFE_TRANSFER + encode(
                    ["address", "address", "uint256", "uint256", "bytes"],
                    [owner, recipient, ids[0], amounts[0], b""])
            else:
                data = ERC1155_BATCH_TRANSFER + encode(
                    ["address", "address", "uint256[]", "uint256[]", "bytes"],
                    [owner, recipient, ids, amounts, b""])
            item = TxItem("nft_transfer", owner, contract, recipient, f"erc1155:{contract}",
                          f"{name} ids {', '.join(map(str, ids))}", str(sum(amounts)), data=data,
                          token_id=",".join(map(str, ids)),
                          external=not addressbook.is_own_wallet(db, recipient),
                          verify=("erc1155", contract, ids[0], amounts[0]) if len(ids) == 1 else None)
            short = [p for p in group if p["have"] < p["amount"]]
            if short:
                item.status, item.reason = "NOT_OWNER", (
                    f"wallet holds {short[0]['have']} of id {short[0]['token_id']}")
            items.append(item)
    else:
        raise TxEngineError("standard must be erc721 or erc1155")
    notes = []
    if already:
        notes.append(f"{already} selected NFT(s) are already in the destination wallet; skipped.")
    if mode == "distribute":
        notes.append(f"{len({p['recipient'] for p in picks})} recipient(s); NFTs are assigned in "
                     "order, round-robin, unless your list gave each NFT its own recipient.")
    return items, notes
