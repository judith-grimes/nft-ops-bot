"""Wallet groups, Stage 6.5.2: roles, smart groups, health, bulk moves and after-mint rules.

* **Roles**: ``minting`` (default wallets for Mint and Eligibility), ``funding`` (pays top-ups),
  ``vault`` (safe storage: the only destination an after-mint rule may send NFTs to).
* **Smart groups** are computed, never stored, so they are always current. Their names start
  with ``smart:`` and work anywhere a group name works (scans, mints, schedules, transfers).
* **After-mint rule** per group: ``keep`` (default) or ``vault`` (send freshly minted NFTs to the
  vault wallet). It only acts when you set it, and every send goes through the normal transfer
  preview, spending limits, STOP and pre-broadcast checks.
"""

from __future__ import annotations

import time

from .db import Database
from .safety import from_wei, to_wei

ROLES = {"": "No role", "minting": "Minting", "funding": "Funding", "vault": "Vault"}
AFTER_MINT = {"keep": "Keep in the wallet", "vault": "Send to the vault wallet"}
SMART = {
    "smart:ready": "Ready to mint (checked in 24 h)",
    "smart:low-gas": "Low on gas",
    "smart:holds-nfts": "Holds NFTs",
}
DEFAULT_SMART = {"chain_id": 4663, "low_gas": "0.002"}


class GroupError(Exception):
    pass


def smart_settings(db: Database) -> dict:
    return {**DEFAULT_SMART, **(db.get_setting("smart_groups", {}) or {})}


def smart_members(db: Database, name: str) -> list[str]:
    keyed = [r["address"] for r in db.all("SELECT address FROM wallets WHERE has_key = 1 ORDER BY address")]
    if name == "smart:ready":
        ready = {r["address"] for r in db.all(
            "SELECT DISTINCT address FROM wallet_readiness WHERE status = 'Ready' AND checked_at >= ?",
            (time.time() - 86_400,))}
        return [a for a in keyed if a in ready]
    if name == "smart:low-gas":
        s = smart_settings(db)
        limit = to_wei(s["low_gas"])
        bal = {r["address"]: int(r["amount"]) for r in db.all(
            "SELECT address, amount FROM balances WHERE chain_id = ? AND asset = 'native'", (int(s["chain_id"]),))}
        return [a for a in keyed if a in bal and bal[a] < limit]
    if name == "smart:holds-nfts":
        holders = {r["address"] for r in db.all("SELECT DISTINCT address FROM os_holdings")} | {
            r["owner"] for r in db.all("SELECT DISTINCT owner FROM nft_tokens")}
        return [a for a in keyed if a in holders]
    if name.startswith("smart:eligible:"):
        try:
            _, _, slug, stage = name.split(":", 3)
        except ValueError:
            raise GroupError(f"unknown smart group {name}") from None
        rows = db.all("SELECT DISTINCT address FROM eligibility WHERE slug = ? AND stage_index = ? "
                      "AND status = 'ELIGIBLE' ORDER BY address", (slug, int(stage)))
        return [r["address"] for r in rows]
    raise GroupError(f"unknown smart group {name}")


STAR = "★ "     # smart groups are shown (and selected) by a name starting with a star


def smart_list(db: Database) -> list[dict]:
    """Smart groups with display names that work anywhere a group name is accepted."""
    out = [{"name": STAR + v, "key": k, "smart": True, "size": len(smart_members(db, k))} for k, v in SMART.items()]
    names: dict[tuple[str, int], str] = {}
    try:
        from . import collections
        for c in collections.list_all(db):
            for st in c["stages"]:
                names[(c["slug"], st["index"])] = st["name"]
    except Exception:  # noqa: BLE001, S110 - names are cosmetic
        pass
    for r in db.all("SELECT slug, stage_index, COUNT(DISTINCT address) AS n FROM eligibility "
                    "WHERE status = 'ELIGIBLE' GROUP BY slug, stage_index ORDER BY slug, stage_index"):
        phase = names.get((r["slug"], r["stage_index"]), f"phase {r['stage_index']}")
        out.append({"name": f"{STAR}Eligible: {r['slug']} · {phase}", "smart": True, "size": r["n"],
                    "key": f"smart:eligible:{r['slug']}:{r['stage_index']}"})
    return out


def resolve_smart(db: Database, name: str) -> list[str] | None:
    """Members of a smart group given by display name or key; None if it is not a smart group."""
    if name.startswith("smart:"):
        return smart_members(db, name)
    if not name.startswith(STAR):
        return None
    for g in smart_list(db):
        if g["name"] == name:
            return smart_members(db, g["key"])
    raise GroupError(f"smart group {name!r} has no wallets right now")


def set_options(db: Database, name: str, role: str | None = None, after_mint: str | None = None,
                vault: str | None = None) -> None:
    if not db.one("SELECT 1 FROM groups WHERE name = ?", (name,)):
        raise GroupError("group not found")
    if role is not None:
        if role not in ROLES:
            raise GroupError("role must be minting, funding, vault or empty")
        db.execute("UPDATE groups SET role = ? WHERE name = ?", (role, name))
    if vault is not None:
        if vault and not db.one("SELECT 1 FROM wallets WHERE address = ?", (vault,)):
            raise GroupError("the vault must be one of your own wallets")
        db.execute("UPDATE groups SET vault = ? WHERE name = ?", (vault or None, name))
    if after_mint is not None:
        if after_mint not in AFTER_MINT:
            raise GroupError("after-mint rule must be keep or vault")
        if after_mint == "vault" and not vault_for(db, name):
            raise GroupError("choose a vault wallet first (or give a group the Vault role)")
        db.execute("UPDATE groups SET after_mint = ? WHERE name = ?", (after_mint, name))


def vault_for(db: Database, name: str) -> str | None:
    """The group's own vault wallet, else the first wallet of a group with the Vault role."""
    row = db.one("SELECT vault FROM groups WHERE name = ?", (name,))
    if row and row["vault"]:
        return row["vault"]
    v = db.one("SELECT wg.address FROM wallet_groups wg JOIN groups g ON g.id = wg.group_id "
               "WHERE g.role = 'vault' ORDER BY wg.address LIMIT 1")
    return v["address"] if v else None


def role_group(db: Database, role: str) -> str | None:
    row = db.one("SELECT name FROM groups WHERE role = ? ORDER BY name LIMIT 1", (role,))
    return row["name"] if row else None


def after_mint_target(db: Database, wallet: str) -> str | None:
    """Vault address if any group of this wallet says 'send minted NFTs to the vault'."""
    for g in db.all("SELECT g.name FROM groups g JOIN wallet_groups wg ON wg.group_id = g.id "
                    "WHERE wg.address = ? AND g.after_mint = 'vault' ORDER BY g.name", (wallet,)):
        target = vault_for(db, g["name"])
        if target and target != wallet:
            return target
    return None


def health(db: Database, chain_id: int) -> list[dict]:
    s = smart_settings(db)
    low = to_wei(s["low_gas"])
    ready = {r["address"] for r in db.all(
        "SELECT DISTINCT address FROM wallet_readiness WHERE status = 'Ready' AND checked_at >= ?",
        (time.time() - 86_400,))}
    bal = {r["address"]: int(r["amount"]) for r in db.all(
        "SELECT address, amount FROM balances WHERE chain_id = ? AND asset = 'native'", (chain_id,))}
    out = []
    for g in db.all("SELECT id, name, COALESCE(role, '') AS role, COALESCE(after_mint, 'keep') AS after_mint, "
                    "vault FROM groups ORDER BY name"):
        members = db.all("SELECT w.address, w.label, w.has_key FROM wallet_groups wg JOIN wallets w "
                         "ON w.address = wg.address WHERE wg.group_id = ? ORDER BY w.label, w.address", (g["id"],))
        known = [m for m in members if m["address"] in bal]
        out.append({
            "name": g["name"], "role": g["role"], "after_mint": g["after_mint"], "vault": vault_for(db, g["name"]),
            "own_vault": g["vault"], "size": len(members), "with_keys": sum(1 for m in members if m["has_key"]),
            "balance": from_wei(sum(bal[m["address"]] for m in known)),
            "balance_known": len(known), "ready": sum(1 for m in members if m["address"] in ready),
            "low_gas": sum(1 for m in known if bal[m["address"]] < low),
            "members": [{"address": m["address"], "label": m["label"],
                         "balance": from_wei(bal[m["address"]]) if m["address"] in bal else None} for m in members],
        })
    return out


def move(db: Database, addresses: list[str], source: str, target: str, add) -> int:
    """Move wallets from one group to another (``add`` = WalletService.add_to_group)."""
    if source == target:
        raise GroupError("choose a different target group")
    add(addresses, target)
    row = db.one("SELECT id FROM groups WHERE name = ?", (source,))
    if row:
        db.executemany("DELETE FROM wallet_groups WHERE address = ? AND group_id = ?",
                       [(a, row["id"]) for a in addresses])
    return len(addresses)


def split(db: Database, name: str, size: int, prefix: str, add) -> list[str]:
    """Split a group (or a smart group) into new groups of ``size`` wallets: prefix-1, prefix-2, ..."""
    if not 1 <= size <= 1000:
        raise GroupError("batch size must be 1-1000")
    if name.startswith("smart:"):
        members = smart_members(db, name)
    else:
        members = [r["address"] for r in db.all(
            "SELECT wg.address FROM wallet_groups wg JOIN groups g ON g.id = wg.group_id WHERE g.name = ? "
            "ORDER BY wg.address", (name,))]
    if not members:
        raise GroupError("that group has no wallets")
    prefix = " ".join((prefix or name.replace("smart:", "")).split())[:40]
    made = []
    for i in range(0, len(members), size):
        new = f"{prefix}-{i // size + 1}"
        add(members[i:i + size], new)
        made.append(new)
    return made
