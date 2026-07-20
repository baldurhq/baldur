"""
Adaptive Threshold for Circuit Breaker

Automatic CB threshold adjustment by Emergency Level.
The more severe the situation, the more conservative (looser) the setting, to
prevent a self-induced blackout.

Design rationale:
- NORMAL: 5 failures / 60s (standard detection speed)
- ELEVATED: 7.5 failures / 90s (slightly conservative)
- HIGH: 10 failures / 120s (not fooled by spurious errors)
- CRITICAL: 15 failures / 180s (detects only real outages)
- LOCKDOWN: ∞ / ∞ (auto OPEN forbidden)

Key insight:
    In a crisis you must go "more conservative," not "more sensitive."
    If the CB reacts to transient errors caused by network jitter,
    a self-induced blackout occurs.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from baldur.services.circuit_breaker.models import (
    AdaptiveThresholdPolicy,
    ThresholdMultiplier,
)

if TYPE_CHECKING:
    from baldur.interfaces.emergency import EmergencyManager as EmergencyModeManager

logger = structlog.get_logger()


# =============================================================================
# Emergency Level to Adaptive Threshold Mapping
# =============================================================================

# Emergency Level string mapping (Enum value -> Threshold Level)
EMERGENCY_LEVEL_MAPPING = {
    0: "NORMAL",  # EmergencyLevel.NORMAL
    1: "ELEVATED",  # EmergencyLevel.LEVEL_1
    2: "HIGH",  # EmergencyLevel.LEVEL_2
    3: "LOCKDOWN",  # EmergencyLevel.LEVEL_3 = LOCKDOWN
}


@dataclass
class AdjustedThreshold:
    """
    Adjusted CB threshold.

    Attributes:
        failure_threshold: Adjusted failure-count threshold
        window_seconds: Adjusted observation window (seconds)
        emergency_level: Applied Emergency Level
        multiplier: Applied multiplier info
        is_lockdown: Whether in the LOCKDOWN state (auto OPEN forbidden)
    """

    failure_threshold: float
    window_seconds: float
    emergency_level: str
    multiplier: ThresholdMultiplier
    is_lockdown: bool = False


class AdaptiveThresholdManager:
    """
    Manager for automatic CB threshold adjustment by Emergency Level.

    Adjusts CB thresholds automatically according to the system Emergency Level.
    The more severe the situation, the more conservative (looser) the setting,
    to prevent a self-induced blackout.

    Usage:
        manager = AdaptiveThresholdManager()

        # Look up the threshold for the current Emergency Level
        threshold = manager.get_adjusted_threshold()

        # Look up the threshold for a specific Emergency Level
        threshold = manager.get_adjusted_threshold(emergency_level="HIGH")

        # Check whether automatic OPEN is allowed
        if manager.should_allow_auto_open():
            circuit_breaker.open()
    """

    def __init__(
        self,
        policy: AdaptiveThresholdPolicy | None = None,
        emergency_manager: EmergencyModeManager | None = None,
    ):
        """
        Initialize AdaptiveThresholdManager.

        Args:
            policy: Adaptive Threshold policy (None to use defaults)
            emergency_manager: Emergency Mode Manager (uses the global instance if not injected)
        """
        self.policy = policy or AdaptiveThresholdPolicy()
        self._emergency_manager = emergency_manager

    @property
    def emergency_manager(self):
        """Lazy-load the Emergency Manager."""
        if self._emergency_manager is None:
            from baldur.factory.registry import ProviderRegistry

            self._emergency_manager = ProviderRegistry.emergency_manager.safe_get()
            if self._emergency_manager is None:
                logger.warning(
                    "adaptive_threshold.emergency_manager_unavailable_fallback"
                )
        return self._emergency_manager

    def get_current_emergency_level(self) -> str:
        """
        Return the current Emergency Level as a string.

        Returns:
            str: Emergency Level ("NORMAL", "ELEVATED", "HIGH", "CRITICAL", "LOCKDOWN")
        """
        if self.emergency_manager is None:
            return "NORMAL"

        try:
            level = self.emergency_manager.get_current_level()
            # Map EmergencyLevel.LEVEL_X.value to the Adaptive Threshold Level
            level_value = level.value if hasattr(level, "value") else int(level)
            return EMERGENCY_LEVEL_MAPPING.get(level_value, "NORMAL")
        except Exception as e:
            logger.warning(
                "adaptive_threshold.get_emergency_level_failed",
                error=e,
            )
            return "NORMAL"

    def get_adjusted_threshold(
        self,
        emergency_level: str | None = None,
        service_id: str | None = None,
    ) -> AdjustedThreshold:
        """
        Return the adjusted threshold for the Emergency Level.

        Args:
            emergency_level: Explicit Emergency Level (None uses the current level)
            service_id: Service ID (for per-service override checks, future extension)

        Returns:
            AdjustedThreshold: adjusted threshold info
        """
        if not self.policy.enabled:
            # Return defaults when Adaptive Threshold is disabled
            return AdjustedThreshold(
                failure_threshold=float(self.policy.base_failure_threshold),
                window_seconds=float(self.policy.base_window_seconds),
                emergency_level="DISABLED",
                multiplier=ThresholdMultiplier(failure=1.0, window=1.0),
                is_lockdown=False,
            )

        # Determine the Emergency Level
        level = emergency_level or self.get_current_emergency_level()

        # Look up the multiplier (NORMAL default if absent)
        multiplier = self.policy.level_multipliers.get(
            level,
            self.policy.level_multipliers.get(
                "NORMAL", ThresholdMultiplier(failure=1.0, window=1.0)
            ),
        )

        # Compute the adjusted threshold
        adjusted_failure = self.policy.base_failure_threshold * multiplier.failure
        adjusted_window = self.policy.base_window_seconds * multiplier.window

        is_lockdown = level == "LOCKDOWN" or multiplier.failure == float("inf")

        logger.debug(
            "adaptive_threshold.event",
            threshold_level=level,
            adjusted_failure=adjusted_failure,
            adjusted_window=adjusted_window,
            is_lockdown=is_lockdown,
        )

        return AdjustedThreshold(
            failure_threshold=adjusted_failure,
            window_seconds=adjusted_window,
            emergency_level=level,
            multiplier=multiplier,
            is_lockdown=is_lockdown,
        )

    def should_allow_auto_open(
        self,
        service_id: str | None = None,
    ) -> tuple[bool, str]:
        """
        Decide whether automatic OPEN is allowed.

        Automatic OPEN is forbidden in the LOCKDOWN state.

        Args:
            service_id: Service ID (for future per-service policy)

        Returns:
            Tuple[bool, str]: (whether allowed, reason if denied)
        """
        threshold = self.get_adjusted_threshold(service_id=service_id)

        if threshold.is_lockdown:
            return (
                False,
                f"LOCKDOWN: Auto OPEN blocked - {threshold.multiplier.description}",
            )

        return True, ""

    def should_allow_auto_close(
        self,
        service_id: str | None = None,
    ) -> tuple[bool, str]:
        """
        Decide whether automatic CLOSE is allowed.

        Automatic CLOSE is also forbidden in the LOCKDOWN state (Freeze Mode).

        Args:
            service_id: Service ID (for future per-service policy)

        Returns:
            Tuple[bool, str]: (whether allowed, reason if denied)
        """
        threshold = self.get_adjusted_threshold(service_id=service_id)

        if threshold.is_lockdown:
            return False, "LOCKDOWN: Auto CLOSE blocked - Freeze Mode active"

        return True, ""

    def check_threshold_exceeded(
        self,
        failure_count: int,
        window_start_time: float,
        current_time: float,
        service_id: str | None = None,
    ) -> tuple[bool, AdjustedThreshold]:
        """
        Check whether the failure count exceeds the adjusted threshold.

        Args:
            failure_count: Current failure count
            window_start_time: Window start time (Unix timestamp)
            current_time: Current time (Unix timestamp)
            service_id: Service ID

        Returns:
            Tuple[bool, AdjustedThreshold]: (whether exceeded, applied threshold)
        """
        threshold = self.get_adjusted_threshold(service_id=service_id)

        # Never exceeded under LOCKDOWN (auto OPEN forbidden)
        if threshold.is_lockdown:
            return False, threshold

        # Check whether we are within the window
        elapsed = current_time - window_start_time
        if elapsed > threshold.window_seconds:
            # Outside the window → treat as a counter reset
            return False, threshold

        # Check whether the failure count exceeds the threshold
        exceeded = failure_count >= threshold.failure_threshold

        if exceeded:
            logger.info(
                "adaptive_threshold.threshold_exceeded",
                failure_count=failure_count,
                threshold=threshold.failure_threshold,
                emergency_level=threshold.emergency_level,
            )

        return exceeded, threshold


# =============================================================================
# Convenience Functions
# =============================================================================


_manager_instance: AdaptiveThresholdManager | None = None
_manager_instance_lock = threading.Lock()


def get_adaptive_threshold_manager() -> AdaptiveThresholdManager:
    """
    Return the global AdaptiveThresholdManager instance.

    Manages the instance with the singleton pattern.
    """
    global _manager_instance
    if _manager_instance is None:
        with _manager_instance_lock:
            if _manager_instance is None:
                _manager_instance = AdaptiveThresholdManager()
    return _manager_instance


def reset_adaptive_threshold_manager() -> None:
    """Reset singleton instance for test isolation."""
    global _manager_instance
    _manager_instance = None


def get_adjusted_cb_threshold(
    service_id: str | None = None,
) -> tuple[float, float]:
    """
    Return the CB threshold for the current Emergency Level.

    A convenience function for looking up the threshold simply, without using
    AdaptiveThresholdManager directly.

    Args:
        service_id: Service ID (for future per-service policy)

    Returns:
        Tuple[float, float]: (failure-count threshold, window seconds)
    """
    manager = get_adaptive_threshold_manager()
    threshold = manager.get_adjusted_threshold(service_id=service_id)
    return threshold.failure_threshold, threshold.window_seconds


def should_allow_cb_auto_open(service_id: str | None = None) -> bool:
    """
    Convenience check for whether CB automatic OPEN is allowed.

    Args:
        service_id: Service ID

    Returns:
        bool: Whether automatic OPEN is allowed
    """
    manager = get_adaptive_threshold_manager()
    allowed, _ = manager.should_allow_auto_open(service_id=service_id)
    return allowed
