"""Emergency STOP and spending limits, enforced in the backend at the last moment before broadcast.

The pre-broadcast gate is synchronous and runs inside osnm_z.submission right before
eth_sendRawTransaction. It either reserves the transaction's worst-case cost and lets it go,
or raises GateBlocked so that nothing is sent.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from osnm_z.errors import MintError
from osnm_z.hooks import PreBroadcastCheck

from .db import Database
from .events import EventBus

WEI = 10**18
DEFAULT_LIMITS = {
    # Amounts are in the chain's native token (ETH on Robinhood Chain, Ethereum, Base, ...).
    "max_value_per_tx": "0.05",
    "max_gas_cost_per_tx": "0.01",
    "max_total_per_operation": "0.5",
    "max_daily": "1.0",
}
LIMIT_LABELS = {
    "max_value_per_tx": "maximum mint value per transaction",
    "max_gas_cost_per_tx": "maximum gas cost per transaction",
    "max_total_per_operation": "maximum total spend per operation",
    "max_daily": "maximum spend per 24 hours (per network)",
}


class GateBlocked(MintError):
    """The pre-broadcast gate refused the transaction; nothing was sent."""


def to_wei(amount: str | float | Decimal) -> int:
    try:
        value = Decimal(str(amount).strip())
    except InvalidOperation:
        raise ValueError(f"not a number: {amount!r}") from None
    if value.is_nan() or value < 0:
        raise ValueError("amount must be zero or more")
    return int(value * WEI)


def from_wei(amount: int) -> str:
    text = f"{Decimal(amount) / WEI:.9f}".rstrip("0").rstrip(".")
    return text or "0"


@dataclass(slots=True)
class OperationBudget:
    """Spending state shared by every wallet in one operation."""

    job_id: int
    chain_id: int
    cap_wei: int                      # min(global operation limit, user's cap for this job)
    reserved_wei: int = 0
    in_flight: dict[str, int] = field(default_factory=dict)  # tx hash -> reserved worst case


class Safety:
    def __init__(self, db: Database, bus: EventBus) -> None:
        self._db = db
        self._bus = bus
        self._lock = threading.RLock()
        self._stopped: bool = bool(db.get_setting("stopped", False))
        self._on_stop: list[Callable[[], None]] = []

    # ----- STOP ------------------------------------------------------------------------
    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def on_stop(self, callback: Callable[[], None]) -> None:
        self._on_stop.append(callback)

    def stop(self, reason: str = "STOP pressed") -> None:
        with self._lock:
            self._stopped = True
            self._db.set_setting("stopped", True)
            for callback in self._on_stop:
                callback()
        self._bus.publish("safety", stopped=True, reason=reason)

    def resume(self, confirmation: str) -> None:
        if confirmation.strip().upper() != "RESUME":
            raise ValueError('type RESUME to confirm')
        with self._lock:
            self._stopped = False
            self._db.set_setting("stopped", False)
        self._bus.publish("safety", stopped=False, reason="resumed by user")

    # ----- limits ----------------------------------------------------------------------
    def limits_text(self) -> dict[str, str]:
        saved = self._db.get_setting("limits", {}) or {}
        return {k: str(saved.get(k, v)) for k, v in DEFAULT_LIMITS.items()}

    def limits_wei(self) -> dict[str, int]:
        return {k: to_wei(v) for k, v in self.limits_text().items()}

    def set_limits(self, values: dict[str, str]) -> dict[str, str]:
        current = self.limits_text()
        for key, value in values.items():
            if key not in DEFAULT_LIMITS:
                raise ValueError(f"unknown limit {key}")
            to_wei(value)  # validate
            current[key] = str(value).strip()
        self._db.set_setting("limits", current)
        return current

    def daily_spent_wei(self, chain_id: int, extra_in_flight: int = 0) -> int:
        """Spend in the last 24h: actual cost where known, else the reserved worst case."""
        since = time.time() - 86_400
        rows = self._db.all(
            "SELECT status, value_wei, max_cost_wei, gas_cost_wei FROM history "
            "WHERE chain_id = ? AND created_at >= ? AND tx_hash IS NOT NULL",
            (chain_id, since),
        )
        total = 0
        for r in rows:
            if r["gas_cost_wei"] is not None:
                total += int(r["gas_cost_wei"])
                if r["status"] == "confirmed" and r["value_wei"]:
                    total += int(r["value_wei"])
            elif r["max_cost_wei"]:
                total += int(r["max_cost_wei"])
        return total + extra_in_flight

    def per_tx_problems(self, value_wei: int, gas_cost_wei: int) -> list[str]:
        limits = self.limits_wei()
        problems = []
        if value_wei > limits["max_value_per_tx"]:
            problems.append(f"mint value {from_wei(value_wei)} exceeds the "
                            f"{LIMIT_LABELS['max_value_per_tx']} ({from_wei(limits['max_value_per_tx'])})")
        if gas_cost_wei > limits["max_gas_cost_per_tx"]:
            problems.append(f"max gas cost {from_wei(gas_cost_wei)} exceeds the "
                            f"{LIMIT_LABELS['max_gas_cost_per_tx']} "
                            f"({from_wei(limits['max_gas_cost_per_tx'])})")
        return problems

    # ----- gate ------------------------------------------------------------------------
    def make_gate(self, budget: OperationBudget, wallet: str,
                  on_pass: Callable[[PreBroadcastCheck], None],
                  is_cancelled: Callable[[], bool]) -> Callable[[PreBroadcastCheck], None]:
        def gate(check: PreBroadcastCheck) -> None:
            with self._lock:
                if self._stopped:
                    raise GateBlocked("emergency STOP is active; transaction was not sent")
                if is_cancelled():
                    raise GateBlocked("wallet run was cancelled; transaction was not sent")
                if check.chain_id != budget.chain_id:
                    raise GateBlocked("transaction chain does not match the operation")
                if check.wallet.lower() != wallet.lower():
                    raise GateBlocked("transaction wallet does not match the run")
                gas_cost = check.gas_limit * check.max_fee_per_gas
                problems = self.per_tx_problems(check.value_wei, gas_cost)
                worst = check.worst_case_cost_wei
                if budget.reserved_wei + worst > budget.cap_wei:
                    problems.append(
                        f"operation total would reach {from_wei(budget.reserved_wei + worst)}, "
                        f"over the operation limit {from_wei(budget.cap_wei)}")
                daily_limit = self.limits_wei()["max_daily"]
                daily = self.daily_spent_wei(budget.chain_id)
                if daily + worst > daily_limit:
                    problems.append(f"24h spend would reach {from_wei(daily + worst)}, over the "
                                    f"daily limit {from_wei(daily_limit)}")
                if problems:
                    raise GateBlocked("blocked by spending limit: " + "; ".join(problems))
                budget.reserved_wei += worst
                budget.in_flight[check.transaction_hash] = worst
                # on_pass records the hash and marks the run as broadcasting while still
                # holding the lock, so a concurrent STOP cannot cancel it mid-broadcast.
                on_pass(check)
        return gate
