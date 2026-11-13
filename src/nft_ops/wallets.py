"""Wallet registry: labels, groups, watch-only addresses, CSV import/export (never keys)."""

from __future__ import annotations

import csv
import io
import re
import time

from eth_utils.address import is_address, to_checksum_address

from .db import Database
from .vault import Vault, VaultError, parse_private_key

_HEX64 = re.compile(r"(?<![0-9a-fA-Fx])(?:0x)?[0-9a-fA-F]{64}(?![0-9a-fA-F])")
MAX_LABEL = 60
MAX_GROUP = 40


class WalletError(Exception):
    pass


def checksum(address: str) -> str:
    if not isinstance(address, str) or not is_address(address.strip()):
        raise WalletError(f"invalid address: {address!r}"[:80])
    return to_checksum_address(address.strip())


def _clean_label(label: str) -> str:
    label = " ".join(str(label).split())
    if len(label) > MAX_LABEL:
        raise WalletError(f"label is longer than {MAX_LABEL} characters")
    return label


class WalletService:
    def __init__(self, db: Database, vault: Vault) -> None:
        self.db = db
        self.vault = vault

    # ----- listing ---------------------------------------------------------------------
    def list(self) -> list[dict]:
        wallets = self.db.all("SELECT address, label, has_key, created_at FROM wallets "
                              "ORDER BY created_at, address")
        memberships = self.db.all(
            "SELECT wg.address, g.name FROM wallet_groups wg JOIN groups g ON g.id = wg.group_id"
        )
        by_wallet: dict[str, list[str]] = {}
        for m in memberships:
            by_wallet.setdefault(m["address"], []).append(m["name"])
        for w in wallets:
            w["groups"] = sorted(by_wallet.get(w["address"], []))
            w["has_key"] = bool(w["has_key"]) and self.vault.unlocked and self.vault.has(w["address"])
        return wallets

    def groups(self) -> list[dict]:
        return self.db.all(
            "SELECT g.id, g.name, COUNT(wg.address) AS size FROM groups g "
            "LEFT JOIN wallet_groups wg ON wg.group_id = g.id GROUP BY g.id ORDER BY g.name"
        )

    def resolve_selection(self, addresses: list[str] | None = None, group: str | None = None,
                          exclude: list[str] | None = None) -> list[str]:
        """Turn 'these addresses' / 'this group' / 'all' into a checked address list."""
        from . import groups as smart   # local import: groups -> collections -> ... avoids a cycle
        members = smart.resolve_smart(self.db, group) if group else None
        if addresses:
            chosen = [checksum(a) for a in addresses]
        elif members is not None:
            chosen = members
        elif group:
            chosen = [r["address"] for r in self.db.all(
                "SELECT wg.address FROM wallet_groups wg JOIN groups g ON g.id = wg.group_id "
                "WHERE g.name = ? ORDER BY wg.address", (group,))]
        else:
            chosen = [r["address"] for r in self.db.all("SELECT address FROM wallets")]
        excluded = {checksum(a) for a in (exclude or [])}
        known = {r["address"] for r in self.db.all("SELECT address FROM wallets")}
        unknown = [a for a in chosen if a not in known]
        if unknown:
            raise WalletError(f"not registered: {unknown[0]}")
        return list(dict.fromkeys(a for a in chosen if a not in excluded))

    # ----- adding ----------------------------------------------------------------------
    def import_keys(self, text: str, group: str | None = None) -> dict:
        """Lines of 'KEY' or 'LABEL,KEY' / 'LABEL KEY'. Keys go only into the vault."""
        entries: list[tuple[str, bytes]] = []
        errors: list[str] = []
        for number, raw_line in enumerate(text.splitlines(), 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            match = list(_HEX64.finditer(line))
            if len(match) != 1:
                errors.append(f"line {number}: expected exactly one private key")
                continue
            label = (line[:match[0].start()] + line[match[0].end():]).strip(" ,;\t:")
            try:
                entries.append((_clean_label(label), parse_private_key(match[0].group())))
            except (VaultError, WalletError) as error:
                errors.append(f"line {number}: {error}")
        if errors:
            raise WalletError("; ".join(errors[:5]) + (" …" if len(errors) > 5 else ""))
        if not entries:
            raise WalletError("no keys found")
        results = self.vault.add_keys([k for _, k in entries])
        added = 0
        for (label, _), (address, _new) in zip(entries, results, strict=True):
            added += self._upsert(address, label, has_key=True)
        if group:
            self.add_to_group([a for a, _ in results], group)
        return {"imported": len(results), "new": added, "addresses": [a for a, _ in results]}

    def generate(self, count: int, label_prefix: str = "gen", group: str | None = None) -> list[str]:
        addresses = self.vault.generate(count)
        start = self.db.one("SELECT COUNT(*) AS n FROM wallets")["n"]
        for offset, address in enumerate(addresses, 1):
            self._upsert(address, _clean_label(f"{label_prefix}-{start + offset}"), has_key=True)
        if group:
            self.add_to_group(addresses, group)
        return addresses

    def add_watch_only(self, address: str, label: str = "") -> str:
        address = checksum(address)
        self._upsert(address, _clean_label(label), has_key=False)
        return address

    def _upsert(self, address: str, label: str, has_key: bool) -> int:
        existing = self.db.one("SELECT label, has_key FROM wallets WHERE address = ?", (address,))
        if existing is None:
            self.db.execute(
                "INSERT INTO wallets(address, label, has_key, created_at) VALUES(?, ?, ?, ?)",
                (address, label, int(has_key), time.time()),
            )
            return 1
        self.db.execute(
            "UPDATE wallets SET label = CASE WHEN ? != '' THEN ? ELSE label END, "
            "has_key = MAX(has_key, ?) WHERE address = ?",
            (label, label, int(has_key), address),
        )
        return 0

    # ----- editing ---------------------------------------------------------------------
    def set_label(self, address: str, label: str) -> None:
        self.db.execute("UPDATE wallets SET label = ? WHERE address = ?",
                        (_clean_label(label), checksum(address)))

    def remove(self, addresses: list[str]) -> int:
        addresses = [checksum(a) for a in addresses]
        if self.vault.unlocked:
            self.vault.remove(addresses)
        removed = 0
        for address in addresses:
            removed += self.db.execute("DELETE FROM wallets WHERE address = ?", (address,)).rowcount
        return removed

    def create_group(self, name: str) -> None:
        name = " ".join(name.split())
        if name.startswith("★") or name.startswith("smart:"):
            raise WalletError("names starting with ★ are reserved for smart groups")
        if not name or len(name) > MAX_GROUP:
            raise WalletError(f"group name must be 1-{MAX_GROUP} characters")
        self.db.execute("INSERT OR IGNORE INTO groups(name) VALUES(?)", (name,))

    def rename_group(self, old: str, new: str) -> None:
        new = " ".join(new.split())
        if not new or len(new) > MAX_GROUP:
            raise WalletError(f"group name must be 1-{MAX_GROUP} characters")
        self.db.execute("UPDATE groups SET name = ? WHERE name = ?", (new, old))

    def delete_group(self, name: str) -> None:
        self.db.execute("DELETE FROM groups WHERE name = ?", (name,))

    def add_to_group(self, addresses: list[str], group: str) -> None:
        self.create_group(group)
        gid = self.db.one("SELECT id FROM groups WHERE name = ?", (" ".join(group.split()),))["id"]
        self.db.executemany("INSERT OR IGNORE INTO wallet_groups(address, group_id) VALUES(?, ?)",
                            [(checksum(a), gid) for a in addresses])

    def remove_from_group(self, addresses: list[str], group: str) -> None:
        row = self.db.one("SELECT id FROM groups WHERE name = ?", (group,))
        if row:
            self.db.executemany("DELETE FROM wallet_groups WHERE address = ? AND group_id = ?",
                                [(checksum(a), row["id"]) for a in addresses])

    # ----- CSV -------------------------------------------------------------------------
    def export_csv(self) -> str:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["address", "label", "groups", "has_key"])
        for w in self.list():
            writer.writerow([w["address"], safe_cell(w["label"]), safe_cell(";".join(w["groups"])),
                             "yes" if w["has_key"] else "no"])
        return out.getvalue()

    def validate_csv(self, text: str) -> dict:
        """Check a CSV of address,label,groups. Rejects anything that looks like a key."""
        rows, problems = [], []
        if looks_secret(text):
            return {"ok": False, "rows": [],
                    "problems": ["file seems to contain a private key or seed phrase — refused. "
                                 "Import keys only through the Import keys box."]}
        reader = csv.DictReader(io.StringIO(text))
        fields = [f.strip().lower() for f in (reader.fieldnames or [])]
        if "address" not in fields:
            return {"ok": False, "rows": [], "problems": ["missing 'address' column"]}
        seen: set[str] = set()
        for number, raw in enumerate(reader, 2):
            row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
            try:
                address = checksum(row.get("address", ""))
            except WalletError as error:
                problems.append(f"row {number}: {error}")
                continue
            if address in seen:
                problems.append(f"row {number}: duplicate {address}")
                continue
            seen.add(address)
            try:
                label = _clean_label(row.get("label", ""))
            except WalletError as error:
                problems.append(f"row {number}: {error}")
                continue
            groups = [g.strip() for g in row.get("groups", "").split(";") if g.strip()]
            rows.append({"address": address, "label": label, "groups": groups})
        return {"ok": not problems, "rows": rows, "problems": problems}

    def import_csv(self, text: str) -> dict:
        checked = self.validate_csv(text)
        if not checked["ok"]:
            raise WalletError("; ".join(checked["problems"][:5]))
        for row in checked["rows"]:
            self._upsert(row["address"], row["label"], has_key=False)
            for group in row["groups"]:
                self.add_to_group([row["address"]], group)
        return {"imported": len(checked["rows"])}


_KEY_LIKE = re.compile(r"(?<![0-9a-fA-F])(?:0x)?[0-9a-fA-F]{64}(?![0-9a-fA-F])")
_SEED_LIKE = re.compile(r"(?:\b[a-z]{3,8}\b[ \t]+){11,}\b[a-z]{3,8}\b")


def looks_secret(text: str) -> bool:
    """A 64-hex token (private key) or 12+ lowercase words in a row (seed phrase)."""
    return bool(_KEY_LIKE.search(text) or _SEED_LIKE.search(text))


def safe_cell(value: object) -> str:
    """Stop spreadsheet apps from running a cell as a formula (CSV injection)."""
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text
