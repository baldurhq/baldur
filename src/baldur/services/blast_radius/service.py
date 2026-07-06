"""
Blast Radius DNA Service - failure impact range management service

Audit Integration (85_AUDIT_INTEGRATION_OVERVIEW.md Phase 1):
- Policy configuration: log_blast_radius_audit (action="set_policy")
- Dependency addition: log_blast_radius_audit (action="add_dependency")
- Service isolation: log_blast_radius_audit (action="isolate_service")
- Isolation release: log_blast_radius_audit (action="release_isolation")
"""

from __future__ import annotations

import uuid
from threading import Lock

import structlog

from baldur.audit.helpers import log_blast_radius_audit

from .models import (
    BlastRadiusLevel,
    BlastRadiusPolicy,
    ImpactAssessment,
    ServiceDependencyEdge,
)

logger = structlog.get_logger()


class BlastRadiusService:
    """
    Blast Radius DNA service

    Analyzes and manages the failure impact range.
    """

    _instance: BlastRadiusService | None = None
    _lock = Lock()

    def __new__(cls) -> BlastRadiusService:
        """Singleton pattern"""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance for test isolation."""
        cls._instance = None

    def __init__(self):
        if self._initialized:
            return

        self._policies: dict[str, BlastRadiusPolicy] = {}
        self._dependencies: list[ServiceDependencyEdge] = []
        self._assessments: list[ImpactAssessment] = []
        self._isolated_services: set[str] = set()
        self._enabled = True
        self._initialized = True

        logger.info("blast_radius_service.initialized")

    def set_policy(
        self,
        service_name: str,
        level: BlastRadiusLevel = BlastRadiusLevel.MINIMAL,
        affected_services: list[str] | None = None,
        max_affected_percentage: float = 10.0,
        auto_isolate: bool = True,
    ) -> BlastRadiusPolicy:
        """
        Configure the impact range policy

        Args:
            service_name: service name
            level: impact range level
            affected_services: list of affected services
            max_affected_percentage: maximum affected percentage
            auto_isolate: whether to auto-isolate

        Returns:
            BlastRadiusPolicy: the configured policy
        """
        policy_id = str(uuid.uuid4())[:8]
        policy = BlastRadiusPolicy(
            policy_id=policy_id,
            service_name=service_name,
            level=level,
            affected_services=affected_services or [],
            max_affected_percentage=max_affected_percentage,
            auto_isolate=auto_isolate,
        )
        self._policies[service_name] = policy
        logger.info(
            "blast.radius_policy_set",
            service_name=service_name,
            blast_radius_level=level.value,
        )

        # === Audit record: policy configuration (85_AUDIT_INTEGRATION Phase 1) ===
        log_blast_radius_audit(
            experiment_id=policy_id,
            blast_radius=level.value,
            target_service=service_name,
            action="set_policy",
            allowed=True,
            target_domain=service_name,
            traffic_percent=max_affected_percentage,
            reason=f"Policy configured with auto_isolate={auto_isolate}",
        )

        return policy

    def get_policy(self, service_name: str) -> BlastRadiusPolicy | None:
        """Look up a policy"""
        return self._policies.get(service_name)

    def add_dependency(
        self,
        source_service: str,
        target_service: str,
        dependency_type: str = "sync",
        criticality: str = "medium",
    ) -> ServiceDependencyEdge:
        """
        Add a service dependency

        Args:
            source_service: source service
            target_service: target service
            dependency_type: dependency type (sync, async, weak)
            criticality: criticality (low, medium, high, critical)

        Returns:
            ServiceDependencyEdge: the created dependency
        """
        dependency = ServiceDependencyEdge(
            source_service=source_service,
            target_service=target_service,
            dependency_type=dependency_type,
            criticality=criticality,
        )
        self._dependencies.append(dependency)

        # === Audit record: dependency addition (85_AUDIT_INTEGRATION Phase 1) ===
        log_blast_radius_audit(
            experiment_id=f"dep-{source_service}-{target_service}",
            blast_radius=criticality,
            target_service=target_service,
            action="add_dependency",
            allowed=True,
            target_domain=source_service,
            reason=f"Dependency added: {source_service} -> {target_service} ({dependency_type})",
        )

        return dependency

    def get_dependencies(self, service: str) -> dict[str, list[ServiceDependencyEdge]]:
        """
        Look up service dependencies

        Args:
            service: service name

        Returns:
            Dict: upstream and downstream dependencies
        """
        upstream = [d for d in self._dependencies if d.target_service == service]
        downstream = [d for d in self._dependencies if d.source_service == service]

        return {
            "upstream": upstream,
            "downstream": downstream,
        }

    def assess_impact(
        self,
        service_name: str,
        trigger_event: str,
        failing_services: list[str],
        total_users: int = 1000,
    ) -> ImpactAssessment:
        """
        Perform an impact assessment

        Args:
            service_name: service name
            trigger_event: trigger event
            failing_services: list of failing services
            total_users: total number of users (for estimation)

        Returns:
            ImpactAssessment: the impact assessment result
        """
        assessment_id = str(uuid.uuid4())[:8]

        # Cascading impact analysis
        all_affected, dependencies_analyzed = self._analyze_cascading_impact(
            failing_services
        )

        # Determine the impact level
        level = self._determine_blast_radius_level(len(all_affected))

        # Compute the affected percentage
        affected_percentage = self._calculate_affected_percentage(len(all_affected))

        # Cascading risk check
        cascading_risk = self._check_cascading_risk(all_affected)

        # Generate recommendations
        recommendations = self._generate_recommendations(
            level, cascading_risk, all_affected
        )

        assessment = self._create_assessment(
            assessment_id=assessment_id,
            service_name=service_name,
            trigger_event=trigger_event,
            level=level,
            all_affected=all_affected,
            total_users=total_users,
            affected_percentage=affected_percentage,
            dependencies_analyzed=dependencies_analyzed,
            cascading_risk=cascading_risk,
            recommendations=recommendations,
        )

        self._assessments.append(assessment)
        logger.info(
            "impact.assessed_services",
            assessment_id=assessment_id,
            blast_radius_level=level.value,
            all_affected_count=len(all_affected),
        )

        # Auto-isolation check
        self._check_and_auto_isolate(service_name, level, failing_services)

        return assessment

    def _analyze_cascading_impact(
        self, failing_services: list[str]
    ) -> tuple[set[str], int]:
        """
        Cascading impact analysis

        Args:
            failing_services: list of failing services

        Returns:
            tuple: (set of affected services, number of dependencies analyzed)
        """
        all_affected = set(failing_services)
        to_check = list(failing_services)
        dependencies_analyzed = 0

        while to_check:
            service = to_check.pop(0)
            deps = self.get_dependencies(service)
            dependencies_analyzed += len(deps["upstream"]) + len(deps["downstream"])

            for dep in deps["upstream"]:
                if dep.source_service not in all_affected:
                    all_affected.add(dep.source_service)
                    if dep.dependency_type == "sync" and dep.criticality in [
                        "high",
                        "critical",
                    ]:
                        to_check.append(dep.source_service)

        return all_affected, dependencies_analyzed

    def _determine_blast_radius_level(self, affected_count: int) -> BlastRadiusLevel:
        """
        Determine the impact level

        Args:
            affected_count: number of affected services

        Returns:
            BlastRadiusLevel: the impact level
        """
        if affected_count <= 1:
            return BlastRadiusLevel.MINIMAL
        if affected_count <= 3:
            return BlastRadiusLevel.CONTAINED
        if affected_count <= 5:
            return BlastRadiusLevel.MODERATE
        if affected_count <= 10:
            return BlastRadiusLevel.EXTENSIVE
        return BlastRadiusLevel.CRITICAL

    def _calculate_affected_percentage(self, affected_count: int) -> float:
        """
        Compute the affected percentage

        Args:
            affected_count: number of affected services

        Returns:
            float: the affected percentage
        """
        total_services = max(len(self._get_all_services()), 1)
        return (affected_count / total_services) * 100

    def _check_cascading_risk(self, all_affected: set[str]) -> bool:
        """
        Cascading risk check

        Args:
            all_affected: set of affected services

        Returns:
            bool: whether there is a cascading risk
        """
        return any(
            d.dependency_type == "sync" and d.criticality == "critical"
            for d in self._dependencies
            if d.source_service in all_affected or d.target_service in all_affected
        )

    def _create_assessment(
        self,
        assessment_id: str,
        service_name: str,
        trigger_event: str,
        level: BlastRadiusLevel,
        all_affected: set[str],
        total_users: int,
        affected_percentage: float,
        dependencies_analyzed: int,
        cascading_risk: bool,
        recommendations: list[str],
    ) -> ImpactAssessment:
        """Create an ImpactAssessment object"""
        return ImpactAssessment(
            assessment_id=assessment_id,
            service_name=service_name,
            trigger_event=trigger_event,
            level=level,
            affected_services=list(all_affected),
            affected_users_estimate=int(total_users * affected_percentage / 100),
            affected_percentage=affected_percentage,
            dependencies_analyzed=dependencies_analyzed,
            cascading_risk=cascading_risk,
            recommendations=recommendations,
        )

    def _check_and_auto_isolate(
        self,
        service_name: str,
        level: BlastRadiusLevel,
        failing_services: list[str],
    ) -> None:
        """Check the auto-isolation condition and execute it"""
        policy = self._policies.get(service_name)
        if policy and policy.auto_isolate and level.value in ["extensive", "critical"]:
            self._auto_isolate(failing_services)

    def _get_all_services(self) -> set[str]:
        """List of all services"""
        services = set()
        for dep in self._dependencies:
            services.add(dep.source_service)
            services.add(dep.target_service)
        for policy in self._policies.values():
            services.update(policy.affected_services)
        return services

    def _generate_recommendations(
        self,
        level: BlastRadiusLevel,
        cascading_risk: bool,
        affected_services: set[str],
    ) -> list[str]:
        """Generate recommendations"""
        recommendations = []

        if level in [BlastRadiusLevel.EXTENSIVE, BlastRadiusLevel.CRITICAL]:
            recommendations.append(
                "Immediate escalation to the incident response team required"
            )
            recommendations.append(
                "Consider an emergency rollback for the affected services"
            )

        if cascading_risk:
            recommendations.append(
                "Cascading failure risk: switching to asynchronous communication is recommended"
            )
            recommendations.append(
                "Circuit Breaker configuration needs to be strengthened"
            )

        if len(affected_services) > 3:
            recommendations.append(
                "Review service separation to isolate the impact range"
            )

        if not recommendations:
            recommendations.append("Currently controllable within the impact range")

        return recommendations

    def _auto_isolate(self, services: list[str]) -> None:
        """Execute auto-isolation"""
        for service in services:
            if service not in self._isolated_services:
                self._isolated_services.add(service)
                logger.warning(
                    "service.auto_isolated",
                    target_service=service,
                )

                # === Audit record: auto-isolation (85_AUDIT_INTEGRATION Phase 1) ===
                log_blast_radius_audit(
                    experiment_id=f"auto-isolate-{service}",
                    blast_radius="auto",
                    target_service=service,
                    action="auto_isolate",
                    allowed=True,
                    reason="Auto-isolation triggered by impact assessment",
                )

    def isolate_service(self, service: str) -> bool:
        """Manual isolation"""
        if service not in self._isolated_services:
            self._isolated_services.add(service)
            logger.info(
                "service.isolated",
                target_service=service,
            )

            # === Audit record: manual isolation (85_AUDIT_INTEGRATION Phase 1) ===
            log_blast_radius_audit(
                experiment_id=f"manual-isolate-{service}",
                blast_radius="manual",
                target_service=service,
                action="isolate_service",
                allowed=True,
                reason="Manual service isolation",
            )

            return True
        return False

    def release_isolation(self, service: str) -> bool:
        """Release isolation"""
        if service in self._isolated_services:
            self._isolated_services.discard(service)
            logger.info(
                "service.isolation_released",
                target_service=service,
            )

            # === Audit record: isolation release (85_AUDIT_INTEGRATION Phase 1) ===
            log_blast_radius_audit(
                experiment_id=f"release-{service}",
                blast_radius="released",
                target_service=service,
                action="release_isolation",
                allowed=True,
                reason="Service isolation released",
            )

            return True
        return False

    def is_isolated(self, service: str) -> bool:
        """Check the isolation state"""
        return service in self._isolated_services

    def get_isolated_services(self) -> list[str]:
        """List of isolated services"""
        return list(self._isolated_services)

    def get_assessments(
        self,
        service_name: str | None = None,
        min_level: BlastRadiusLevel | None = None,
        limit: int = 100,
    ) -> list[ImpactAssessment]:
        """
        Look up the impact assessment history

        Args:
            service_name: service name filter
            min_level: minimum impact level
            limit: maximum count

        Returns:
            List[ImpactAssessment]: list of assessments
        """
        assessments = self._assessments[-limit:]

        if service_name:
            assessments = [a for a in assessments if a.service_name == service_name]

        if min_level:
            level_order = [lv.value for lv in BlastRadiusLevel]
            min_index = level_order.index(min_level.value)
            assessments = [
                a for a in assessments if level_order.index(a.level.value) >= min_index
            ]

        return assessments

    def build_dependency_graph(self) -> dict:
        """
        Build the dependency graph

        Returns:
            Dict: graph data
        """
        nodes = set()
        edges = []

        for dep in self._dependencies:
            nodes.add(dep.source_service)
            nodes.add(dep.target_service)
            edges.append(
                {
                    "source": dep.source_service,
                    "target": dep.target_service,
                    "type": dep.dependency_type,
                    "criticality": dep.criticality,
                }
            )

        return {
            "nodes": list(nodes),
            "edges": edges,
            "isolated": list(self._isolated_services),
        }

    def enable(self) -> None:
        """Enable the service"""
        self._enabled = True

    def disable(self) -> None:
        """Disable the service"""
        self._enabled = False

    def clear(self) -> None:
        """Clear all data (for tests)"""
        self._policies.clear()
        self._dependencies.clear()
        self._assessments.clear()
        self._isolated_services.clear()
