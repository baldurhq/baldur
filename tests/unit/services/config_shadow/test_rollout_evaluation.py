"""
Tests for ShadowEvaluatorService.evaluate_for_rollout() and get_latest_for_rollout().

Target: services/config_shadow/service.py (commit 300)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.services.config_shadow.models import EvaluationStatus


class TestEvaluateForRolloutBehavior:
    """evaluate_for_rollout() 동작 검증."""

    @pytest.fixture
    def shadow_service(self):
        """ShadowEvaluatorService with mocked dependencies."""
        from baldur.services.config_shadow.service import ShadowEvaluatorService

        svc = ShadowEvaluatorService(
            journal_repo=MagicMock(),
            evaluators=[],
            metrics_provider=MagicMock(),
        )
        return svc

    @pytest.fixture
    def _mock_celery_task(self):
        """Mock the Celery task imported inside submit_evaluation."""
        with patch(
            "baldur.adapters.celery.tasks.config_shadow.run_shadow_evaluation",
        ) as mock_task:
            mock_task.delay = MagicMock()
            yield mock_task

    def test_returns_pending_evaluation(self, shadow_service, _mock_celery_task):
        """evaluate_for_rollout은 PENDING 상태의 ShadowEvaluation을 반환한다."""
        evaluation = shadow_service.evaluate_for_rollout(
            rollout_id="rollout-001",
            config_type="circuit_breaker",
            baseline_config={"threshold": 5},
            candidate_config={"threshold": 10},
        )
        assert evaluation.status == EvaluationStatus.PENDING

    def test_evaluation_has_rollout_id(self, shadow_service, _mock_celery_task):
        """반환된 evaluation에 rollout_id가 설정된다."""
        evaluation = shadow_service.evaluate_for_rollout(
            rollout_id="rollout-xyz",
            config_type="circuit_breaker",
            baseline_config={},
            candidate_config={},
        )
        assert evaluation.rollout_id == "rollout-xyz"

    def test_stores_in_rollout_evaluations_cache(
        self, shadow_service, _mock_celery_task
    ):
        """평가 결과가 _rollout_evaluations에 캐시된다."""
        evaluation = shadow_service.evaluate_for_rollout(
            rollout_id="rollout-001",
            config_type="circuit_breaker",
            baseline_config={},
            candidate_config={},
        )
        assert shadow_service._rollout_evaluations["rollout-001"] is evaluation

    def test_get_latest_for_rollout_retrieves_cached(
        self, shadow_service, _mock_celery_task
    ):
        """get_latest_for_rollout은 캐시된 evaluation을 반환한다."""
        evaluation = shadow_service.evaluate_for_rollout(
            rollout_id="rollout-001",
            config_type="circuit_breaker",
            baseline_config={},
            candidate_config={},
        )
        result = shadow_service.get_latest_for_rollout("rollout-001")
        assert result is evaluation

    def test_get_latest_for_rollout_unknown_returns_none(self, shadow_service):
        """없는 rollout_id로 조회하면 None을 반환한다."""
        result = shadow_service.get_latest_for_rollout("nonexistent")
        assert result is None

    def test_subsequent_evaluate_for_rollout_overwrites_cache(
        self, shadow_service, _mock_celery_task
    ):
        """동일 rollout_id에 대해 재평가 시 캐시가 갱신된다."""
        first = shadow_service.evaluate_for_rollout(
            rollout_id="rollout-001",
            config_type="circuit_breaker",
            baseline_config={},
            candidate_config={"v": 1},
        )
        second = shadow_service.evaluate_for_rollout(
            rollout_id="rollout-001",
            config_type="circuit_breaker",
            baseline_config={},
            candidate_config={"v": 2},
        )
        assert shadow_service.get_latest_for_rollout("rollout-001") is second
        assert first is not second


class TestRolloutEvaluationTriggerContract:
    """has_rollout_evaluation_trigger() contract — dormant default + v1.1 tripwire."""

    @pytest.fixture
    def shadow_service(self):
        """ShadowEvaluatorService with mocked dependencies."""
        from baldur.services.config_shadow.service import ShadowEvaluatorService

        return ShadowEvaluatorService(
            journal_repo=MagicMock(),
            evaluators=[],
            metrics_provider=MagicMock(),
        )

    def test_trigger_is_dormant_returns_false(self, shadow_service):
        """has_rollout_evaluation_trigger() returns False while the v1.1 trigger is unwired.

        Hardcoded structural contract: no production path calls evaluate_for_rollout
        yet, so a rollout-linked evaluation can never be produced. The canary shadow
        gate consults this literal to warn-and-skip instead of permanently blocking.

        This assertion doubles as a v1.1 tripwire — once a production trigger is wired
        and the probe flips to True, this test fails, forcing the v1.1 author to update
        the gate semantics and its tests together.
        """
        assert shadow_service.has_rollout_evaluation_trigger() is False
