"""
Unit tests for Config Shadow Celery task.

검증 항목:
- run_shadow_evaluation 태스크 속성 계약 (name, max_retries, acks_late)
- 정상 실행: service.execute_evaluation 호출 + 결과 dict 반환
- 예외 시 self.retry 호출

테스트 대상: baldur.adapters.celery.tasks.config_shadow
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.celery.tasks.config_shadow import run_shadow_evaluation
from baldur.services.config_shadow.models import (
    EvaluationReport,
    EvaluationStatus,
    ShadowEvaluation,
)
from baldur.services.config_shadow.service import ShadowEvaluatorService


class TestRunShadowEvaluationContract:
    """run_shadow_evaluation 태스크 설계 계약값 검증."""

    def test_task_name(self):
        """태스크 name: 'baldur.tasks.config_shadow.run_shadow_evaluation'."""
        assert (
            run_shadow_evaluation.name
            == "baldur.tasks.config_shadow.run_shadow_evaluation"
        )

    def test_max_retries_is_1(self):
        """max_retries: 1."""
        assert run_shadow_evaluation.max_retries == 1

    def test_default_retry_delay_is_30(self):
        """default_retry_delay: 30."""
        assert run_shadow_evaluation.default_retry_delay == 30

    def test_acks_late_is_true(self):
        """acks_late: True."""
        assert run_shadow_evaluation.acks_late is True


class TestRunShadowEvaluationBehavior:
    """run_shadow_evaluation 실행 동작 검증."""

    @patch("baldur.services.config_shadow.get_shadow_evaluator_service")
    def test_successful_execution_returns_result_dict(self, mock_get_service):
        """정상 실행 시 evaluation_id, status, passed를 포함한 dict 반환."""
        now = datetime.now(UTC)
        mock_evaluation = ShadowEvaluation(
            evaluation_id="abc123",
            rollout_id=None,
            status=EvaluationStatus.COMPLETED,
            created_at=now,
            report=EvaluationReport(
                events_analyzed=10,
                time_range_start=now,
                time_range_end=now,
                passed=True,
            ),
        )

        mock_service = MagicMock(spec=ShadowEvaluatorService)
        mock_service.execute_from_params.return_value = mock_evaluation
        mock_get_service.return_value = mock_service

        with patch.object(run_shadow_evaluation, "retry", side_effect=RuntimeError):
            result = run_shadow_evaluation.run(
                evaluation_id="abc123",
                config_type="circuit_breaker",
                baseline_config={"failure_threshold": 5},
                candidate_config={"failure_threshold": 3},
                service_name="payment",
            )

        assert result == {
            "evaluation_id": "abc123",
            "status": "completed",
            "passed": True,
        }
        mock_service.execute_from_params.assert_called_once_with(
            evaluation_id="abc123",
            config_type="circuit_breaker",
            baseline_config={"failure_threshold": 5},
            candidate_config={"failure_threshold": 3},
            service_name="payment",
            time_window_hours=336,
            region="",
            rollout_id=None,
        )

    @patch("baldur.services.config_shadow.get_shadow_evaluator_service")
    def test_exception_triggers_retry(self, mock_get_service):
        """예외 발생 시 self.retry가 호출된다."""
        mock_get_service.side_effect = RuntimeError("service unavailable")

        with patch.object(
            run_shadow_evaluation, "retry", side_effect=RuntimeError("retry")
        ) as mock_retry:
            with pytest.raises(RuntimeError, match="retry"):
                run_shadow_evaluation.run(evaluation_id="abc123")

        mock_retry.assert_called_once()

    @patch("baldur.services.config_shadow.get_shadow_evaluator_service")
    def test_failed_evaluation_returns_none_passed(self, mock_get_service):
        """evaluation.report가 None이면 passed=None."""
        now = datetime.now(UTC)
        mock_evaluation = ShadowEvaluation(
            evaluation_id="abc123",
            rollout_id=None,
            status=EvaluationStatus.FAILED,
            created_at=now,
            report=None,
        )

        mock_service = MagicMock(spec=ShadowEvaluatorService)
        mock_service.execute_from_params.return_value = mock_evaluation
        mock_get_service.return_value = mock_service

        with patch.object(run_shadow_evaluation, "retry", side_effect=RuntimeError):
            result = run_shadow_evaluation.run(
                evaluation_id="abc123",
                config_type="circuit_breaker",
                baseline_config={},
                candidate_config={},
            )

        assert result["passed"] is None
        assert result["status"] == "failed"
