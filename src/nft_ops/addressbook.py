"""Destination allowlist (address book) and saved recipient lists."""

from __future__ import annotations

import csv
import io
import json
import time
from decimal import Decimal, InvalidOperation

from .db import Database
from .wallets import WalletError, _clean_label, checksum, looks_secret

MAX_LIST_ROWS = 2000


def add(db: Database, address: str, label: str = "") -> str:
    address = checksum(address)
    db.execute("INSERT INTO address_book(address, label, created_at) VALUES(?, ?, ?) "
               "ON CONFLICT(address) DO UPDATE SET label = excluded.label",
               (address, _clean_label(label), time.time()))
    return address


def remove(db: Database, address: str) -> None:
    db.execute("DELETE FROM address_book WHERE address = ?", (checksum(address),))


def entries(db: Database) -> list[dict]:
    return db.all("SELECT * FROM address_book ORDER BY label, address")


def is_own_wallet(db: Database, address: str) -> bool:
    return db.one("SELECT 1 FROM wallets WHERE address = ?", (checksum(address),)) is not None


def is_allowed_destination(db: Database, address: str) -> bool:
    """Your own registered wallets and address-book entries are allowed destinations."""
    address = checksum(address)
    return is_own_wallet(db, address) or db.one(
        "SELECT 1 FROM address_book WHERE address = ?", (address,)) is not None


def _decimal(text: str, field: str, row: int) -> str:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise WalletError(f"row {row}: {field} is not a number") from None
    if value.is_nan() or value <= 0:
        raise WalletError(f"row {row}: {field} must be above zero")
    return str(value)


def parse_recipients(text: str) -> dict:
    """CSV with columns address and optionally amount, percent, label, source.

    Also accepts plain lines 'address' or 'address,amount' without a header.
    """
    if looks_secret(text):
        return {"ok": False, "rows": [], "problems": [
            "this looks like it contains a private key or seed phrase — refused"]}
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return {"ok": False, "rows": [], "problems": ["no recipients"]}
    has_header = "address" in lines[0].lower()
    reader = csv.reader(io.StringIO("\n".join(lines)))
    header = [h.strip().lower() for h in next(reader)] if has_header else ["address", "amount"]
    rows, problems, seen = [], [], set()
    for number, cells in enumerate(reader, 2 if has_header else 1):
        record = dict(zip(header, (c.strip() for c in cells), strict=False))
        try:
            entry = {"address": checksum(record.get("address", ""))}
            if record.get("source"):
                entry["source"] = checksum(record["source"])
            if record.get("amount"):
                entry["amount"] = _decimal(record["amount"], "amount", number)
            if record.get("percent"):
                entry["percent"] = _decimal(record["percent"].rstrip("%"), "percent", number)
            if record.get("label"):
                entry["label"] = _clean_label(record["label"])
            if record.get("token_id"):
                entry["token_id"] = str(int(record["token_id"]))
        except (WalletError, ValueError) as error:
            problems.append(str(error) if str(error).startswith("row") else f"row {number}: {error}")
            continue
        key = (entry.get("source"), entry["address"], entry.get("token_id"))
        if key in seen:
            problems.append(f"row {number}: duplicate {entry['address']}")
            continue
        seen.add(key)
        rows.append(entry)
    if len(rows) > MAX_LIST_ROWS:
        problems.append(f"too many rows (max {MAX_LIST_ROWS})")
    return {"ok": not problems and bool(rows), "rows": rows, "problems": problems}


def save_list(db: Database, name: str, rows: list[dict]) -> None:
    name = " ".join(name.split())
    if not name or len(name) > 40:
        raise WalletError("list name must be 1-40 characters")
    db.execute("INSERT INTO recipient_lists(name, rows, updated_at) VALUES(?, ?, ?) "
               "ON CONFLICT(name) DO UPDATE SET rows = excluded.rows, updated_at = excluded.updated_at",
               (name, json.dumps(rows), time.time()))


def lists(db: Database) -> list[dict]:
    out = db.all("SELECT name, rows, updated_at FROM recipient_lists ORDER BY name")
    for r in out:
        r["rows"] = json.loads(r["rows"])
    return out


def delete_list(db: Database, name: str) -> None:
    db.execute("DELETE FROM recipient_lists WHERE name = ?", (name,))
