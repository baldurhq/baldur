"""
Unit tests for Config Shadow Evaluator data models.

검증 항목:
- EvaluationStatus enum 값 계약
- EvaluatorResult 기본값 계약
- EvaluationReport 기본값 계약
- ShadowEvaluation 기본값 계약 (time_window_hours=336)
- SimulationResult / BudgetSimulationResult 기본값 계약
- Dataclass 불변성 (frozen이 아닌 것 확인)

테스트 대상: baldur.services.config_shadow.models
"""

from datetime import UTC, datetime

from baldur.services.config_shadow.models import (
    BudgetSimulationResult,
    EvaluationReport,
    EvaluationStatus,
    EvaluatorResult,
    ShadowEvaluation,
    SimulationResult,
)


class TestEvaluationStatusContract:
    """EvaluationStatus enum 설계 계약값 검증."""

    def test_pending_value(self):
        """PENDING 상태 문자열: 'pending'."""
        assert EvaluationStatus.PENDING.value == "pending"

    def test_running_value(self):
        """RUNNING 상태 문자열: 'running'."""
        assert EvaluationStatus.RUNNING.value == "running"

    def test_completed_value(self):
        """COMPLETED 상태 문자열: 'completed'."""
        assert EvaluationStatus.COMPLETED.value == "completed"

    def test_failed_value(self):
        """FAILED 상태 문자열: 'failed'."""
        assert EvaluationStatus.FAILED.value == "failed"

    def test_member_count_is_four(self):
        """EvaluationStatus는 정확히 4개 멤버를 가진다."""
        assert len(EvaluationStatus) == 4

    def test_is_str_subclass(self):
        """EvaluationStatus는 str을 상속하여 문자열 비교 가능."""
        assert EvaluationStatus.PENDING == "pending"


class TestEvaluatorResultContract:
    """EvaluatorResult 기본값 계약 검증."""

    def test_baseline_metrics_default_is_empty_dict(self):
        """baseline_metrics 기본값: 빈 딕셔너리."""
        result = EvaluatorResult(
            evaluator_name="test", passed=True, confidence_score=0.9
        )
        assert result.baseline_metrics == {}

    def test_candidate_metrics_default_is_empty_dict(self):
        """candidate_metrics 기본값: 빈 딕셔너리."""
        result = EvaluatorResult(
            evaluator_name="test", passed=True, confidence_score=0.9
        )
        assert result.candidate_metrics == {}

    def test_delta_default_is_empty_dict(self):
        """delta 기본값: 빈 딕셔너리."""
        result = EvaluatorResult(
            evaluator_name="test", passed=True, confidence_score=0.9
        )
        assert result.delta == {}

    def test_details_default_is_empty_string(self):
        """details 기본값: 빈 문자열."""
        result = EvaluatorResult(
            evaluator_name="test", passed=True, confidence_score=0.9
        )
        assert result.details == ""

    def test_warnings_default_is_empty_list(self):
        """warnings 기본값: 빈 리스트."""
        result = EvaluatorResult(
            evaluator_name="test", passed=True, confidence_score=0.9
        )
        assert result.warnings == []


class TestEvaluationReportContract:
    """EvaluationReport 기본값 계약 검증."""

    def test_passed_default_is_false(self):
        """passed 기본값: False."""
        now = datetime.now(UTC)
        report = EvaluationReport(
            events_analyzed=0, time_range_start=now, time_range_end=now
        )
        assert report.passed is False

    def test_confidence_score_default_is_zero(self):
        """confidence_score 기본값: 0.0."""
        now = datetime.now(UTC)
        report = EvaluationReport(
            events_analyzed=0, time_range_start=now, time_range_end=now
        )
        assert report.confidence_score == 0.0

    def test_summary_default_is_empty_string(self):
        """summary 기본값: 빈 문자열."""
        now = datetime.now(UTC)
        report = EvaluationReport(
            events_analyzed=0, time_range_start=now, time_range_end=now
        )
        assert report.summary == ""

    def test_evaluator_results_default_is_empty_list(self):
        """evaluator_results 기본값: 빈 리스트."""
        now = datetime.now(UTC)
        report = EvaluationReport(
            events_analyzed=0, time_range_start=now, time_range_end=now
        )
        assert report.evaluator_results == []


class TestShadowEvaluationContract:
    """ShadowEvaluation 설계 계약값 검증."""

    def test_time_window_hours_default_is_336(self):
        """time_window_hours 기본값: 336 (14일)."""
        now = datetime.now(UTC)
        evaluation = ShadowEvaluation(
            evaluation_id="test",
            rollout_id=None,
            status=EvaluationStatus.PENDING,
            created_at=now,
        )
        assert evaluation.time_window_hours == 336

    def test_config_type_default_is_empty_string(self):
        """config_type 기본값: 빈 문자열."""
        now = datetime.now(UTC)
        evaluation = ShadowEvaluation(
            evaluation_id="test",
            rollout_id=None,
            status=EvaluationStatus.PENDING,
            created_at=now,
        )
        assert evaluation.config_type == ""

    def test_completed_at_default_is_none(self):
        """completed_at 기본값: None."""
        now = datetime.now(UTC)
        evaluation = ShadowEvaluation(
            evaluation_id="test",
            rollout_id=None,
            status=EvaluationStatus.PENDING,
            created_at=now,
        )
        assert evaluation.completed_at is None

    def test_report_default_is_none(self):
        """report 기본값: None."""
        now = datetime.now(UTC)
        evaluation = ShadowEvaluation(
            evaluation_id="test",
            rollout_id=None,
            status=EvaluationStatus.PENDING,
            created_at=now,
        )
        assert evaluation.report is None

    def test_error_message_default_is_empty_string(self):
        """error_message 기본값: 빈 문자열."""
        now = datetime.now(UTC)
        evaluation = ShadowEvaluation(
            evaluation_id="test",
            rollout_id=None,
            status=EvaluationStatus.PENDING,
            created_at=now,
        )
        assert evaluation.error_message == ""


class TestSimulationResultContract:
    """SimulationResult 기본값 계약 검증."""

    def test_open_count_default_is_zero(self):
        """open_count 기본값: 0."""
        result = SimulationResult()
        assert result.open_count == 0

    def test_total_open_seconds_default_is_zero(self):
        """total_open_seconds 기본값: 0.0."""
        result = SimulationResult()
        assert result.total_open_seconds == 0.0

    def test_avg_recovery_seconds_default_is_zero(self):
        """avg_recovery_seconds 기본값: 0.0."""
        result = SimulationResult()
        assert result.avg_recovery_seconds == 0.0


class TestBudgetSimulationResultContract:
    """BudgetSimulationResult 기본값 계약 검증."""

    def test_total_drain_percent_default_is_zero(self):
        """total_drain_percent 기본값: 0.0."""
        result = BudgetSimulationResult()
        assert result.total_drain_percent == 0.0

    def test_critical_episodes_default_is_zero(self):
        """critical_episodes 기본값: 0."""
        result = BudgetSimulationResult()
        assert result.critical_episodes == 0

    def test_max_burn_rate_1h_default_is_zero(self):
        """max_burn_rate_1h 기본값: 0.0."""
        result = BudgetSimulationResult()
        assert result.max_burn_rate_1h == 0.0


class TestDataclassInstanceIsolationBehavior:
    """Dataclass default_factory 인스턴스 격리 검증."""

    def test_evaluator_result_warnings_are_independent(self):
        """EvaluatorResult 인스턴스 간 warnings 리스트가 공유되지 않는다."""
        r1 = EvaluatorResult(evaluator_name="a", passed=True, confidence_score=0.5)
        r2 = EvaluatorResult(evaluator_name="b", passed=True, confidence_score=0.5)
        r1.warnings.append("warn")
        assert r2.warnings == []

    def test_shadow_evaluation_configs_are_independent(self):
        """ShadowEvaluation 인스턴스 간 baseline_config가 공유되지 않는다."""
        now = datetime.now(UTC)
        e1 = ShadowEvaluation(
            evaluation_id="a",
            rollout_id=None,
            status=EvaluationStatus.PENDING,
            created_at=now,
        )
        e2 = ShadowEvaluation(
            evaluation_id="b",
            rollout_id=None,
            status=EvaluationStatus.PENDING,
            created_at=now,
        )
        e1.baseline_config["key"] = "value"
        assert e2.baseline_config == {}


class TestEvaluationContextContract:
    """EvaluationContext 설계 계약값 검증."""

    def test_time_window_seconds_default_is_300(self):
        """time_window_seconds 기본값: 300 (5분)."""
        from baldur.services.config_shadow.models import EvaluationContext

        ctx = EvaluationContext(
            baseline_config={},
            candidate_config={},
        )
        assert ctx.time_window_seconds == 300

    def test_baseline_labels_default_is_empty_dict(self):
        """baseline_labels 기본값: 빈 딕셔너리."""
        from baldur.services.config_shadow.models import EvaluationContext

        ctx = EvaluationContext(
            baseline_config={},
            candidate_config={},
        )
        assert ctx.baseline_labels == {}

    def test_candidate_labels_default_is_empty_dict(self):
        """candidate_labels 기본값: 빈 딕셔너리."""
        from baldur.services.config_shadow.models import EvaluationContext

        ctx = EvaluationContext(
            baseline_config={},
            candidate_config={},
        )
        assert ctx.candidate_labels == {}

    def test_service_name_default_is_empty_string(self):
        """service_name 기본값: 빈 문자열."""
        from baldur.services.config_shadow.models import EvaluationContext

        ctx = EvaluationContext(
            baseline_config={},
            candidate_config={},
        )
        assert ctx.service_name == ""

    def test_events_default_is_empty_list(self):
        """events 기본값: 빈 리스트."""
        from baldur.services.config_shadow.models import EvaluationContext

        ctx = EvaluationContext(
            baseline_config={},
            candidate_config={},
        )
        assert ctx.events == []


class TestEvaluationContextInstanceIsolationBehavior:
    """EvaluationContext default_factory 인스턴스 격리 검증."""

    def test_events_are_independent(self):
        """EvaluationContext 인스턴스 간 events 리스트가 공유되지 않는다."""
        from baldur.services.config_shadow.models import EvaluationContext

        ctx1 = EvaluationContext(baseline_config={}, candidate_config={})
        ctx2 = EvaluationContext(baseline_config={}, candidate_config={})
        ctx1.events.append("dummy")
        assert ctx2.events == []

    def test_labels_are_independent(self):
        """EvaluationContext 인스턴스 간 labels 딕셔너리가 공유되지 않는다."""
        from baldur.services.config_shadow.models import EvaluationContext

        ctx1 = EvaluationContext(baseline_config={}, candidate_config={})
        ctx2 = EvaluationContext(baseline_config={}, candidate_config={})
        ctx1.baseline_labels["key"] = "value"
        assert ctx2.baseline_labels == {}
