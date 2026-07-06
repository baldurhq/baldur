"""
Unit tests for Error Budget Evaluator.

검증 항목:
- evaluator name 계약: "error_budget"
- _simulate: error_budget_critical 이벤트 처리, critical_threshold 적용
- _check_pass_criteria: critical_episodes 증가 → fail, drain_ratio > 1.5 → fail
- _calculate_confidence: 이벤트 수별 신뢰도 구간 (5/20/50 경계)
- evaluate: 전체 플로우
- 엣지 케이스: 빈 이벤트, 비 budget 이벤트만

테스트 대상: baldur.services.config_shadow.evaluators.error_budget
"""

from datetime import UTC, datetime

import pytest

from baldur.interfaces.event_journal import JournalEntry
from baldur.services.config_shadow.evaluators.error_budget import (
    ErrorBudgetEvaluator,
)
from baldur.services.config_shadow.models import (
    BudgetSimulationResult,
    EvaluationContext,
)


def _make_entry(
    event_type: str,
    context: dict | None = None,
    service_name: str = "svc",
) -> JournalEntry:
    return JournalEntry(
        sequence=0,
        event_type=event_type,
        source="test",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        service_name=service_name,
        context=context or {},
    )


class TestErrorBudgetEvaluatorContract:
    """ErrorBudgetEvaluator 설계 계약값 검증."""

    def test_name_is_error_budget(self):
        """evaluator name: 'error_budget'."""
        evaluator = ErrorBudgetEvaluator()
        assert evaluator.name == "error_budget"

    def test_event_types_contains_error_budget_critical(self):
        """event_types: ['error_budget_critical']."""
        evaluator = ErrorBudgetEvaluator()
        assert evaluator.event_types == ["error_budget_critical"]

    def test_confidence_below_5_events_is_0_2(self):
        """budget 이벤트 5개 미만: 신뢰도 0.2."""
        evaluator = ErrorBudgetEvaluator()
        events = [_make_entry("error_budget_critical") for _ in range(4)]
        assert evaluator._calculate_confidence(events) == pytest.approx(0.2)

    def test_confidence_5_to_19_events_is_0_5(self):
        """budget 이벤트 5~19개: 신뢰도 0.5."""
        evaluator = ErrorBudgetEvaluator()
        events = [_make_entry("error_budget_critical") for _ in range(10)]
        assert evaluator._calculate_confidence(events) == pytest.approx(0.5)

    def test_confidence_20_to_49_events_is_0_8(self):
        """budget 이벤트 20~49개: 신뢰도 0.8."""
        evaluator = ErrorBudgetEvaluator()
        events = [_make_entry("error_budget_critical") for _ in range(30)]
        assert evaluator._calculate_confidence(events) == pytest.approx(0.8)

    def test_confidence_50_plus_events_is_0_95(self):
        """budget 이벤트 50개 이상: 신뢰도 0.95."""
        evaluator = ErrorBudgetEvaluator()
        events = [_make_entry("error_budget_critical") for _ in range(60)]
        assert evaluator._calculate_confidence(events) == pytest.approx(0.95)

    def test_pass_criteria_critical_episodes_increase_fails(self):
        """후보의 critical_episodes가 baseline보다 많으면 fail."""
        evaluator = ErrorBudgetEvaluator()
        baseline = BudgetSimulationResult(critical_episodes=2)
        candidate = BudgetSimulationResult(critical_episodes=3)
        assert evaluator._check_pass_criteria(baseline, candidate) is False

    def test_pass_criteria_drain_ratio_threshold_is_1_5(self):
        """후보의 drain이 baseline의 1.5배 초과 시 fail."""
        evaluator = ErrorBudgetEvaluator()
        baseline = BudgetSimulationResult(total_drain_percent=10.0)
        candidate_pass = BudgetSimulationResult(total_drain_percent=15.0)
        candidate_fail = BudgetSimulationResult(total_drain_percent=15.1)
        assert evaluator._check_pass_criteria(baseline, candidate_pass) is True
        assert evaluator._check_pass_criteria(baseline, candidate_fail) is False


class TestErrorBudgetSimulationBehavior:
    """ErrorBudgetEvaluator._simulate 동작 검증."""

    def test_empty_events_returns_zero_drain(self):
        """빈 이벤트: drain=0, critical=0."""
        evaluator = ErrorBudgetEvaluator()
        result = evaluator._simulate([], {"critical_threshold_percent": 10})
        assert result.total_drain_percent == 0.0
        assert result.critical_episodes == 0
        assert result.max_burn_rate_1h == 0.0

    def test_non_budget_events_are_ignored(self):
        """error_budget_critical 외 이벤트는 무시된다."""
        evaluator = ErrorBudgetEvaluator()
        events = [
            _make_entry("circuit_breaker_opened"),
            _make_entry("some_event"),
        ]
        result = evaluator._simulate(events, {"critical_threshold_percent": 10})
        assert result.total_drain_percent == 0.0

    def test_critical_threshold_counts_episodes(self):
        """budget_remaining_percent < critical_threshold 시 critical_episodes 증가."""
        evaluator = ErrorBudgetEvaluator()
        events = [
            _make_entry(
                "error_budget_critical",
                context={
                    "budget_remaining_percent": 5,
                    "burn_rate_1h": 2.0,
                    "drain_amount": 3.0,
                },
            ),
            _make_entry(
                "error_budget_critical",
                context={
                    "budget_remaining_percent": 15,
                    "burn_rate_1h": 1.0,
                    "drain_amount": 2.0,
                },
            ),
        ]
        result = evaluator._simulate(events, {"critical_threshold_percent": 10})
        assert result.critical_episodes == 1  # 첫 번째만 임계값 미달
        assert result.total_drain_percent == pytest.approx(5.0)
        assert result.max_burn_rate_1h == pytest.approx(2.0)

    def test_accumulates_drain_amount(self):
        """drain_amount가 누적된다."""
        evaluator = ErrorBudgetEvaluator()
        events = [
            _make_entry(
                "error_budget_critical",
                context={"budget_remaining_percent": 50, "drain_amount": 2.5},
            ),
            _make_entry(
                "error_budget_critical",
                context={"budget_remaining_percent": 50, "drain_amount": 3.5},
            ),
        ]
        result = evaluator._simulate(events, {"critical_threshold_percent": 10})
        assert result.total_drain_percent == pytest.approx(6.0)

    def test_tracks_max_burn_rate(self):
        """max_burn_rate_1h는 최대값을 추적한다."""
        evaluator = ErrorBudgetEvaluator()
        events = [
            _make_entry(
                "error_budget_critical",
                context={"budget_remaining_percent": 50, "burn_rate_1h": 1.5},
            ),
            _make_entry(
                "error_budget_critical",
                context={"budget_remaining_percent": 50, "burn_rate_1h": 3.0},
            ),
            _make_entry(
                "error_budget_critical",
                context={"budget_remaining_percent": 50, "burn_rate_1h": 2.0},
            ),
        ]
        result = evaluator._simulate(events, {"critical_threshold_percent": 10})
        assert result.max_burn_rate_1h == pytest.approx(3.0)

    def test_burn_rate_fast_critical_triggers_episode(self):
        """burn_rate >= burn_rate_fast_critical 시 critical_episodes 증가."""
        evaluator = ErrorBudgetEvaluator()
        events = [
            _make_entry(
                "error_budget_critical",
                context={
                    "budget_remaining_percent": 50,
                    "burn_rate_1h": 15.0,
                    "drain_amount": 1.0,
                },
            ),
        ]
        # burn_rate_fast_critical=14.4 (default) → 15.0 >= 14.4 → critical
        result = evaluator._simulate(events, {"critical_threshold_percent": 10})
        assert result.critical_episodes == 1

    def test_burn_rate_fast_critical_configurable(self):
        """burn_rate_fast_critical 설정으로 임계값을 조정할 수 있다."""
        evaluator = ErrorBudgetEvaluator()
        events = [
            _make_entry(
                "error_budget_critical",
                context={
                    "budget_remaining_percent": 50,
                    "burn_rate_1h": 15.0,
                    "drain_amount": 1.0,
                },
            ),
        ]
        # burn_rate_fast_critical=20 → 15.0 < 20 → no critical
        result = evaluator._simulate(
            events,
            {"critical_threshold_percent": 10, "burn_rate_fast_critical": 20},
        )
        assert result.critical_episodes == 0

    def test_missing_context_fields_default_to_zero(self):
        """context에 값이 없으면 기본값(0/100)으로 처리."""
        evaluator = ErrorBudgetEvaluator()
        events = [_make_entry("error_budget_critical", context={})]
        result = evaluator._simulate(events, {"critical_threshold_percent": 10})
        assert result.total_drain_percent == 0.0
        assert result.max_burn_rate_1h == 0.0
        # budget_remaining_percent default=100, > threshold(10) → no critical episode
        assert result.critical_episodes == 0


class TestErrorBudgetEvaluateFullFlowBehavior:
    """ErrorBudgetEvaluator.evaluate 전체 플로우 검증."""

    def test_evaluate_returns_result_with_all_metrics(self):
        """evaluate가 baseline/candidate 메트릭과 delta를 반환한다."""
        evaluator = ErrorBudgetEvaluator()
        events = [
            _make_entry(
                "error_budget_critical",
                context={
                    "budget_remaining_percent": 5,
                    "burn_rate_1h": 2.0,
                    "drain_amount": 3.0,
                },
            )
            for _ in range(10)
        ]
        context = EvaluationContext(
            baseline_config={"critical_threshold_percent": 10},
            candidate_config={"critical_threshold_percent": 5},
            events=events,
        )
        result = evaluator.evaluate(context)
        assert result.evaluator_name == "error_budget"
        assert "total_drain_percent" in result.baseline_metrics
        assert "critical_episodes" in result.candidate_metrics
        assert "drain_percent_delta" in result.delta
        assert "critical_episodes_delta" in result.delta

    def test_evaluate_empty_events_passes(self):
        """빈 이벤트: drain=0이므로 passed=True."""
        evaluator = ErrorBudgetEvaluator()
        context = EvaluationContext(
            baseline_config={"critical_threshold_percent": 10},
            candidate_config={"critical_threshold_percent": 5},
        )
        result = evaluator.evaluate(context)
        assert result.passed is True


class TestErrorBudgetPassCriteriaEdgeCaseBehavior:
    """_check_pass_criteria 엣지 케이스 검증."""

    def test_baseline_zero_drain_any_candidate_passes(self):
        """baseline drain=0일 때 후보 drain 비율 체크 건너뜀."""
        evaluator = ErrorBudgetEvaluator()
        baseline = BudgetSimulationResult(total_drain_percent=0.0, critical_episodes=0)
        candidate = BudgetSimulationResult(
            total_drain_percent=100.0, critical_episodes=0
        )
        assert evaluator._check_pass_criteria(baseline, candidate) is True

    def test_equal_critical_episodes_passes(self):
        """동일 critical_episodes: pass."""
        evaluator = ErrorBudgetEvaluator()
        baseline = BudgetSimulationResult(critical_episodes=3)
        candidate = BudgetSimulationResult(critical_episodes=3)
        assert evaluator._check_pass_criteria(baseline, candidate) is True

    def test_both_zero_passes(self):
        """양쪽 모두 0: pass."""
        evaluator = ErrorBudgetEvaluator()
        baseline = BudgetSimulationResult()
        candidate = BudgetSimulationResult()
        assert evaluator._check_pass_criteria(baseline, candidate) is True
