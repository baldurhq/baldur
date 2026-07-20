"""
Cell evacuation policy — tick-based state machine + hysteresis.

An idempotent state-transition function called on every tick of
LeaderScheduler's aggregate_all() loop.

Architectural decisions:
- Hysteresis — consecutive counters (CellInfo.metadata) + bidirectional reset
  on state transition
- Tick-based state machine — no threading.Thread/time.sleep()
- Dropped bulkhead.max_concurrent = 0 — Hash Ring routing handles global
  blocking
- max_evacuated_ratio — hard limit that prevents cascading failure
- CellRegistry = SoT — Gate/Blast are notified fire-and-forget via Celery

Dependencies:
- CellRegistry: cell state management (SoT, control plane)
- RegionalIsolationGate: audit log / event emission (notification,
  fire-and-forget)
- BlastRadiusService: audit log (notification, fire-and-forget)
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from baldur.services.event_bus.bus.event_types import EventType
from baldur.services.event_bus.emitter import EventEmitterMixin
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.services.cell_topology.models import (  # noqa: F401
        CellInfo,
    )
    from baldur.services.cell_topology.models import (
        CellState as _CellState,
    )
    from baldur.services.cell_topology.registry import CellRegistry
    from baldur.settings.cell_topology import CellTopologySettings

logger = structlog.get_logger()


@dataclass
class EvacuationRecord:
    """Evacuation record."""

    cell_id: str
    trigger_health_score: float
    started_at: datetime = field(default_factory=lambda: utc_now())
    completed_at: datetime | None = None
    reason: str = ""
    affected_services: list[str] = field(default_factory=list)


class CellEvacuationPolicy(EventEmitterMixin):
    """
    Cell evacuation policy — tick-based state machine.

    evaluate() is called on every tick
    (health_check_interval_seconds=10s) of LeaderScheduler's aggregate_all()
    loop.

    Each call reads the cell's current state (CellState) and metadata, then
    decides the next state transition. It never creates a thread.

    Leader handoff safety: last_state_change and last_state_change_time are
    synced to L2 (Redis) (doc 388, Q6). The hysteresis counters
    (evacuation_below_count, recovery_above_count) are deliberately
    excluded — resetting them is the conservative, safe path.

    Toggle:
    - Only active when CellTopologySettings.evacuation_enabled=True
    - When False, evaluate() returns immediately

    Usage:
        policy = get_cell_evacuation_policy()
        for cell_id, info in registry.get_all_cells().items():
            policy.evaluate(cell_id, info.health_score)
    """

    _event_source = "cell_evacuation_policy"

    def __init__(self, settings: CellTopologySettings | None = None):
        from baldur.settings.cell_topology import get_cell_topology_settings

        self._settings = settings or get_cell_topology_settings()
        self._evacuation_history: deque[EvacuationRecord] = deque(
            maxlen=self._settings.evacuation_history_max_size,
        )

    # =========================================================================
    # evaluate() — state machine tick
    # =========================================================================

    def evaluate(self, cell_id: str, health_score: float) -> bool:
        """
        State machine tick — decides the cell's next transition on each call.

        Idempotent: same state with the same input yields the same result.
        Leader-handoff safe: every counter/timestamp is persisted in
        CellInfo.metadata.

        Args:
            cell_id: Cell identifier
            health_score: Current health score (0.0~1.0)

        Returns:
            Whether a state transition occurred
        """
        if not self._settings.enabled or not self._settings.evacuation_enabled:
            return False

        from baldur.services.cell_topology import get_cell_registry
        from baldur.services.cell_topology.models import CellState

        registry = get_cell_registry()
        cell = registry.get_cell_info(cell_id)
        if not cell:
            return False

        state = cell.state

        # WARMUP cells are not subject to evacuation evaluation
        if state == CellState.WARMUP:
            return False

        if state == CellState.ACTIVE:
            return self._tick_active(
                cell_id,
                health_score,
                cell,
                registry,
                CellState,
            )

        if state == CellState.DRAINING:
            return self._tick_draining(
                cell_id,
                cell,
                registry,
                CellState,
            )

        if state == CellState.ISOLATED:
            return self._tick_isolated(
                cell_id,
                health_score,
                cell,
                registry,
                CellState,
            )

        return False

    # =========================================================================
    # State: ACTIVE — hysteresis-based evacuation decision
    # =========================================================================

    def _tick_active(
        self,
        cell_id: str,
        health_score: float,
        cell: CellInfo,
        registry: CellRegistry,
        CellState: type[_CellState],
    ) -> bool:
        """
        Evaluate whether an ACTIVE cell needs to be evacuated.

        Hysteresis: transition to DRAINING only after
        evacuation_consecutive_count (default 3) consecutive ticks at or below
        the threshold. Exceeding the threshold resets below_count to 0
        immediately.
        """
        threshold = self._settings.evacuation_health_threshold

        if health_score <= threshold:
            # At or below the threshold — increment the counter
            below_count = cell.metadata.get("evacuation_below_count", 0) + 1
            cell.metadata["evacuation_below_count"] = below_count

            if below_count < self._settings.evacuation_consecutive_count:
                logger.debug(
                    "cell_evacuation.health_below_threshold",
                    cell_id=cell_id,
                    health_score=health_score,
                    threshold=threshold,
                    below_count=below_count,
                    required_count=self._settings.evacuation_consecutive_count,
                )
                return False

            # === Global evacuation limit — prevents cascading failure ===
            if not self._check_global_evacuation_limit(cell_id, registry):
                return False

            # === Consecutive count reached — transition to DRAINING ===
            reason = (
                f"Health score {health_score:.2f} <= {threshold} "
                f"for {below_count} consecutive ticks"
            )
            logger.warning(
                "cell_evacuation.draining_started",
                cell_id=cell_id,
                reason=reason,
            )

            # Reset both counters on transition (prevents ghost counters)
            cell.metadata["evacuation_below_count"] = 0
            cell.metadata["recovery_above_count"] = 0

            # Record the list of affected services
            cell.metadata["evacuation_affected_services"] = list(cell.assigned_services)
            cell.metadata["evacuation_trigger_score"] = health_score

            # SoT: CellRegistry state transition
            registry.set_cell_state(cell_id, CellState.DRAINING, reason)

            # Record the DRAINING transition time right away, so the drain
            # timer starts immediately instead of waiting for _tick_draining()
            # to record the time on its first tick
            cell.metadata["last_state_change_time"] = time.time()

            # Blocking new traffic is achieved by CellRegistry.get_cell_for_key()
            # automatically skipping DRAINING cells during Hash Ring traversal.
            logger.info(
                "cell_evacuation.draining_confirmed",
                cell_id=cell_id,
            )

            self._emit_event(
                EventType.CELL_EVACUATION_STARTED,
                {
                    "cell_id": cell_id,
                    "reason": reason,
                },
            )

            # Record the evacuation history entry
            self._evacuation_history.append(
                EvacuationRecord(
                    cell_id=cell_id,
                    trigger_health_score=health_score,
                    reason=reason,
                    affected_services=list(cell.assigned_services),
                )
            )
            return True
        # Above the threshold — reset the counter
        if cell.metadata.get("evacuation_below_count", 0) > 0:
            cell.metadata["evacuation_below_count"] = 0
        return False

    def _check_global_evacuation_limit(
        self,
        cell_id: str,
        registry: CellRegistry,
    ) -> bool:
        """
        Check whether the isolated ratio across all cells exceeds
        max_evacuated_ratio.

        Isolation beyond a certain ratio is refused to prevent cascading
        failure.

        Returns:
            True if evacuation may proceed, False if the limit is exceeded.
        """
        total_cells = len(registry.get_all_cells())
        if total_cells == 0:
            return False

        active_cells = len(registry.get_active_cells())
        evacuated_ratio = 1.0 - (active_cells / total_cells)

        if evacuated_ratio >= self._settings.max_evacuated_ratio:
            logger.critical(
                "cell_evacuation.global_limit_reached",
                cell_id=cell_id,
                evacuated_ratio=evacuated_ratio,
                max_evacuated_ratio=self._settings.max_evacuated_ratio,
                active_cells=active_cells,
                total_cells=total_cells,
            )
            return False

        return True

    # =========================================================================
    # State: DRAINING — transition to ISOLATED once the drain time elapses
    # =========================================================================

    def _tick_draining(
        self,
        cell_id: str,
        cell: CellInfo,
        registry: CellRegistry,
        CellState: type[_CellState],
    ) -> bool:
        """
        Check whether the drain period of a DRAINING cell has elapsed.

        Compares the timestamp in metadata['last_state_change'] against the
        current time, and transitions to ISOLATED once drain_seconds +
        grace_buffer have elapsed.

        Even across a leader handoff, the metadata synced to Redis L2 can be
        read to resume the pipeline safely.
        """
        last_change = cell.metadata.get("last_state_change", {})
        if last_change.get("to") != CellState.DRAINING.value:
            # Metadata missing or mismatched — DRAINING was cancelled externally
            logger.warning(
                "cell_evacuation.metadata_mismatch",
                cell_id=cell_id,
            )
            self._emit_event(
                EventType.CELL_EVACUATION_CANCELLED,
                {
                    "cell_id": cell_id,
                    "reason": "metadata_mismatch",
                },
            )
            return False

        # Elapsed-time check — time.time() + grace buffer
        drain_started = cell.metadata.get("last_state_change_time")
        if drain_started is None:
            # No timestamp recorded: record the current time and wait for the
            # next tick
            cell.metadata["last_state_change_time"] = time.time()
            return False

        elapsed = time.time() - drain_started
        required = (
            self._settings.evacuation_traffic_drain_seconds
            + self._settings.evacuation_drain_grace_seconds
        )

        if elapsed < required:
            logger.debug(
                "cell_evacuation.drain_timer_waiting",
                cell_id=cell_id,
                elapsed=elapsed,
                required=required,
            )
            return False

        # === Drain complete — transition to ISOLATED ===
        reason = f"Drain period elapsed ({elapsed:.1f}s >= {required:.1f}s)"
        logger.info(
            "cell_evacuation.drain_completed",
            cell_id=cell_id,
        )

        # Reset both counters
        cell.metadata["evacuation_below_count"] = 0
        cell.metadata["recovery_above_count"] = 0

        # SoT: CellRegistry state transition (runs first)
        registry.set_cell_state(cell_id, CellState.ISOLATED, reason)

        self._emit_event(
            EventType.CELL_EVACUATION_COMPLETED,
            {
                "cell_id": cell_id,
            },
        )

        # Fire-and-forget: audit log and event emission
        self._notify_isolation_gate(
            cell_id,
            reason,
            duration_seconds=self._settings.isolation_notification_duration_seconds,
        )
        self._notify_blast_radius(
            cell_id,
            cell.metadata.get("evacuation_affected_services", []),
        )

        # Log the service redistribution
        affected = cell.metadata.get("evacuation_affected_services", [])
        logger.info(
            "cell_topology.cell_isolated_services_redistributed",
            cell_id=cell_id,
            services_count=len(affected),
        )
        for svc in affected:
            new_cell = registry.get_cell_for_key(svc)
            logger.info(
                "cell_topology.service_redistributed",
                service=svc,
                from_cell=cell_id,
                to_cell=new_cell,
            )

        return True

    # =========================================================================
    # State: ISOLATED — hysteresis-based automatic recovery
    # =========================================================================

    def _tick_isolated(
        self,
        cell_id: str,
        health_score: float,
        cell: CellInfo,
        registry: CellRegistry,
        CellState: type[_CellState],
    ) -> bool:
        """
        Decide whether an ISOLATED cell can recover automatically.

        Hysteresis: recover to ACTIVE only after
        recovery_consecutive_count (default 5) consecutive ticks at or above
        recovery_health_threshold (default 0.7).
        Asymmetric by design: evacuation (3 ticks) is fast, recovery
        (5 ticks) is conservative.
        """
        recovery_threshold = self._settings.recovery_health_threshold

        if health_score >= recovery_threshold:
            above_count = cell.metadata.get("recovery_above_count", 0) + 1
            cell.metadata["recovery_above_count"] = above_count

            if above_count < self._settings.recovery_consecutive_count:
                logger.debug(
                    "cell_evacuation.health_above_threshold",
                    cell_id=cell_id,
                    health_score=health_score,
                    recovery_threshold=recovery_threshold,
                    above_count=above_count,
                    required_count=self._settings.recovery_consecutive_count,
                )
                return False

            # === Consecutive count reached — recover to ACTIVE ===
            reason = (
                f"Health score {health_score:.2f} >= {recovery_threshold} "
                f"for {above_count} consecutive ticks"
            )
            logger.info(
                "cell_evacuation.restoring_started",
                cell_id=cell_id,
                reason=reason,
            )

            # Reset both counters on transition
            cell.metadata["evacuation_below_count"] = 0
            cell.metadata["recovery_above_count"] = 0

            # SoT: CellRegistry state transition
            registry.set_cell_state(cell_id, CellState.ACTIVE, reason)

            # Fire-and-forget: audit log notification
            self._notify_restore_region(cell_id)

            # Mark the evacuation history entry complete (newest-first scan)
            self._complete_evacuation_record(cell_id)

            logger.info(
                "cell_evacuation.cell_restored",
                cell_id=cell_id,
            )
            self._emit_event(
                EventType.CELL_RESTORED,
                {
                    "cell_id": cell_id,
                    "trigger": "auto",
                },
            )
            return True
        # Below the threshold — reset the counter
        if cell.metadata.get("recovery_above_count", 0) > 0:
            cell.metadata["recovery_above_count"] = 0
        return False

    # =========================================================================
    # Manual recovery
    # =========================================================================

    def restore_cell(self, cell_id: str) -> bool:
        """
        Manually restore a cell.

        Ignores the automatic recovery hysteresis and switches to ACTIVE
        immediately. Used for administrator intervention.

        Args:
            cell_id: Cell identifier

        Returns:
            Whether the restoration succeeded
        """
        if not self._settings.enabled:
            return False

        try:
            from baldur.services.cell_topology import get_cell_registry
            from baldur.services.cell_topology.models import CellState

            registry = get_cell_registry()
            cell = registry.get_cell_info(cell_id)
            if not cell:
                return False

            old_state = cell.state

            # Reset both counters
            cell.metadata["evacuation_below_count"] = 0
            cell.metadata["recovery_above_count"] = 0

            # SoT: CellRegistry state transition
            registry.set_cell_state(cell_id, CellState.ACTIVE, "Manual restoration")

            # Manual restore during DRAINING -> evacuation cancelled event
            if old_state == CellState.DRAINING:
                self._emit_event(
                    EventType.CELL_EVACUATION_CANCELLED,
                    {
                        "cell_id": cell_id,
                        "reason": "manual_restore",
                    },
                )

            # Fire-and-forget: audit log notification
            self._notify_restore_region(cell_id)

            logger.info(
                "cell_evacuation.cell_restored",
                cell_id=cell_id,
            )
            self._emit_event(
                EventType.CELL_RESTORED,
                {
                    "cell_id": cell_id,
                    "trigger": "manual",
                },
            )
            return True

        except Exception as e:
            logger.exception(
                "cell.manual_restore_failed",
                cell_id=cell_id,
                error=e,
            )
            return False

    # =========================================================================
    # Fire-and-forget notification — Celery apply_async
    # =========================================================================

    def _notify_isolation_gate(
        self,
        cell_id: str,
        reason: str,
        *,
        duration_seconds: int = 3600,
    ) -> None:
        """Notify RegionalIsolationGate of isolation (fire-and-forget)."""
        try:
            from baldur.adapters.celery.tasks import (
                notify_cell_isolation,
            )

            notify_cell_isolation.apply_async(
                kwargs={
                    "cell_id": cell_id,
                    "reason": reason,
                    "duration_seconds": duration_seconds,
                },
            )
        except ImportError:
            # Celery not in use: synchronous fallback
            self._notify_isolation_gate_sync(
                cell_id,
                reason,
                duration_seconds=duration_seconds,
            )
        except Exception as e:
            logger.warning(
                "cell_policy.isolation_gate_notify_failed",
                cell_id=cell_id,
                method="async",
                error=e,
            )
            self._notify_isolation_gate_sync(
                cell_id,
                reason,
                duration_seconds=duration_seconds,
            )

    def _notify_isolation_gate_sync(
        self,
        cell_id: str,
        reason: str,
        *,
        duration_seconds: int = 3600,
    ) -> None:
        """RegionalIsolationGate synchronous fallback."""
        try:
            from baldur.services.isolation.regional_gate import (
                get_regional_isolation_gate,
            )

            gate = get_regional_isolation_gate()
            gate.isolate_region(
                region=cell_id,
                reason=reason,
                duration_seconds=duration_seconds,
            )
        except ImportError:
            logger.debug("cell_policy.region_isolation_gate_unavailable")
        except Exception as e:
            logger.warning(
                "cell_policy.isolation_gate_notify_failed",
                cell_id=cell_id,
                method="sync",
                error=e,
            )

    def _notify_blast_radius(self, cell_id: str, affected_services: list[str]) -> None:
        """Notify BlastRadiusService of the policy setting (fire-and-forget)."""
        try:
            from baldur.adapters.celery.tasks import (
                notify_cell_blast_radius,
            )

            notify_cell_blast_radius.apply_async(
                kwargs={
                    "cell_id": cell_id,
                    "affected_services": affected_services,
                },
            )
        except ImportError:
            self._notify_blast_radius_sync(cell_id, affected_services)
        except Exception as e:
            logger.warning(
                "cell_policy.blast_radius_notify_failed",
                cell_id=cell_id,
                method="async",
                error=e,
            )
            self._notify_blast_radius_sync(cell_id, affected_services)

    def _notify_blast_radius_sync(
        self, cell_id: str, affected_services: list[str]
    ) -> None:
        """BlastRadiusService synchronous fallback."""
        try:
            from baldur.services.blast_radius.models import (
                BlastRadiusLevel,
            )
            from baldur.services.blast_radius.service import (
                BlastRadiusService,
            )

            blast_service = BlastRadiusService()
            blast_service.set_policy(
                service_name=cell_id,
                level=BlastRadiusLevel.CRITICAL,
                affected_services=affected_services,
                max_affected_percentage=0.0,
                auto_isolate=True,
            )
        except ImportError:
            pass
        except Exception as e:
            logger.warning(
                "cell_policy.blast_radius_notify_failed",
                cell_id=cell_id,
                method="sync",
                error=e,
            )

    def _notify_restore_region(self, cell_id: str) -> None:
        """Notify RegionalIsolationGate of restoration (fire-and-forget)."""
        try:
            from baldur.adapters.celery.tasks import (
                notify_cell_restoration,
            )

            notify_cell_restoration.apply_async(
                kwargs={"cell_id": cell_id},
            )
        except ImportError:
            self._notify_restore_region_sync(cell_id)
        except Exception as e:
            logger.warning(
                "cell_policy.restore_region_notify_failed",
                cell_id=cell_id,
                method="async",
                error=e,
            )
            self._notify_restore_region_sync(cell_id)

    def _notify_restore_region_sync(self, cell_id: str) -> None:
        """RegionalIsolationGate restoration synchronous fallback."""
        try:
            from baldur.services.isolation.regional_gate import (
                get_regional_isolation_gate,
            )

            gate = get_regional_isolation_gate()
            gate.restore_region(cell_id)
        except ImportError:
            logger.debug("cell_policy.region_isolation_gate_unavailable")
        except Exception as e:
            logger.warning(
                "cell_policy.restore_region_notify_failed",
                cell_id=cell_id,
                method="sync",
                error=e,
            )

    # =========================================================================
    # Internal utilities
    # =========================================================================

    def _complete_evacuation_record(self, cell_id: str) -> None:
        """Mark the open evacuation record for this cell_id as complete."""
        for record in reversed(self._evacuation_history):
            if record.cell_id == cell_id and record.completed_at is None:
                record.completed_at = utc_now()
                break

    # =========================================================================
    # Queries
    # =========================================================================

    def get_evacuation_history(self) -> list[EvacuationRecord]:
        """Evacuation history."""
        return list(self._evacuation_history)


# =============================================================================
# Singleton
# =============================================================================

_policy: CellEvacuationPolicy | None = None
_policy_lock = threading.Lock()


def get_cell_evacuation_policy() -> CellEvacuationPolicy:
    """Return the CellEvacuationPolicy singleton."""
    global _policy
    if _policy is None:
        with _policy_lock:
            if _policy is None:
                _policy = CellEvacuationPolicy()
    return _policy


def reset_cell_evacuation_policy() -> None:
    """Reset the singleton (for tests)."""
    global _policy
    with _policy_lock:
        _policy = None
