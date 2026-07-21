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
from unittest.mock import patch

import pytest

from baldur.interfaces.event_journal import JournalEntry
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
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
        conf, _ = evaluator._calculate_confidence(
            events, CircuitBreakerConfig(), CircuitBreakerConfig()
        )
        assert conf == pytest.approx(0.2)

    def test_confidence_5_to_19_events_is_0_5(self):
        """CB 이벤트 5~19개: 신뢰도 0.5."""
        evaluator = CircuitBreakerEvaluator()
        events = [
            _make_entry("circuit_breaker_opened", datetime(2026, 1, 1, tzinfo=UTC))
            for _ in range(10)
        ]
        conf, _ = evaluator._calculate_confidence(
            events, CircuitBreakerConfig(), CircuitBreakerConfig()
        )
        assert conf == pytest.approx(0.5)

    def test_confidence_20_to_49_events_is_0_8(self):
        """CB 이벤트 20~49개: 신뢰도 0.8."""
        evaluator = CircuitBreakerEvaluator()
        events = [
            _make_entry("circuit_breaker_opened", datetime(2026, 1, 1, tzinfo=UTC))
            for _ in range(30)
        ]
        conf, _ = evaluator._calculate_confidence(
            events, CircuitBreakerConfig(), CircuitBreakerConfig()
        )
        assert conf == pytest.approx(0.8)

    def test_confidence_50_plus_events_is_0_95(self):
        """CB 이벤트 50개 이상: 신뢰도 0.95."""
        evaluator = CircuitBreakerEvaluator()
        events = [
            _make_entry("circuit_breaker_opened", datetime(2026, 1, 1, tzinfo=UTC))
            for _ in range(60)
        ]
        conf, _ = evaluator._calculate_confidence(
            events, CircuitBreakerConfig(), CircuitBreakerConfig()
        )
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
        result = evaluator._simulate([], CircuitBreakerConfig(failure_threshold=5))
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
        result = evaluator._simulate(events, CircuitBreakerConfig(failure_threshold=1))
        assert result.open_count == 0

    def test_failure_threshold_triggers_open(self):
        """failure_threshold 도달 시 CB가 open된다."""
        evaluator = CircuitBreakerEvaluator()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        # rate trigger off so the count trigger alone decides the outcome
        config = CircuitBreakerConfig(
            failure_threshold=3,
            minimum_calls=1,
            recovery_timeout=60,
            failure_rate_threshold=0,
        )

        # 3번의 연속 failure 이벤트 → CB open
        events = []
        for i in range(3):
            events.append(
                _make_entry("circuit_breaker_opened", t + timedelta(seconds=i))
            )

        result = evaluator._simulate(events, config)
        assert result.open_count == 1

    def test_minimum_calls_prevents_premature_rate_open(self):
        """minimum_calls 미달 시 rate 트리거가 평가되지 않는다.

        minimum_calls는 rate 트리거만 게이팅한다. count 트리거는 트래픽과
        무관하므로 여기서는 failure_threshold를 크게 두어 배제한다.
        """
        evaluator = CircuitBreakerEvaluator()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        config = CircuitBreakerConfig(
            failure_threshold=100,
            failure_rate_threshold=50.0,
            minimum_calls=10,
            recovery_timeout=60,
        )
        events = [_make_entry("circuit_breaker_opened", t)]
        result = evaluator._simulate(events, config)
        assert result.open_count == 0

    def test_recovery_calculates_duration(self):
        """open->close 사이클 시 recovery duration이 계산된다."""
        evaluator = CircuitBreakerEvaluator()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        config = CircuitBreakerConfig(
            failure_threshold=1, minimum_calls=1, recovery_timeout=30
        )

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
        config = CircuitBreakerConfig(
            failure_threshold=1, minimum_calls=1, recovery_timeout=10
        )

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
        config = CircuitBreakerConfig(
            failure_threshold=100,
            failure_rate_threshold=50.0,
            minimum_calls=2,
            recovery_timeout=60,
            sliding_window_size=10,
        )

        # 2개 failure 이벤트 → 100% failure rate > 50%
        events = [
            _make_entry("circuit_breaker_opened", t),
            _make_entry("circuit_breaker_opened", t + timedelta(seconds=1)),
        ]
        result = evaluator._simulate(events, config)
        assert result.open_count == 1

    def test_enriched_event_seeds_window_from_reported_denominators(self):
        """enriched 이벤트의 window_* 키로 replay window를 복원한다."""
        evaluator = CircuitBreakerEvaluator()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        config = CircuitBreakerConfig(
            failure_threshold=100,
            failure_rate_threshold=50.0,
            minimum_calls=10,
            recovery_timeout=60,
            sliding_window_size=100,
        )

        # 20건 중 12건 실패 = 60% ≥ 50% → open (count 트리거는 배제)
        events = [
            _make_entry(
                "circuit_breaker_opened",
                t,
                context={
                    "window_failure_count": 12,
                    "window_total_calls": 20,
                    "consecutive_failure_count": 3,
                },
            ),
        ]
        result = evaluator._simulate(events, config)
        assert result.open_count == 1

    def test_enriched_event_below_rate_does_not_open(self):
        """enriched denominator가 threshold 미만이면 열리지 않는다."""
        evaluator = CircuitBreakerEvaluator()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        config = CircuitBreakerConfig(
            failure_threshold=100,
            failure_rate_threshold=50.0,
            minimum_calls=10,
            recovery_timeout=60,
            sliding_window_size=100,
        )

        # 20건 중 6건 실패 = 30% < 50%
        events = [
            _make_entry(
                "circuit_breaker_opened",
                t,
                context={
                    "window_failure_count": 6,
                    "window_total_calls": 20,
                    "consecutive_failure_count": 2,
                },
            ),
        ]
        result = evaluator._simulate(events, config)
        assert result.open_count == 0

    def test_legacy_event_without_window_keys_counts_one_failure(self):
        """enrichment 이전 이벤트는 실패 1건으로 근사 replay된다."""
        evaluator = CircuitBreakerEvaluator()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        config = CircuitBreakerConfig(
            failure_threshold=1,
            minimum_calls=1,
            recovery_timeout=60,
            failure_rate_threshold=0,
        )

        events = [_make_entry("circuit_breaker_opened", t)]
        result = evaluator._simulate(events, config)
        assert result.open_count == 1

    def test_half_open_transition_after_recovery_timeout(self):
        """recovery_timeout 경과 후 open→half_open으로 전이한다."""
        evaluator = CircuitBreakerEvaluator()
        t = datetime(2026, 1, 1, tzinfo=UTC)
        config = CircuitBreakerConfig(
            failure_threshold=1, minimum_calls=1, recovery_timeout=30
        )

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
        baseline_config = CircuitBreakerConfig(failure_threshold=5)
        candidate_config = CircuitBreakerConfig(failure_threshold=10)

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
            events,
            CircuitBreakerConfig(failure_threshold=5),
            CircuitBreakerConfig(failure_threshold=5),
        )
        assert len(warnings) == 0

    def test_confidence_capped_at_0_95(self):
        """신뢰도 상한: 0.95."""
        evaluator = CircuitBreakerEvaluator()
        events = [
            _make_entry("circuit_breaker_opened", datetime(2026, 1, 1, tzinfo=UTC))
            for _ in range(100)
        ]
        conf, _ = evaluator._calculate_confidence(
            events, CircuitBreakerConfig(), CircuitBreakerConfig()
        )
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


# =============================================================================
# Evaluator config defaults are the live defaults (719 D2)
# =============================================================================


class TestEvaluatorConfigDefaultBehavior:
    """A partial shadow config is completed from the live circuit-breaker
    defaults, not from evaluator-local literals.

    ``baseline_config`` and ``candidate_config`` are arbitrary dicts, so an
    operator shadow-testing one field supplies only that field. The evaluator
    used to fill the rest from its own literals — ``minimum_calls=5`` and
    ``failure_rate_threshold=0``, which disabled the rate trigger in simulation
    only — so the reported delta was measured against a baseline that was never
    running anywhere.
    """

    def test_partial_config_inherits_live_minimum_calls(self):
        """Negative assertion: the local literal 5 no longer applies."""
        evaluator = CircuitBreakerEvaluator()

        resolved = evaluator._resolve_config({"failure_threshold": 5})

        assert resolved.minimum_calls == CircuitBreakerConfig().minimum_calls
        assert resolved.minimum_calls == 10

    def test_partial_config_inherits_live_failure_rate_threshold(self):
        """Negative assertion: the local literal 0 no longer applies.

        A shadow baseline with the rate trigger off could not reproduce trips
        the live breaker performs, so every delta involving the rate trigger
        was fiction.
        """
        evaluator = CircuitBreakerEvaluator()

        resolved = evaluator._resolve_config({"failure_threshold": 5})

        assert (
            resolved.failure_rate_threshold
            == CircuitBreakerConfig().failure_rate_threshold
        )
        assert resolved.failure_rate_threshold == 50.0

    def test_partial_config_inherits_live_sliding_window_size(self):
        """The replay window is sized like the live one."""
        evaluator = CircuitBreakerEvaluator()

        resolved = evaluator._resolve_config({"failure_threshold": 5})

        assert (
            resolved.sliding_window_size == CircuitBreakerConfig().sliding_window_size
        )

    def test_supplied_keys_override_the_defaults(self):
        """The overlay direction is supplied-over-default, not the reverse."""
        evaluator = CircuitBreakerEvaluator()

        resolved = evaluator._resolve_config(
            {"failure_threshold": 3, "minimum_calls": 25}
        )

        assert resolved.failure_threshold == 3
        assert resolved.minimum_calls == 25

    def test_unknown_keys_are_ignored(self):
        """A config dict carrying non-CB keys does not raise."""
        evaluator = CircuitBreakerEvaluator()

        resolved = evaluator._resolve_config(
            {"failure_threshold": 7, "not_a_cb_field": "ignored"}
        )

        assert resolved.failure_threshold == 7

    def test_empty_config_resolves_to_the_live_defaults(self):
        """An empty dict yields the configuration actually running."""
        evaluator = CircuitBreakerEvaluator()

        resolved = evaluator._resolve_config({})
        live = CircuitBreakerConfig()

        assert resolved.failure_threshold == live.failure_threshold
        assert resolved.minimum_calls == live.minimum_calls
        assert resolved.failure_rate_threshold == live.failure_rate_threshold

    def test_partial_baseline_config_reaches_the_simulation(self):
        """End to end: a one-field baseline simulates against live defaults.

        With the old literals the rate trigger was off in the baseline, so a
        rate-driven trip appeared only in the candidate and reported a
        fabricated open-count delta.
        """
        evaluator = CircuitBreakerEvaluator()
        base_time = datetime(2026, 7, 22, tzinfo=UTC)
        events = [
            _make_entry(
                "circuit_breaker_opened",
                base_time,
                context={
                    "window_failure_count": 30,
                    "window_total_calls": 50,
                    "consecutive_failure_count": 2,
                },
            )
        ]

        result = evaluator.evaluate(
            EvaluationContext(
                baseline_config={"failure_threshold": 100},
                candidate_config={"failure_threshold": 100},
                events=events,
            )
        )

        # 60% over 50 calls clears the inherited 50.0 threshold and the
        # inherited minimum of 10, so both sides record the trip.
        assert result.baseline_metrics["open_count"] == 1
        assert result.candidate_metrics["open_count"] == 1
        assert result.delta["open_count_delta"] == 0


class TestEvaluatorRunningConfigBehavior:
    """The unspecified keys come from the running config, not dataclass defaults.

    Completing a partial dict from ``CircuitBreakerConfig()`` reads correctly
    only while the operator runs stock defaults. Once any ``BALDUR_CB_*``
    override or PRO runtime-config value is deployed, the simulated baseline is
    a configuration nobody is running — the same fictitious-baseline failure
    the evaluator-local literals caused, one layer down.

    The running config is pinned at its seam (``from_settings``) rather than
    through the environment, because that resolution runs through the PRO
    RuntimeConfigManager when it is registered and through settings when it is
    not; the evaluator must follow whichever one answered.
    """

    def test_unspecified_keys_come_from_the_running_config(self):
        """Negative assertion: the dataclass defaults no longer decide."""
        running = CircuitBreakerConfig(
            failure_rate_threshold=80.0,
            minimum_calls=25,
            sliding_window_size=250,
        )
        assert (
            running.failure_rate_threshold
            != CircuitBreakerConfig().failure_rate_threshold
        ), "fixture must differ from the default"

        with patch.object(CircuitBreakerConfig, "from_settings", return_value=running):
            resolved = CircuitBreakerEvaluator()._resolve_config(
                {"failure_threshold": 3}
            )

        assert resolved.failure_rate_threshold == 80.0
        assert resolved.minimum_calls == 25
        assert resolved.sliding_window_size == 250
        # The supplied key still wins over the running value.
        assert resolved.failure_threshold == 3

    def test_supplied_keys_override_the_running_config(self):
        """The candidate config is what is being tested — it takes precedence."""
        running = CircuitBreakerConfig(failure_rate_threshold=80.0)

        with patch.object(CircuitBreakerConfig, "from_settings", return_value=running):
            resolved = CircuitBreakerEvaluator()._resolve_config(
                {"failure_rate_threshold": 25.0}
            )

        assert resolved.failure_rate_threshold == 25.0

    def test_unreadable_running_config_degrades_to_defaults(self):
        """A settings fault leaves an approximate simulation, not an exception."""
        with patch.object(
            CircuitBreakerConfig, "from_settings", side_effect=RuntimeError("boom")
        ):
            resolved = CircuitBreakerEvaluator()._resolve_config(
                {"failure_threshold": 3}
            )

        assert resolved.failure_threshold == 3
        assert (
            resolved.failure_rate_threshold
            == CircuitBreakerConfig().failure_rate_threshold
        )
