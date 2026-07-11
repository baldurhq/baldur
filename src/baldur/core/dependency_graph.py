"""Service dependency graph — a directed graph of service dependencies.

Manages service dependencies as a directed graph to support
cascading-impact analysis, upstream/downstream traversal, and
topological-sort-based sequential recovery ordering.

Consumers:
- BlastRadiusIntegration: cascading-impact assessment before a CB OPEN
- MeshCoordinator: downstream failure propagation, dampening overrides,
  sequential recovery
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()

__all__ = ["ServiceDependencyGraph", "ServiceDependencyNode"]


@dataclass
class ServiceDependencyNode:
    """Service dependency node.

    Attributes:
        service_id: Service ID
        depends_on: Services this service depends on
        dependents: Services that depend on this service
        criticality: Service criticality level
    """

    service_id: str
    depends_on: list[str] = field(default_factory=list)
    dependents: list[str] = field(default_factory=list)
    criticality: str = "medium"


class ServiceDependencyGraph:
    """Service dependency graph with internal synchronization.

    Manages the dependency information used to analyze cascading-failure
    impact when a CB OPENs. Every public method acquires the internal
    lock once for the whole operation, so long-lived holders of this
    graph (e.g. the mesh coordinator) can traverse concurrently with
    registrations without observing a partially applied update.
    Traversal results are fresh lists — no live internal state escapes.
    """

    def __init__(self) -> None:
        self._dependencies: dict[str, ServiceDependencyNode] = {}
        # One lock for the whole graph: public methods acquire it ONCE
        # per operation and delegate to _*_unlocked helpers, so a
        # multi-step traversal can never interleave with a registration
        # (public lock-wrapping methods never call other public
        # lock-wrapping methods on this instance).
        self._lock = threading.Lock()

    def register_service(
        self,
        service_id: str,
        depends_on: list[str] | None = None,
        criticality: str = "medium",
    ) -> None:
        """Register a service and its dependencies.

        Args:
            service_id: Service ID
            depends_on: Services this service depends on
            criticality: Service criticality level
        """
        depends_on = depends_on or []

        with self._lock:
            if service_id in self._dependencies:
                dep = self._dependencies[service_id]
                dep.depends_on = depends_on
                dep.criticality = criticality
            else:
                self._dependencies[service_id] = ServiceDependencyNode(
                    service_id=service_id,
                    depends_on=depends_on,
                    criticality=criticality,
                )

            for dep_service in depends_on:
                if dep_service not in self._dependencies:
                    self._dependencies[dep_service] = ServiceDependencyNode(
                        service_id=dep_service,
                    )
                self._dependencies[dep_service].dependents.append(service_id)

    def set_criticality(self, service_id: str, criticality: str) -> bool:
        """Set the criticality of an already-registered service.

        Args:
            service_id: Service ID
            criticality: New criticality level

        Returns:
            True when the service exists and was updated, False otherwise.
        """
        with self._lock:
            node = self._dependencies.get(service_id)
            if node is None:
                return False
            node.criticality = criticality
            return True

    def get_dependents(self, service_id: str) -> list[str]:
        """Get the services that depend on a service.

        Args:
            service_id: Service ID

        Returns:
            Services that depend on the given service.
        """
        with self._lock:
            return self._get_dependents_unlocked(service_id)

    def get_cascading_affected(
        self,
        service_id: str,
        visited: set | None = None,
    ) -> list[str]:
        """Get every service cascadingly affected by a service.

        Args:
            service_id: Service ID
            visited: Already-visited services (cycle protection)

        Returns:
            Recursively affected services.
        """
        with self._lock:
            return self._get_cascading_affected_unlocked(service_id, visited)

    def get_critical_dependents(self, service_id: str) -> list[str]:
        """Get the critical services that depend on a service.

        Args:
            service_id: Service ID

        Returns:
            Affected services whose criticality is "critical".
        """
        with self._lock:
            affected = self._get_cascading_affected_unlocked(service_id, None)
            return [
                s
                for s in affected
                if s in self._dependencies
                and self._dependencies[s].criticality == "critical"
            ]

    def get_dependencies(self, service_id: str) -> list[str]:
        """Get the downstream services a service depends on.

        Args:
            service_id: Service ID

        Returns:
            Services the given service depends on (depends_on).
        """
        with self._lock:
            node = self._dependencies.get(service_id)
            if node is None:
                return []
            return list(node.depends_on)

    def get_dependents_recursive(
        self,
        service_id: str,
        max_depth: int = 1,
        _visited: set[str] | None = None,
        _current_depth: int = 0,
    ) -> list[tuple[str, int]]:
        """Recursive upstream traversal for dampening propagation.

        Returns:
            [(service_name, depth)] where depth is the distance from the
            origin service.

        Cycle protection: a visited set prevents re-traversal, following
        the BFS + visited pattern of get_cascading_affected(). Circular
        dependencies detected during the traversal are logged and counted
        AFTER the graph lock is released, so the metrics/logging pipeline
        never runs under the lock.
        """
        circular: list[tuple[str, str]] = []
        with self._lock:
            results = self._get_dependents_recursive_unlocked(
                service_id,
                max_depth,
                _visited if _visited is not None else set(),
                _current_depth,
                circular,
            )
        for origin, dependent in circular:
            self._emit_circular_dependency(origin, dependent)
        return results

    def topological_sort_subset(
        self,
        services: list[str],
        direction: str = "leaves_first",
    ) -> list[str]:
        """Topologically sort a subset of services.

        direction="leaves_first": downstream (dependency-free leaves) →
        upstream order (for recovery).
        direction="roots_first": upstream (roots) → downstream order.

        Kahn's algorithm on the subgraph. The subgraph edges are read
        under the lock; the sort itself runs on that local snapshot.
        """
        subset = set(services)
        if not subset:
            return []

        in_degree: dict[str, int] = dict.fromkeys(subset, 0)
        adjacency: dict[str, list[str]] = {s: [] for s in subset}

        with self._lock:
            for s in subset:
                if s not in self._dependencies:
                    continue
                for dep in self._dependencies[s].depends_on:
                    if dep in subset:
                        adjacency[dep].append(s)
                        in_degree[s] += 1

        queue = [s for s in subset if in_degree[s] == 0]
        result: list[str] = []

        while queue:
            queue.sort()
            node = queue.pop(0)
            result.append(node)
            for neighbor in adjacency.get(node, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        remaining = [s for s in subset if s not in result]
        result.extend(sorted(remaining))

        if direction == "leaves_first":
            return result
        return list(reversed(result))

    def clear(self) -> None:
        """Clear all dependency information."""
        with self._lock:
            self._dependencies.clear()

    # ── Locked traversal helpers (caller holds self._lock) ──────────

    def _get_dependents_unlocked(self, service_id: str) -> list[str]:
        """Direct dependents of a service; caller holds the lock."""
        node = self._dependencies.get(service_id)
        if node is None:
            return []
        return list(set(node.dependents))

    def _get_cascading_affected_unlocked(
        self,
        service_id: str,
        visited: set | None,
    ) -> list[str]:
        """Recursive cascading-impact collection; caller holds the lock."""
        if visited is None:
            visited = set()

        if service_id in visited:
            return []

        visited.add(service_id)
        affected = []

        for dependent in self._get_dependents_unlocked(service_id):
            if dependent not in visited:
                affected.append(dependent)
                affected.extend(
                    self._get_cascading_affected_unlocked(dependent, visited)
                )

        return list(set(affected))

    def _get_dependents_recursive_unlocked(
        self,
        service_id: str,
        max_depth: int,
        visited: set[str],
        current_depth: int,
        circular: list[tuple[str, str]],
    ) -> list[tuple[str, int]]:
        """Depth-tagged upstream traversal; caller holds the lock.

        Circular dependencies are collected into ``circular`` for
        post-release emission instead of being logged inline.
        """
        if current_depth >= max_depth or service_id in visited:
            return []

        visited.add(service_id)
        results: list[tuple[str, int]] = []

        for dependent in self._get_dependents_unlocked(service_id):
            if dependent in visited:
                circular.append((service_id, dependent))
                continue
            results.append((dependent, current_depth + 1))
            results.extend(
                self._get_dependents_recursive_unlocked(
                    dependent, max_depth, visited, current_depth + 1, circular
                )
            )

        return results

    def _emit_circular_dependency(self, service_id: str, dependent: str) -> None:
        """Log + count one circular dependency found during a traversal.

        Runs after the graph lock is released — the metrics/logging
        pipeline must never execute under the graph lock.
        """
        logger.warning(
            "dependency_graph.circular_dependency_detected",
            service=service_id,
            dependent=dependent,
        )
        try:
            from baldur.metrics.prometheus import get_metrics

            metrics = get_metrics()
            # `_initialized` is a private impl detail; not on Protocol.
            initialized = getattr(metrics, "_initialized", False)
            if initialized and hasattr(
                metrics, "mesh_circular_dependency_detected_total"
            ):
                metrics.mesh_circular_dependency_detected_total.inc()
        except Exception:
            pass
