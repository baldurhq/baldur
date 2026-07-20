"""
Dependency Analyzer - dependency analysis and root-cause suppression.

Assesses blast radius before recovery and, during cascading failures, alerts
only on the root cause.

Capabilities:
1. Blast-radius assessment before recovery (impact analysis)
2. Root-cause based alert suppression (avoids duplicate alerts in a cascade)
3. Recovery prioritization (infrastructure → application)
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()


# =============================================================================
# Component dependency map (Meta-Watchdog only)
# =============================================================================

# Components that depend on each infrastructure component
# (infrastructure → application).
# If redis dies, circuit_breaker, dlq and recovery_pipeline are affected.
COMPONENT_DEPENDENCIES: dict[str, list[str]] = {
    "redis": ["circuit_breaker", "dlq", "recovery_pipeline"],
    "database": ["recovery_pipeline"],
    "celery_broker": ["dlq"],
}

# Reverse dependency map (component -> root cause).
# When circuit_breaker is failing, redis may be the root cause.
REVERSE_DEPENDENCIES: dict[str, str] = {}
for _root, _deps in COMPONENT_DEPENDENCIES.items():
    for _dep in _deps:
        REVERSE_DEPENDENCIES[_dep] = _root


@dataclass
class RecoveryImpactAssessment:
    """Recovery impact assessment result."""

    component: str
    """Target component."""

    can_proceed: bool
    """Whether recovery may proceed."""

    blast_radius_level: str
    """Blast radius level (MINIMAL/MODERATE/EXTENSIVE/CRITICAL)."""

    affected_components: list[str]
    """Affected components."""

    block_reason: str | None = None
    """Reason recovery was blocked (when can_proceed=False)."""

    warnings: list[str] = field(default_factory=list)
    """Warning messages."""


@dataclass
class SuppressionResult:
    """Alert suppression result."""

    component: str
    """Target component."""

    suppressed: bool
    """Whether the alert was suppressed."""

    root_cause: str | None
    """Root cause component."""

    reason: str
    """Why the alert was (or was not) suppressed."""


class DependencyAnalyzer:
    """
    Dependency analyzer.

    Capabilities:
    1. Blast-radius assessment before recovery
    2. Root-cause based alert suppression
    3. Recovery prioritization

    Example:
        analyzer = DependencyAnalyzer()

        # Assess recovery impact
        assessment = analyzer.assess_recovery_impact("redis", {"dlq", "circuit_breaker"})
        if assessment.can_proceed:
            perform_recovery()

        # Decide whether to suppress the alert
        result = analyzer.should_suppress_alert("circuit_breaker", {"redis", "circuit_breaker"})
        if not result.suppressed:
            send_alert()
    """

    def __init__(
        self,
        dependencies: dict[str, list[str]] | None = None,
        reverse_deps: dict[str, str] | None = None,
    ):
        """
        Initialize.

        Args:
            dependencies: component dependency map (defaults when None)
            reverse_deps: reverse dependency map (defaults when None)
        """
        self._dependencies = dependencies or COMPONENT_DEPENDENCIES.copy()
        self._reverse_deps = reverse_deps or REVERSE_DEPENDENCIES.copy()

    def assess_recovery_impact(
        self,
        component: str,
        failing_components: set[str] | None = None,
    ) -> RecoveryImpactAssessment:
        """
        Assess impact before recovery.

        Evaluates the effect the recovery target has on other components.

        Args:
            component: recovery target component
            failing_components: components currently failing

        Returns:
            RecoveryImpactAssessment
        """
        failing = failing_components or set()

        # Collect the components that depend on this one
        affected = self._dependencies.get(component, [])
        affected_count = len(affected)

        # Determine the level
        if affected_count >= 5:
            level = "CRITICAL"
            can_proceed = False
            block_reason = f"Too many dependent components ({affected_count})"
        elif affected_count >= 3:
            level = "EXTENSIVE"
            can_proceed = True
            block_reason = None
        elif affected_count >= 1:
            level = "MODERATE"
            can_proceed = True
            block_reason = None
        else:
            level = "MINIMAL"
            can_proceed = True
            block_reason = None

        # Build warnings
        warnings: list[str] = []
        if level in ("EXTENSIVE", "CRITICAL"):
            warnings.append(f"Recovery may affect {affected_count} components")

        # Extra warning when the affected set overlaps already-failing components
        overlap = set(affected) & failing
        if overlap:
            warnings.append(f"Already failing components affected: {overlap}")

        return RecoveryImpactAssessment(
            component=component,
            can_proceed=can_proceed,
            blast_radius_level=level,
            affected_components=affected,
            block_reason=block_reason,
            warnings=warnings,
        )

    def should_suppress_alert(
        self,
        component: str,
        failed_components: set[str],
    ) -> SuppressionResult:
        """
        Decide alert suppression based on the root cause.

        Example: when Redis fails, suppress the CB/DLQ alerts because redis is
        the root cause.

        Args:
            component: component the alert is about
            failed_components: all components currently failing

        Returns:
            SuppressionResult
        """
        # Look up this component's root cause
        root_cause = self._reverse_deps.get(component)

        if root_cause and root_cause in failed_components:
            # The root cause is failing too, so suppress this component's alert
            return SuppressionResult(
                component=component,
                suppressed=True,
                root_cause=root_cause,
                reason=f"Suppressed: {root_cause} is the root cause",
            )

        return SuppressionResult(
            component=component,
            suppressed=False,
            root_cause=None,
            reason="No root cause detected, alert should proceed",
        )

    def get_recovery_priority(
        self,
        failed_components: set[str],
    ) -> list[str]:
        """
        Determine the recovery order.

        The root cause (infrastructure) must be recovered first so that the
        dependent components recover with it.

        Args:
            failed_components: failing components

        Returns:
            Components ordered by priority (recover the first entries first)
        """
        priority: list[str] = []

        # First: root-cause components (infrastructure such as redis, database)
        root_causes = set(self._dependencies.keys())
        for root in root_causes:
            if root in failed_components:
                priority.append(root)

        # Second: the remaining components
        for comp in failed_components:
            if comp not in priority:
                priority.append(comp)

        return priority

    def get_dependent_components(self, component: str) -> list[str]:
        """
        Return the components that depend on the given component.

        Args:
            component: component name

        Returns:
            Dependent components
        """
        return self._dependencies.get(component, [])

    def get_root_cause(self, component: str) -> str | None:
        """
        Return the root cause of a component.

        Args:
            component: component name

        Returns:
            Root cause component name (None when there is none)
        """
        return self._reverse_deps.get(component)

    def add_dependency(self, root: str, dependent: str) -> None:
        """
        Add a dependency.

        Args:
            root: root component (infrastructure)
            dependent: dependent component (application)
        """
        if root not in self._dependencies:
            self._dependencies[root] = []
        if dependent not in self._dependencies[root]:
            self._dependencies[root].append(dependent)
        self._reverse_deps[dependent] = root

    def remove_dependency(self, root: str, dependent: str) -> None:
        """
        Remove a dependency.

        Args:
            root: root component
            dependent: dependent component
        """
        if root in self._dependencies:
            try:
                self._dependencies[root].remove(dependent)
            except ValueError:
                pass
        if self._reverse_deps.get(dependent) == root:
            del self._reverse_deps[dependent]


# =============================================================================
# Singleton
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

get_dependency_analyzer, configure_dependency_analyzer, reset_dependency_analyzer = (
    make_singleton_factory("dependency_analyzer", DependencyAnalyzer)
)
