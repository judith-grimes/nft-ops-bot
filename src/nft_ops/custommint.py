"""Custom mints for contracts that are not OpenSea SeaDrop drops.

The user picks the function and fills its arguments with literals or placeholders:
  {quantity}  the quantity per wallet
  {wallet}    the minting wallet's own address
  {proof}     that wallet's Merkle proof (from a pasted JSON or CSV list)
Every wallet becomes one transaction that the transaction engine simulates, limits, confirms
(type MINT) and verifies by the Transfer events in its receipt.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from eth_utils.address import is_address, to_checksum_address

from .contracts import (
    NATIVE_PLACEHOLDER,
    ContractError,
    _json_list,
    coerce,
    encode_call,
    fill_placeholders,
    parse_signature,
)
from .db import Database
from .safety import to_wei
from .txengine import TxItem
from .wallets import checksum

PLACEHOLDERS = ("{quantity}", "{wallet}", "{proof}", "{price_wei}", "{native}")


def parse_proofs(text: str) -> dict[str, list[str]]:
    """JSON {"0xaddr": ["0x..", ...]} or lines 'address,0xproof1 0xproof2 ...'."""
    text = text.strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    out: dict[str, list[str]] = {}
    if isinstance(data, dict):
        items = data.get("proofs", data).items() if isinstance(data.get("proofs", data), dict) else []
        for address, proof in items:
            if is_address(address) and isinstance(proof, list):
                out[address.lower()] = [str(p) for p in proof]
        if not out:
            raise ContractError("proof JSON must map addresses to lists of 0x… hashes")
        return out
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        address, _, rest = line.replace("\t", ",").partition(",")
        if not is_address(address.strip()):
            raise ContractError(f"proof line {number}: not an address")
        out[address.strip().lower()] = [p for p in rest.replace(",", " ").split() if p]
    return out


def plan(db: Database, chain_id: int, contract: str, signature: str, arg_templates: list[str],
         quantity: int, price_per_unit: str, wallets: list[str], proofs_text: str = "",
         start_at: float | None = None, gas_limit: int = 0, resimulate: bool = True,
         label: str = "", wait_for_open_minutes: float = 0) -> tuple[list[TxItem], list[str]]:
    contract = to_checksum_address(contract)
    _, types = parse_signature(signature)
    if len(arg_templates) != len(types):
        raise ContractError(f"{signature} takes {len(types)} argument(s)")
    if not 1 <= quantity <= 1000:
        raise ContractError("quantity must be 1-1000")
    try:
        unit = to_wei(price_per_unit or "0")
    except ValueError:
        raise ContractError("price per NFT must be a number (0 for free mints)") from None
    if start_at and wait_for_open_minutes:
        raise ContractError("choose either a start time or 'send when the sale opens', not both")
    if wait_for_open_minutes and not 1 <= wait_for_open_minutes <= 24 * 60:
        raise ContractError("maximum wait must be 1 minute to 24 hours")
    if wait_for_open_minutes and not gas_limit:
        raise ContractError("'send when the sale opens' needs a gas limit, because the sale is "
                            "not open yet for the test run")
    if start_at and not gas_limit:
        raise ContractError("a scheduled mint needs a gas limit, because the sale may not be open "
                            "yet for the test run")
    if gas_limit and not 21_000 <= gas_limit <= 30_000_000:
        raise ContractError("gas limit must be 21000-30000000")
    proofs = parse_proofs(proofs_text)
    if any("{proof}" in t for t in arg_templates) and not proofs:
        raise ContractError("this function uses {proof}: paste the allowlist proofs")
    try:
        Decimal(str(quantity))
    except InvalidOperation:
        raise ContractError("bad quantity") from None

    items: list[TxItem] = []
    missing = 0
    for wallet in (checksum(w) for w in wallets):
        args, status, reason = [], "READY", ""
        proof = proofs.get(wallet.lower())
        uses_proof = any("{proof}" in str(t) for t in arg_templates)
        if uses_proof and proof is None:
            status, reason = "NO_PROOF", "no allowlist proof for this wallet"
            missing += 1
        mapping = {"{quantity}": str(quantity), "{wallet}": wallet, "{proof}": proof or [],
                   "{price_wei}": str(unit), "{native}": NATIVE_PLACEHOLDER}
        for kind, template in zip(types, arg_templates, strict=True):
            template = str(template).strip()
            if kind.startswith("(") or (kind.endswith("]") and template.startswith("[")):
                value = fill_placeholders(_json_list(template, kind), mapping)
            else:
                value = mapping.get(template, template)
            args.append(coerce(kind, value))
        data = encode_call(signature, args) if status == "READY" else b""
        item = TxItem("custom_mint", wallet, contract, wallet, f"erc721:{contract}",
                      f"{label or contract[:10]} ×{quantity}", str(quantity), value=unit * quantity,
                      data=data, external=True, verify=("minted", contract, quantity),
                      scheduled_at=start_at, resimulate=resimulate, fixed_gas=gas_limit,
                      wait_for_open=wait_for_open_minutes * 60)
        item.status, item.reason = status, reason
        items.append(item)
    notes = [f"Each wallet calls {signature} once, paying {price_per_unit or '0'} × {quantity}."]
    if missing:
        notes.append(f"{missing} wallet(s) have no proof in your list and will be skipped.")
    if wait_for_open_minutes:
        notes.append(f"Waits up to {wait_for_open_minutes:g} min: one wallet's mint is test-run on every "
                     "new block, and all wallets send the moment it would succeed."
                     + ("" if resimulate else " FAST MODE: no per-wallet re-check before sending."))
    if start_at:
        notes.append("Scheduled: wallets wait, refresh their nonce 3 s before, and send at the "
                     "start time." + ("" if resimulate else " FAST MODE is on: no last-second "
                                      "test run, so a reverting mint still costs gas."))
    return items, notes
