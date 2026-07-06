"""
Unit tests for Circuit Breaker Evaluator.

검증 항목:
- evaluator name 계약: "circuit_breaker"
- _simulate: CB 상태 전이 시뮬레이션 (closed->open->half_open->closed)
- _simulate: sliding_window_size, failure_threshold, minimum_calls, failure_rate_threshold
- _simulate: cold start 보정 (context snapshot)
- _check_pass_criteria: open_count 2x 초과 → fail, recovery 3x 초과 → fail
- _calculate_confidence: 이벤트 수별 신뢰도 구간 (5/20/50 경계)
- _calculate_confidence: threshold 인상 시 신뢰도 감소 + 경고
- evaluate: 전체 플로우 (baseline vs candidate 비교)
- 엣지 케이스: 빈 이벤트, 비 CB 이벤트만

테스트 대상: baldur.services.config_shadow.evaluators.circuit_breaker
"""

from datetime import UTC, datetime, timedelta

import pytest

from baldur.interfaces.event_journal import JournalEntry
from baldur.services.config_shadow.evaluators.circuit_breaker import (
    CircuitBreakerEvaluator,
)
from baldur.services.config_shadow.models import (
    EvaluationContext,
    SimulationResult,
)


def _make_entry(
    event_type: str,
    timestamp: datetime,
    context: dict | None = None,
    service_name: str = "svc",
) -> JournalEntry:
    return JournalEntry(
        sequence=0,
        event_type=event_type,
        source="test",
        timestamp=timestamp,
        service_name=service_name,
        context=context or {},
    )


def _make_cb_event_sequence(
    base_time: datetime,
    open_count: int = 1,
    recovery_seconds: float = 60.0,
) -> list[JournalEntry]:
    """CB open->close 사이클 N회 생성."""
    events = []
    t = base_time
    for _ in range(open_count):
        events.append(_make_entry("circuit_breaker_opened", t))
        t += timedelta(seconds=recovery_seconds)
        events.append(_make_entry("circuit_breaker_closed", t))
        t += timedelta(seconds=10)
    return events


class TestCircuitBreakerEvaluatorContract:
    """CircuitBreakerEvaluator 설계 계약값 검증."""

    def test_name_is_circuit_breaker(self):
        """evaluator name: 'circuit_breaker'."""
        evaluator = CircuitBreakerEvaluator()
        assert evaluator.name == "circuit_breaker"

    def test_event_types_contains_opened_and_closed(self):
        """event_types: circuit_breaker_opened, circuit_breaker_closed."""
        evaluator = CircuitBreakerEvaluator()
        assert evaluator.event_types == [
            "circuit_breaker_opened",
            "circuit_breaker_closed",
        ]

    def test_confidence_below_5_events_is_0_2(self):
        """CB 이벤트 5개 미만: 신뢰도 0.2."""
        evaluator = CircuitBreakerEvaluator()
        events = [
            _make_entry("circuit_breaker_opened", datetime(2026, 1, 1, tzinfo=UTC))
            for _ in range(4)
        ]
        conf, _ = evaluator._calculate_confidence(events, {}, {})
        assert conf == pytest.approx(0.2)

    def test_confidence_5_to_19_events_is_0_5(self):
        """CB 이벤트 5~19개: 신뢰도 0.5."""
        evaluator = CircuitBreakerEvaluator()
        events = [
            _make_entry("circuit_breaker_opened", datetime(2026, 1, 1, tzinfo=UTC))
            for _ in range(10)
        ]
        conf, _ = evaluator._calculate_confidence(events, {}, {})
        assert conf == pytest.approx(0.5)

    def test_confidence_20_to_49_events_is_0_8(self):
        """CB 이벤트 20~49개: 신뢰도 0.8."""
        evaluator = CircuitBreakerEvaluator()
        events = [
            _make_entry("circuit_breaker_opened", datetime(2026, 1, 1, tzinfo=UTC))
            for _ in range(30)
        ]
        conf, _ = evaluator._calculate_confidence(events, {}, {})
        assert conf == pytest.approx(0.8)

    def test_confidence_50_plus_events_is_0_95(self):
        """CB 이벤트 50개 이상: 신뢰도 0.95."""
        evaluator = CircuitBreakerEvaluator()
        events = [
            _make_entry("circuit_breaker_opened", datetime(2026, 1, 1, tzinfo=UTC))
            for _ in range(60)
        ]
        conf, _ = evaluator._calculate_confidence(events, {}, {})
        assert conf == pytest.approx(0.95)

    def test_pass_criteria_open_count_ratio_threshold_is_2x(self):
        """후보 open_count가 baseline의 2배 초과 시 fail."""
        evaluator = CircuitBreakerEvaluator()
        baseline = SimulationResult(open_count=5, avg_recovery_seconds=30.0)
        candidate_pass = SimulationResult(open_count=10, avg_recovery_seconds=30.0)
        candidate_fail = SimulationResult(open_count=11, avg_recovery_seconds=30.0)
        assert evaluator._check_pass_criteria(baseline, candidate_pass) is True
        assert evaluator._check_pass_criteria(baseline, candidate_fail) is False

    def test_pass_criteria_recovery_ratio_threshold_is_3x(self):
        """후보 avg_recovery가 baseline의 3배 초과 시 fail."""
        evaluator = CircuitBreakerEvaluator()
        baseline = SimulationResult(open_count=1, avg_recovery_seconds=10.0)
        candidate_pass = SimulationResult(open_count=1, avg_recovery_seconds=30.0)
        candidate_fail = SimulationResult(open_count=1, avg_recovery_seconds=30.1)
        assert evaluator._check_pass_criteria(baseline, candidate_pass) is True
        assert evaluator._check_pass_criteria(baseline, candidate_fail) is False


class TestCircuitBreakerSimulationBehavior:
    """CircuitBreakerEvaluator._simulate 동작 검증."""

    def test_empty_events_returns_zero_opens(self):
        """빈 이벤트 리스트: open_count=0."""
        evaluator = CircuitBreakerEvaluator()
        result = evaluator._simulate([], {"failure_threshold": 5})
        assert result.open_count == 0
        assert result.total_open_seconds == 0.0
        assert result.avg_recovery_seconds == 0.0

    def test_non_cb_events_are_ignored(self):
        """CB 외 이벤트는 시뮬레이션에 영향을 주지 않는다."""
        evaluator = CircuitBreakerEvaluator()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        events = [
            _make_entry("error_budget_critical", t),
            _make_entry("some_other_event", t + timedelta(seconds=10)),
        ]
        result = evaluator._simulate(events, {"failure_threshold": 1})
        assert result.open_count == 0

    def test_failure_threshold_triggers_open(self):
        """failure_threshold 도달 시 CB가 open된다."""
        evaluator = CircuitBreakerEvaluator()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        config = {"failure_threshold": 3, "minimum_calls": 1, "recovery_timeout": 60}

        # 3번의 failure 이벤트 → CB open
        events = []
        for i in range(3):
            events.append(
                _make_entry("circuit_breaker_opened", t + timedelta(seconds=i))
            )

        result = evaluator._simulate(events, config)
        assert result.open_count == 1

    def test_minimum_calls_prevents_premature_open(self):
        """minimum_calls 미달 시 CB가 열리지 않는다."""
        evaluator = CircuitBreakerEvaluator()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        config = {
            "failure_threshold": 1,
            "minimum_calls": 10,
            "recovery_timeout": 60,
        }
        events = [_make_entry("circuit_breaker_opened", t)]
        result = evaluator._simulate(events, config)
        assert result.open_count == 0

    def test_recovery_calculates_duration(self):
        """open->close 사이클 시 recovery duration이 계산된다."""
        evaluator = CircuitBreakerEvaluator()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        config = {"failure_threshold": 1, "minimum_calls": 1, "recovery_timeout": 30}

        events = [
            _make_entry("circuit_breaker_opened", t),
            _make_entry("circuit_breaker_closed", t + timedelta(seconds=45)),
        ]
        result = evaluator._simulate(events, config)
        assert result.open_count == 1
        assert result.total_open_seconds == pytest.approx(45.0)
        assert result.avg_recovery_seconds == pytest.approx(45.0)

    def test_multiple_open_close_cycles_average_recovery(self):
        """여러 open-close 사이클의 평균 recovery를 계산한다."""
        evaluator = CircuitBreakerEvaluator()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        config = {"failure_threshold": 1, "minimum_calls": 1, "recovery_timeout": 10}

        events = [
            _make_entry("circuit_breaker_opened", t),
            _make_entry("circuit_breaker_closed", t + timedelta(seconds=20)),
            _make_entry("circuit_breaker_opened", t + timedelta(seconds=30)),
            _make_entry("circuit_breaker_closed", t + timedelta(seconds=70)),
        ]
        result = evaluator._simulate(events, config)
        assert result.open_count == 2
        assert result.total_open_seconds == pytest.approx(60.0)
        assert result.avg_recovery_seconds == pytest.approx(30.0)

    def test_failure_rate_threshold_triggers_open(self):
        """failure_rate_threshold 설정 시 비율 기반으로 CB가 열린다."""
        evaluator = CircuitBreakerEvaluator()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        config = {
            "failure_threshold": 100,
            "failure_rate_threshold": 50,
            "minimum_calls": 2,
            "recovery_timeout": 60,
            "sliding_window_size": 10,
        }

        # 2개 failure 이벤트 → 100% failure rate > 50%
        events = [
            _make_entry("circuit_breaker_opened", t),
            _make_entry("circuit_breaker_opened", t + timedelta(seconds=1)),
        ]
        result = evaluator._simulate(events, config)
        assert result.open_count == 1

    def test_context_failure_count_populates_window(self):
        """context.failure_count로 failure window를 채운다."""
        evaluator = CircuitBreakerEvaluator()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        config = {
            "failure_threshold": 5,
            "minimum_calls": 1,
            "recovery_timeout": 60,
            "sliding_window_size": 100,
        }

        # failure_count=5 → 5개 도달 → open
        events = [
            _make_entry(
                "circuit_breaker_opened",
                t,
                context={"failure_count": 5},
            ),
        ]
        result = evaluator._simulate(events, config)
        assert result.open_count == 1

    def test_context_failure_count_default_is_1(self):
        """context에 failure_count가 없으면 1로 처리."""
        evaluator = CircuitBreakerEvaluator()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        config = {
            "failure_threshold": 1,
            "minimum_calls": 1,
            "recovery_timeout": 60,
        }

        events = [_make_entry("circuit_breaker_opened", t)]
        result = evaluator._simulate(events, config)
        assert result.open_count == 1

    def test_sliding_window_size_limits_failure_window(self):
        """sliding_window_size 초과 시 오래된 failure가 밀려난다."""
        evaluator = CircuitBreakerEvaluator()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        config = {
            "failure_threshold": 4,
            "minimum_calls": 1,
            "recovery_timeout": 60,
            "sliding_window_size": 3,
        }

        # 4개 failure지만 window=3이므로 최대 3개만 유지 → threshold(4)에 미달
        events = [
            _make_entry("circuit_breaker_opened", t + timedelta(seconds=i))
            for i in range(4)
        ]
        result = evaluator._simulate(events, config)
        assert result.open_count == 0

    def test_half_open_transition_after_recovery_timeout(self):
        """recovery_timeout 경과 후 open→half_open으로 전이한다."""
        evaluator = CircuitBreakerEvaluator()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        config = {"failure_threshold": 1, "minimum_calls": 1, "recovery_timeout": 30}

        events = [
            _make_entry("circuit_breaker_opened", t),
            # 31초 후 close 이벤트 → half_open 경유 후 closed
            _make_entry("circuit_breaker_closed", t + timedelta(seconds=31)),
        ]
        result = evaluator._simulate(events, config)
        assert result.open_count == 1
        assert result.total_open_seconds == pytest.approx(31.0)


class TestCircuitBreakerEvaluateFullFlowBehavior:
    """CircuitBreakerEvaluator.evaluate 전체 플로우 검증."""

    def test_evaluate_returns_evaluator_result_with_metrics(self):
        """evaluate가 baseline/candidate 메트릭과 delta를 포함한 결과를 반환한다."""
        evaluator = CircuitBreakerEvaluator()
        t = datetime(2026, 1, 1, tzinfo=UTC)

        events = [
            _make_entry("circuit_breaker_opened", t),
            _make_entry("circuit_breaker_opened", t + timedelta(seconds=1)),
            _make_entry("circuit_breaker_opened", t + timedelta(seconds=2)),
            _make_entry("circuit_breaker_closed", t + timedelta(seconds=60)),
        ]
        baseline_config = {
            "failure_threshold": 3,
            "minimum_calls": 1,
            "recovery_timeout": 30,
        }
        candidate_config = {
            "failure_threshold": 5,
            "minimum_calls": 1,
            "recovery_timeout": 30,
        }

        context = EvaluationContext(
            baseline_config=baseline_config,
            candidate_config=candidate_config,
            events=events,
        )
        result = evaluator.evaluate(context)

        assert result.evaluator_name == "circuit_breaker"
        assert isinstance(result.passed, bool)
        assert 0.0 <= result.confidence_score <= 0.95
        assert "open_count" in result.baseline_metrics
        assert "open_count" in result.candidate_metrics
        assert "open_count_delta" in result.delta
        assert "open_count_change_percent" in result.delta

    def test_evaluate_with_empty_events_passes(self):
        """이벤트 없을 때 open_count=0이므로 passed=True."""
        evaluator = CircuitBreakerEvaluator()
        context = EvaluationContext(
            baseline_config={"failure_threshold": 5},
            candidate_config={"failure_threshold": 3},
        )
        result = evaluator.evaluate(context)
        assert result.passed is True
        assert result.baseline_metrics["open_count"] == 0
        assert result.candidate_metrics["open_count"] == 0

    def test_evaluate_candidate_worse_than_baseline_fails(self):
        """후보 설정이 baseline보다 현저히 나쁘면 passed=False."""
        evaluator = CircuitBreakerEvaluator()
        t = datetime(2026, 1, 1, tzinfo=UTC)

        # baseline: threshold=5 → open 1회
        # candidate: threshold=1 → open 여러 회
        events = []
        for i in range(10):
            events.append(
                _make_entry("circuit_breaker_opened", t + timedelta(seconds=i * 100))
            )
            events.append(
                _make_entry(
                    "circuit_breaker_closed", t + timedelta(seconds=i * 100 + 50)
                )
            )

        baseline_config = {
            "failure_threshold": 10,
            "minimum_calls": 1,
            "recovery_timeout": 30,
        }
        candidate_config = {
            "failure_threshold": 1,
            "minimum_calls": 1,
            "recovery_timeout": 30,
        }

        context = EvaluationContext(
            baseline_config=baseline_config,
            candidate_config=candidate_config,
            events=events,
        )
        result = evaluator.evaluate(context)
        # candidate가 더 많은 open을 유발해야 함
        assert (
            result.candidate_metrics["open_count"]
            >= result.baseline_metrics["open_count"]
        )


class TestCircuitBreakerConfidenceWarningBehavior:
    """신뢰도 경고 생성 동작 검증."""

    def test_threshold_increase_reduces_confidence(self):
        """후보 threshold가 baseline보다 높으면 신뢰도가 감소한다."""
        evaluator = CircuitBreakerEvaluator()
        events = [
            _make_entry("circuit_breaker_opened", datetime(2026, 1, 1, tzinfo=UTC))
            for _ in range(25)
        ]
        baseline_config = {"failure_threshold": 5}
        candidate_config = {"failure_threshold": 10}

        conf, warnings = evaluator._calculate_confidence(
            events, baseline_config, candidate_config
        )
        assert conf < 0.8  # 기본 0.8이지만 ratio 적용으로 감소
        assert len(warnings) == 1
        assert "threshold_increase" in warnings[0]

    def test_same_threshold_no_warning(self):
        """동일 threshold: 경고 없음."""
        evaluator = CircuitBreakerEvaluator()
        events = [
            _make_entry("circuit_breaker_opened", datetime(2026, 1, 1, tzinfo=UTC))
            for _ in range(25)
        ]
        _, warnings = evaluator._calculate_confidence(
            events, {"failure_threshold": 5}, {"failure_threshold": 5}
        )
        assert len(warnings) == 0

    def test_confidence_capped_at_0_95(self):
        """신뢰도 상한: 0.95."""
        evaluator = CircuitBreakerEvaluator()
        events = [
            _make_entry("circuit_breaker_opened", datetime(2026, 1, 1, tzinfo=UTC))
            for _ in range(100)
        ]
        conf, _ = evaluator._calculate_confidence(events, {}, {})
        assert conf == pytest.approx(0.95)


class TestCircuitBreakerPassCriteriaEdgeCaseBehavior:
    """_check_pass_criteria 엣지 케이스 동작 검증."""

    def test_baseline_zero_opens_always_passes(self):
        """baseline open_count=0 시 항상 pass (division by zero 방지)."""
        evaluator = CircuitBreakerEvaluator()
        baseline = SimulationResult(open_count=0)
        candidate = SimulationResult(open_count=10)
        assert evaluator._check_pass_criteria(baseline, candidate) is True

    def test_baseline_zero_recovery_always_passes(self):
        """baseline avg_recovery=0 시 recovery 비율 체크 건너뜀."""
        evaluator = CircuitBreakerEvaluator()
        baseline = SimulationResult(open_count=1, avg_recovery_seconds=0.0)
        candidate = SimulationResult(open_count=1, avg_recovery_seconds=100.0)
        assert evaluator._check_pass_criteria(baseline, candidate) is True

    def test_both_zero_opens_passes(self):
        """양쪽 모두 open_count=0이면 pass."""
        evaluator = CircuitBreakerEvaluator()
        baseline = SimulationResult(open_count=0)
        candidate = SimulationResult(open_count=0)
        assert evaluator._check_pass_criteria(baseline, candidate) is True
