"""
DependencyAnalyzer 테스트.

의존성 분석, Root Cause 억제, 복구 우선순위 테스트.
"""

import pytest

from baldur.meta.dependency_analyzer import (
    COMPONENT_DEPENDENCIES,
    DependencyAnalyzer,
    RecoveryImpactAssessment,
    SuppressionResult,
    get_dependency_analyzer,
    reset_dependency_analyzer,
)


class TestRecoveryImpactAssessment:
    """RecoveryImpactAssessment 데이터클래스 테스트."""

    def test_creation(self):
        """생성 테스트."""
        assessment = RecoveryImpactAssessment(
            component="redis",
            can_proceed=True,
            blast_radius_level="MODERATE",
            affected_components=["circuit_breaker", "dlq"],
        )

        assert assessment.component == "redis"
        assert assessment.can_proceed is True
        assert assessment.blast_radius_level == "MODERATE"
        assert len(assessment.affected_components) == 2

    def test_default_values(self):
        """기본값 테스트."""
        assessment = RecoveryImpactAssessment(
            component="test",
            can_proceed=True,
            blast_radius_level="MINIMAL",
            affected_components=[],
        )

        assert assessment.block_reason is None
        assert assessment.warnings == []


class TestSuppressionResult:
    """SuppressionResult 데이터클래스 테스트."""

    def test_suppressed(self):
        """억제된 결과 테스트."""
        result = SuppressionResult(
            component="circuit_breaker",
            suppressed=True,
            root_cause="redis",
            reason="Suppressed: redis is the root cause",
        )

        assert result.suppressed is True
        assert result.root_cause == "redis"

    def test_not_suppressed(self):
        """억제되지 않은 결과 테스트."""
        result = SuppressionResult(
            component="redis",
            suppressed=False,
            root_cause=None,
            reason="No root cause detected",
        )

        assert result.suppressed is False
        assert result.root_cause is None


class TestDependencyAnalyzer:
    """DependencyAnalyzer 테스트."""

    @pytest.fixture
    def analyzer(self):
        """Analyzer fixture."""
        return DependencyAnalyzer()

    def test_initialization_with_defaults(self, analyzer):
        """기본값 초기화 테스트."""
        assert analyzer._dependencies == COMPONENT_DEPENDENCIES

    def test_initialization_with_custom_deps(self):
        """커스텀 의존성으로 초기화 테스트."""
        custom_deps = {"custom": ["dep1", "dep2"]}
        analyzer = DependencyAnalyzer(dependencies=custom_deps)

        assert analyzer._dependencies == custom_deps

    def test_assess_recovery_impact_minimal(self, analyzer):
        """MINIMAL 영향 평가 테스트."""
        assessment = analyzer.assess_recovery_impact("unknown_component")

        assert assessment.can_proceed is True
        assert assessment.blast_radius_level == "MINIMAL"
        assert assessment.affected_components == []

    def test_assess_recovery_impact_moderate(self, analyzer):
        """MODERATE 영향 평가 테스트."""
        # database는 1개 컴포넌트에 영향
        assessment = analyzer.assess_recovery_impact("database")

        assert assessment.can_proceed is True
        assert assessment.blast_radius_level == "MODERATE"

    def test_assess_recovery_impact_extensive(self):
        """EXTENSIVE 영향 평가 테스트."""
        custom_deps = {"big": ["a", "b", "c"]}  # 3개
        analyzer = DependencyAnalyzer(dependencies=custom_deps)

        assessment = analyzer.assess_recovery_impact("big")

        assert assessment.can_proceed is True
        assert assessment.blast_radius_level == "EXTENSIVE"

    def test_assess_recovery_impact_critical(self):
        """CRITICAL 영향 평가 테스트."""
        custom_deps = {"huge": ["a", "b", "c", "d", "e"]}  # 5개
        analyzer = DependencyAnalyzer(dependencies=custom_deps)

        assessment = analyzer.assess_recovery_impact("huge")

        assert assessment.can_proceed is False
        assert assessment.blast_radius_level == "CRITICAL"
        assert assessment.block_reason is not None

    def test_assess_recovery_with_failing_overlap(self, analyzer):
        """이미 실패 중인 컴포넌트와 겹치는 경우 테스트."""
        assessment = analyzer.assess_recovery_impact(
            "redis",
            failing_components={"circuit_breaker"},
        )

        # 경고 포함 확인
        assert any("Already failing" in w for w in assessment.warnings)

    def test_should_suppress_alert_suppressed(self, analyzer):
        """알림 억제 테스트."""
        # circuit_breaker의 root cause는 redis
        result = analyzer.should_suppress_alert(
            "circuit_breaker",
            failed_components={"redis", "circuit_breaker"},
        )

        assert result.suppressed is True
        assert result.root_cause == "redis"

    def test_should_suppress_alert_not_suppressed(self, analyzer):
        """알림 비억제 테스트."""
        # redis 자체는 root cause가 없음
        result = analyzer.should_suppress_alert(
            "redis",
            failed_components={"redis"},
        )

        assert result.suppressed is False
        assert result.root_cause is None

    def test_should_suppress_root_cause_not_failing(self, analyzer):
        """Root cause가 실패하지 않은 경우 테스트."""
        # circuit_breaker만 실패 (redis는 정상)
        result = analyzer.should_suppress_alert(
            "circuit_breaker",
            failed_components={"circuit_breaker"},
        )

        # redis가 정상이므로 억제 안 함
        assert result.suppressed is False

    def test_get_recovery_priority(self, analyzer):
        """복구 우선순위 테스트."""
        failed = {"circuit_breaker", "redis", "dlq"}
        priority = analyzer.get_recovery_priority(failed)

        # redis가 root cause이므로 먼저
        assert priority.index("redis") < priority.index("circuit_breaker")
        assert priority.index("redis") < priority.index("dlq")

    def test_get_recovery_priority_no_root_cause(self, analyzer):
        """Root cause 없는 복구 우선순위 테스트."""
        failed = {"unknown1", "unknown2"}
        priority = analyzer.get_recovery_priority(failed)

        assert len(priority) == 2

    def test_get_dependent_components(self, analyzer):
        """의존 컴포넌트 조회 테스트."""
        deps = analyzer.get_dependent_components("redis")

        assert "circuit_breaker" in deps
        assert "dlq" in deps
        assert "recovery_pipeline" in deps

    def test_get_root_cause(self, analyzer):
        """Root cause 조회 테스트."""
        root = analyzer.get_root_cause("circuit_breaker")

        assert root == "redis"

    def test_get_root_cause_none(self, analyzer):
        """Root cause 없음 테스트."""
        root = analyzer.get_root_cause("redis")

        assert root is None

    def test_add_dependency(self, analyzer):
        """의존성 추가 테스트."""
        analyzer.add_dependency("new_root", "new_dep")

        assert "new_dep" in analyzer.get_dependent_components("new_root")
        assert analyzer.get_root_cause("new_dep") == "new_root"

    def test_add_dependency_existing_root(self, analyzer):
        """기존 root에 의존성 추가 테스트."""
        analyzer.add_dependency("redis", "new_dep")

        assert "new_dep" in analyzer.get_dependent_components("redis")

    def test_remove_dependency(self, analyzer):
        """의존성 제거 테스트."""
        analyzer.add_dependency("test_root", "test_dep")
        analyzer.remove_dependency("test_root", "test_dep")

        assert "test_dep" not in analyzer.get_dependent_components("test_root")


class TestSingleton:
    """싱글톤 테스트."""

    def test_get_dependency_analyzer(self):
        """get_dependency_analyzer 테스트."""
        reset_dependency_analyzer()

        analyzer1 = get_dependency_analyzer()
        analyzer2 = get_dependency_analyzer()

        assert analyzer1 is analyzer2

        reset_dependency_analyzer()

    def test_reset_dependency_analyzer(self):
        """reset_dependency_analyzer 테스트."""
        reset_dependency_analyzer()

        analyzer1 = get_dependency_analyzer()
        reset_dependency_analyzer()
        analyzer2 = get_dependency_analyzer()

        assert analyzer1 is not analyzer2

        reset_dependency_analyzer()
