"""
Unit tests for Config Shadow Evaluator Service.

검증 항목:
- submit_evaluation: PENDING 상태 반환, evaluation_id 생성, Celery 태스크 호출
- execute_evaluation: 상태 전이 (PENDING→RUNNING→COMPLETED/FAILED)
- execute_evaluation: 알 수 없는 evaluator → FAILED + error_message
- execute_evaluation: 예외 발생 시 FAILED + error_message
- get_evaluation: 존재/미존재 ID 조회
- compare_candidates: 여러 후보 비교
- _find_evaluator: config_type으로 evaluator 매칭
- _default_evaluators: CB + ErrorBudget 2개 등록

테스트 대상: baldur.services.config_shadow.service
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from baldur.interfaces.event_journal import (
    EventJournalRepository,
    JournalEntry,
    JournalQueryFilter,
    JournalQueryResult,
)
from baldur.services.config_shadow.evaluators import ConfigEvaluator
from baldur.services.config_shadow.models import (
    EvaluationStatus,
    EvaluatorResult,
)
from baldur.services.config_shadow.service import ShadowEvaluatorService


def _make_mock_journal(entries: list[JournalEntry] | None = None) -> MagicMock:
    mock = MagicMock(spec=EventJournalRepository)
    mock.query.return_value = JournalQueryResult(
        entries=entries or [], truncated=False, total_count=len(entries or [])
    )
    return mock


def _make_mock_evaluator(
    name: str,
    passed: bool = True,
    event_types: list[str] | None = None,
) -> MagicMock:
    mock = MagicMock(spec=ConfigEvaluator)
    mock.name = name
    mock.event_types = event_types or [
        "circuit_breaker_opened",
        "circuit_breaker_closed",
    ]
    mock.evaluate.return_value = EvaluatorResult(
        evaluator_name=name,
        passed=passed,
        confidence_score=0.8,
        details="test details",
    )
    return mock


class TestShadowEvaluatorServiceContract:
    """ShadowEvaluatorService 설계 계약값 검증."""

    def test_default_evaluators_contains_circuit_breaker_and_error_budget(self):
        """기본 evaluator: circuit_breaker, error_budget 2개."""
        journal = _make_mock_journal()
        service = ShadowEvaluatorService(journal_repo=journal)
        names = [e.name for e in service._evaluators]
        assert "circuit_breaker" in names
        assert "error_budget" in names
        assert len(names) == 2

    def test_default_time_window_hours_is_336(self):
        """submit_evaluation의 기본 time_window_hours: 336."""
        journal = _make_mock_journal()
        service = ShadowEvaluatorService(journal_repo=journal)

        with patch(
            "baldur.services.config_shadow.service.ShadowEvaluatorService.submit_evaluation",
            wraps=service.submit_evaluation,
        ):
            with patch(
                "baldur.adapters.celery.tasks.config_shadow.run_shadow_evaluation"
            ) as mock_task:
                mock_task.delay = MagicMock()
                evaluation = service.submit_evaluation(
                    config_type="circuit_breaker",
                    baseline_config={},
                    candidate_config={},
                )
        assert evaluation.time_window_hours == 336


class TestSubmitEvaluationBehavior:
    """submit_evaluation 동작 검증."""

    @patch("baldur.adapters.celery.tasks.config_shadow.run_shadow_evaluation")
    def test_returns_pending_evaluation(self, mock_task):
        """PENDING 상태의 ShadowEvaluation을 반환한다."""
        mock_task.delay = MagicMock()
        journal = _make_mock_journal()
        service = ShadowEvaluatorService(journal_repo=journal)

        result = service.submit_evaluation(
            config_type="circuit_breaker",
            baseline_config={"failure_threshold": 5},
            candidate_config={"failure_threshold": 3},
            service_name="payment",
        )

        assert result.status == EvaluationStatus.PENDING
        assert result.evaluation_id != ""
        assert result.config_type == "circuit_breaker"
        assert result.service_name == "payment"

    @patch("baldur.adapters.celery.tasks.config_shadow.run_shadow_evaluation")
    def test_triggers_celery_task_with_full_params(self, mock_task):
        """Celery 태스크를 전체 evaluation 파라미터와 함께 호출한다."""
        mock_task.delay = MagicMock()
        journal = _make_mock_journal()
        service = ShadowEvaluatorService(journal_repo=journal)

        result = service.submit_evaluation(
            config_type="circuit_breaker",
            baseline_config={"failure_threshold": 5},
            candidate_config={"failure_threshold": 3},
            service_name="payment",
        )

        mock_task.delay.assert_called_once_with(
            evaluation_id=result.evaluation_id,
            config_type="circuit_breaker",
            baseline_config={"failure_threshold": 5},
            candidate_config={"failure_threshold": 3},
            service_name="payment",
            time_window_hours=336,
            region="",
            rollout_id=None,
        )

    @patch("baldur.adapters.celery.tasks.config_shadow.run_shadow_evaluation")
    def test_stores_evaluation_in_internal_dict(self, mock_task):
        """생성된 evaluation이 내부 딕셔너리에 저장된다."""
        mock_task.delay = MagicMock()
        journal = _make_mock_journal()
        service = ShadowEvaluatorService(journal_repo=journal)

        result = service.submit_evaluation(
            config_type="circuit_breaker",
            baseline_config={},
            candidate_config={},
        )

        assert service.get_evaluation(result.evaluation_id) is result


class TestExecuteEvaluationBehavior:
    """execute_evaluation 동작 검증."""

    def test_successful_execution_transitions_to_completed(self):
        """성공 시 RUNNING → COMPLETED 전이."""
        journal = _make_mock_journal()
        mock_eval = _make_mock_evaluator("circuit_breaker", passed=True)
        service = ShadowEvaluatorService(journal_repo=journal, evaluators=[mock_eval])

        with patch(
            "baldur.adapters.celery.tasks.config_shadow.run_shadow_evaluation"
        ) as mock_task:
            mock_task.delay = MagicMock()
            submitted = service.submit_evaluation(
                config_type="circuit_breaker",
                baseline_config={},
                candidate_config={},
            )

        result = service.execute_evaluation(submitted.evaluation_id)

        assert result.status == EvaluationStatus.COMPLETED
        assert result.report is not None
        assert result.report.passed is True
        assert result.completed_at is not None

    def test_unknown_config_type_transitions_to_failed(self):
        """매칭 evaluator 없으면 FAILED + error_message."""
        journal = _make_mock_journal()
        mock_eval = _make_mock_evaluator("circuit_breaker")
        service = ShadowEvaluatorService(journal_repo=journal, evaluators=[mock_eval])

        with patch(
            "baldur.adapters.celery.tasks.config_shadow.run_shadow_evaluation"
        ) as mock_task:
            mock_task.delay = MagicMock()
            submitted = service.submit_evaluation(
                config_type="unknown_type",
                baseline_config={},
                candidate_config={},
            )

        result = service.execute_evaluation(submitted.evaluation_id)

        assert result.status == EvaluationStatus.FAILED
        assert "No evaluator for config_type: unknown_type" in result.error_message

    def test_unknown_evaluation_id_raises_value_error(self):
        """존재하지 않는 evaluation_id → ValueError."""
        journal = _make_mock_journal()
        service = ShadowEvaluatorService(journal_repo=journal)

        with pytest.raises(ValueError, match="Unknown evaluation_id"):
            service.execute_evaluation("nonexistent-id")

    def test_exception_during_evaluation_transitions_to_failed(self):
        """evaluator 예외 발생 시 FAILED + error_message."""
        journal = _make_mock_journal()
        mock_eval = _make_mock_evaluator("circuit_breaker")
        mock_eval.evaluate.side_effect = RuntimeError("simulation error")
        service = ShadowEvaluatorService(journal_repo=journal, evaluators=[mock_eval])

        with patch(
            "baldur.adapters.celery.tasks.config_shadow.run_shadow_evaluation"
        ) as mock_task:
            mock_task.delay = MagicMock()
            submitted = service.submit_evaluation(
                config_type="circuit_breaker",
                baseline_config={},
                candidate_config={},
            )

        result = service.execute_evaluation(submitted.evaluation_id)

        assert result.status == EvaluationStatus.FAILED
        assert "simulation error" in result.error_message

    def test_report_contains_event_count_and_time_range(self):
        """report에 events_analyzed, time_range_start/end가 포함된다."""
        now = datetime.now(UTC)
        entries = [
            JournalEntry(
                sequence=i,
                event_type="circuit_breaker_opened",
                source="test",
                timestamp=now,
                service_name="svc",
            )
            for i in range(3)
        ]
        journal = _make_mock_journal(entries)
        mock_eval = _make_mock_evaluator("circuit_breaker")
        service = ShadowEvaluatorService(journal_repo=journal, evaluators=[mock_eval])

        with patch(
            "baldur.adapters.celery.tasks.config_shadow.run_shadow_evaluation"
        ) as mock_task:
            mock_task.delay = MagicMock()
            submitted = service.submit_evaluation(
                config_type="circuit_breaker",
                baseline_config={},
                candidate_config={},
            )

        result = service.execute_evaluation(submitted.evaluation_id)
        assert result.report.events_analyzed == 3
        assert result.report.time_range_start is not None
        assert result.report.time_range_end is not None

    def test_journal_query_uses_service_name_region_and_event_types(self):
        """journal query에 service_name, region, event_types가 전달된다."""
        journal = _make_mock_journal()
        mock_eval = _make_mock_evaluator("circuit_breaker")
        service = ShadowEvaluatorService(journal_repo=journal, evaluators=[mock_eval])

        with patch(
            "baldur.adapters.celery.tasks.config_shadow.run_shadow_evaluation"
        ) as mock_task:
            mock_task.delay = MagicMock()
            submitted = service.submit_evaluation(
                config_type="circuit_breaker",
                baseline_config={},
                candidate_config={},
                service_name="payment",
                region="us-east-1",
            )

        service.execute_evaluation(submitted.evaluation_id)

        call_args = journal.query.call_args[0][0]
        assert isinstance(call_args, JournalQueryFilter)
        assert call_args.service_name == "payment"
        assert call_args.region == "us-east-1"
        assert call_args.event_types == [
            "circuit_breaker_opened",
            "circuit_breaker_closed",
        ]


class TestExecuteFromParamsBehavior:
    """execute_from_params 동작 검증 (Celery 워커 경로)."""

    def test_creates_evaluation_from_params_and_runs(self):
        """파라미터로부터 evaluation을 생성하고 실행한다."""
        journal = _make_mock_journal()
        mock_eval = _make_mock_evaluator("circuit_breaker", passed=True)
        service = ShadowEvaluatorService(journal_repo=journal, evaluators=[mock_eval])

        result = service.execute_from_params(
            evaluation_id="test-id-123",
            config_type="circuit_breaker",
            baseline_config={"failure_threshold": 5},
            candidate_config={"failure_threshold": 3},
            service_name="payment",
        )

        assert result.evaluation_id == "test-id-123"
        assert result.status == EvaluationStatus.COMPLETED
        assert result.report is not None
        assert result.report.passed is True

    def test_stores_evaluation_retrievable_via_get_evaluation(self):
        """execute_from_params로 생성된 evaluation이 get_evaluation()으로 조회된다."""
        journal = _make_mock_journal()
        mock_eval = _make_mock_evaluator("circuit_breaker", passed=True)
        service = ShadowEvaluatorService(journal_repo=journal, evaluators=[mock_eval])

        result = service.execute_from_params(
            evaluation_id="worker-eval-001",
            config_type="circuit_breaker",
            baseline_config={},
            candidate_config={},
        )

        retrieved = service.get_evaluation("worker-eval-001")
        assert retrieved is result
        assert retrieved.status == EvaluationStatus.COMPLETED

    def test_unknown_config_type_returns_failed(self):
        """매칭 evaluator 없으면 FAILED."""
        journal = _make_mock_journal()
        mock_eval = _make_mock_evaluator("circuit_breaker")
        service = ShadowEvaluatorService(journal_repo=journal, evaluators=[mock_eval])

        result = service.execute_from_params(
            evaluation_id="test-id",
            config_type="unknown",
            baseline_config={},
            candidate_config={},
        )

        assert result.status == EvaluationStatus.FAILED
        assert "No evaluator for config_type: unknown" in result.error_message


class TestEvaluationIdFormat:
    """evaluation_id 형식 검증."""

    @patch("baldur.adapters.celery.tasks.config_shadow.run_shadow_evaluation")
    def test_evaluation_id_is_12_hex_chars(self, mock_task):
        """evaluation_id는 12자리 hex 문자열이다."""
        mock_task.delay = MagicMock()
        journal = _make_mock_journal()
        service = ShadowEvaluatorService(journal_repo=journal)

        result = service.submit_evaluation(
            config_type="circuit_breaker",
            baseline_config={},
            candidate_config={},
        )

        assert len(result.evaluation_id) == 12
        int(result.evaluation_id, 16)  # valid hex


class TestGetEvaluationBehavior:
    """get_evaluation 동작 검증."""

    def test_returns_none_for_unknown_id(self):
        """존재하지 않는 ID → None."""
        journal = _make_mock_journal()
        service = ShadowEvaluatorService(journal_repo=journal)
        assert service.get_evaluation("nonexistent") is None


class TestCompareCandidatesBehavior:
    """compare_candidates 동작 검증."""

    @patch("baldur.adapters.celery.tasks.config_shadow.run_shadow_evaluation")
    def test_submits_evaluation_per_candidate(self, mock_task):
        """후보 수만큼 evaluation을 제출한다."""
        mock_task.delay = MagicMock()
        journal = _make_mock_journal()
        service = ShadowEvaluatorService(journal_repo=journal)

        candidates = [
            {"failure_threshold": 3},
            {"failure_threshold": 7},
            {"failure_threshold": 10},
        ]
        results = service.compare_candidates(
            config_type="circuit_breaker",
            baseline_config={"failure_threshold": 5},
            candidates=candidates,
        )

        assert len(results) == 3
        assert mock_task.delay.call_count == 3
        ids = {r.evaluation_id for r in results}
        assert len(ids) == 3  # 모두 고유한 ID


class TestFindEvaluatorBehavior:
    """_find_evaluator 동작 검증."""

    def test_finds_matching_evaluator(self):
        """config_type과 일치하는 evaluator를 반환한다."""
        journal = _make_mock_journal()
        mock_cb = _make_mock_evaluator("circuit_breaker")
        mock_eb = _make_mock_evaluator("error_budget")
        service = ShadowEvaluatorService(
            journal_repo=journal, evaluators=[mock_cb, mock_eb]
        )

        assert service._find_evaluator("circuit_breaker") is mock_cb
        assert service._find_evaluator("error_budget") is mock_eb

    def test_returns_none_for_unknown_type(self):
        """일치하는 evaluator 없으면 None."""
        journal = _make_mock_journal()
        service = ShadowEvaluatorService(journal_repo=journal, evaluators=[])
        assert service._find_evaluator("unknown") is None
