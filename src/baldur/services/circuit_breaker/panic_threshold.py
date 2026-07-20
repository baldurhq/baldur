"""
Panic Threshold for Circuit Breaker

When 70% or more Circuit Breakers are OPEN simultaneously, this is judged not as
an individual service problem but as a collapse of the entire infrastructure. At
that point the autonomous-operation engine declares Emergency Level 3 on its own
and halts all automatic recovery.

Operation flow:
    get_open_circuits() → compute OPEN ratio → exceeds 70%?
                                            ↓ Yes
                                    PANIC THRESHOLD TRIGGERED!
                                            ↓
                                    Auto-declare Emergency Level 3
                                            ↓
                                    Global Lockdown (Freeze Mode)
                                            ↓
                                    Halt all automatic recovery:
                                    - stop Replay
                                    - stop Canary Recovery
                                    - forbid Auto OPEN/CLOSE
                                    - await manual intervention
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from baldur.audit.helpers import log_panic_threshold_audit
from baldur.services.circuit_breaker.models import PanicThresholdConfig
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.interfaces.emergency import EmergencyManager as EmergencyModeManager
    from baldur.services.circuit_breaker import CircuitBreakerService

logger = structlog.get_logger()


# =============================================================================
# Panic Threshold Result
# =============================================================================


@dataclass
class PanicThresholdResult:
    """
    Panic Threshold check result.

    Attributes:
        triggered: Whether the Panic Threshold was triggered
        open_rate: Current OPEN ratio (%)
        open_count: Number of CBs in the OPEN state
        total_count: Total number of registered CBs
        open_circuits: List of services in the OPEN state
        action_taken: Action taken
        halted_systems: List of halted systems
        reason: Reason for triggering / not triggering
        timestamp: Check time
    """

    triggered: bool = False
    open_rate: float = 0.0
    open_count: int = 0
    total_count: int = 0
    open_circuits: list[str] = field(default_factory=list)
    action_taken: str | None = None
    halted_systems: list[str] = field(default_factory=list)
    reason: str | None = None
    timestamp: str = field(default_factory=lambda: utc_now().isoformat())


# =============================================================================
# Panic Threshold Monitor
# =============================================================================


class PanicThresholdMonitor:
    """
    Monitors the system-wide OPEN ratio and triggers the Panic Threshold.

    When 70% or more of all CBs are OPEN, the system is judged to be in total
    collapse and Emergency Level 3 is declared automatically.

    Usage:
        monitor = PanicThresholdMonitor()

        # Check periodically (e.g., every 5 seconds)
        result = monitor.check_panic_threshold()

        if result.triggered:
            # Panic triggered - already escalated to Emergency Level 3
            send_critical_alert(result)
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
            config: Panic Threshold configuration (None to use defaults)
            circuit_breaker_service: CB service (uses the global instance if not injected)
            emergency_manager: Emergency Manager (uses the global instance if not injected)
        """
        self.config = config or PanicThresholdConfig()
        self._cb_service = circuit_breaker_service
        self._emergency_manager = emergency_manager
        self._consecutive_triggers = 0
        self._last_check_result: PanicThresholdResult | None = None

    @property
    def cb_service(self):
        """Lazy-load the Circuit Breaker Service."""
        if self._cb_service is None:
            try:
                from baldur.services.circuit_breaker import CircuitBreakerService

                self._cb_service = CircuitBreakerService()
            except ImportError:
                logger.warning("panic_threshold.circuitbreakerservice_available")
        return self._cb_service

    @property
    def emergency_manager(self):
        """Lazy-load the Emergency Manager."""
        if self._emergency_manager is None:
            from baldur.factory.registry import ProviderRegistry

            self._emergency_manager = ProviderRegistry.emergency_manager.safe_get()
            if self._emergency_manager is None:
                logger.warning("panic_threshold.emergencymodemanager_available")
        return self._emergency_manager

    def check_panic_threshold(self) -> PanicThresholdResult:
        """
        Check the system-wide OPEN ratio and trigger the Panic Threshold.

        Returns:
            PanicThresholdResult: detection result and action taken
        """
        if not self.config.enabled:
            return PanicThresholdResult(
                triggered=False, reason="Panic Threshold disabled"
            )

        # 1. Collect all Circuit states
        open_circuits, total_circuits = self._get_circuit_stats()

        # 2. Check the minimum service count (false-positive prevention)
        min_services = getattr(self.config, "min_registered_services", 3)
        if len(total_circuits) < min_services:
            result = PanicThresholdResult(
                triggered=False,
                open_rate=0.0,
                open_count=len(open_circuits),
                total_count=len(total_circuits),
                open_circuits=open_circuits,
                reason=f"Insufficient services ({len(total_circuits)} < {min_services})",
            )
            self._last_check_result = result
            return result

        # 3. Compute the OPEN ratio
        open_rate = (len(open_circuits) / len(total_circuits)) * 100

        # 4. Check threshold exceedance
        if open_rate >= self.config.threshold_percent:
            self._consecutive_triggers += 1

            # Trigger Panic when the consecutive-detection count is met
            consecutive_required = getattr(
                self.config, "consecutive_triggers_required", 2
            )
            if self._consecutive_triggers >= consecutive_required:
                result = self._trigger_panic(
                    open_rate=open_rate,
                    open_circuits=open_circuits,
                    total_circuits=total_circuits,
                )
                self._last_check_result = result
                return result
            result = PanicThresholdResult(
                triggered=False,
                open_rate=open_rate,
                open_count=len(open_circuits),
                total_count=len(total_circuits),
                open_circuits=open_circuits,
                reason=f"Threshold exceeded but waiting for consecutive triggers "
                f"({self._consecutive_triggers}/{consecutive_required})",
            )
            self._last_check_result = result
            return result
        self._consecutive_triggers = 0

        result = PanicThresholdResult(
            triggered=False,
            open_rate=open_rate,
            open_count=len(open_circuits),
            total_count=len(total_circuits),
            open_circuits=open_circuits,
            reason=f"Below threshold ({open_rate:.1f}% < {self.config.threshold_percent}%)",
        )
        self._last_check_result = result
        return result

    def _get_circuit_stats(self) -> tuple[list[str], list[str]]:
        """
        Collect all Circuit states.

        Returns:
            tuple[List[str], List[str]]: (list of OPEN services, list of all services)
        """
        if self.cb_service is None:
            return [], []

        try:
            # Query all states from the CB service
            all_states = self.cb_service.repository.get_all_states()

            open_circuits = []
            total_circuits = []

            for state in all_states:
                service_name = state.service_name
                total_circuits.append(service_name)

                # Check OPEN state
                if state.state.lower() == "open":
                    open_circuits.append(service_name)

            return open_circuits, total_circuits
        except Exception as e:
            logger.warning(
                "panic_threshold.get_circuit_stats_failed",
                error=e,
            )
            return [], []

    def _trigger_panic(
        self,
        open_rate: float,
        open_circuits: list[str],
        total_circuits: list[str],
    ) -> PanicThresholdResult:
        """
        Trigger the Panic Threshold and declare Emergency Level 3.

        Args:
            open_rate: Current OPEN ratio
            open_circuits: List of services in the OPEN state
            total_circuits: List of all services

        Returns:
            PanicThresholdResult: trigger result
        """
        halted_systems = ["replay", "auto_open", "auto_close"]
        action_taken = "emergency_level_3_escalation"

        logger.critical(
            "panic.threshold_triggered_circuits",
            open_circuits_count=len(open_circuits),
            total_circuits_count=len(total_circuits),
            open_rate=open_rate,
        )

        # 1. Audit record
        self._log_panic_audit(
            open_rate=open_rate,
            open_circuits=open_circuits,
            total_count=len(total_circuits),
            action_taken=action_taken,
            halted_systems=halted_systems,
        )

        # 2. Auto-declare Emergency Level 3
        if self.config.action == "freeze":
            self._escalate_to_level_3(
                open_rate=open_rate,
                open_circuits=open_circuits,
            )

            # 3. Activate Freeze Mode
            self._activate_freeze_mode(open_rate=open_rate)

        # 4. Alert the operations team (implementation delegated to the alert service)
        self._notify_critical(
            open_rate=open_rate,
            open_count=len(open_circuits),
            total_count=len(total_circuits),
            open_circuits=open_circuits,
            halted_systems=halted_systems,
        )

        return PanicThresholdResult(
            triggered=True,
            open_rate=open_rate,
            open_count=len(open_circuits),
            total_count=len(total_circuits),
            open_circuits=open_circuits,
            action_taken=action_taken,
            halted_systems=halted_systems,
            reason=f"Panic Threshold triggered (Open Rate: {open_rate:.1f}%)",
        )

    def _log_panic_audit(
        self,
        open_rate: float,
        open_circuits: list[str],
        total_count: int,
        action_taken: str,
        halted_systems: list[str],
    ) -> None:
        """Audit record for Panic Threshold trigger."""
        log_panic_threshold_audit(
            open_rate=open_rate,
            threshold=self.config.threshold_percent,
            open_count=len(open_circuits),
            total_count=total_count,
            open_circuits=open_circuits,
            action_taken=action_taken,
            halted_systems=halted_systems,
        )

    def _escalate_to_level_3(
        self,
        open_rate: float,
        open_circuits: list[str],
    ) -> None:
        """Escalate to Emergency Level 3."""
        if self.emergency_manager is None:
            logger.warning("panic_threshold.emergencymanager_available_escalation")
            return

        try:
            from baldur.models.emergency import EmergencyLevel

            self.emergency_manager.escalate_to_level(
                level=EmergencyLevel.LEVEL_3,
                reason=f"Panic Threshold: {open_rate:.1f}% of circuits are OPEN",
                triggered_by="PanicThresholdMonitor",
            )

            logger.warning(
                "panic_threshold.escalated_emergency_level",
                open_rate=open_rate,
                open_circuits_count=len(open_circuits),
            )
        except Exception as e:
            logger.exception(
                "panic_threshold.escalate_level_failed",
                error=e,
            )

    def _activate_freeze_mode(self, open_rate: float) -> None:
        """Activate Freeze Mode."""
        try:
            from baldur.services.circuit_breaker.freeze_mode import (
                FreezeReason,
                get_freeze_mode_manager,
            )

            manager = get_freeze_mode_manager()
            manager.activate(
                reason=f"{FreezeReason.PANIC_THRESHOLD} (Open Rate: {open_rate:.1f}%)",
                activated_by="PanicThresholdMonitor",
            )
        except Exception as e:
            logger.warning(
                "panic_threshold.activate_freeze_mode_failed",
                error=e,
            )

    def _notify_critical(
        self,
        open_rate: float,
        open_count: int,
        total_count: int,
        open_circuits: list[str],
        halted_systems: list[str],
    ) -> None:
        """Send an urgent alert to the operations team."""
        # Alerts are handled by a separate system (only logging here)
        logger.critical(
            "panic.threshold_emergency_level",
            open_count=open_count,
            total_count=total_count,
            open_rate=open_rate,
            halted_systems_list=", ".join(halted_systems),
            open_circuits=", ".join(open_circuits),
        )

    def get_last_result(self) -> PanicThresholdResult | None:
        """
        Return the last check result.

        Returns:
            Optional[PanicThresholdResult]: last check result
        """
        return self._last_check_result

    def reset_consecutive_count(self) -> None:
        """Reset the consecutive-detection counter (for tests/debugging)."""
        self._consecutive_triggers = 0


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


def check_panic_threshold() -> PanicThresholdResult:
    """
    Convenience function for the Panic Threshold check.

    Returns:
        PanicThresholdResult: check result
    """
    return get_panic_threshold_monitor().check_panic_threshold()


def is_panic_threshold_triggered() -> bool:
    """
    Convenience check for whether the Panic Threshold was triggered.

    Checks based on the last check result.

    Returns:
        bool: Whether the Panic Threshold was triggered
    """
    monitor = get_panic_threshold_monitor()
    last_result = monitor.get_last_result()
    if last_result is None:
        return False
    return last_result.triggered
