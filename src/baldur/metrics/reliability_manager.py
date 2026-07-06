"""
Metric Reliability Manager.

Coordinates metric data reliability across all fallback layers:
1. Real-time Push Events
2. DB Query (Manual Sync)
3. Redis Air-Gap
4. L1 Local Snapshot
5. Safe Defaults

Implements Conservative Fallback with gradual stabilization.

Design Philosophy:
- "When unknown, be conservative"
- Gradual recovery from strict mode
- Transparent reliability reporting
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

import structlog

logger = structlog.get_logger()


class ReliabilityLevel(str, Enum):
    """Metric reliability level."""

    HIGH = "high"  # Real-time push or recently synced
    MEDIUM = "medium"  # L1 snapshot or slightly stale data
    LOW = "low"  # Stale snapshot
    UNKNOWN = "unknown"  # No data → use safe defaults
    RECOVERING = "recovering"  # Recovering (gradual relaxation)


class OperatingMode(str, Enum):
    """System operating mode."""

    NORMAL = "normal"  # Normal operation
    CAUTIOUS = "cautious"  # Cautious (some restrictions)
    STRICT = "strict"  # Strict (conservative settings)
    EMERGENCY = "emergency"  # Emergency (minimum features)


@dataclass
class ReliabilityThresholds:
    """Thresholds used to evaluate reliability."""

    # Data age thresholds (seconds)
    high_max_age: float = 60.0  # within 1 minute = HIGH
    medium_max_age: float = 300.0  # within 5 minutes = MEDIUM
    low_max_age: float = 3600.0  # within 1 hour = LOW

    # Recovery
    stabilization_duration: float = 60.0  # Stabilization window (seconds)
    consecutive_syncs_for_normal: int = (
        3  # Successful syncs required to return to NORMAL
    )


@dataclass
class MetricReliabilityState:
    """Metric reliability state."""

    domain: str
    reliability_level: ReliabilityLevel = ReliabilityLevel.UNKNOWN
    operating_mode: OperatingMode = OperatingMode.STRICT

    last_sync_time: float | None = None
    last_sync_source: str = "none"
    consecutive_successful_syncs: int = 0

    # Recovery
    stabilization_start: float | None = None
    mode_transition_time: float | None = None

    # Information about the value currently in use
    current_value: Any = None
    value_source: str = "none"  # "push", "db", "airgap", "snapshot", "default"

    @property
    def is_data_fresh(self) -> bool:
        """Whether the data is considered fresh."""
        return self.reliability_level in (
            ReliabilityLevel.HIGH,
            ReliabilityLevel.MEDIUM,
        )

    @property
    def age_seconds(self) -> float | None:
        """Time elapsed since the last sync."""
        if self.last_sync_time is None:
            return None
        return time.time() - self.last_sync_time

    @property
    def stabilization_progress(self) -> float:
        """Stabilization progress (0.0 ~ 1.0)."""
        if self.operating_mode != OperatingMode.CAUTIOUS:
            return 1.0 if self.operating_mode == OperatingMode.NORMAL else 0.0

        if self.stabilization_start is None:
            return 0.0

        elapsed = time.time() - self.stabilization_start
        return min(1.0, elapsed / 60.0)  # 60 second baseline


class MetricReliabilityManager:
    """
    Metric reliability manager.

    Tracks the state of multiple data sources and decides which fallback
    strategy and operating mode to apply.

    Features:
    - Multi-source reliability tracking
    - Conservative fallback mode
    - Gradual stabilization on recovery
    - Prometheus metric export for monitoring

    Example:
        >>> manager = MetricReliabilityManager()
        >>>
        >>> # Report a successful sync
        >>> manager.report_sync_success("payment", "push", value=5)
        >>>
        >>> # Inspect reliability
        >>> state = manager.get_reliability_state("payment")
        >>> if state.is_data_fresh:
        ...     use_value(state.current_value)
        >>> else:
        ...     use_safe_default()
    """

    def __init__(
        self,
        thresholds: ReliabilityThresholds | None = None,
        safe_defaults_provider: Callable[[str], Any] | None = None,
    ):
        """
        Initialize MetricReliabilityManager.

        Args:
            thresholds: reliability evaluation thresholds
            safe_defaults_provider: safe-default provider (domain -> value)
        """
        self._thresholds = thresholds or ReliabilityThresholds()
        self._safe_defaults_provider = safe_defaults_provider
        self._states: dict[str, MetricReliabilityState] = {}
        self._lock = threading.Lock()
        self._global_mode = OperatingMode.NORMAL
        self._mode_listeners: list[Callable[[str, OperatingMode], None]] = []

    def get_reliability_state(self, domain: str) -> MetricReliabilityState:
        """
        Return the reliability state for a domain.

        Args:
            domain: domain name

        Returns:
            MetricReliabilityState
        """
        with self._lock:
            if domain not in self._states:
                self._states[domain] = MetricReliabilityState(domain=domain)

            state = self._states[domain]
            self._update_reliability_level(state)
            return state

    def report_sync_success(
        self,
        domain: str,
        source: str,
        value: Any,
    ) -> MetricReliabilityState:
        """
        Report a successful sync.

        Args:
            domain: domain name
            source: sync source ("push", "db", "airgap", "snapshot")
            value: synced value

        Returns:
            updated state
        """
        with self._lock:
            if domain not in self._states:
                self._states[domain] = MetricReliabilityState(domain=domain)

            state = self._states[domain]
            state.last_sync_time = time.time()
            state.last_sync_source = source
            state.current_value = value
            state.value_source = source
            state.consecutive_successful_syncs += 1

            self._update_reliability_level(state)
            self._update_operating_mode(state)

            return state

    def report_sync_failure(
        self,
        domain: str,
        source: str,
        reason: str = "unknown",
    ) -> MetricReliabilityState:
        """
        Report a failed sync.

        Args:
            domain: domain name
            source: source that failed
            reason: failure reason

        Returns:
            updated state
        """
        with self._lock:
            if domain not in self._states:
                self._states[domain] = MetricReliabilityState(domain=domain)

            state = self._states[domain]
            state.consecutive_successful_syncs = 0

            logger.warning(
                "reliability.sync_failed",
                healing_domain=domain,
                source=source,
                reason=reason,
            )

            self._update_reliability_level(state)
            self._update_operating_mode(state)

            return state

    def _update_reliability_level(self, state: MetricReliabilityState) -> None:
        """Update the reliability level."""
        age = state.age_seconds

        if age is None:
            state.reliability_level = ReliabilityLevel.UNKNOWN
        elif age <= self._thresholds.high_max_age:
            state.reliability_level = ReliabilityLevel.HIGH
        elif age <= self._thresholds.medium_max_age:
            state.reliability_level = ReliabilityLevel.MEDIUM
        elif age <= self._thresholds.low_max_age:
            state.reliability_level = ReliabilityLevel.LOW
        else:
            state.reliability_level = ReliabilityLevel.UNKNOWN

    def _update_operating_mode(self, state: MetricReliabilityState) -> None:
        """Update the operating mode (with gradual relaxation)."""
        old_mode = state.operating_mode

        if state.reliability_level == ReliabilityLevel.UNKNOWN:
            # No data → strict mode
            state.operating_mode = OperatingMode.STRICT
            state.stabilization_start = None

        elif state.reliability_level == ReliabilityLevel.LOW:
            # Stale data → cautious mode
            if old_mode == OperatingMode.STRICT:
                # STRICT → CAUTIOUS: begin stabilization
                state.operating_mode = OperatingMode.CAUTIOUS
                state.stabilization_start = time.time()
            elif old_mode == OperatingMode.CAUTIOUS:
                # Stabilization in progress
                pass
            else:
                state.operating_mode = OperatingMode.CAUTIOUS
                state.stabilization_start = time.time()

        elif state.reliability_level in (
            ReliabilityLevel.HIGH,
            ReliabilityLevel.MEDIUM,
        ):
            # Fresh data
            if old_mode in (OperatingMode.STRICT, OperatingMode.EMERGENCY):
                # Recovering from strict mode → gradual relaxation
                state.operating_mode = OperatingMode.CAUTIOUS
                state.stabilization_start = time.time()
                logger.info(
                    "reliability.starting_stabilization",
                    reliability_domain=state.domain,
                    stabilization_duration=self._thresholds.stabilization_duration,
                )
            elif old_mode == OperatingMode.CAUTIOUS:
                # Check stabilization progress
                if (
                    state.stabilization_start is not None
                    and time.time() - state.stabilization_start
                    >= self._thresholds.stabilization_duration
                    and state.consecutive_successful_syncs
                    >= self._thresholds.consecutive_syncs_for_normal
                ):
                    # Stabilization complete → return to NORMAL
                    state.operating_mode = OperatingMode.NORMAL
                    state.stabilization_start = None
                    logger.info(
                        "reliability.stabilization_complete_entering_normal",
                        reliability_domain=state.domain,
                    )
            # NORMAL stays as NORMAL

        # Notify on mode change
        if old_mode != state.operating_mode:
            state.mode_transition_time = time.time()
            self._notify_mode_change(state.domain, state.operating_mode)

    def _notify_mode_change(self, domain: str, new_mode: OperatingMode) -> None:
        """Notify mode-change listeners."""
        for listener in self._mode_listeners:
            try:
                listener(domain, new_mode)
            except Exception as e:
                logger.warning(
                    "reliability.mode_listener_failed",
                    error=e,
                )

    def register_mode_listener(
        self,
        listener: Callable[[str, OperatingMode], None],
    ) -> None:
        """
        Register a mode-change listener.

        Args:
            listener: (domain, new_mode) callback
        """
        self._mode_listeners.append(listener)

    def get_effective_value(
        self,
        domain: str,
        category: str = "default",
    ) -> tuple[Any, str, ReliabilityLevel]:
        """
        Return the effective value (applying fallback chain).

        Fallback order:
        1. Currently synced value (HIGH/MEDIUM)
        2. L1 snapshot
        3. Safe defaults

        Args:
            domain: domain name
            category: value category

        Returns:
            (value, source, reliability_level)
        """
        state = self.get_reliability_state(domain)

        # Use the freshly synced value when available
        if state.is_data_fresh and state.current_value is not None:
            return (state.current_value, state.value_source, state.reliability_level)

        # Try the L1 snapshot
        try:
            from baldur.metrics.snapshot_storage import get_snapshot_storage

            storage = get_snapshot_storage()
            snapshot_value = storage.load_value(category, domain)

            if snapshot_value is not None:
                snapshot_age = storage.get_snapshot_age() or float("inf")

                if snapshot_age <= self._thresholds.low_max_age:
                    return (snapshot_value, "snapshot", ReliabilityLevel.LOW)
        except Exception as e:
            logger.debug(
                "reliability.snapshot_fallback_failed",
                error=e,
            )

        # Fall back to safe defaults
        if self._safe_defaults_provider:
            try:
                default_value = self._safe_defaults_provider(domain)
                return (default_value, "default", ReliabilityLevel.UNKNOWN)
            except Exception as e:
                logger.warning(
                    "reliability.safe_defaults_provider_failed",
                    error=e,
                )

        return (None, "none", ReliabilityLevel.UNKNOWN)

    def get_all_states(self) -> dict[str, MetricReliabilityState]:
        """Return state for every known domain."""
        with self._lock:
            # Refresh reliability levels
            for state in self._states.values():
                self._update_reliability_level(state)
            return dict(self._states)

    def get_global_health(self) -> dict[str, Any]:
        """
        Return the overall system health.

        Returns:
            health summary
        """
        states = self.get_all_states()

        if not states:
            return {
                "status": "unknown",
                "domains": 0,
                "healthy": 0,
                "degraded": 0,
                "unhealthy": 0,
            }

        healthy = sum(
            1
            for s in states.values()
            if s.reliability_level in (ReliabilityLevel.HIGH, ReliabilityLevel.MEDIUM)
        )
        degraded = sum(
            1 for s in states.values() if s.reliability_level == ReliabilityLevel.LOW
        )
        unhealthy = sum(
            1
            for s in states.values()
            if s.reliability_level == ReliabilityLevel.UNKNOWN
        )

        if unhealthy > 0:
            status = "unhealthy"
        elif degraded > 0:
            status = "degraded"
        else:
            status = "healthy"

        return {
            "status": status,
            "domains": len(states),
            "healthy": healthy,
            "degraded": degraded,
            "unhealthy": unhealthy,
            "global_mode": self._global_mode.value,
        }

    def force_strict_mode(self, domain: str, reason: str = "manual") -> None:
        """
        Force a domain into strict mode.

        Args:
            domain: domain name
            reason: reason for the transition
        """
        with self._lock:
            if domain not in self._states:
                self._states[domain] = MetricReliabilityState(domain=domain)

            state = self._states[domain]
            old_mode = state.operating_mode
            state.operating_mode = OperatingMode.STRICT
            state.stabilization_start = None
            state.consecutive_successful_syncs = 0

            logger.warning(
                "reliability.forced_strict_mode",
                healing_domain=domain,
                reason=reason,
            )

            if old_mode != OperatingMode.STRICT:
                self._notify_mode_change(domain, OperatingMode.STRICT)

    def get_global_mode(self) -> OperatingMode:
        """
        Return the current global operating mode.

        Returns:
            current global mode
        """
        return self._global_mode

    def force_global_mode(
        self,
        mode: OperatingMode,
        reason: str = "manual",
    ) -> None:
        """
        Force the global operating mode.

        Applies the same mode to every domain.

        Args:
            mode: target operating mode
            reason: reason for the transition
        """
        with self._lock:
            old_mode = self._global_mode
            self._global_mode = mode

            logger.warning(
                "reliability.global_mode_changed",
                old_mode=old_mode.value,
                mode=mode.value,
                reason=reason,
            )

            # Apply the same mode to every domain
            for domain, state in self._states.items():
                if state.operating_mode != mode:
                    state.operating_mode = mode
                    state.stabilization_start = (
                        None if mode == OperatingMode.STRICT else time.time()
                    )
                    self._notify_mode_change(domain, mode)

    def reset(self) -> None:
        """Reset all state."""
        with self._lock:
            self._states.clear()


# =============================================================================
# Singleton Instance
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

get_reliability_manager, configure_reliability_manager, reset_reliability_manager = (
    make_singleton_factory("reliability_manager", MetricReliabilityManager)
)


__all__ = [
    "ReliabilityLevel",
    "OperatingMode",
    "ReliabilityThresholds",
    "MetricReliabilityState",
    "MetricReliabilityManager",
    "configure_reliability_manager",
    "get_reliability_manager",
    "reset_reliability_manager",
]
