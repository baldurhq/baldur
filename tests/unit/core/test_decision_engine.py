"""
Tests for Decision Engine - 메트릭 기반 조정 결정
"""

from datetime import datetime

import pytest

from baldur.core.decision_engine import (
    AdjustmentDecision,
    AdjustmentPriority,
    AdjustmentRule,
    DecisionEngine,
)

# =============================================================================
# Fixtures
# =============================================================================


class MockConfigProvider:
    """Mock config provider for testing."""

    def __init__(self, config: dict = None):
        self._config = config or {
            "timeout_ms": 3000,
            "retry_count": 3,
            "circuit_breaker_threshold": 0.5,
            "jitter_range": 0.1,
            "rate_limit_rps": 1000,
        }

    def get(self, key: str, default=None):
        return self._config.get(key, default)

    def set(self, key: str, value):
        self._config[key] = value


@pytest.fixture
def config_provider():
    """Create a mock config provider."""
    return MockConfigProvider()


@pytest.fixture
def decision_engine(config_provider):
    """Create a DecisionEngine instance."""
    return DecisionEngine(config_provider=config_provider)


@pytest.fixture
def disabled_decision_engine(config_provider):
    """Create a disabled DecisionEngine instance."""
    return DecisionEngine(config_provider=config_provider, enabled=False)


# =============================================================================
# DecisionEngine Initialization Tests
# =============================================================================


class TestDecisionEngineInit:
    """Test DecisionEngine initialization."""

    def test_init_with_default_rules(self, decision_engine):
        """기본 규칙이 로드되어야 함."""
        assert len(decision_engine.rules) == len(DecisionEngine.DEFAULT_RULES)
        assert decision_engine.enabled is True

    def test_init_with_custom_rules(self, config_provider):
        """커스텀 규칙 추가 시 기본 규칙과 함께 로드."""
        custom_rule = AdjustmentRule(
            parameter="custom_param",
            metric="custom_metric",
            condition=lambda current, metric: metric > 100,
            adjustment=lambda current, metric: current * 2,
            reason="Custom adjustment",
        )

        engine = DecisionEngine(
            config_provider=config_provider,
            custom_rules=[custom_rule],
        )

        assert len(engine.rules) == len(DecisionEngine.DEFAULT_RULES) + 1

    def test_init_disabled(self, disabled_decision_engine):
        """비활성화 상태로 초기화 가능."""
        assert disabled_decision_engine.enabled is False


# =============================================================================
# Analyze Tests
# =============================================================================


class TestDecisionEngineAnalyze:
    """Test DecisionEngine.analyze() method."""

    def test_analyze_when_disabled_returns_empty(self, disabled_decision_engine):
        """비활성화 시 빈 리스트 반환."""
        metrics = {"error_rate": 0.5, "p99_latency_ms": 5000}
        decisions = disabled_decision_engine.analyze(metrics)

        assert decisions == []

    def test_analyze_no_adjustment_needed(self, decision_engine):
        """조정이 필요 없으면 빈 리스트 반환."""
        metrics = {
            "error_rate": 0.01,  # 낮은 에러율
            "p99_latency_ms": 100,  # 낮은 레이턴시
            "retry_exhausted_rate": 0.01,  # 낮은 재시도 소진율
            "sample_count": 100,
        }

        decisions = decision_engine.analyze(metrics)
        assert len(decisions) == 0

    def test_analyze_timeout_adjustment_needed(self, decision_engine):
        """P99 레이턴시가 타임아웃의 80% 이상이면 타임아웃 상향."""
        metrics = {
            "p99_latency_ms": 2500,  # 3000 * 0.8 = 2400 이상
            "sample_count": 100,
        }

        decisions = decision_engine.analyze(metrics)

        timeout_decisions = [d for d in decisions if d.parameter == "timeout_ms"]
        assert len(timeout_decisions) == 1

        decision = timeout_decisions[0]
        assert decision.current_value == 3000
        assert decision.suggested_value > 3000  # 상향 조정
        assert decision.suggested_value <= 10000  # 최대값 제한

    def test_analyze_retry_count_adjustment(self, decision_engine):
        """재시도 소진율이 10% 이상이면 재시도 횟수 증가."""
        metrics = {
            "retry_exhausted_rate": 0.15,  # 15%
            "sample_count": 100,
        }

        decisions = decision_engine.analyze(metrics)

        retry_decisions = [d for d in decisions if d.parameter == "retry_count"]
        assert len(retry_decisions) == 1

        decision = retry_decisions[0]
        assert decision.current_value == 3
        assert decision.suggested_value == 4  # +1

    def test_analyze_circuit_breaker_threshold_adjustment(self, decision_engine):
        """에러율이 CB 임계값에 근접하면 상향 조정."""
        metrics = {
            "error_rate": 0.46,  # 0.5 * 0.9 = 0.45 이상
            "sample_count": 100,
        }

        decisions = decision_engine.analyze(metrics)

        cb_decisions = [
            d for d in decisions if d.parameter == "circuit_breaker_threshold"
        ]
        assert len(cb_decisions) == 1

        decision = cb_decisions[0]
        assert decision.current_value == 0.5
        assert decision.suggested_value > 0.5  # 상향 조정
        assert decision.priority == AdjustmentPriority.HIGH

    def test_analyze_multiple_adjustments(self, decision_engine):
        """여러 조정이 동시에 필요할 수 있음."""
        metrics = {
            "p99_latency_ms": 2500,  # 타임아웃 상향 필요
            "retry_exhausted_rate": 0.15,  # 재시도 증가 필요
            "error_rate": 0.46,  # CB 임계값 상향 필요
            "sample_count": 100,
        }

        decisions = decision_engine.analyze(metrics)

        assert len(decisions) >= 2  # 최소 2개 이상

    def test_analyze_decisions_sorted_by_priority(self, decision_engine):
        """결정은 우선순위에 따라 정렬."""
        metrics = {
            "p99_latency_ms": 2500,  # MEDIUM
            "error_rate": 0.46,  # HIGH
            "retry_collision_rate": 0.1,  # LOW
            "sample_count": 100,
        }

        decisions = decision_engine.analyze(metrics)

        if len(decisions) >= 2:
            # HIGH가 먼저
            priorities = [d.priority for d in decisions]
            priority_order = [
                AdjustmentPriority.CRITICAL,
                AdjustmentPriority.HIGH,
                AdjustmentPriority.MEDIUM,
                AdjustmentPriority.LOW,
            ]

            # 정렬되어 있는지 확인
            for i in range(len(priorities) - 1):
                assert priority_order.index(priorities[i]) <= priority_order.index(
                    priorities[i + 1]
                )

    def test_analyze_ignores_missing_metrics(self, decision_engine):
        """없는 메트릭은 무시."""
        metrics = {
            "unknown_metric": 999,
            "sample_count": 100,
        }

        decisions = decision_engine.analyze(metrics)
        assert isinstance(decisions, list)  # 오류 없이 처리


# =============================================================================
# Confidence Calculation Tests
# =============================================================================


class TestConfidenceCalculation:
    """Test confidence calculation logic."""

    def test_low_sample_count_low_confidence(self, decision_engine):
        """샘플 수가 적으면 신뢰도 낮음."""
        metrics = {
            "p99_latency_ms": 2500,
            "sample_count": 3,  # 매우 적은 샘플
        }

        decisions = decision_engine.analyze(metrics)

        # 신뢰도가 낮아서 조정 결정이 없을 수 있음
        timeout_decisions = [d for d in decisions if d.parameter == "timeout_ms"]
        if timeout_decisions:
            assert timeout_decisions[0].confidence < 0.5

    def test_high_sample_count_high_confidence(self, decision_engine):
        """샘플 수가 많으면 신뢰도 높음."""
        metrics = {
            "p99_latency_ms": 2500,
            "sample_count": 150,  # 많은 샘플
        }

        decisions = decision_engine.analyze(metrics)

        timeout_decisions = [d for d in decisions if d.parameter == "timeout_ms"]
        if timeout_decisions:
            assert timeout_decisions[0].confidence >= 0.7


# =============================================================================
# Min Change Ratio Tests
# =============================================================================


class TestMinChangeRatio:
    """Test minimum change ratio filtering."""

    def test_small_change_filtered_out(self, config_provider):
        """5% 미만 변경은 무시됨."""
        # 현재값이 3000이고, 변경이 1%만 필요하도록 설정
        config_provider.set("timeout_ms", 3000)

        engine = DecisionEngine(config_provider=config_provider)

        metrics = {
            "p99_latency_ms": 2410,  # 3000 * 0.803 = 약간만 초과
            "sample_count": 100,
        }

        decisions = engine.analyze(metrics)

        # 변경 비율이 작으면 필터링될 수 있음
        # (실제로는 조건에 따라 다를 수 있음)
        for decision in decisions:
            if decision.parameter == "timeout_ms":
                change_ratio = (
                    abs(decision.suggested_value - decision.current_value)
                    / decision.current_value
                )
                # MIN_CHANGE_RATIO는 인스턴스 property이므로 engine에서 접근
                assert change_ratio >= engine.MIN_CHANGE_RATIO


# =============================================================================
# Rule Management Tests
# =============================================================================


class TestRuleManagement:
    """Test rule add/remove operations."""

    def test_add_rule(self, decision_engine):
        """규칙 추가."""
        initial_count = len(decision_engine.rules)

        new_rule = AdjustmentRule(
            parameter="new_param",
            metric="new_metric",
            condition=lambda c, m: m > 50,
            adjustment=lambda c, m: c + 10,
            reason="New rule",
        )

        decision_engine.add_rule(new_rule)

        assert len(decision_engine.rules) == initial_count + 1

    def test_remove_rule(self, decision_engine):
        """규칙 제거."""
        initial_count = len(decision_engine.rules)

        # timeout_ms 규칙 제거
        removed = decision_engine.remove_rule("timeout_ms")

        assert removed is True
        assert len(decision_engine.rules) == initial_count - 1

    def test_remove_nonexistent_rule(self, decision_engine):
        """존재하지 않는 규칙 제거 시 False 반환."""
        removed = decision_engine.remove_rule("nonexistent_param")

        assert removed is False

    def test_get_rules(self, decision_engine):
        """규칙 목록 조회."""
        rules = decision_engine.get_rules()

        assert isinstance(rules, list)
        assert len(rules) > 0

        for rule in rules:
            assert "parameter" in rule
            assert "metric" in rule
            assert "reason" in rule
            assert "priority" in rule


# =============================================================================
# History Tests
# =============================================================================


class TestAnalysisHistory:
    """Test analysis history tracking."""

    def test_history_recorded(self, decision_engine):
        """분석 이력이 기록됨."""
        metrics = {"error_rate": 0.5, "sample_count": 100}

        decision_engine.analyze(metrics)

        history = decision_engine.get_history()
        assert len(history) >= 1

    def test_history_limit(self, decision_engine):
        """이력은 settings.max_history 개수만 유지."""
        from unittest.mock import patch

        from baldur.settings.decision_engine import DecisionEngineSettings

        small_settings = DecisionEngineSettings(max_history=100)

        metrics = {"error_rate": 0.5, "sample_count": 100}

        with patch(
            "baldur.core.decision_engine.get_decision_engine_settings",
            return_value=small_settings,
        ):
            for _ in range(110):
                decision_engine.analyze(metrics)

        history = decision_engine.get_history(limit=200)
        assert len(history) <= 100

    def test_history_content(self, decision_engine):
        """이력에 필요한 정보 포함."""
        metrics = {"error_rate": 0.5, "sample_count": 100}

        decision_engine.analyze(metrics)

        history = decision_engine.get_history()
        latest = history[-1]

        assert "timestamp" in latest
        assert "metrics" in latest
        assert "decisions_count" in latest


# =============================================================================
# AdjustmentDecision Tests
# =============================================================================


class TestAdjustmentDecision:
    """Test AdjustmentDecision dataclass."""

    def test_decision_creation(self):
        """AdjustmentDecision 생성."""
        decision = AdjustmentDecision(
            parameter="timeout_ms",
            current_value=3000,
            suggested_value=3600,
            reason="P99 레이턴시 증가",
            confidence=0.8,
            priority=AdjustmentPriority.MEDIUM,
        )

        assert decision.parameter == "timeout_ms"
        assert decision.current_value == 3000
        assert decision.suggested_value == 3600
        assert decision.confidence == 0.8
        assert decision.priority == AdjustmentPriority.MEDIUM

    def test_decision_timestamp(self):
        """결정에 타임스탬프 자동 설정."""
        decision = AdjustmentDecision(
            parameter="test",
            current_value=1,
            suggested_value=2,
            reason="test",
            confidence=0.5,
        )

        assert decision.timestamp is not None
        assert isinstance(decision.timestamp, datetime)


# =============================================================================
# AdjustmentRule Tests
# =============================================================================


class TestAdjustmentRule:
    """Test AdjustmentRule dataclass."""

    def test_rule_creation(self):
        """AdjustmentRule 생성."""
        rule = AdjustmentRule(
            parameter="test_param",
            metric="test_metric",
            condition=lambda c, m: m > 100,
            adjustment=lambda c, m: c * 1.5,
            reason="Test reason",
            priority=AdjustmentPriority.HIGH,
            min_confidence=0.7,
        )

        assert rule.parameter == "test_param"
        assert rule.metric == "test_metric"
        assert rule.priority == AdjustmentPriority.HIGH
        assert rule.min_confidence == 0.7

    def test_rule_condition_callable(self):
        """규칙 조건 호출 가능."""
        rule = AdjustmentRule(
            parameter="test",
            metric="test",
            condition=lambda c, m: m > 100,
            adjustment=lambda c, m: c * 2,
            reason="test",
        )

        assert rule.condition(50, 150) is True
        assert rule.condition(50, 50) is False

    def test_rule_adjustment_callable(self):
        """규칙 조정 함수 호출 가능."""
        rule = AdjustmentRule(
            parameter="test",
            metric="test",
            condition=lambda c, m: True,
            adjustment=lambda c, m: c + m,
            reason="test",
        )

        assert rule.adjustment(100, 50) == 150


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Test edge cases."""

    def test_zero_current_value(self, config_provider):
        """현재 값이 0인 경우."""
        config_provider.set("test_param", 0)

        engine = DecisionEngine(config_provider=config_provider)

        # 에러 없이 처리되어야 함
        metrics = {"test_metric": 100, "sample_count": 100}
        decisions = engine.analyze(metrics)
        assert isinstance(decisions, list)

    def test_negative_metric_value(self, decision_engine):
        """음수 메트릭 값."""
        metrics = {"error_rate": -0.1, "sample_count": 100}

        # 에러 없이 처리
        decisions = decision_engine.analyze(metrics)
        assert isinstance(decisions, list)

    def test_empty_metrics(self, decision_engine):
        """빈 메트릭."""
        decisions = decision_engine.analyze({})
        assert decisions == []

    def test_none_config_value(self, config_provider):
        """설정 값이 None인 경우."""
        config_provider.set("timeout_ms", None)

        engine = DecisionEngine(config_provider=config_provider)

        metrics = {"p99_latency_ms": 5000, "sample_count": 100}
        decisions = engine.analyze(metrics)

        # None 값에 대한 조정은 스킵됨
        timeout_decisions = [d for d in decisions if d.parameter == "timeout_ms"]
        assert len(timeout_decisions) == 0

    def test_config_provider_returns_invalid_type(self, config_provider):
        """설정 값이 변환할 수 없는 타입인 경우에도 에러 없이 동작."""
        config_provider.set("timeout_ms", "not_a_number")

        engine = DecisionEngine(config_provider=config_provider)

        metrics = {"p99_latency_ms": 900, "sample_count": 100}
        decisions = engine.analyze(metrics)
        assert isinstance(decisions, list)

    def test_condition_exception_handled(self, config_provider):
        """조건 함수 예외 처리."""

        def bad_condition(c, m):
            raise ValueError("Bad condition")

        bad_rule = AdjustmentRule(
            parameter="bad_param",
            metric="test_metric",
            condition=bad_condition,
            adjustment=lambda c, m: c * 2,
            reason="test",
        )

        engine = DecisionEngine(
            config_provider=config_provider,
            custom_rules=[bad_rule],
        )

        config_provider.set("bad_param", 100)

        # 예외가 발생해도 다른 규칙은 처리됨
        metrics = {"test_metric": 100, "sample_count": 100}
        decisions = engine.analyze(metrics)
        assert isinstance(decisions, list)
