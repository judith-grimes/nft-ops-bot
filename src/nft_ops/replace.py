"""Speed up or cancel a stuck transaction by replacing it (same nonce, higher fees).

Speed up: the identical transaction (to, value, data, gas) with higher fees.
Cancel:   a 0-value transfer to yourself with the same nonce and higher fees.
Only one transaction per nonce can ever be included, so either the original or the
replacement confirms — never both.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from dataclasses import dataclass

from eth_utils.address import to_checksum_address

from osnm_z.rpc_protocol import decode_fee_inputs
from osnm_z.transaction import Eip1559Transaction, sign_eip1559_transaction

from .networks import RpcError
from .safety import from_wei

BUMP = 1.15            # nodes require at least +10% on both fees to accept a replacement
TTL_SECONDS = 120
CONFIRM_WORD = "REPLACE"
TRACK_SECONDS = 600
POLL_SECONDS = 2.0


class ReplaceError(Exception):
    pass


@dataclass(slots=True)
class Replacement:
    token: str
    mode: str
    history_id: int
    chain_id: int
    address: str
    nonce: int
    original_hash: str
    to: str
    value: int
    data: bytes
    gas: int
    max_fee: int
    priority_fee: int
    expires_at: float


class Replacer:
    def __init__(self, engine) -> None:   # a TxEngine; reuses its db, pool, vault, safety, bus
        self.e = engine
        self.pending: dict[str, Replacement] = {}
        self._tasks: set[asyncio.Task] = set()

    async def preview(self, history_id: int, mode: str) -> dict:
        if mode not in ("speedup", "cancel"):
            raise ReplaceError("mode must be speedup or cancel")
        db, pool = self.e.db, self.e.pool
        row = db.one("SELECT * FROM history WHERE id = ?", (history_id,))
        if row is None or not row["tx_hash"] or row["nonce"] is None:
            raise ReplaceError("this entry has no sent transaction to replace")
        if row["status"] not in ("submitted", "pending"):
            raise ReplaceError(f"this transaction is {row['status']}, nothing to replace")
        address = to_checksum_address(row["address"])
        if not self.e.vault.has(address):
            raise ReplaceError("no key for this wallet")
        chain_id = int(row["chain_id"])
        receipt = await pool.call(chain_id, "eth_getTransactionReceipt", [row["tx_hash"]])
        if receipt:
            raise ReplaceError("it already confirmed; refresh History")
        latest_nonce = int(await pool.call(chain_id, "eth_getTransactionCount", [address, "latest"]), 16)
        if latest_nonce > int(row["nonce"]):
            raise ReplaceError("a transaction with this nonce is already confirmed")
        tx = await pool.call(chain_id, "eth_getTransactionByHash", [row["tx_hash"]])
        if mode == "speedup" and not tx:
            raise ReplaceError("the network no longer knows this transaction (it was dropped); "
                               "use Cancel, or send it again from its page")
        block, prio = await pool.batch(chain_id, [("eth_getBlockByNumber", ["latest", False]),
                                                  ("eth_maxPriorityFeePerGas", [])])
        if isinstance(block, RpcError) or isinstance(prio, RpcError):
            raise ReplaceError("could not read network fees")
        current = decode_fee_inputs(block, prio)
        old_max = int(tx.get("maxFeePerGas") or tx.get("gasPrice") or "0x0", 16) if tx else 0
        old_prio = int(tx.get("maxPriorityFeePerGas") or tx.get("gasPrice") or "0x0", 16) if tx else 0
        new_prio = max(math.ceil(old_prio * BUMP), current.max_priority_fee_per_gas, 1)
        new_max = max(math.ceil(old_max * BUMP), current.max_fee_per_gas, new_prio)
        if mode == "speedup":
            to, value = to_checksum_address(tx["to"]), int(tx["value"], 16)
            data, gas = bytes.fromhex(tx["input"][2:]), int(tx["gas"], 16)
        else:
            to, value, data = address, 0, b""
            gas = math.ceil(int(await pool.call(chain_id, "eth_estimateGas",
                                                [{"from": address, "to": address, "value": "0x0"}]), 16) * 1.2)
        gas_cost = gas * new_max
        limits = self.e.safety.limits_wei()
        problems = []
        if gas_cost > limits["max_gas_cost_per_tx"]:
            problems.append(f"max gas cost {from_wei(gas_cost)} is over the per-transaction limit")
        balance = int(await pool.call(chain_id, "eth_getBalance", [address, "latest"]), 16)
        if balance < gas_cost + value:
            problems.append(f"wallet has {from_wei(balance)}, needs {from_wei(gas_cost + value)}")
        token = secrets.token_urlsafe(12)
        if not problems:
            self.pending[token] = Replacement(token, mode, history_id, chain_id, address,
                                              int(row["nonce"]), row["tx_hash"], to, value, data, gas,
                                              new_max, new_prio, time.time() + TTL_SECONDS)
        return {
            "token": None if problems else token, "mode": mode, "address": address,
            "nonce": int(row["nonce"]), "original_hash": row["tx_hash"], "chain_id": chain_id,
            "old_max_fee_gwei": f"{old_max / 1e9:.4f}" if old_max else "unknown",
            "new_max_fee_gwei": f"{new_max / 1e9:.4f}", "new_priority_fee_gwei": f"{new_prio / 1e9:.4f}",
            "max_gas_cost": from_wei(gas_cost), "problems": problems, "confirm_word": CONFIRM_WORD,
        }

    async def execute(self, token: str, confirmation: str) -> dict:
        if confirmation.strip().upper() != CONFIRM_WORD:
            raise ReplaceError(f"type {CONFIRM_WORD} to confirm")
        r = self.pending.pop(token, None)
        if r is None or r.expires_at < time.time():
            raise ReplaceError("preview expired; open it again")
        safety, db = self.e.safety, self.e.db
        with safety.lock:
            if safety.stopped:
                raise ReplaceError("emergency STOP is active; nothing sent")
            gas_cost = r.gas * r.max_fee
            if safety.daily_spent_wei(r.chain_id) + gas_cost > safety.limits_wei()["max_daily"]:
                raise ReplaceError("blocked: over the 24h spending limit")
        signed = sign_eip1559_transaction(Eip1559Transaction(
            chain_id=r.chain_id, nonce=r.nonce, max_priority_fee_per_gas=r.priority_fee,
            max_fee_per_gas=r.max_fee, gas_limit=r.gas, target=r.to, value=r.value,
            calldata=r.data), self.e.vault.signer(r.address))
        new_id = db.add_history(
            operation=r.mode, chain_id=r.chain_id, address=r.address, tx_hash=signed.hash_hex,
            nonce=r.nonce, value_wei=str(r.value), spend_wei="0",
            max_cost_wei=str(r.gas * r.max_fee), status="submitted", recipient=r.to,
            error=f"replaces {r.original_hash}")
        url = await self.e.pool.best_url(r.chain_id)
        broadcast = bool((db.get_setting("network", {}) or {}).get("broadcast_all", True))
        try:
            await self.e.pool.send_raw(r.chain_id, url, "0x" + signed.raw.hex(), broadcast)
        except RpcError as error:
            db.update_history(new_id, status="failed", error=f"rejected: {error}")
            raise ReplaceError(f"the network rejected the replacement: {error}") from None
        task = asyncio.create_task(self._track(r, signed.hash_hex, new_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self.e.bus.publish("replace", history_id=r.history_id, mode=r.mode, tx_hash=signed.hash_hex)
        return {"tx_hash": signed.hash_hex, "history_id": new_id}

    async def _track(self, r: Replacement, new_hash: str, new_id: int) -> None:
        db, pool = self.e.db, self.e.pool
        deadline = time.time() + TRACK_SECONDS
        while time.time() < deadline:
            await asyncio.sleep(POLL_SECONDS)
            try:
                old, new = await pool.batch(r.chain_id, [("eth_getTransactionReceipt", [r.original_hash]),
                                                         ("eth_getTransactionReceipt", [new_hash])])
            except RpcError:
                continue
            loser_status = "cancelled" if r.mode == "cancel" else "replaced"
            for receipt, row_id, other_id, other_status, note in (
                    (new, new_id, r.history_id, loser_status,
                     "cancelled by a 0-value replacement" if r.mode == "cancel" else "replaced by a faster copy"),
                    (old, r.history_id, new_id, "not_needed", "the original confirmed first")):
                if isinstance(receipt, dict) and receipt.get("blockHash"):
                    cost = int(receipt["gasUsed"], 16) * int(receipt.get("effectiveGasPrice") or "0x0", 16)
                    ok = receipt.get("status") == "0x1"
                    db.update_history(row_id, status="confirmed" if ok else "failed",
                                      gas_used=int(receipt["gasUsed"], 16), gas_cost_wei=str(cost),
                                      error=None if ok else "transaction reverted")
                    db.update_history(other_id, status=other_status, gas_cost_wei="0",
                                      error=f"{note} (nonce {r.nonce})")
                    self.e.bus.publish("replace_done", history_id=r.history_id, winner=row_id)
                    return
