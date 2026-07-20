"""
PreWarmer — orchestrator that signals pre-adjustments to existing modules
ahead of an event.

It implements no new logic; it only calls the public APIs of existing modules.
The Global Baseline pattern keeps steady-state original values in a single
snapshot, and adding/ending an event triggers a declarative re-evaluation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Protocol, runtime_checkable

import structlog

from baldur.services.capacity_reservation.event_calendar import (
    EffectiveMultipliers,
    EventCalendar,
    ScheduledEvent,
)
from baldur.settings.capacity_reservation import (
    CapacityReservationSettings,
    get_capacity_reservation_settings,
)

logger = structlog.get_logger()

STATE_KEY_GLOBAL_BASELINE = "capacity_reservation:global_baseline"

POOL_EXPANSION_BASE_CONNECTIONS = 10
"""Base connections added per 1x of pool_multiplier. e.g. 1.5x -> 15, 3.0x -> 30."""


@runtime_checkable
class SafetyValveMetricsProvider(Protocol):
    """Provider of the metrics the Safety Valve decision needs."""

    def get_cpu_usage(self) -> float:
        """Current CPU usage (0.0 ~ 1.0)."""
        ...

    def get_error_rate(self) -> float:
        """Current error rate (0.0 ~ 1.0)."""
        ...


@dataclass
class AdjustmentRecord:
    """A single adjustment record."""

    target: str
    original_value: Any
    adjusted_value: Any
    applied: bool = False


@dataclass
class WarmUpResult:
    """Warm-up execution result."""

    event_id: str
    success: bool
    adjustments: list[AdjustmentRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0


@dataclass
class CoolDownResult:
    """Cool-down execution result."""

    event_id: str
    success: bool
    restored: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0


class PreWarmer:
    """Orchestrator that signals pre-adjustments to existing modules."""

    def __init__(
        self,
        calendar: EventCalendar,
        rate_controller: Any | None = None,
        pool_watchdog: Any | None = None,
        bulkhead: Any | None = None,
        graceful_degradation: Any | None = None,
        event_bus: Any | None = None,
        metrics_provider: SafetyValveMetricsProvider | None = None,
        recovery_gate: Any | None = None,
        state_backend: Any | None = None,
        settings: CapacityReservationSettings | None = None,
    ) -> None:
        self._calendar = calendar
        self._rate_controller = rate_controller
        self._pool_watchdog = pool_watchdog
        self._bulkhead = bulkhead
        self._graceful_degradation = graceful_degradation
        self._event_bus = event_bus
        self._metrics_provider = metrics_provider
        self._recovery_gate = recovery_gate
        self._state_backend = state_backend
        self._settings = settings or get_capacity_reservation_settings()
        self._lock = Lock()
        self._global_baseline: dict[str, Any] | None = None
        self._safety_valve_activated_at: float | None = None
        self._current_multipliers: EffectiveMultipliers | None = None

    def initialize(self) -> None:
        """Detect and restore an orphaned baseline on system startup."""
        if not self._state_backend:
            return

        try:
            saved_baseline = self._state_backend.get(STATE_KEY_GLOBAL_BASELINE)
            active_events = self._calendar.get_active()

            if saved_baseline and not active_events:
                self._restore_from(saved_baseline)
                self._state_backend.delete(STATE_KEY_GLOBAL_BASELINE)
                logger.warning("capacity_reservation.orphan_baseline_restored")
            elif saved_baseline and active_events:
                self._global_baseline = saved_baseline
                self._reconcile_settings()
                logger.info(
                    "capacity_reservation.baseline_resumed",
                    active_event_count=len(active_events),
                )
        except Exception as exc:
            logger.exception(
                "capacity_reservation.prewarmer_init_failed",
                error=str(exc),
            )

    def warm_up(self, event: ScheduledEvent) -> WarmUpResult:
        """Called N minutes before an event starts. Temporarily adjusts settings."""
        start = time.monotonic()
        adjustments: list[AdjustmentRecord] = []
        errors: list[str] = []

        if self._settings.dry_run:
            logger.info(
                "capacity_reservation.warmup_dry_run",
                event_id=event.event_id,
                name=event.name,
                expected_rps_multiplier=event.expected_rps_multiplier,
                pool_multiplier=event.pool_multiplier,
                bulkhead_extra_permits=event.bulkhead_extra_permits,
                suppress_degradation=event.suppress_degradation,
            )
            return WarmUpResult(
                event_id=event.event_id,
                success=True,
                adjustments=[],
                duration_seconds=time.monotonic() - start,
            )

        with self._lock:
            if self._global_baseline is None:
                self._global_baseline = self._capture_current_settings()
                self._persist_baseline()

        try:
            self._reconcile_settings(adjustments, errors)
            self._expand_pool(event, adjustments, errors)
            self._publish_event_started(event, errors)
        except Exception as exc:
            errors.append(f"warm_up unexpected error: {exc}")
            logger.exception(
                "capacity_reservation.warmup_error",
                event_id=event.event_id,
                error=str(exc),
            )

        success = len(errors) == 0
        duration = time.monotonic() - start

        if not success:
            self._rollback_all(adjustments)

        logger.info(
            "capacity_reservation.warmup_completed",
            event_id=event.event_id,
            success=success,
            adjustment_count=len([a for a in adjustments if a.applied]),
            error_count=len(errors),
            duration_seconds=duration,
        )

        return WarmUpResult(
            event_id=event.event_id,
            success=success,
            adjustments=adjustments,
            errors=errors,
            duration_seconds=duration,
        )

    def cool_down(self, event: ScheduledEvent) -> CoolDownResult:
        """
        Called when an event ends.
        Performs a declarative re-evaluation rather than a per-event revert.
        """
        start = time.monotonic()
        restored: list[str] = []
        errors: list[str] = []

        if self._settings.dry_run:
            logger.info(
                "capacity_reservation.cooldown_dry_run",
                event_id=event.event_id,
            )
            return CoolDownResult(
                event_id=event.event_id,
                success=True,
                duration_seconds=time.monotonic() - start,
            )

        remaining = self._calendar.get_active()
        remaining = [e for e in remaining if e.event_id != event.event_id]

        if not remaining:
            self._restore_from_baseline(restored, errors)
            with self._lock:
                self._global_baseline = None
                self._current_multipliers = None
            if self._state_backend:
                try:
                    self._state_backend.delete(STATE_KEY_GLOBAL_BASELINE)
                except Exception as exc:
                    errors.append(f"baseline cleanup failed: {exc}")
        else:
            adjustments: list[AdjustmentRecord] = []
            self._reconcile_settings(adjustments, errors)
            restored = [a.target for a in adjustments if a.applied]

        self._publish_event_ended(event, errors)

        success = len(errors) == 0
        duration = time.monotonic() - start

        logger.info(
            "capacity_reservation.cooldown_completed",
            event_id=event.event_id,
            success=success,
            restored=restored,
            remaining_events=len(remaining),
            error_count=len(errors),
            duration_seconds=duration,
        )

        return CoolDownResult(
            event_id=event.event_id,
            success=success,
            restored=restored,
            errors=errors,
            duration_seconds=duration,
        )

    def get_active_adjustments(self) -> dict[str, Any]:
        """Currently applied adjustment state."""
        with self._lock:
            result: dict[str, Any] = {}
            if self._global_baseline is not None:
                result["global_baseline"] = dict(self._global_baseline)
            if self._current_multipliers is not None:
                result["effective_multipliers"] = {
                    "rate_multiplier": self._current_multipliers.rate_multiplier,
                    "pool_multiplier": self._current_multipliers.pool_multiplier,
                    "bulkhead_extra_permits": self._current_multipliers.bulkhead_extra_permits,
                    "suppress_degradation": self._current_multipliers.suppress_degradation,
                }
            if self._safety_valve_activated_at is not None:
                result["safety_valve_active"] = True
            return result

    # ─── Safety Valve ─────────────────────────────────────────────────────────

    def check_safety_valve(self) -> bool:
        """True when a hard limit is exceeded. Called by the scheduler each cycle."""
        if self._metrics_provider is None:
            return False
        try:
            cpu = self._metrics_provider.get_cpu_usage()
            error_rate = self._metrics_provider.get_error_rate()
            return (
                cpu > self._settings.safety_valve_cpu_threshold
                or error_rate > self._settings.safety_valve_error_rate_threshold
            )
        except Exception as exc:
            logger.exception(
                "capacity_reservation.safety_valve_check_error",
                error=str(exc),
            )
            return False

    def emergency_override(self) -> None:
        """Safety Valve fired — immediately exit event mode and go CRITICAL.

        Under ``dry_run`` (the default) this only logs and skips the real
        CRITICAL transition, consistent with the setting's "only logs without
        applying" contract; organic overload is already handled by the
        backpressure system.
        """
        if self._settings.dry_run:
            logger.warning(
                "capacity_reservation.safety_valve_dry_run",
                cpu_threshold=self._settings.safety_valve_cpu_threshold,
                error_rate_threshold=(self._settings.safety_valve_error_rate_threshold),
            )
            return

        if self._graceful_degradation is not None:
            try:
                from baldur.settings.backpressure import BackpressureLevel

                self._graceful_degradation.update_level(BackpressureLevel.CRITICAL)
            except Exception as exc:
                logger.exception(
                    "capacity_reservation.emergency_override_failed",
                    error=str(exc),
                )

        self._safety_valve_activated_at = time.monotonic()
        logger.warning("capacity_reservation.safety_valve_activated")

    def check_safety_valve_recovery(self) -> bool:
        """Return to event mode once min_hold_seconds has elapsed and it is safe."""
        if self._safety_valve_activated_at is None:
            return False

        elapsed = time.monotonic() - self._safety_valve_activated_at
        if elapsed < self._settings.safety_valve_min_hold_seconds:
            return False

        if self._recovery_gate is not None:
            try:
                allowed, _ = self._recovery_gate.check_recovery_allowed()
                if not allowed:
                    return False
            except Exception:
                return False

        if self.check_safety_valve():
            return False

        self._safety_valve_activated_at = None
        self._reconcile_settings()
        logger.info("capacity_reservation.safety_valve_recovered")
        return True

    @property
    def safety_valve_active(self) -> bool:
        return self._safety_valve_activated_at is not None

    # ─── Reconciliation (declarative re-evaluation) ──────────────────────────

    def _reconcile_settings(
        self,
        adjustments: list[AdjustmentRecord] | None = None,
        errors: list[str] | None = None,
    ) -> None:
        """Recompute settings from the active events (reconciliation loop)."""
        if adjustments is None:
            adjustments = []
        if errors is None:
            errors = []

        effective = self._calendar.get_effective_multipliers()
        with self._lock:
            self._current_multipliers = effective

        if not effective.source_event_ids:
            return

        baseline = self._global_baseline
        if baseline is None:
            return

        self._apply_rate_controller(baseline, effective, adjustments, errors)
        self._apply_bulkhead(baseline, effective, adjustments, errors)
        self._apply_degradation(effective, adjustments, errors)

    def _apply_rate_controller(
        self,
        baseline: dict[str, Any],
        effective: EffectiveMultipliers,
        adjustments: list[AdjustmentRecord],
        errors: list[str],
    ) -> None:
        if self._rate_controller is None:
            return
        try:
            original_min_rate = baseline.get("min_rate_per_second")
            if original_min_rate is None:
                return
            new_min_rate = original_min_rate * effective.rate_multiplier
            self._rate_controller._settings.min_rate_per_second = new_min_rate
            adjustments.append(
                AdjustmentRecord(
                    target="rate_controller.min_rate_per_second",
                    original_value=original_min_rate,
                    adjusted_value=new_min_rate,
                    applied=True,
                )
            )
        except Exception as exc:
            errors.append(f"rate_controller adjustment failed: {exc}")

    def _apply_bulkhead(
        self,
        baseline: dict[str, Any],
        effective: EffectiveMultipliers,
        adjustments: list[AdjustmentRecord],
        errors: list[str],
    ) -> None:
        if self._bulkhead is None:
            return
        try:
            original_max = baseline.get("bulkhead_max_concurrent")
            if original_max is None:
                return
            new_max = original_max + effective.bulkhead_extra_permits
            self._bulkhead._state.max_concurrent = new_max
            adjustments.append(
                AdjustmentRecord(
                    target="bulkhead.max_concurrent",
                    original_value=original_max,
                    adjusted_value=new_max,
                    applied=True,
                )
            )
        except Exception as exc:
            errors.append(f"bulkhead adjustment failed: {exc}")

    def _apply_degradation(
        self,
        effective: EffectiveMultipliers,
        adjustments: list[AdjustmentRecord],
        errors: list[str],
    ) -> None:
        if self._graceful_degradation is None:
            return
        if not effective.suppress_degradation:
            return
        try:
            from baldur.settings.backpressure import BackpressureLevel

            self._graceful_degradation.update_level(BackpressureLevel.NONE)
            adjustments.append(
                AdjustmentRecord(
                    target="graceful_degradation.level",
                    original_value="auto",
                    adjusted_value=BackpressureLevel.NONE.value,
                    applied=True,
                )
            )
        except Exception as exc:
            errors.append(f"graceful_degradation adjustment failed: {exc}")

    # ─── Pool Expansion ──────────────────────────────────────────────────────

    def _expand_pool(
        self,
        event: ScheduledEvent,
        adjustments: list[AdjustmentRecord],
        errors: list[str],
    ) -> None:
        if self._pool_watchdog is None:
            return
        try:
            capped_multiplier = min(
                event.pool_multiplier,
                self._settings.max_pool_multiplier,
            )
            additional = max(
                1, int(capped_multiplier * POOL_EXPANSION_BASE_CONNECTIONS)
            )

            handler = getattr(self._pool_watchdog, "_recovery_handler", None)
            if handler is not None:
                result = handler.expand_pool(additional)
                adjustments.append(
                    AdjustmentRecord(
                        target="pool_watchdog.expand_pool",
                        original_value=None,
                        adjusted_value=additional,
                        applied=bool(result),
                    )
                )
                logger.info(
                    "capacity_reservation.pool_expanded",
                    event_id=event.event_id,
                    additional=additional,
                    result=result,
                )
        except Exception as exc:
            errors.append(f"pool_watchdog adjustment failed: {exc}")

    # ─── Global Baseline ─────────────────────────────────────────────────────

    def _capture_current_settings(self) -> dict[str, Any]:
        """Capture current module settings into one snapshot (Global Baseline)."""
        baseline: dict[str, Any] = {}

        if self._rate_controller is not None:
            try:
                baseline["min_rate_per_second"] = (
                    self._rate_controller._settings.min_rate_per_second
                )
            except Exception:
                pass

        if self._bulkhead is not None:
            try:
                state = self._bulkhead.get_state()
                baseline["bulkhead_max_concurrent"] = state.max_concurrent
            except Exception:
                pass

        if self._graceful_degradation is not None:
            try:
                baseline["graceful_degradation_suppressed"] = True
            except Exception:
                pass

        return baseline

    def _persist_baseline(self) -> None:
        """Save the Global Baseline to the StateBackend."""
        if not self._state_backend or not self._global_baseline:
            return
        try:
            max_horizon = self._calculate_max_event_horizon()
            ttl = max_horizon + 3600
            self._state_backend.set(
                STATE_KEY_GLOBAL_BASELINE,
                self._global_baseline,
                ttl_seconds=int(ttl),
            )
        except Exception as exc:
            logger.exception(
                "capacity_reservation.baseline_persist_failed",
                error=str(exc),
            )

    def _calculate_max_event_horizon(self) -> float:
        """Seconds until the latest-ending active event."""
        import datetime as dt

        active = self._calendar.get_active()
        if not active:
            return 3600.0
        now = dt.datetime.now(dt.UTC)
        max_end = max(e.end_time for e in active)
        return max(0.0, (max_end - now).total_seconds())

    def _restore_from_baseline(
        self,
        restored: list[str],
        errors: list[str],
    ) -> None:
        """Restore all settings from the Global Baseline."""
        with self._lock:
            baseline = self._global_baseline
        if baseline is None:
            return
        self._restore_from(baseline, restored, errors)

    def _restore_from(
        self,
        baseline: dict[str, Any],
        restored: list[str] | None = None,
        errors: list[str] | None = None,
    ) -> None:
        """Restore settings from the given baseline dict."""
        if restored is None:
            restored = []
        if errors is None:
            errors = []

        original_min_rate = baseline.get("min_rate_per_second")
        if original_min_rate is not None and self._rate_controller is not None:
            try:
                self._rate_controller._settings.min_rate_per_second = original_min_rate
                restored.append("rate_controller.min_rate_per_second")
            except Exception as exc:
                errors.append(f"rate_controller restore failed: {exc}")

        original_bulkhead = baseline.get("bulkhead_max_concurrent")
        if original_bulkhead is not None and self._bulkhead is not None:
            try:
                self._bulkhead._state.max_concurrent = original_bulkhead
                restored.append("bulkhead.max_concurrent")
            except Exception as exc:
                errors.append(f"bulkhead restore failed: {exc}")

        if self._graceful_degradation is not None and baseline.get(
            "graceful_degradation_suppressed"
        ):
            try:
                from baldur.settings.backpressure import BackpressureLevel

                self._graceful_degradation.update_level(BackpressureLevel.NONE)
                restored.append("graceful_degradation.level")
            except Exception as exc:
                errors.append(f"graceful_degradation restore failed: {exc}")

    # ─── EventBus ─────────────────────────────────────────────────────────────

    def _publish_event_started(
        self,
        event: ScheduledEvent,
        errors: list[str],
    ) -> None:
        if self._event_bus is None:
            return
        try:
            from baldur.services.event_bus.bus import EventType

            self._event_bus.emit(
                EventType.SCHEDULED_EVENT_STARTED,
                event.to_event_context(),
                source="capacity_reservation",
            )
        except Exception as exc:
            errors.append(f"EventBus publish STARTED failed: {exc}")

    def _publish_event_ended(
        self,
        event: ScheduledEvent,
        errors: list[str],
    ) -> None:
        if self._event_bus is None:
            return
        try:
            from baldur.services.event_bus.bus import EventType

            self._event_bus.emit(
                EventType.SCHEDULED_EVENT_ENDED,
                {"event_id": event.event_id},
                source="capacity_reservation",
            )
        except Exception as exc:
            errors.append(f"EventBus publish ENDED failed: {exc}")

    # ─── Rollback ─────────────────────────────────────────────────────────────

    def _rollback_all(self, adjustments: list[AdjustmentRecord]) -> None:
        """Roll back applied adjustments and restore the Global Baseline."""
        logger.warning(
            "capacity_reservation.rollback_started",
            adjustment_count=len([a for a in adjustments if a.applied]),
        )

        restored: list[str] = []
        errors: list[str] = []
        self._restore_from_baseline(restored, errors)

        for adj in adjustments:
            adj.applied = False

        with self._lock:
            self._global_baseline = None
            self._current_multipliers = None

        if self._state_backend:
            try:
                self._state_backend.delete(STATE_KEY_GLOBAL_BASELINE)
            except Exception:
                pass

        if errors:
            logger.error(
                "capacity_reservation.rollback_partial_failure",
                errors=errors,
            )
        else:
            logger.info(
                "capacity_reservation.rollback_completed",
                restored=restored,
            )
