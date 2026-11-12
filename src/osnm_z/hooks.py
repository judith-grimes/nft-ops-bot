"""nft-ops addition (not part of upstream osnm-z).

Two context-local hooks that let a host application run many mints in one process:

* ``LOG_SINK``: when set, osnm_z.logging sends each message to this callable instead of
  printing it, so every wallet's log lines can be routed to its own dashboard panel.
* ``PRE_BROADCAST_GATE``: when set, osnm_z.submission calls it synchronously immediately
  before a signed transaction is broadcast. Raising from it aborts the broadcast; nothing is
  sent. The host uses it to enforce the emergency STOP and spending limits at the last
  possible moment.

Both are ContextVars, so each asyncio task (one per wallet) carries its own values.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass

# (level, message, countdown_utc_ns) where level is info|success|warn|error|section|progress
LogSink = Callable[[str, str, int | None], None]


@dataclass(frozen=True, slots=True)
class PreBroadcastCheck:
    chain_id: int
    wallet: str
    target: str
    value_wei: int
    gas_limit: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int
    nonce: int
    transaction_hash: str

    @property
    def worst_case_cost_wei(self) -> int:
        return self.value_wei + self.gas_limit * self.max_fee_per_gas


LOG_SINK: ContextVar[LogSink | None] = ContextVar("osnm_z_log_sink", default=None)
PRE_BROADCAST_GATE: ContextVar[Callable[[PreBroadcastCheck], None] | None] = ContextVar(
    "osnm_z_pre_broadcast_gate", default=None
)


def emit(level: str, message: object, countdown_utc_ns: int | None = None) -> bool:
    """Send a log line to the active sink. Returns False when no sink is set."""
    sink = LOG_SINK.get()
    if sink is None:
        return False
    sink(level, str(message), countdown_utc_ns)
    return True
