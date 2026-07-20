"""
Blast Radius Integration for Circuit Breaker

Before a CB automatically OPENs, this analyzes the cascading-failure impact
and holds the OPEN if the impact is at the CRITICAL level.

Integration flow:
    record_failure() called
            │
            ▼
    ┌───────────────────┐
    │ increment failures│
    └────────┬──────────┘
             │
             ▼
        threshold exceeded?
             │
        Yes  │  No
             │   └──▶ exit
             ▼
    ┌───────────────────┐
    │  Blast Radius     │
    │  assess_impact()  │
    └────────┬──────────┘
             │
             ▼
        level == CRITICAL?
             │
        Yes  │  No
             │   └──▶ proceed with OPEN
             ▼
    ┌───────────────────────────────────────┐
    │  Hold OPEN                             │
    │  - alert the operations team           │
    │  - Audit: record GOVERNANCE_BLOCKED    │
    │  - await manual approval               │
    └───────────────────────────────────────┘
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog

from baldur.audit.helpers import log_governance_blocked_cb_audit
from baldur.core.dependency_graph import (  # noqa: F401
    ServiceDependencyGraph,
    ServiceDependencyNode,
)
from baldur.models.blast_radius import BlastRadiusLevel
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# =============================================================================
# Blast Radius Assessment Result
# =============================================================================


@dataclass
class BlastRadiusAssessment:
    """
    Blast Radius impact assessment result.

    Attributes:
        assessment_id: Unique assessment ID
        level: Impact level
        trigger_service: Trigger service (CB OPEN target)
        affected_services: List of affected services
        affected_count: Number of affected services
        cascading_risk: Whether there is cascading-failure risk
        critical_services_affected: List of affected critical services
        recommendation: Recommended action
        details: Additional detail
        timestamp: Assessment time
    """

    assessment_id: str = field(default_factory=lambda: f"blast-{uuid.uuid4().hex[:8]}")
    level: BlastRadiusLevel = BlastRadiusLevel.MINIMAL
    trigger_service: str = ""
    affected_services: list[str] = field(default_factory=list)
    affected_count: int = 0
    cascading_risk: bool = False
    critical_services_affected: list[str] = field(default_factory=list)
    recommendation: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: utc_now().isoformat())

    def should_block_auto_open(self) -> bool:
        """Whether automatic OPEN should be blocked."""
        return self.level == BlastRadiusLevel.CRITICAL


# =============================================================================
# Blast Radius Integration Manager
# =============================================================================


class BlastRadiusIntegration:
    """
    Manager integrating Circuit Breaker with Blast Radius.

    Before a CB automatically OPENs, this analyzes the cascading-failure impact
    and holds the OPEN if the impact is at the CRITICAL level.

    Usage:
        integration = BlastRadiusIntegration()

        # Register dependencies
        integration.register_dependency("order-api", depends_on=["payment-api", "inventory-api"])
        integration.register_dependency("cart-api", depends_on=["payment-api"])

        # Assess impact before CB OPEN
        assessment = integration.assess_impact(
            trigger_service="payment-api",
            trigger_event="CB auto-opening due to timeout errors",
        )

        if assessment.should_block_auto_open():
            # Hold OPEN, alert the operations team
            send_alert(assessment)
    """

    _instance: BlastRadiusIntegration | None = None

    def __new__(cls):
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return

        self._dependency_graph = ServiceDependencyGraph()
        self._service_criticality: dict[str, str] = {}
        self._config = BlastRadiusConfig()
        self._last_assessment: BlastRadiusAssessment | None = None
        self._initialized = True

        logger.debug("blast_radius_integration.initialized")

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance (for tests)."""
        cls._instance = None

    @property
    def dependency_graph(self) -> ServiceDependencyGraph:
        """Access the service dependency graph.

        The returned graph is the live, internally synchronized
        instance: registrations and multi-step traversals may run
        concurrently, and traversal methods return fresh lists (no live
        internal state escapes the graph).
        """
        return self._dependency_graph

    # =========================================================================
    # Configuration
    # =========================================================================

    def configure(
        self,
        critical_threshold: int = 6,
        extensive_threshold: int = 4,
        moderate_threshold: int = 2,
        block_on_critical: bool = True,
        alert_on_extensive: bool = True,
    ) -> None:
        """
        Blast Radius configuration.

        Args:
            critical_threshold: CRITICAL level threshold (number of affected services)
            extensive_threshold: EXTENSIVE level threshold
            moderate_threshold: MODERATE level threshold
            block_on_critical: Whether to block auto OPEN on CRITICAL
            alert_on_extensive: Whether to send an alert on EXTENSIVE
        """
        self._config = BlastRadiusConfig(
            critical_threshold=critical_threshold,
            extensive_threshold=extensive_threshold,
            moderate_threshold=moderate_threshold,
            block_on_critical=block_on_critical,
            alert_on_extensive=alert_on_extensive,
        )
        logger.info(
            "blast_radius_integration.configured",
            critical_threshold=critical_threshold,
        )

    # =========================================================================
    # Dependency Management
    # =========================================================================

    def register_dependency(
        self,
        service_id: str,
        depends_on: list[str] | None = None,
        criticality: str = "medium",
    ) -> None:
        """
        Register a service dependency.

        Args:
            service_id: Service ID
            depends_on: List of services this service depends on
            criticality: Service criticality
        """
        self._dependency_graph.register_service(
            service_id=service_id,
            depends_on=depends_on,
            criticality=criticality,
        )
        self._service_criticality[service_id] = criticality

        logger.debug(
            "blast_radius_integration.dependency_registered",
            service_id=service_id,
            criticality=criticality,
            depends_on=depends_on,
        )

    def set_service_criticality(self, service_id: str, criticality: str) -> None:
        """
        Set the service criticality.

        Args:
            service_id: Service ID
            criticality: criticality level
        """
        self._service_criticality[service_id] = criticality
        self._dependency_graph.set_criticality(service_id, criticality)

    def clear_dependencies(self) -> None:
        """Clear all dependency information."""
        self._dependency_graph.clear()
        self._service_criticality.clear()

    # =========================================================================
    # Impact Assessment
    # =========================================================================

    def assess_impact(
        self,
        trigger_service: str,
        trigger_event: str = "",
        failing_services: list[str] | None = None,
    ) -> BlastRadiusAssessment:
        """
        Assess impact on CB OPEN.

        Args:
            trigger_service: Trigger service (CB OPEN target)
            trigger_event: Trigger event description
            failing_services: List of additionally failing services

        Returns:
            BlastRadiusAssessment: Impact assessment result
        """
        failing_services = failing_services or []
        all_failing = list(set([trigger_service] + failing_services))

        # 1. Collect cascadingly affected services
        affected_services = set()
        for service in all_failing:
            affected = self._dependency_graph.get_cascading_affected(service)
            affected_services.update(affected)

        # Exclude the trigger service itself
        affected_services.discard(trigger_service)
        affected_list = list(affected_services)
        affected_count = len(affected_list)

        # 2. Check critical-service impact
        critical_affected = [
            s for s in affected_list if self._service_criticality.get(s) == "critical"
        ]

        # 3. Determine level
        level = self._determine_level(affected_count, critical_affected)

        # 4. Determine cascading-failure risk
        cascading_risk = (
            affected_count >= self._config.moderate_threshold
            or len(critical_affected) > 0
        )

        # 5. Determine recommended action
        recommendation = self._get_recommendation(level, critical_affected)

        # Build the assessment result
        assessment = BlastRadiusAssessment(
            level=level,
            trigger_service=trigger_service,
            affected_services=affected_list,
            affected_count=affected_count,
            cascading_risk=cascading_risk,
            critical_services_affected=critical_affected,
            recommendation=recommendation,
            details={
                "trigger_event": trigger_event,
                "failing_services": all_failing,
                "config": {
                    "critical_threshold": self._config.critical_threshold,
                    "block_on_critical": self._config.block_on_critical,
                },
            },
        )

        self._last_assessment = assessment

        logger.info(
            "blast_radius_integration.impact_assessed",
            trigger_service=trigger_service,
            blast_radius_level=level.value,
            affected_count=affected_count,
            critical_affected_count=len(critical_affected),
        )

        return assessment

    def _determine_level(
        self,
        affected_count: int,
        critical_affected: list[str],
    ) -> BlastRadiusLevel:
        """Determine the impact level."""
        # If a critical service is affected, always CRITICAL
        if critical_affected:
            return BlastRadiusLevel.CRITICAL

        # Decide based on the number of affected services
        if affected_count >= self._config.critical_threshold:
            return BlastRadiusLevel.CRITICAL
        if affected_count >= self._config.extensive_threshold:
            return BlastRadiusLevel.EXTENSIVE
        if affected_count >= self._config.moderate_threshold:
            return BlastRadiusLevel.MODERATE
        return BlastRadiusLevel.MINIMAL

    def _get_recommendation(
        self,
        level: BlastRadiusLevel,
        critical_affected: list[str],
    ) -> str:
        """Determine the recommended action."""
        if level == BlastRadiusLevel.CRITICAL:
            if critical_affected:
                return (
                    f"Recommend blocking CB OPEN: critical services affected ({', '.join(critical_affected)}). "
                    f"Manual approval required."
                )
            return "Recommend blocking CB OPEN: blast radius too wide. Manual approval required."
        if level == BlastRadiusLevel.EXTENSIVE:
            return "CB OPEN may proceed, but a warning alert to the operations team is required."
        if level == BlastRadiusLevel.MODERATE:
            return "CB OPEN may proceed; enhanced monitoring recommended."
        return "CB OPEN may proceed; minimal impact."

    # =========================================================================
    # Auto OPEN Decision
    # =========================================================================

    def should_auto_open(
        self,
        service_id: str,
        trigger_event: str = "threshold_exceeded",
    ) -> tuple[bool, str | None, BlastRadiusAssessment | None]:
        """
        Decide whether automatic OPEN is allowed.

        Args:
            service_id: Service ID
            trigger_event: Trigger event

        Returns:
            tuple[bool, Optional[str], Optional[BlastRadiusAssessment]]:
                (whether allowed, reason if denied, assessment result)
        """
        # 1. Assess Blast Radius impact
        assessment = self.assess_impact(
            trigger_service=service_id,
            trigger_event=f"CB auto-opening: {trigger_event}",
        )

        # 2. Block if CRITICAL and block_on_critical
        if (
            assessment.level == BlastRadiusLevel.CRITICAL
            and self._config.block_on_critical
        ):
            reason = (
                f"Blast Radius CRITICAL: {assessment.affected_count} services affected. "
                f"Cascading risk: {assessment.cascading_risk}"
            )

            # Audit record
            self._log_governance_blocked(service_id, assessment)

            return False, reason, assessment

        # 3. Warn only if EXTENSIVE
        if (
            assessment.level == BlastRadiusLevel.EXTENSIVE
            and self._config.alert_on_extensive
        ):
            logger.warning(
                "blast_radius_integration.cb_auto_open_proceeding",
                service_id=service_id,
                assessment=assessment.affected_count,
            )

        return True, None, assessment

    def _log_governance_blocked(
        self,
        service_id: str,
        assessment: BlastRadiusAssessment,
    ) -> None:
        """GOVERNANCE_BLOCKED audit record."""
        log_governance_blocked_cb_audit(
            service_id=service_id,
            action="auto_open",
            block_reason="blast_radius_critical",
            blast_radius_level=assessment.level.value.upper(),
            affected_services=assessment.affected_services,
            assessment_id=assessment.assessment_id,
        )

    # =========================================================================
    # Status
    # =========================================================================

    def get_last_assessment(self) -> BlastRadiusAssessment | None:
        """Look up the last assessment result."""
        return self._last_assessment

    def get_status(self) -> dict[str, Any]:
        """Look up the current status."""
        return {
            "registered_services": len(self._service_criticality),
            "config": {
                "critical_threshold": self._config.critical_threshold,
                "extensive_threshold": self._config.extensive_threshold,
                "moderate_threshold": self._config.moderate_threshold,
                "block_on_critical": self._config.block_on_critical,
                "alert_on_extensive": self._config.alert_on_extensive,
            },
            "last_assessment": (
                {
                    "assessment_id": self._last_assessment.assessment_id,
                    "level": self._last_assessment.level.value,
                    "affected_count": self._last_assessment.affected_count,
                    "timestamp": self._last_assessment.timestamp,
                }
                if self._last_assessment
                else None
            ),
            "timestamp": utc_now().isoformat(),
        }


# =============================================================================
# Blast Radius Config
# =============================================================================


@dataclass
class BlastRadiusConfig:
    """Blast Radius configuration."""

    critical_threshold: int = 6  # CRITICAL threshold (number of affected services)
    extensive_threshold: int = 4  # EXTENSIVE threshold
    moderate_threshold: int = 2  # MODERATE threshold
    block_on_critical: bool = True  # Block auto OPEN on CRITICAL
    alert_on_extensive: bool = True  # Send alert on EXTENSIVE


# =============================================================================
# Module-level Convenience Functions
# =============================================================================


_integration: BlastRadiusIntegration | None = None
_integration_lock = threading.Lock()


def get_blast_radius_integration() -> BlastRadiusIntegration:
    """
    Return the BlastRadiusIntegration singleton instance.

    Returns:
        BlastRadiusIntegration: singleton instance
    """
    global _integration
    if _integration is None:
        with _integration_lock:
            if _integration is None:
                _integration = BlastRadiusIntegration()
    return _integration


def reset_blast_radius_integration() -> None:
    """Reset singleton instance (for tests)."""
    global _integration
    _integration = None
    BlastRadiusIntegration.reset_instance()


def assess_cb_open_impact(
    service_id: str,
    trigger_event: str = "threshold_exceeded",
) -> BlastRadiusAssessment:
    """
    Assess impact on CB OPEN.

    Args:
        service_id: Service ID
        trigger_event: Trigger event

    Returns:
        BlastRadiusAssessment: Impact assessment result
    """
    return get_blast_radius_integration().assess_impact(
        trigger_service=service_id,
        trigger_event=trigger_event,
    )


def should_allow_cb_auto_open(
    service_id: str,
    trigger_event: str = "threshold_exceeded",
) -> tuple[bool, str | None]:
    """
    Whether CB automatic OPEN is allowed.

    Args:
        service_id: Service ID
        trigger_event: Trigger event

    Returns:
        tuple[bool, Optional[str]]: (whether allowed, reason if denied)
    """
    allowed, reason, _ = get_blast_radius_integration().should_auto_open(
        service_id=service_id,
        trigger_event=trigger_event,
    )
    return allowed, reason


def register_service_dependency(
    service_id: str,
    depends_on: list[str] | None = None,
    criticality: str = "medium",
) -> None:
    """
    Register a service dependency.

    Args:
        service_id: Service ID
        depends_on: List of services depended on
        criticality: criticality level
    """
    get_blast_radius_integration().register_dependency(
        service_id=service_id,
        depends_on=depends_on,
        criticality=criticality,
    )
