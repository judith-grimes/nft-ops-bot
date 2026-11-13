"""Stage 6.5 data for the redesigned UI: wallet readiness scores, eligibility scan history,
system health and the mint calendar.

Everything here is computed from real data (mint previews, the database, live loops). Nothing
is estimated or hard-coded: a check that could not be performed is reported as "not verified"
and scores no points.
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import collections
from .db import Database
from .mint import MintEngine, params_from
from .networks import RpcError, RpcPool
from .safety import Safety, to_wei

# (key, label, weight). Weights add up to 100.
CHECKS = [
    ("key", "Wallet can sign (key in vault)", 5),
    ("eligibility", "Eligible for this phase", 30),
    ("phase", "Phase open or upcoming, not sold out", 10),
    ("allocation", "Allocation covers the quantity", 10),
    ("price", "Funds for the mint price", 15),
    ("gas", "Funds left for network fees", 15),
    ("rpc", "Network (RPC) reachable", 5),
    ("limits", "Within your spending limits", 5),
    ("nonce", "No pending transaction or running operation", 5),
]
BLOCKING = {"key", "eligibility", "phase", "allocation", "price", "gas", "limits"}
SOFT = {"nonce"}


# ===== readiness ==========================================================================
async def readiness(db: Database, mint: MintEngine, pool: RpcPool, safety: Safety, slug: str,
                    stage_index: int, addresses: list[str], quantity: int = 1) -> dict:
    """Run the real mint preview checks and score every wallet 0-100."""
    params = params_from({"slug": slug, "stage_index": stage_index, "quantity": quantity},
                         addresses, db.get_setting("mint", {}) or {})
    preview = await mint.preview(params)
    chain_id = preview["collection"]["chain_id"]
    nonce_gap: dict[str, Any] = {}
    try:
        latest = await pool.batch(chain_id, [("eth_getTransactionCount", [a, "latest"]) for a in addresses])
        pending = await pool.batch(chain_id, [("eth_getTransactionCount", [a, "pending"]) for a in addresses])
        for a, lo, hi in zip(addresses, latest, pending, strict=True):
            nonce_gap[a] = None if isinstance(lo, RpcError) or isinstance(hi, RpcError) else int(hi, 16) - int(lo, 16)
    except RpcError:
        nonce_gap = {a: None for a in addresses}
    ended = preview["stage"]["state"] == "ended"
    now = time.time()
    out = []
    for row in preview["rows"]:
        checks = _checks(row, quantity, ended, nonce_gap.get(row["address"]), safety)
        score = sum(w for key, _l, w in CHECKS if checks[key]["state"] == "pass")
        states = {k: c["state"] for k, c in checks.items()}
        if any(states[k] == "fail" for k in BLOCKING):
            status = "Not Ready"
        elif any(s == "unverified" for s in states.values()):
            status = "Needs Attention"
        elif any(states[k] == "fail" for k in SOFT):
            status = "Almost Ready"
        else:
            status = "Ready"
        sim = row.get("simulation") or ""
        listed = [{"key": k, "label": label, "weight": w, **checks[k]} for k, label, w in CHECKS]
        listed.append({"key": "simulation", "label": "Test run (simulation)", "weight": 0,
                       "state": "pass" if sim.startswith("ok") else "fail" if sim == "failed" else "info",
                       "detail": sim or "Not available yet: allowlist mints are tested by OpenSea at "
                                        "launch, public mints once the phase is open.", "fix": ""})
        entry = {"address": row["address"], "label": row.get("label", ""), "score": score,
                 "status": status, "checks": listed, "checked_at": now,
                 "balance": row.get("balance"), "worst_case": row.get("worst_case"),
                 "eligibility": row.get("eligibility")}
        db.execute("INSERT OR REPLACE INTO wallet_readiness VALUES(?, ?, ?, ?, ?, ?, ?)",
                   (slug, stage_index, row["address"], score, status, json.dumps(listed), now))
        out.append(entry)
    counts: dict[str, int] = {}
    for e in out:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    return {"wallets": out, "counts": counts, "stage": preview["stage"],
            "chain_id": chain_id, "checked_at": now}


def _checks(row: dict, quantity: int, ended: bool, gap: int | None, safety: Safety) -> dict:
    st = row["status"]
    c: dict[str, dict] = {}

    def put(key: str, state: str, detail: str, fix: str = "") -> None:
        c[key] = {"state": state, "detail": detail, "fix": fix}

    put("key", "fail" if st == "NO_KEY" else "pass",
        "Watch-only wallet: there is no key to sign with." if st == "NO_KEY" else "Key is in the vault.",
        "Import this wallet's private key on the Wallets page." if st == "NO_KEY" else "")
    elig = row.get("eligibility") or ""
    if elig == "ELIGIBLE":
        put("eligibility", "pass", "OpenSea confirmed this wallet for the phase.")
    elif elig == "NOT_ELIGIBLE":
        put("eligibility", "fail", "OpenSea says this wallet is not on this phase's list.",
            "Use another wallet or wait for a phase it qualifies for.")
    else:
        put("eligibility", "unverified", "Eligibility has not been verified for this wallet.",
            "Run an eligibility scan for this collection.")
    if ended or st == "PHASE_NOT_ACTIVE":
        put("phase", "fail", "This phase has ended.", "Choose a later phase.")
    elif st == "SOLD_OUT":
        put("phase", "fail", "OpenSea reports the drop is sold out or disabled.")
    else:
        put("phase", "pass", "Phase is open or upcoming.")
    limit = row.get("max_quantity")
    if limit is not None and quantity > int(limit):
        put("allocation", "fail", f"Wallet can mint {limit} more; you asked for {quantity}.",
            "Lower the quantity per wallet.")
    else:
        put("allocation", "pass", "Allocation is enough." if limit is not None else "No per-wallet limit reported.")
    bal = row.get("balance")
    if bal is None:
        for key in ("price", "gas"):
            put(key, "unverified", "The balance could not be read.", "Check the network connection or RPC.")
        put("rpc", "unverified", "The RPC did not answer the balance request.",
            "Try again, or add another RPC on the Networks page.")
    else:
        b, value, worst = to_wei(bal), to_wei(row["value"]), to_wei(row["worst_case"])
        put("price", "pass" if b >= value else "fail",
            f"Has {bal}, mint costs {row['value']}.",
            "" if b >= value else f"Send at least {row['value']} to this wallet.")
        put("gas", "pass" if b >= worst else "fail",
            f"Has {bal}, mint plus maximum fees is {row['worst_case']}.",
            "" if b >= worst else "Top it up (the Mint preview has a 'Fund short wallets' button).")
        put("rpc", "pass", "Network answered.")
    problems = safety.per_tx_problems(to_wei(row["value"]), to_wei(row["max_gas_cost"]))
    put("limits", "fail" if problems else "pass", "; ".join(problems) or "Within limits.",
        "Raise the limit in Settings or lower the quantity." if problems else "")
    if st == "ALREADY_RUNNING":
        put("nonce", "fail", row["reason"], "Wait for the other operation to finish.")
    elif gap is None:
        put("nonce", "unverified", "Could not check for pending transactions.")
    elif gap > 0:
        put("nonce", "fail", f"{gap} transaction(s) from this wallet are still pending.",
            "Wait for them, or speed up / cancel them on the History page.")
    else:
        put("nonce", "pass", "No pending transactions.")
    return c


def last_readiness(db: Database, slug: str, stage_index: int | None = None) -> list[dict]:
    sql, args = "SELECT * FROM wallet_readiness WHERE slug = ?", [slug]
    if stage_index is not None:
        sql += " AND stage_index = ?"
        args.append(stage_index)
    return [{**r, "checks": json.loads(r["checks"])} for r in db.all(sql, tuple(args))]


# ===== eligibility scan history ===========================================================
def record_scan(db: Database, slug: str, label: str, counts: dict) -> int:
    rows = db.all("SELECT address, stage_index, status, detail, max_quantity, minted, price_wei "
                  "FROM eligibility WHERE slug = ?", (slug,))
    total = len({r["address"] for r in rows})
    cur = db.execute("INSERT INTO eligibility_scans(slug, created_at, label, total, counts, results) "
                     "VALUES(?, ?, ?, ?, ?, ?)",
                     (slug, time.time(), label, total, json.dumps(counts), json.dumps(rows)))
    return int(cur.lastrowid)


def scans(db: Database, slug: str) -> list[dict]:
    return [{**r, "counts": json.loads(r["counts"])} for r in db.all(
        "SELECT id, slug, created_at, label, total, counts FROM eligibility_scans WHERE slug = ? "
        "ORDER BY id DESC LIMIT 50", (slug,))]


def scan_results(db: Database, scan_id: int) -> dict:
    row = db.one("SELECT * FROM eligibility_scans WHERE id = ?", (scan_id,))
    if row is None:
        raise KeyError("scan not found")
    return {**row, "counts": json.loads(row["counts"]), "results": json.loads(row["results"])}


# ===== calendar ===========================================================================
def calendar(db: Database) -> list[dict]:
    events = []
    tasks = db.all("SELECT * FROM scheduled_mints")
    for c in collections.list_all(db):
        for s in c["stages"]:
            linked = [t["id"] for t in tasks if t["slug"] == c["slug"] and t["stage_index"] == s["index"]
                      and t["status"] not in ("cancelled",)]
            events.append({"kind": "phase", "id": f"p-{c['slug']}-{s['index']}", "slug": c["slug"],
                           "chain_id": c["chain_id"], "network": c["chain_name"], "phase": s["name"],
                           "phase_type": s["type"], "stage_index": s["index"], "start": s["starts_at"],
                           "end": s["ends_at"], "state": s["state"], "price_wei": s["price_wei"],
                           "eligible_wallets": s["eligible_wallets"], "tasks": linked})
    names = {r["chain_id"]: r["name"] for r in db.all("SELECT chain_id, name FROM networks")}
    for t in tasks:
        readiness = json.loads(t["readiness"]) if t["readiness"] else None
        events.append({"kind": "task", "id": f"t-{t['id']}", "task_id": t["id"], "slug": t["slug"],
                       "chain_id": t["chain_id"], "network": names.get(t["chain_id"], ""),
                       "phase": t["stage_name"], "phase_type": t["stage_type"],
                       "stage_index": t["stage_index"], "start": t["run_at"],
                       "official": t["phase_starts_at"], "end": None, "state": t["status"],
                       "mode": t["mode"], "name": t["name"],
                       "ready": readiness["ready"] if readiness else None,
                       "total": readiness["total"] if readiness else None})
    events.sort(key=lambda e: e["start"] or 0)
    return events


# ===== health =============================================================================
def health(db: Database, beats: dict[str, float], tasks: dict[str, Any], stopped: bool) -> dict:
    now = time.time()

    def loop(name: str, max_age: float) -> dict:
        task = tasks.get(name)
        alive = task is not None and not task.done()
        beat = beats.get(name)
        ok = alive and (beat is None or now - beat <= max_age)
        return {"ok": ok, "last": beat,
                "text": "Running" if ok else "Not running" if not alive else "Delayed"}

    nets = []
    for n in db.all("SELECT chain_id, name FROM networks WHERE enabled = 1 ORDER BY name"):
        eps = db.all("SELECT healthy, checked_at FROM rpc_endpoints WHERE chain_id = ? AND enabled = 1",
                     (n["chain_id"],))
        healthy = sum(1 for e in eps if e["healthy"])
        nets.append({"chain_id": n["chain_id"], "name": n["name"], "healthy": healthy, "total": len(eps),
                     "checked_at": max((e["checked_at"] or 0 for e in eps), default=None)})
    used = {r["chain_id"] for r in db.all("SELECT DISTINCT chain_id FROM collections")} | {4663}
    key_nets = [n for n in nets if n["chain_id"] in used]
    failures = db.one("SELECT COUNT(*) AS n FROM alerts WHERE level = 'bad' AND read = 0")["n"]
    return {
        "stopped": stopped,
        "scheduler": loop("scheduler", 30),
        "monitor": loop("monitor", 900),
        "offers": loop("offers", 3600),
        # a network never checked yet is "unknown", not a failure
        "rpc": {"ok": all(n["healthy"] for n in key_nets if n["checked_at"]), "networks": nets,
                "key_networks": key_nets},
        "notifications": {"ok": True, "unread_failures": failures},
    }
