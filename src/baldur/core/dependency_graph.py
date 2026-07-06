"""
Service Dependency Graph — 서비스 간 의존성 방향 그래프 관리.

서비스 의존성을 방향 그래프로 관리하여 연쇄 영향 분석,
상류/하류 탐색, 위상 정렬 기반 순차 복구 순서 결정을 지원한다.

소비자:
- BlastRadiusIntegration: CB OPEN 시 연쇄 영향 평가
- MeshCoordinator: 하류 장애 전파, 감쇠 오버라이드, 순차 복구
- (향후) ChaosImpactPredictor, ErrorBudgetPropagation 등
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()


@dataclass
class ServiceDependencyNode:
    """
    서비스 의존성 노드 정보.

    Attributes:
        service_id: 서비스 ID
        depends_on: 이 서비스가 의존하는 서비스 목록
        dependents: 이 서비스에 의존하는 서비스 목록
        criticality: 서비스 criticality
    """

    service_id: str
    depends_on: list[str] = field(default_factory=list)
    dependents: list[str] = field(default_factory=list)
    criticality: str = "medium"


class ServiceDependencyGraph:
    """
    서비스 의존성 그래프 관리.

    CB OPEN 시 연쇄 장애 영향을 분석하기 위한 의존성 정보를 관리합니다.
    """

    def __init__(self):
        self._dependencies: dict[str, ServiceDependencyNode] = {}

    def register_service(
        self,
        service_id: str,
        depends_on: list[str] | None = None,
        criticality: str = "medium",
    ) -> None:
        """
        서비스와 의존성 등록.

        Args:
            service_id: 서비스 ID
            depends_on: 이 서비스가 의존하는 서비스 목록
            criticality: 서비스 criticality
        """
        depends_on = depends_on or []

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

    def get_dependents(self, service_id: str) -> list[str]:
        """
        서비스에 의존하는 서비스 목록 조회.

        Args:
            service_id: 서비스 ID

        Returns:
            List[str]: 의존하는 서비스 목록
        """
        if service_id not in self._dependencies:
            return []
        return list(set(self._dependencies[service_id].dependents))

    def get_cascading_affected(
        self,
        service_id: str,
        visited: set | None = None,
    ) -> list[str]:
        """
        연쇄적으로 영향받는 모든 서비스 조회.

        Args:
            service_id: 서비스 ID
            visited: 방문한 서비스 (순환 방지)

        Returns:
            List[str]: 영향받는 서비스 목록 (재귀적)
        """
        if visited is None:
            visited = set()

        if service_id in visited:
            return []

        visited.add(service_id)
        affected = []

        for dependent in self.get_dependents(service_id):
            if dependent not in visited:
                affected.append(dependent)
                affected.extend(self.get_cascading_affected(dependent, visited))

        return list(set(affected))

    def get_critical_dependents(self, service_id: str) -> list[str]:
        """
        서비스에 의존하는 critical 서비스 목록 조회.

        Args:
            service_id: 서비스 ID

        Returns:
            List[str]: critical 서비스 목록
        """
        affected = self.get_cascading_affected(service_id)
        return [
            s
            for s in affected
            if s in self._dependencies
            and self._dependencies[s].criticality == "critical"
        ]

    def get_dependencies(self, service_id: str) -> list[str]:
        """
        서비스가 의존하는 하류 서비스 목록 조회.

        Args:
            service_id: 서비스 ID

        Returns:
            이 서비스가 의존하는 서비스 목록 (depends_on)
        """
        if service_id not in self._dependencies:
            return []
        return list(self._dependencies[service_id].depends_on)

    def get_dependents_recursive(
        self,
        service_id: str,
        max_depth: int = 1,
        _visited: set[str] | None = None,
        _current_depth: int = 0,
    ) -> list[tuple[str, int]]:
        """
        감쇠 전파를 위한 재귀적 상류 서비스 탐색.

        Returns:
            [(service_name, depth)] — depth는 원본 서비스로부터의 거리.

        순환 참조 방어: visited set으로 이미 방문한 노드는 재탐색하지 않는다.
        기존 get_cascading_affected()의 BFS + visited 패턴을 따른다.
        """
        if _visited is None:
            _visited = set()
        if _current_depth >= max_depth or service_id in _visited:
            return []

        _visited.add(service_id)
        results: list[tuple[str, int]] = []

        for dependent in self.get_dependents(service_id):
            if dependent in _visited:
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
                continue
            results.append((dependent, _current_depth + 1))
            results.extend(
                self.get_dependents_recursive(
                    dependent, max_depth, _visited, _current_depth + 1
                )
            )

        return results

    def topological_sort_subset(
        self,
        services: list[str],
        direction: str = "leaves_first",
    ) -> list[str]:
        """
        주어진 서비스 부분집합에 대해 위상 정렬 수행.

        direction="leaves_first": 하류(의존 없는 리프) → 상류 순서 (복구용)
        direction="roots_first": 상류(루트) → 하류 순서

        Kahn's algorithm on subgraph.
        """
        subset = set(services)
        if not subset:
            return []

        in_degree: dict[str, int] = dict.fromkeys(subset, 0)
        adjacency: dict[str, list[str]] = {s: [] for s in subset}

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
        """모든 의존성 정보 초기화."""
        self._dependencies.clear()
