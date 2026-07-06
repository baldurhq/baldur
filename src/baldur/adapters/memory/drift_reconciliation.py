"""
Drift Reconciliation Module

Resolves state drift between L1 and L2 caches when L2 recovers.
Recovery uses the "Most Restrictive Wins" strategy for safety-first behavior.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger()


class DriftReconciliationResult(str, Enum):
    """Drift reconciliation outcome."""

    L1_WINS = "l1_wins"  # L1 state is more restrictive → propagate to L2
    L2_WINS = "l2_wins"  # L2 state is more restrictive → propagate to L1
    TIMESTAMP_L1 = "timestamp_l1"  # Same state, L1 is newer
    TIMESTAMP_L2 = "timestamp_l2"  # Same state, L2 is newer
    # 476 D7: HALF_OPEN-XOR resolution. Exactly one side is HALF_OPEN —
    # resolve by timestamp instead of "Most Restrictive Wins" because
    # post-476 L2-Lua-driven OPEN→HALF_OPEN transitions would otherwise
    # be reverted to OPEN by stale L1 priority.
    TIMESTAMP_HALF_OPEN_L1 = "timestamp_half_open_l1"
    TIMESTAMP_HALF_OPEN_L2 = "timestamp_half_open_l2"
    NO_DRIFT = "no_drift"  # No drift (states are identical)
    SKIPPED = "skipped"  # Skipped (e.g., no data)


@dataclass
class DriftReconciliationRecord:
    """
    Drift reconciliation record.

    Captures the resolution of an L1/L2 state mismatch after L2 recovery.
    """

    service_name: str
    l1_state: str
    l2_state: str
    l1_updated_at: datetime | None
    l2_updated_at: datetime | None
    winner: str  # "l1", "l2", or "both" (identical)
    result: DriftReconciliationResult
    reconciled_at: datetime = field(default_factory=lambda: utc_now())
    jitter_seconds: float = 0.0


class DriftReconciler:
    """
    Resolves state drift after L2 recovery.

    While L2 is unavailable only L1 is updated; once L2 recovers the two caches
    can disagree. This class resolves drift using a "Most Restrictive Wins"
    strategy.

    Priority: OPEN (3) > HALF_OPEN (2) > CLOSED (1)
    - More restrictive state wins (safety first)
    - On equal state the most recent timestamp wins

    Thundering herd mitigation:
    - On L2 recovery every pod could rush to write at the same time and
      overload L2.
    - Apply jitter so writes are spread out over time.
    """

    # State priority: higher means more restrictive
    STATE_PRIORITY: dict[str, int] = {
        "open": 3,  # Most restrictive (wins)
        "half_open": 2,
        "closed": 1,  # Most permissive
    }

    def __init__(
        self,
        min_jitter_seconds: float = 0.0,
        max_jitter_seconds: float = 5.0,
        on_reconciled: Callable[[DriftReconciliationRecord], None] | None = None,
    ):
        """
        Args:
            min_jitter_seconds: minimum jitter (seconds)
            max_jitter_seconds: maximum jitter (seconds)
            on_reconciled: callback fired after reconciliation (metrics, logging, etc.)
        """
        self._min_jitter = min_jitter_seconds
        self._max_jitter = max_jitter_seconds
        self._on_reconciled = on_reconciled
        self._reconciliation_history: list[DriftReconciliationRecord] = []
        self._lock = threading.RLock()
        self._max_history = 1000

    def get_jitter(self) -> float:
        """Generate a jitter value between 0 and max."""
        return random.uniform(self._min_jitter, self._max_jitter)

    def reconcile(
        self,
        service_name: str,
        l1_state: str,
        l2_state: str,
        l1_updated_at: datetime | None = None,
        l2_updated_at: datetime | None = None,
    ) -> tuple[str, DriftReconciliationResult]:
        """
        Drift resolution strategy:
        1. More restrictive state wins (Most Restrictive Wins)
        2. On equal level, the most recent timestamp wins

        Args:
            service_name: service name
            l1_state: L1 state (closed, half_open, open)
            l2_state: L2 state
            l1_updated_at: L1 last update time
            l2_updated_at: L2 last update time

        Returns:
            (winning state, reconciliation result)
        """
        l1_priority = self.STATE_PRIORITY.get(l1_state.lower(), 0)
        l2_priority = self.STATE_PRIORITY.get(l2_state.lower(), 0)

        l1_is_half_open = l1_state.lower() == "half_open"
        l2_is_half_open = l2_state.lower() == "half_open"

        # No drift if states are equal
        if l1_state.lower() == l2_state.lower():
            winner_state = l1_state
            result = DriftReconciliationResult.NO_DRIFT
            winner = "both"
        elif l1_is_half_open ^ l2_is_half_open:
            # 476 D7: HALF_OPEN-XOR exception. Exactly one side is HALF_OPEN —
            # resolve by timestamp (newer wins) rather than "Most Restrictive
            # Wins". HALF_OPEN is a deliberate permission window that L2
            # owns post-476 via the atomic Lua acquire path; the priority
            # rule designed for L1-authoritative state would otherwise
            # reverse legitimate transitions on L2-recovery drift.
            winner_state, result, winner = self._resolve_half_open_xor(
                l1_state, l2_state, l1_updated_at, l2_updated_at
            )
            logger.info(
                "drift_reconciler.half_open_xor_resolved",
                service_name=service_name,
                l1_state=l1_state.upper(),
                l2_state=l2_state.upper(),
                winner=winner,
            )
        elif l1_priority > l2_priority:
            # L1 is more restrictive → propagate to L2
            winner_state = l1_state
            result = DriftReconciliationResult.L1_WINS
            winner = "l1"
            logger.info(
                "drift_reconciler.reconciled_wins_over_more",
                service_name=service_name,
                l1_state=l1_state.upper(),
                l2_state=l2_state.upper(),
            )
        elif l2_priority > l1_priority:
            # L2 is more restrictive → propagate to L1
            winner_state = l2_state
            result = DriftReconciliationResult.L2_WINS
            winner = "l2"
            logger.info(
                "drift_reconciler.reconciled_wins_over_more",
                service_name=service_name,
                l2_state=l2_state.upper(),
                l1_state=l1_state.upper(),
            )
        else:
            # Same level: compare timestamps
            winner_state, result, winner = self._resolve_by_timestamp(
                l1_state, l2_state, l1_updated_at, l2_updated_at
            )

        # Persist the record
        record = DriftReconciliationRecord(
            service_name=service_name,
            l1_state=l1_state,
            l2_state=l2_state,
            l1_updated_at=l1_updated_at,
            l2_updated_at=l2_updated_at,
            winner=winner,
            result=result,
        )

        with self._lock:
            self._reconciliation_history.append(record)
            if len(self._reconciliation_history) > self._max_history:
                self._reconciliation_history = self._reconciliation_history[
                    -self._max_history :
                ]

        # Run the callback
        if self._on_reconciled:
            try:
                self._on_reconciled(record)
            except Exception as e:
                logger.warning(
                    "drift_reconciler.callback_failed",
                    error=e,
                )

        return winner_state, result

    def _resolve_by_timestamp(
        self,
        l1_state: str,
        l2_state: str,
        l1_updated_at: datetime | None,
        l2_updated_at: datetime | None,
    ) -> tuple[str, DriftReconciliationResult, str]:
        """Pick the winner based on timestamps."""
        if l1_updated_at and l2_updated_at:
            if l1_updated_at > l2_updated_at:
                return l1_state, DriftReconciliationResult.TIMESTAMP_L1, "l1"
            return l2_state, DriftReconciliationResult.TIMESTAMP_L2, "l2"
        if l1_updated_at:
            return l1_state, DriftReconciliationResult.TIMESTAMP_L1, "l1"
        if l2_updated_at:
            return l2_state, DriftReconciliationResult.TIMESTAMP_L2, "l2"
        # No timestamps → trust L1 (local data)
        return l1_state, DriftReconciliationResult.TIMESTAMP_L1, "l1"

    def _resolve_half_open_xor(
        self,
        l1_state: str,
        l2_state: str,
        l1_updated_at: datetime | None,
        l2_updated_at: datetime | None,
    ) -> tuple[str, DriftReconciliationResult, str]:
        """Resolve a HALF_OPEN-XOR drift by timestamp.

        Returns the newer side. When both timestamps are missing, trust the
        HALF_OPEN side (the deliberate permission window) over the
        priority-based fallback to honor the post-476 L2-authoritative
        transition contract.
        """
        if l1_updated_at and l2_updated_at:
            if l1_updated_at > l2_updated_at:
                return (
                    l1_state,
                    DriftReconciliationResult.TIMESTAMP_HALF_OPEN_L1,
                    "l1",
                )
            return (
                l2_state,
                DriftReconciliationResult.TIMESTAMP_HALF_OPEN_L2,
                "l2",
            )
        if l1_updated_at:
            return l1_state, DriftReconciliationResult.TIMESTAMP_HALF_OPEN_L1, "l1"
        if l2_updated_at:
            return l2_state, DriftReconciliationResult.TIMESTAMP_HALF_OPEN_L2, "l2"

        # No timestamps — prefer the HALF_OPEN side (deliberate permission
        # window) so we don't lose the L2-Lua-driven transition.
        if l2_state.lower() == "half_open":
            return l2_state, DriftReconciliationResult.TIMESTAMP_HALF_OPEN_L2, "l2"
        return l1_state, DriftReconciliationResult.TIMESTAMP_HALF_OPEN_L1, "l1"

    def schedule_reconciliation_sync(
        self,
        service_name: str,
        do_reconcile: Callable[[], None],
    ) -> float:
        """
        Apply jitter, sleep synchronously, then run reconciliation.

        Args:
            service_name: service name
            do_reconcile: function that performs the actual reconciliation

        Returns:
            applied jitter (seconds)
        """
        jitter = self.get_jitter()

        if jitter > 0:
            logger.debug(
                "drift_reconciler.scheduling_reconciliation_jitter_applied",
                service_name=service_name,
                jitter=jitter,
            )
            time.sleep(jitter)

        do_reconcile()
        return jitter

    async def schedule_reconciliation_async(
        self,
        service_name: str,
        do_reconcile: Callable[[], None],
    ) -> float:
        """
        Apply jitter, sleep asynchronously, then run reconciliation.

        Args:
            service_name: service name
            do_reconcile: function that performs the actual reconciliation

        Returns:
            applied jitter (seconds)
        """
        jitter = self.get_jitter()

        if jitter > 0:
            logger.info(
                "drift_reconciler.scheduling_reconciliation_jitter_applied",
                service_name=service_name,
                jitter=jitter,
            )
            await asyncio.sleep(jitter)

        do_reconcile()
        return jitter

    def get_history(self) -> list[DriftReconciliationRecord]:
        """Return the reconciliation history."""
        with self._lock:
            return list(self._reconciliation_history)

    def get_stats(self) -> dict[str, Any]:
        """Return reconciliation statistics."""
        with self._lock:
            history = list(self._reconciliation_history)

        if not history:
            return {
                "total_reconciliations": 0,
                "by_result": {},
                "by_winner": {},
                "affected_services": [],
            }

        by_result: dict[str, int] = {}
        by_winner: dict[str, int] = {}
        services = set()

        for record in history:
            result_name = record.result.value
            by_result[result_name] = by_result.get(result_name, 0) + 1
            by_winner[record.winner] = by_winner.get(record.winner, 0) + 1
            services.add(record.service_name)

        return {
            "total_reconciliations": len(history),
            "by_result": by_result,
            "by_winner": by_winner,
            "affected_services": list(services),
            "last_reconciliation": (
                history[-1].reconciled_at.isoformat() if history else None
            ),
        }

    def clear_history(self) -> None:
        """Clear history (for tests)."""
        with self._lock:
            self._reconciliation_history.clear()


# =============================================================================
# Module-level Singleton
# =============================================================================

_drift_reconciler: DriftReconciler | None = None
_drift_reconciler_lock = threading.Lock()


def get_drift_reconciler() -> DriftReconciler:
    """Get the singleton DriftReconciler instance."""
    global _drift_reconciler
    if _drift_reconciler is None:
        with _drift_reconciler_lock:
            if _drift_reconciler is None:
                _drift_reconciler = DriftReconciler()
    return _drift_reconciler
