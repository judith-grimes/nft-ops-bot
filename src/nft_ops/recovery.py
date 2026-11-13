"""Restart recovery (audit C1, C2).

Mint and transfer operations run in memory. If NFT Ops is closed while one is waiting or in
flight, this module makes the records honest on the next start:

* operations still marked ``running`` become ``interrupted``;
* wallets that had not broadcast yet become ``not_submitted`` with a plain reason;
* wallets that had already broadcast become ``pending`` and are reconciled from on-chain
  receipts by :func:`reconcile_pending` (confirmed, or failed with the gas actually paid).
"""

from __future__ import annotations

import time
from collections.abc import Iterable

from .db import Database
from .networks import RpcError, RpcPool

NOT_SENT = {"queued", "preparing", "waiting", "scheduled", "simulating"}
IN_FLIGHT = {"submitted", "broadcasting", "pending"}
CLOSED_REASON = "NFT Ops was closed before this wallet's transaction was sent"
TRACK_WINDOW = 3 * 86_400


def recover_interrupted(db: Database) -> dict:
    jobs = db.all("SELECT id FROM jobs WHERE status = 'running'")
    not_sent = in_flight = 0
    now = time.time()
    for job in jobs:
        for row in db.all("SELECT id, status, tx_hash FROM history WHERE job_id = ?", (job["id"],)):
            if row["status"] in IN_FLIGHT or (row["tx_hash"] and row["status"] in NOT_SENT):
                db.update_history(row["id"], status="pending")
                in_flight += 1
            elif row["status"] in NOT_SENT:
                db.update_history(row["id"], status="not_submitted", error=CLOSED_REASON)
                not_sent += 1
        db.execute("UPDATE jobs SET status = 'interrupted', finished_at = ? WHERE id = ?",
                   (now, job["id"]))
    return {"jobs": len(jobs), "not_sent": not_sent, "in_flight": in_flight}


async def reconcile_pending(db: Database, pool: RpcPool, active_jobs: Iterable[int] = ()) -> int:
    """Look up receipts for broadcast transactions no running operation is tracking."""
    active = set(active_jobs)
    rows = db.all("SELECT id, job_id, chain_id, tx_hash FROM history WHERE status IN "
                  "('pending', 'submitted') AND tx_hash IS NOT NULL AND updated_at >= ?",
                  (time.time() - TRACK_WINDOW,))
    settled = 0
    for row in rows:
        if row["job_id"] in active or not row["chain_id"]:
            continue
        try:
            receipt = await pool.call(row["chain_id"], "eth_getTransactionReceipt", [row["tx_hash"]])
        except (RpcError, Exception):  # noqa: BLE001, S112 - try again next round
            continue
        if not receipt:
            continue
        gas_used = int(receipt["gasUsed"], 16)
        cost = str(gas_used * int(receipt.get("effectiveGasPrice") or "0x0", 16))
        ok = receipt.get("status") == "0x1"
        db.update_history(row["id"], status="confirmed" if ok else "failed", gas_used=gas_used,
                          gas_cost_wei=cost, error=None if ok else "transaction reverted on-chain")
        settled += 1
    return settled
