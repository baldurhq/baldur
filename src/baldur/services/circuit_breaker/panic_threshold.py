"""
Panic Threshold for Circuit Breaker

When 70% or more Circuit Breakers are OPEN simultaneously, this is judged not as
an individual service problem but as a collapse of the entire infrastructure. At
that point the autonomous-operation engine declares Emergency Level 3 on its own
and halts all automatic recovery.

Two entry points, deliberately separated:

``evaluate()``
    The pure probe. Reads the cluster's states, computes the OPEN ratio and
    answers whether the threshold is met right now. No counter advances, no
    escalation happens, nothing is written. Safety pre-checks (the chaos guard)
    call this: a guard asking "may this experiment run?" needs the condition
    now, and must not move the state it is reading.

``tick()``
    The periodic step, called only by the scheduled job. It applies the
    consecutive-trigger hysteresis and, when the escalation policy allows,
    declares Emergency Level 3 through the emergency manager -- which is what
    makes the breakers hold their state (Freeze Mode), halts replay, and
    notifies operators.

Operation flow (tick):
    cluster states -> OPEN ratio -> exceeds threshold?
                                 | yes, N ticks running
                                 v
                         Emergency Level 3 declared
                                 v
                    Freeze Mode (breakers hold), replay halted,
                    operators notified, await manual intervention
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.helpers import log_panic_threshold_audit
from baldur.models.emergency import EmergencyLevel
from baldur.services.circuit_breaker.exceptions import (
    CircuitBreakerStateUnavailableError,
)
from baldur.services.circuit_breaker.models import PanicThresholdConfig
from baldur.utils.time import from_iso_string, utc_now

if TYPE_CHECKING:
    from datetime import datetime

    from baldur.interfaces.emergency import EmergencyManager as EmergencyModeManager
    from baldur.services.circuit_breaker import CircuitBreakerService

logger = structlog.get_logger()

__all__ = [
    "PanicThresholdMonitor",
    "PanicThresholdResult",
    "get_panic_threshold_monitor",
    "reset_panic_threshold_monitor",
    "check_panic_threshold",
    "is_panic_threshold_triggered",
]

# The level a confirmed fleet-wide collapse declares.
ESCALATION_LEVEL = EmergencyLevel.LEVEL_3


# =============================================================================
# Panic Threshold Result
# =============================================================================


@dataclass
class PanicThresholdResult:
    """
    Panic Threshold check result.

    Attributes:
        triggered: Whether the OPEN ratio meets the threshold right now
        open_rate: Current OPEN ratio (%)
        open_count: Number of CBs in the OPEN state
        total_count: Total number of registered CBs
        open_circuits: List of services in the OPEN state
        action_taken: Action taken (escalation lane only; None on a probe)
        reason: Reason for triggering / not triggering
        timestamp: Check time
    """

    triggered: bool = False
    open_rate: float = 0.0
    open_count: int = 0
    total_count: int = 0
    open_circuits: list[str] = field(default_factory=list)
    action_taken: str | None = None
    reason: str | None = None
    timestamp: str = field(default_factory=lambda: utc_now().isoformat())


# =============================================================================
# Panic Threshold Monitor
# =============================================================================


class PanicThresholdMonitor:
    """
    Monitors the system-wide OPEN ratio and declares Emergency Level 3.

    When 70% or more of all CBs are OPEN, the system is judged to be in total
    collapse. ``evaluate()`` reports that condition; ``tick()`` acts on it.

    Usage:
        monitor = get_panic_threshold_monitor()

        # Safety pre-check - reports, never acts
        if monitor.evaluate().triggered:
            block_experiment()

        # Periodic lane - hysteresis + escalation
        monitor.tick()
    """

    def __init__(
        self,
        config: PanicThresholdConfig | None = None,
        circuit_breaker_service: CircuitBreakerService | None = None,
        emergency_manager: EmergencyModeManager | None = None,
    ):
        """
        Initialize PanicThresholdMonitor.

        Args:
            config: Panic Threshold configuration (None to build from settings)
            circuit_breaker_service: CB service (uses the process singleton if
                not injected)
            emergency_manager: Emergency Manager (resolved from the registry
                if not injected)
        """
        self.config = config or _config_from_settings()
        self._cb_service = circuit_breaker_service
        self._emergency_manager = emergency_manager
        self._consecutive_triggers = 0
        self._last_result: PanicThresholdResult | None = None
        # The last moment a tick observed a level at or above the escalation
        # level. The cooldown after a freeze is measured from this, not from
        # the emergency state's own deactivation stamp -- see _in_cooldown().
        self._last_seen_level3_at: datetime | None = None

    @property
    def cb_service(self) -> CircuitBreakerService | None:
        """Resolve the Circuit Breaker Service through its process accessor."""
        if self._cb_service is None:
            from baldur.services.circuit_breaker.convenience import (
                get_circuit_breaker_service,
            )

            self._cb_service = get_circuit_breaker_service()
        return self._cb_service

    @property
    def emergency_manager(self) -> EmergencyModeManager | None:
        """Resolve the Emergency Manager, caching the first success."""
        if self._emergency_manager is None:
            from baldur.factory.registry import ProviderRegistry

            self._emergency_manager = ProviderRegistry.emergency_manager.safe_get()
            if self._emergency_manager is None:
                logger.debug("panic_threshold.escalation_unavailable")
        return self._emergency_manager

    # =========================================================================
    # Probe
    # =========================================================================

    def evaluate(self) -> PanicThresholdResult:
        """
        Report whether the system-wide OPEN ratio meets the threshold.

        Side-effect free with respect to every decision this monitor makes:
        the consecutive counter does not move, nothing escalates, and no
        emergency state is written. Only the observation cache is refreshed.

        Raises:
            CircuitBreakerStateUnavailableError: The cluster's states could not
                be read. Deliberately not folded into "not triggered" -- for
                the safety pre-check that consumes this, the safe direction is
                *block*, and an empty list would read as *allow*.

        Returns:
            PanicThresholdResult: the current verdict
        """
        open_circuits, total_circuits = self._get_cluster_stats()
        total = len(total_circuits)
        open_count = len(open_circuits)

        if total < self.config.min_registered_services:
            return self._remember(
                PanicThresholdResult(
                    triggered=False,
                    open_count=open_count,
                    total_count=total,
                    open_circuits=open_circuits,
                    reason=(
                        f"Insufficient services "
                        f"({total} < {self.config.min_registered_services})"
                    ),
                )
            )

        open_rate = (open_count / total) * 100
        triggered = open_rate >= self.config.threshold_percent

        return self._remember(
            PanicThresholdResult(
                triggered=triggered,
                open_rate=open_rate,
                open_count=open_count,
                total_count=total,
                open_circuits=open_circuits,
                reason=(
                    f"Panic Threshold met (Open Rate: {open_rate:.1f}%)"
                    if triggered
                    else f"Below threshold "
                    f"({open_rate:.1f}% < {self.config.threshold_percent}%)"
                ),
            )
        )

    def _get_cluster_stats(self) -> tuple[list[str], list[str]]:
        """
        Collect every Circuit state in the cluster.

        Reads the cluster-scoped repository method, not ``get_all_states()``:
        a system-wide verdict computed from one worker's local view under- or
        over-counts OPEN circuits by exactly the rows that worker has not
        refreshed.

        Returns:
            tuple[list[str], list[str]]: (OPEN services, all services)
        """
        service = self.cb_service
        if service is None:
            raise CircuitBreakerStateUnavailableError(
                "get_cluster_states", "circuit_breaker_service_unavailable"
            )

        all_states = service.repository.get_cluster_states()

        open_circuits = [
            s.service_name for s in all_states if s.state.lower() == "open"
        ]
        total_circuits = [s.service_name for s in all_states]
        return open_circuits, total_circuits

    def _remember(self, result: PanicThresholdResult) -> PanicThresholdResult:
        """Refresh the observation cache. Not a decision -- see ``evaluate``."""
        self._last_result = result
        return result

    # =========================================================================
    # Periodic escalation lane
    # =========================================================================

    def tick(self) -> PanicThresholdResult:
        """
        Advance the hysteresis and escalate when the policy allows.

        The scheduled job is this method's only caller. It is gated on the
        advanced-protection flag: automatic escalation belongs to that
        surface, and the probe above stays live regardless.

        Returns:
            PanicThresholdResult: the tick's verdict, with ``action_taken``
                set when an escalation was confirmed
        """
        settings = _advanced_settings()
        if settings is None:
            # Fail-closed: an unreadable flag is not permission to run. The
            # lane declares a fleet-wide emergency, and the config defaults it
            # would fall back to say "freeze" -- so an install that never
            # enabled the lane would escalate on the strength of a settings
            # error alone.
            return PanicThresholdResult(
                triggered=False, reason="advanced protection unreadable"
            )
        if not settings.enabled:
            return PanicThresholdResult(
                triggered=False, reason="advanced protection disabled"
            )
        self._refresh_config(settings)

        state = self._observe_emergency_level()

        try:
            result = self.evaluate()
        except CircuitBreakerStateUnavailableError as e:
            # The counter is deliberately left where it is: a transient store
            # failure is not evidence that the fleet recovered. Reported here
            # rather than re-raised so a store outage does not make the
            # scheduler log a traceback on every tick for its duration.
            logger.warning("panic_threshold.cluster_read_failed", reason=e.reason)
            return PanicThresholdResult(
                triggered=False, reason="cluster state unavailable"
            )

        if not result.triggered:
            self._consecutive_triggers = 0
            return result

        self._consecutive_triggers += 1
        if self._consecutive_triggers < self.config.consecutive_triggers_required:
            result.reason = (
                f"Threshold exceeded but waiting for consecutive triggers "
                f"({self._consecutive_triggers}/"
                f"{self.config.consecutive_triggers_required})"
            )
            return result

        if self.config.action != "freeze":
            logger.warning(
                "panic_threshold.alert_only",
                open_rate=result.open_rate,
                open_count=result.open_count,
                total_count=result.total_count,
            )
            result.action_taken = "alert_only"
            return result

        self._escalate(result, state)
        return result

    def _refresh_config(self, settings: Any) -> None:
        """Re-read the settings-backed config fields on every tick.

        ``enabled`` is read per tick, so the threshold and the action it is
        judged against must be too -- otherwise a runtime settings reset would
        take effect for one field and not the others.
        """
        self.config = PanicThresholdConfig(
            threshold_percent=settings.panic_threshold_percent,
            action=settings.panic_threshold_action,
            consecutive_triggers_required=self.config.consecutive_triggers_required,
            min_registered_services=self.config.min_registered_services,
        )

    def _observe_emergency_level(self) -> Any | None:
        """Read the emergency state once per tick, stamping an observed freeze.

        The cooldown is measured from the last freeze *this monitor* saw, so
        the observation belongs on every tick -- not only on the ticks that
        reach an escalation attempt. A freeze another subsystem or an operator
        declared holds the breakers just the same, and the cooldown it earns
        them has to survive a tick whose ratio dipped below the threshold.
        """
        manager = self.emergency_manager
        if manager is None:
            logger.debug("panic_threshold.escalation_unavailable")
            return None

        state = self._emergency_state(manager)
        if state is not None:
            self._stamp_observed_freeze(state)
        return state

    def _escalate(self, result: PanicThresholdResult, state: Any | None) -> None:
        """Declare Emergency Level 3, when the escalation policy allows it."""
        manager = self.emergency_manager
        if manager is None:
            logger.debug("panic_threshold.escalation_unavailable")
            return

        if state is None:
            # The policy below is what keeps this lane from fighting a
            # gradual recovery or re-freezing a fleet whose breakers have not
            # moved yet. None of it can be evaluated against a state that
            # could not be read, so the safe direction is to declare nothing.
            logger.warning(
                "panic_threshold.escalation_skipped", reason="state_unreadable"
            )
            return

        if not self._escalation_allowed(state):
            return

        new_state = manager.activate_auto(
            level=ESCALATION_LEVEL,
            reason=f"Panic Threshold: {result.open_rate:.1f}% of circuits are OPEN",
            duration_minutes=None,
        )

        # Confirmed, not assumed: activate_auto returns the *unchanged* state
        # when the kill switch is engaged, so the audit record and the CRITICAL
        # line are owed only when the level actually moved.
        level = getattr(new_state, "level", None)
        if not (isinstance(level, EmergencyLevel) and level >= ESCALATION_LEVEL):
            logger.warning(
                "panic_threshold.escalation_blocked",
                resulting_level=getattr(level, "value", None),
                open_rate=result.open_rate,
            )
            return

        result.action_taken = "emergency_level_3_escalation"
        log_panic_threshold_audit(
            open_rate=result.open_rate,
            threshold=self.config.threshold_percent,
            open_count=result.open_count,
            total_count=result.total_count,
            open_circuits=result.open_circuits,
            action_taken=result.action_taken,
        )
        logger.critical(
            "panic_threshold.triggered",
            open_rate=result.open_rate,
            open_count=result.open_count,
            total_count=result.total_count,
        )

    @staticmethod
    def _emergency_state(manager: EmergencyModeManager) -> Any | None:
        """Read the emergency state, or None when it cannot be read."""
        try:
            return manager.get_state()
        except Exception as e:
            logger.warning("panic_threshold.emergency_state_read_failed", error=str(e))
            return None

    def _stamp_observed_freeze(self, state: Any) -> None:
        """Record that this tick saw a level at or above the escalation level."""
        level = getattr(state, "level", None)
        if isinstance(level, EmergencyLevel) and level >= ESCALATION_LEVEL:
            self._last_seen_level3_at = utc_now()

    def _escalation_allowed(self, state: Any) -> bool:
        """Whether the current emergency state permits a fresh declaration.

        Three conditions, all of which must hold:

        - the level is below the escalation level (a lower level -- the
          corruption shield's, or an operator's -- does not suppress a
          fleet-wide collapse, which outranks it);
        - no gradual recovery is in progress (a walk stepping the level down
          is under the operator's control and must not be fought);
        - the last freeze this monitor observed is older than the emergency
          module's own stabilization period.
        """
        level = getattr(state, "level", None)
        if isinstance(level, EmergencyLevel) and level >= ESCALATION_LEVEL:
            return False

        if getattr(state, "is_recovering", False):
            logger.debug("panic_threshold.escalation_skipped", reason="recovering")
            return False

        if self._in_cooldown(state):
            logger.debug("panic_threshold.escalation_skipped", reason="cooldown")
            return False

        return True

    def _in_cooldown(self, state: Any) -> bool:
        """Whether the stabilization period since the last freeze is still running.

        Measured from the last freeze *this monitor observed*, not from the
        emergency state's ``deactivated_at``. The cooldown exists to give the
        breakers time to leave OPEN once the freeze lifts, and that need is
        the breakers', not the emergency state's: a LEVEL_1 arriving inside
        the window froze nothing, so it must not cut the window short, and
        ``activate_manual`` clears ``deactivated_at`` outright.

        ``deactivated_at`` is the fallback for exactly one case -- a process
        that has never observed a freeze (fresh boot or restart). Precision
        cost of a restart is at most the period itself, the same acceptance
        the hysteresis counter carries.
        """
        period = _stabilization_period_seconds()
        if period is None:
            return False

        reference = self._last_seen_level3_at
        if reference is None:
            reference = _parse_deactivated_at(state)
            if reference is None:
                return False

        return (utc_now() - reference).total_seconds() < period

    # =========================================================================
    # Observation cache
    # =========================================================================

    def get_last_result(self) -> PanicThresholdResult | None:
        """
        Return the last probe result.

        Returns:
            PanicThresholdResult | None: last observed result
        """
        return self._last_result

    def reset_consecutive_count(self) -> None:
        """Reset the consecutive-detection counter (for tests/debugging)."""
        self._consecutive_triggers = 0


# =============================================================================
# Settings-backed configuration
# =============================================================================


def _advanced_settings() -> Any | None:
    """Return the advanced-protection settings, or None when unreadable."""
    try:
        from baldur.settings.circuit_breaker_advanced import (
            get_circuit_breaker_advanced_settings,
        )

        return get_circuit_breaker_advanced_settings()
    except Exception as e:
        logger.warning("panic_threshold.settings_read_failed", error=str(e))
        return None


def _config_from_settings() -> PanicThresholdConfig:
    """Build the monitor config from the advanced-protection settings."""
    settings = _advanced_settings()
    if settings is None:
        return PanicThresholdConfig()
    return PanicThresholdConfig(
        threshold_percent=settings.panic_threshold_percent,
        action=settings.panic_threshold_action,
    )


def _stabilization_period_seconds() -> int | None:
    """Return the emergency module's stabilization period, or None if unreadable.

    Read from settings rather than from the manager's runtime
    ``RecoveryGateConfig``: that one is mutated per process, so the elected
    scheduler process would never see an operator's change to it anyway.
    """
    try:
        from baldur.settings.emergency_mode import get_emergency_mode_settings

        return get_emergency_mode_settings().stabilization_period_seconds
    except Exception as e:
        logger.debug("panic_threshold.stabilization_period_unavailable", error=str(e))
        return None


def _parse_deactivated_at(state: Any) -> datetime | None:
    """Parse the emergency state's ISO deactivation stamp, or None."""
    raw = getattr(state, "deactivated_at", None)
    if not raw:
        return None
    try:
        return from_iso_string(raw)
    except Exception:
        logger.debug("panic_threshold.deactivated_at_unparsable", value=str(raw))
        return None


# =============================================================================
# Convenience Functions
# =============================================================================


_monitor_instance: PanicThresholdMonitor | None = None
_monitor_instance_lock = threading.Lock()


def get_panic_threshold_monitor() -> PanicThresholdMonitor:
    """
    Return the global PanicThresholdMonitor instance.
    """
    global _monitor_instance
    if _monitor_instance is None:
        with _monitor_instance_lock:
            if _monitor_instance is None:
                _monitor_instance = PanicThresholdMonitor()
    return _monitor_instance


def reset_panic_threshold_monitor() -> None:
    """Reset singleton instance for test isolation."""
    global _monitor_instance
    _monitor_instance = None


def check_panic_threshold() -> PanicThresholdResult:
    """
    Convenience function for the side-effect-free Panic Threshold probe.

    Returns:
        PanicThresholdResult: check result
    """
    return get_panic_threshold_monitor().evaluate()


def is_panic_threshold_triggered() -> bool:
    """
    Convenience check for whether the last probe was triggered.

    Reads the observation cache; it does not probe.

    Returns:
        bool: Whether the last observed result was triggered
    """
    last_result = get_panic_threshold_monitor().get_last_result()
    if last_result is None:
        return False
    return last_result.triggered
