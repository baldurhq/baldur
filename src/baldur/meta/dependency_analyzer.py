"""
Dependency Analyzer - 의존성 분석 및 Root Cause 억제.

복구 전 Blast Radius 평가, 연쇄 장애 시 Root Cause만 알림.

기능:
1. 복구 전 Blast Radius 평가 (영향 범위 분석)
2. Root Cause 기반 알림 억제 (연쇄 장애 시 중복 알림 방지)
3. 복구 우선순위 결정 (인프라 → 애플리케이션)
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()


# =============================================================================
# 컴포넌트 의존성 맵 (Meta-Watchdog 전용)
# =============================================================================

# 인프라 컴포넌트가 의존하는 컴포넌트들 (인프라 → 애플리케이션)
# redis가 죽으면 circuit_breaker, dlq, recovery_pipeline이 영향받음
COMPONENT_DEPENDENCIES: dict[str, list[str]] = {
    "redis": ["circuit_breaker", "dlq", "recovery_pipeline"],
    "database": ["recovery_pipeline"],
    "celery_broker": ["dlq"],
}

# 역방향 의존성 맵 (component -> root cause)
# circuit_breaker가 문제일 때 redis가 root cause일 수 있음
REVERSE_DEPENDENCIES: dict[str, str] = {}
for _root, _deps in COMPONENT_DEPENDENCIES.items():
    for _dep in _deps:
        REVERSE_DEPENDENCIES[_dep] = _root


@dataclass
class RecoveryImpactAssessment:
    """복구 영향 평가 결과."""

    component: str
    """대상 컴포넌트."""

    can_proceed: bool
    """복구 진행 가능 여부."""

    blast_radius_level: str
    """영향 범위 레벨 (MINIMAL/MODERATE/EXTENSIVE/CRITICAL)."""

    affected_components: list[str]
    """영향받는 컴포넌트 목록."""

    block_reason: str | None = None
    """차단 사유 (can_proceed=False 시)."""

    warnings: list[str] = field(default_factory=list)
    """경고 메시지 목록."""


@dataclass
class SuppressionResult:
    """알림 억제 결과."""

    component: str
    """대상 컴포넌트."""

    suppressed: bool
    """억제 여부."""

    root_cause: str | None
    """Root Cause 컴포넌트."""

    reason: str
    """억제/비억제 사유."""


class DependencyAnalyzer:
    """
    의존성 분석기.

    기능:
    1. 복구 전 Blast Radius 평가
    2. Root Cause 기반 알림 억제
    3. 복구 우선순위 결정

    사용 예시:
        analyzer = DependencyAnalyzer()

        # 복구 영향 평가
        assessment = analyzer.assess_recovery_impact("redis", {"dlq", "circuit_breaker"})
        if assessment.can_proceed:
            perform_recovery()

        # 알림 억제 판단
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
        초기화.

        Args:
            dependencies: 컴포넌트 의존성 맵 (None이면 기본값)
            reverse_deps: 역방향 의존성 맵 (None이면 기본값)
        """
        self._dependencies = dependencies or COMPONENT_DEPENDENCIES.copy()
        self._reverse_deps = reverse_deps or REVERSE_DEPENDENCIES.copy()

    def assess_recovery_impact(
        self,
        component: str,
        failing_components: set[str] | None = None,
    ) -> RecoveryImpactAssessment:
        """
        복구 전 영향 평가.

        복구 대상 컴포넌트가 다른 컴포넌트에 미치는 영향을 평가합니다.

        Args:
            component: 복구 대상 컴포넌트
            failing_components: 현재 실패 중인 컴포넌트 집합

        Returns:
            RecoveryImpactAssessment
        """
        failing = failing_components or set()

        # 해당 컴포넌트를 의존하는 컴포넌트 수집
        affected = self._dependencies.get(component, [])
        affected_count = len(affected)

        # 레벨 결정
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

        # 경고 생성
        warnings: list[str] = []
        if level in ("EXTENSIVE", "CRITICAL"):
            warnings.append(f"Recovery may affect {affected_count} components")

        # 이미 실패 중인 컴포넌트와 겹치면 추가 경고
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
        Root Cause 기반 알림 억제 판단.

        예: Redis 실패 시 CB/DLQ 알림 억제 (redis가 root cause이므로)

        Args:
            component: 알림 대상 컴포넌트
            failed_components: 현재 실패 중인 모든 컴포넌트

        Returns:
            SuppressionResult
        """
        # 이 컴포넌트의 root cause 확인
        root_cause = self._reverse_deps.get(component)

        if root_cause and root_cause in failed_components:
            # Root cause도 실패 중이면 이 컴포넌트 알림 억제
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
        복구 우선순위 결정.

        Root cause (인프라)를 먼저 복구해야 의존 컴포넌트도 복구됩니다.

        Args:
            failed_components: 실패 컴포넌트 집합

        Returns:
            우선순위 정렬된 컴포넌트 목록 (먼저 복구해야 할 것이 앞)
        """
        priority: list[str] = []

        # 1순위: Root cause 컴포넌트 (redis, database 등 인프라)
        root_causes = set(self._dependencies.keys())
        for root in root_causes:
            if root in failed_components:
                priority.append(root)

        # 2순위: 나머지 컴포넌트
        for comp in failed_components:
            if comp not in priority:
                priority.append(comp)

        return priority

    def get_dependent_components(self, component: str) -> list[str]:
        """
        특정 컴포넌트를 의존하는 컴포넌트 목록 반환.

        Args:
            component: 컴포넌트 이름

        Returns:
            의존 컴포넌트 목록
        """
        return self._dependencies.get(component, [])

    def get_root_cause(self, component: str) -> str | None:
        """
        특정 컴포넌트의 root cause 반환.

        Args:
            component: 컴포넌트 이름

        Returns:
            Root cause 컴포넌트 이름 (없으면 None)
        """
        return self._reverse_deps.get(component)

    def add_dependency(self, root: str, dependent: str) -> None:
        """
        의존성 추가.

        Args:
            root: Root 컴포넌트 (인프라)
            dependent: 의존 컴포넌트 (애플리케이션)
        """
        if root not in self._dependencies:
            self._dependencies[root] = []
        if dependent not in self._dependencies[root]:
            self._dependencies[root].append(dependent)
        self._reverse_deps[dependent] = root

    def remove_dependency(self, root: str, dependent: str) -> None:
        """
        의존성 제거.

        Args:
            root: Root 컴포넌트
            dependent: 의존 컴포넌트
        """
        if root in self._dependencies:
            try:
                self._dependencies[root].remove(dependent)
            except ValueError:
                pass
        if self._reverse_deps.get(dependent) == root:
            del self._reverse_deps[dependent]


# =============================================================================
# 싱글톤
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

get_dependency_analyzer, configure_dependency_analyzer, reset_dependency_analyzer = (
    make_singleton_factory("dependency_analyzer", DependencyAnalyzer)
)
