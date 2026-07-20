"""
Emergency Health Penalty.

Health Score penalty calculation based on Emergency state.

Main features:
- calculate_penalty(namespace): calculate the penalty for the Emergency state
- get_health_score_with_emergency(base_score, namespace): return the adjusted score
- get_penalty_breakdown(namespace): penalty details (for dashboards)

Penalty weights:
- Regional STRICT: -20 points
- Global STRICT: -30 points

Integrates with PropagationHealthMonitor so that the Emergency state is
automatically reflected in the Health Score.

Code reference:
    services/config/propagation_health.py (penalty pattern)
    services/regional_emergency/tracker.py (NamespacedEmergencyTracker)
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import structlog

from baldur.core.serializable import SerializableMixin
from baldur.models.emergency import EmergencyScope
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# =============================================================================
# Constants — documented defaults (actual values loaded from settings at init)
# =============================================================================

REGIONAL_STRICT_PENALTY: float = 20.0
"""Regional STRICT mode penalty: default -20 points."""

GLOBAL_STRICT_PENALTY: float = 30.0
"""Global STRICT mode penalty: default -30 points."""

LEVEL_1_PENALTY: float = 5.0
"""LEVEL_1 (warning level) penalty: default -5 points."""

LEVEL_2_PENALTY: float = 10.0
"""LEVEL_2 (caution level) penalty: default -10 points (when STRICT not applied)."""


@dataclass
class PenaltyBreakdown(SerializableMixin):
    """
    Penalty details.

    Used by the dashboard to show "why the score dropped".
    """

    penalty: float = 0.0
    """Applied penalty (points deducted)."""

    reason: str | None = None
    """Penalty reason (e.g. 'Global STRICT active since 2026-01-22T...')."""

    scope: str | None = None
    """Emergency scope ('global' or 'regional')."""

    emergency_level: str = "NORMAL"
    """Emergency level name."""

    governance_mode: str = "NORMAL"
    """Governance mode ('NORMAL' or 'STRICT')."""

    activated_by: str | None = None
    """Who activated the Emergency."""

    activated_at: str | None = None
    """When the Emergency was activated."""

    namespace: str | None = None
    """Target namespace."""

    calculated_at: str = field(default_factory=lambda: utc_now().isoformat())
    """Calculation time."""


class EmergencyHealthPenalty:
    """
    Health Score penalty calculator based on Emergency state.

    Applies a penalty to the Health Score while an Emergency is active.
    Used in combination with PropagationHealthMonitor.

    Penalty weights:
    - LEVEL_1: -5 points (warning level)
    - LEVEL_2: -10 points (when STRICT not applied)
    - Regional STRICT: -20 points
    - Global STRICT: -30 points

    Usage:
        penalty = EmergencyHealthPenalty()

        # Calculate the penalty
        points = penalty.calculate_penalty("seoul")

        # Apply the penalty
        adjusted_score = penalty.get_health_score_with_emergency(
            base_score=95.0,
            namespace="seoul"
        )

        # Penalty details (for dashboards)
        breakdown = penalty.get_penalty_breakdown("seoul")
    """

    def __init__(
        self,
        tracker: Any | None = None,
        regional_penalty: float | None = None,
        global_penalty: float | None = None,
    ):
        """
        Initialize EmergencyHealthPenalty.

        Args:
            tracker: NamespacedEmergencyTracker instance (auto-resolved if None)
            regional_penalty: Regional STRICT penalty (loaded from settings if None)
            global_penalty: Global STRICT penalty (loaded from settings if None)
        """
        self._tracker = tracker
        self._lock = threading.Lock()

        # Load penalty values from settings (at instance creation time)
        try:
            from baldur.settings.emergency_mode import get_emergency_mode_settings

            s = get_emergency_mode_settings()
            self._regional_penalty = (
                regional_penalty
                if regional_penalty is not None
                else s.penalty_regional_strict
            )
            self._global_penalty = (
                global_penalty
                if global_penalty is not None
                else s.penalty_global_strict
            )
            self._level_1_penalty = s.penalty_level_1
            self._level_2_penalty = s.penalty_level_2
            self._cache_ttl_seconds = s.penalty_cache_ttl_seconds
        except Exception:
            self._regional_penalty = (
                regional_penalty
                if regional_penalty is not None
                else REGIONAL_STRICT_PENALTY
            )
            self._global_penalty = (
                global_penalty if global_penalty is not None else GLOBAL_STRICT_PENALTY
            )
            self._level_1_penalty = LEVEL_1_PENALTY
            self._level_2_penalty = LEVEL_2_PENALTY
            self._cache_ttl_seconds = 5.0

        # Cache (optimization for frequent calls)
        self._cached_penalty: dict[str, float] = {}
        self._cache_timestamp: dict[str, float] = {}

    def _get_tracker(self) -> Any:
        """Obtain the NamespacedEmergencyTracker instance."""
        if self._tracker is None:
            from baldur.services.regional_emergency.tracker import (
                get_namespaced_emergency_tracker,
            )

            self._tracker = get_namespaced_emergency_tracker()
        return self._tracker

    def calculate_penalty(self, namespace: str | None = None) -> float:
        """
        Calculate the penalty for the current Emergency state.

        Penalty rules:
        - Global STRICT: -30 points
        - Regional STRICT: -20 points
        - LEVEL_2 (non-STRICT): -10 points
        - LEVEL_1: -5 points
        - NORMAL: 0 points

        Args:
            namespace: target namespace (current instance if None)

        Returns:
            Penalty points (non-negative)
        """
        import time

        ns = namespace or "global"
        cache_key = f"penalty:{ns}"
        now = time.time()

        # Check the cache
        with self._lock:
            if cache_key in self._cached_penalty:
                cache_time = self._cache_timestamp.get(cache_key, 0)
                if now - cache_time < self._cache_ttl_seconds:
                    return self._cached_penalty[cache_key]

        # Look up the Emergency state
        tracker = self._get_tracker()
        state = tracker.get_effective_state(namespace=namespace)

        # Calculate the penalty
        penalty = 0.0

        if not state.is_active:
            penalty = 0.0
        elif state.governance_mode == "STRICT":
            if state.scope == EmergencyScope.GLOBAL:
                penalty = self._global_penalty
            else:
                penalty = self._regional_penalty
        else:
            # Not STRICT, but the Emergency is active:
            # apply the reduced penalty for the level
            level_severity = getattr(state.emergency_level, "severity", 0)
            if level_severity >= 2:
                penalty = self._level_2_penalty
            elif level_severity >= 1:
                penalty = self._level_1_penalty

        # Store in the cache
        with self._lock:
            self._cached_penalty[cache_key] = penalty
            self._cache_timestamp[cache_key] = now

        logger.debug(
            "emergency_health_penalty.event",
            namespace_id=ns,
            penalty=penalty,
            governance_mode=state.governance_mode,
        )

        return penalty

    def get_health_score_with_emergency(
        self,
        base_score: float,
        namespace: str | None = None,
    ) -> float:
        """
        Return the Health Score with the Emergency penalty applied.

        Args:
            base_score: base Health Score (0-100)
            namespace: target namespace

        Returns:
            Health Score with the penalty applied (0-100, clamped)
        """
        penalty = self.calculate_penalty(namespace=namespace)
        adjusted_score = base_score - penalty

        # Clamp to the 0-100 range
        result = max(0.0, min(100.0, adjusted_score))

        if penalty > 0:
            logger.debug(
                "emergency_health_penalty.applied_penalty",
                base_score=base_score,
                penalty=penalty,
                health_result=result,
            )

        return result

    def get_penalty_breakdown(
        self,
        namespace: str | None = None,
    ) -> PenaltyBreakdown:
        """
        Return the penalty details.

        Used by the dashboard to show "why the score dropped".

        Args:
            namespace: target namespace

        Returns:
            PenaltyBreakdown instance
        """
        tracker = self._get_tracker()
        state = tracker.get_effective_state(namespace=namespace)
        penalty = self.calculate_penalty(namespace=namespace)

        if not state.is_active or penalty == 0:
            return PenaltyBreakdown(
                penalty=0.0,
                reason=None,
                scope=None,
                emergency_level="NORMAL",
                governance_mode="NORMAL",
                namespace=namespace,
            )

        # Build the detailed reason
        scope_str = (
            state.scope.value if hasattr(state.scope, "value") else str(state.scope)
        )
        level_name = getattr(state.emergency_level, "name", str(state.emergency_level))

        reason = (
            f"Emergency {scope_str.upper()} {state.governance_mode} active "
            f"(Level: {level_name})"
        )
        if state.activated_at:
            reason += f" since {state.activated_at}"

        return PenaltyBreakdown(
            penalty=penalty,
            reason=reason,
            scope=scope_str,
            emergency_level=level_name,
            governance_mode=state.governance_mode,
            activated_by=state.activated_by,
            activated_at=state.activated_at,
            namespace=namespace,
        )

    def invalidate_cache(self, namespace: str | None = None) -> None:
        """
        Invalidate the cache.

        Call on an Emergency state change to refresh the cache.

        Args:
            namespace: invalidate only this namespace (all if None)
        """
        with self._lock:
            if namespace:
                cache_key = f"penalty:{namespace}"
                self._cached_penalty.pop(cache_key, None)
                self._cache_timestamp.pop(cache_key, None)
            else:
                self._cached_penalty.clear()
                self._cache_timestamp.clear()

        logger.debug(
            "emergency_health_penalty.cache_invalidated",
            target_namespace=namespace or "all",
        )


# =============================================================================
# Singleton
# =============================================================================

_health_penalty: EmergencyHealthPenalty | None = None
_health_penalty_lock = threading.Lock()


def get_emergency_health_penalty() -> EmergencyHealthPenalty:
    """Return the EmergencyHealthPenalty singleton."""
    global _health_penalty
    if _health_penalty is None:
        with _health_penalty_lock:
            if _health_penalty is None:
                _health_penalty = EmergencyHealthPenalty()
    return _health_penalty


def reset_emergency_health_penalty() -> None:
    """
    Reset the singleton (for tests).

    Drops the singleton instance for isolation between tests.
    """
    global _health_penalty
    with _health_penalty_lock:
        _health_penalty = None
