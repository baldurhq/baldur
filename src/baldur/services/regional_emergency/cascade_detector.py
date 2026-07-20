"""
Regional Cascade Detector.

Multi-region cascading failure detection and GLOBAL escalation recommendation.

When several regions enter STRICT state at the same time, that is treated as a
sign of a global failure and a GLOBAL Emergency escalation is proposed.

Main features:
- check_cascade_condition(): check the cascading failure condition
- get_cascade_status(): look up the current cascade state
- auto_escalate_to_global(): automatic GLOBAL escalation (when configured)

Cascade condition:
- Two or more regions in STRICT state at the same time
- Multiple regions activated within a short window

Code reference:
    coordination/anti_flapping.py (AntiFlappingGuard pattern)
    isolation/regional_gate.py (list_isolated_regions pattern)
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from baldur.core.serializable import SerializableMixin
from baldur.models.emergency import EmergencyLevel, EmergencyScope
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# =============================================================================
# Constants
# =============================================================================

DEFAULT_ESCALATION_THRESHOLD = 2
"""Default escalation threshold: cascade when 2 or more regions are STRICT."""

DEFAULT_CASCADE_WINDOW_MINUTES = 30
"""Time window for the cascade decision (minutes)."""


def _get_escalation_threshold() -> int:
    """Load escalation_threshold from settings."""
    try:
        from baldur.settings.regional_emergency import (
            get_regional_emergency_settings,
        )

        return get_regional_emergency_settings().escalation_threshold
    except ImportError:
        return DEFAULT_ESCALATION_THRESHOLD


def _get_cascade_window_minutes() -> int:
    """Load cascade_window_minutes from settings."""
    try:
        from baldur.settings.regional_emergency import (
            get_regional_emergency_settings,
        )

        return get_regional_emergency_settings().cascade_window_minutes
    except ImportError:
        return DEFAULT_CASCADE_WINDOW_MINUTES


@dataclass
class CascadeDetectionEvent(SerializableMixin):
    """
    Cascade event information.

    Event record created when a cascading failure is detected.
    """

    event_id: str = ""
    """Unique event ID."""

    detected_at: datetime = field(default_factory=lambda: utc_now())
    """Detection time."""

    affected_regions: list[str] = field(default_factory=list)
    """List of affected regions."""

    total_strict_count: int = 0
    """Number of regions in STRICT state."""

    threshold: int = DEFAULT_ESCALATION_THRESHOLD
    """Applied threshold."""

    auto_escalated: bool = False
    """Whether an automatic GLOBAL escalation happened."""

    escalated_at: datetime | None = None
    """Escalation time."""

    escalated_by: str = ""
    """Who escalated ("system" or an admin ID)."""


class RegionalCascadeDetector:
    """
    Multi-region cascading failure detector.

    Recommends a GLOBAL escalation when several regions are STRICT at once.

    Design principles:
    - Recommend only by default (auto_escalate=False)
    - Manual escalation after operator confirmation is the safe path
    - auto_escalate=True switches to GLOBAL automatically (dangerous!)

    Usage:
        detector = RegionalCascadeDetector(threshold=2)

        # Periodic check (e.g. every minute)
        result = detector.check_cascade_condition()

        if result["cascade_detected"]:
            print(f"⚠️ Cascade detected: {result['affected_regions']}")
            print(f"Recommendation: {result['recommendation']}")
    """

    def __init__(
        self,
        tracker: Any | None = None,
        escalation_threshold: int | None = None,
        cascade_window_minutes: int | None = None,
        auto_escalate: bool = False,
    ):
        """
        Initialize RegionalCascadeDetector.

        Args:
            tracker: NamespacedEmergencyTracker instance
            escalation_threshold: STRICT region count threshold (from settings if None)
            cascade_window_minutes: cascade decision window (from settings if None)
            auto_escalate: True escalates to GLOBAL automatically (dangerous,
                default: False)
        """
        self._tracker = tracker
        self._threshold = (
            escalation_threshold
            if escalation_threshold is not None
            else _get_escalation_threshold()
        )
        self._window_minutes = (
            cascade_window_minutes
            if cascade_window_minutes is not None
            else _get_cascade_window_minutes()
        )
        self._auto_escalate = auto_escalate
        self._lock = threading.Lock()

        # Cascade event history (in-memory buffer)
        self._cascade_history: list[CascadeDetectionEvent] = []
        self._max_history_size = 100

    def _get_tracker(self) -> Any:
        """Obtain the NamespacedEmergencyTracker instance."""
        if self._tracker is None:
            from baldur.services.regional_emergency.tracker import (
                get_namespaced_emergency_tracker,
            )

            self._tracker = get_namespaced_emergency_tracker()
        return self._tracker

    def check_cascade_condition(self) -> dict[str, Any]:
        """
        Check the cascading failure condition.

        Returns:
            dict:
                cascade_detected: whether a cascade was detected
                strict_count: number of regions in STRICT state
                affected_regions: list of affected regions
                threshold: applied threshold
                recommendation: recommended action
                auto_escalated: whether an automatic escalation was performed
                checked_at: check time
        """
        tracker = self._get_tracker()

        # Look up the active namespaces
        active_namespaces = tracker.get_all_active_namespaces()

        # Keep only Regional STRICT regions, excluding Global
        regional_strict = []
        for ns in active_namespaces:
            if ns == "global":
                continue
            state = tracker.get_state(namespace=ns)
            if state.governance_mode == "STRICT":
                regional_strict.append(ns)

        strict_count = len(regional_strict)
        cascade_detected = strict_count >= self._threshold

        result = {
            "cascade_detected": cascade_detected,
            "strict_count": strict_count,
            "affected_regions": regional_strict,
            "threshold": self._threshold,
            "recommendation": "",
            "auto_escalated": False,
            "checked_at": utc_now().isoformat(),
        }

        if cascade_detected:
            result["recommendation"] = (
                f"⚠️ {strict_count} regions in STRICT mode (threshold: {self._threshold}). "
                "Consider activating GLOBAL emergency mode."
            )

            logger.warning(
                "cascade_detector.cascade_condition_detected",
                regional_strict=regional_strict,
                strict_count=strict_count,
            )

            # Record the cascade event
            event = self._record_cascade_event(regional_strict)

            # Automatic escalation (when configured)
            if self._auto_escalate:
                self._escalate_to_global(regional_strict, event)
                result["auto_escalated"] = True
                result["recommendation"] = (
                    f"🚨 AUTO-ESCALATED to GLOBAL: {strict_count} regions affected"
                )
        else:
            result["recommendation"] = "✅ No cascade condition detected."

        return result

    def get_cascade_status(self) -> dict[str, Any]:
        """
        Look up the current cascade state.

        Short form of check_cascade_condition().

        Returns:
            dict: cascade state information
        """
        return self.check_cascade_condition()

    def get_recent_cascade_events(self, limit: int = 10) -> list[dict[str, Any]]:
        """
        Look up recent cascade events.

        Args:
            limit: maximum number of entries to return

        Returns:
            List of cascade events (newest first)
        """
        with self._lock:
            events = self._cascade_history[-limit:]
            return [e.to_dict() for e in reversed(events)]

    def manual_escalate_to_global(
        self,
        escalated_by: str,
        reason: str,
    ) -> dict[str, Any]:
        """
        Manual GLOBAL escalation.

        Used when an operator reviews the cascade state and escalates by hand.

        Args:
            escalated_by: who escalated (admin ID)
            reason: escalation reason

        Returns:
            Escalation result
        """
        tracker = self._get_tracker()

        # Check the current state
        active_regions = tracker.get_all_active_namespaces()
        regional_strict = [
            ns
            for ns in active_regions
            if ns != "global" and tracker.get_state(ns).governance_mode == "STRICT"
        ]

        # Activate GLOBAL
        state = tracker.activate_emergency(
            level=EmergencyLevel.LEVEL_3,
            activated_by=escalated_by,
            reason=f"Manual cascade escalation: {reason}. Affected: {regional_strict}",
            scope=EmergencyScope.GLOBAL,
        )

        # Record the event
        event = CascadeDetectionEvent(
            event_id=f"cascade-manual-{utc_now().strftime('%Y%m%d%H%M%S')}",
            affected_regions=regional_strict,
            total_strict_count=len(regional_strict),
            threshold=self._threshold,
            auto_escalated=False,
            escalated_at=utc_now(),
            escalated_by=escalated_by,
        )

        with self._lock:
            self._cascade_history.append(event)
            if len(self._cascade_history) > self._max_history_size:
                self._cascade_history = self._cascade_history[-self._max_history_size :]

        logger.critical(
            "cascade_detector.manual_escalation_global",
            escalated_by=escalated_by,
            regional_strict=regional_strict,
        )

        return {
            "success": True,
            "escalated_to": "GLOBAL",
            "escalated_by": escalated_by,
            "affected_regions": regional_strict,
            "state": state.to_dict(),
        }

    # =========================================================================
    # Private Methods
    # =========================================================================

    def _record_cascade_event(
        self, affected_regions: list[str]
    ) -> CascadeDetectionEvent:
        """Record a cascade event."""
        event = CascadeDetectionEvent(
            event_id=f"cascade-{utc_now().strftime('%Y%m%d%H%M%S')}",
            affected_regions=affected_regions,
            total_strict_count=len(affected_regions),
            threshold=self._threshold,
        )

        with self._lock:
            self._cascade_history.append(event)
            if len(self._cascade_history) > self._max_history_size:
                self._cascade_history = self._cascade_history[-self._max_history_size :]

        return event

    def _escalate_to_global(
        self,
        affected_regions: list[str],
        event: CascadeDetectionEvent,
    ) -> None:
        """Automatic GLOBAL escalation (only when auto_escalate=True)."""
        tracker = self._get_tracker()

        tracker.activate_emergency(
            level=EmergencyLevel.LEVEL_3,
            activated_by="CascadeDetector",
            reason=f"Auto cascade escalation: {len(affected_regions)} regions affected",
            scope=EmergencyScope.GLOBAL,
        )

        # Update the event
        event.auto_escalated = True
        event.escalated_at = utc_now()
        event.escalated_by = "CascadeDetector"

        # Audit log
        try:
            from baldur.services.regional_emergency.escalation_audit import (
                EscalationDecisionType,
                get_escalation_audit_trail,
            )

            audit = get_escalation_audit_trail()
            audit.log_decision(
                decision_type=EscalationDecisionType.CASCADE_ESCALATION,
                decision_reason=f"Auto-escalated: {len(affected_regions)} regions in STRICT",
                namespace="global",
                effective_state={"governance_mode": "STRICT", "scope": "global"},
                triggered_by="CascadeDetector",
            )
        except Exception as e:
            logger.warning(
                "cascade_detector.audit_log_failed",
                error=e,
            )

        logger.critical(
            "cascade_detector.auto_escalated_global_strict",
            affected_regions=affected_regions,
        )


# =============================================================================
# Singleton
# =============================================================================

_cascade_detector: RegionalCascadeDetector | None = None
_detector_lock = threading.Lock()


def get_cascade_detector() -> RegionalCascadeDetector:
    """Return the RegionalCascadeDetector singleton."""
    global _cascade_detector

    if _cascade_detector is None:
        with _detector_lock:
            if _cascade_detector is None:
                _cascade_detector = RegionalCascadeDetector()

    return _cascade_detector


def reset_cascade_detector() -> None:
    """Reset the singleton (for tests)."""
    global _cascade_detector
    with _detector_lock:
        _cascade_detector = None
