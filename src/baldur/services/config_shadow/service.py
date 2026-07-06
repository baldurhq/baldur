"""
Config Shadow Evaluator Service.

설정 변경 사전 시뮬레이션 엔진. 과거 이벤트를 리플레이하여
"새 설정이었다면 어떤 결과가 나왔을까"를 시뮬레이션한다.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

import structlog

from baldur.interfaces.event_journal import (
    EventJournalRepository,
    JournalQueryFilter,
)
from baldur.services.config_shadow.evaluators import ConfigEvaluator
from baldur.services.config_shadow.evaluators.circuit_breaker import (
    CircuitBreakerEvaluator,
)
from baldur.services.config_shadow.evaluators.error_budget import (
    ErrorBudgetEvaluator,
)
from baldur.services.config_shadow.metrics_provider import (
    TimeSeriesMetricsProvider,
)
from baldur.services.config_shadow.models import (
    EvaluationContext,
    EvaluationReport,
    EvaluationStatus,
    ShadowEvaluation,
)  # fmt: skip
from baldur.utils.time import utc_now

logger = structlog.get_logger(__name__)


class ShadowEvaluatorService:
    """Config Shadow Evaluation 서비스."""

    def __init__(
        self,
        journal_repo: EventJournalRepository | None = None,
        evaluators: list[ConfigEvaluator] | None = None,
        metrics_provider: TimeSeriesMetricsProvider | None = None,
    ):
        if journal_repo is None:
            from baldur.factory import ProviderRegistry

            journal_repo = ProviderRegistry.get_event_journal_repo()
        self._journal_repo = journal_repo
        self._evaluators = evaluators or self._default_evaluators()
        self._metrics_provider = metrics_provider
        self._evaluations: dict[str, ShadowEvaluation] = {}
        self._rollout_evaluations: dict[str, ShadowEvaluation] = {}

    def _default_evaluators(self) -> list[ConfigEvaluator]:
        return [
            CircuitBreakerEvaluator(),
            ErrorBudgetEvaluator(),
        ]

    def submit_evaluation(
        self,
        config_type: str,
        baseline_config: dict[str, Any],
        candidate_config: dict[str, Any],
        service_name: str = "",
        time_window_hours: int = 336,
        rollout_id: str | None = None,
        region: str = "",
    ) -> ShadowEvaluation:
        """Shadow Evaluation을 생성하고 비동기 실행을 예약한다.

        즉시 PENDING 상태의 ShadowEvaluation을 반환한다.
        실제 시뮬레이션은 Celery 워커가 수행한다.
        클라이언트는 get_evaluation()으로 상태를 폴링한다.

        Returns:
            PENDING 상태의 ShadowEvaluation (evaluation_id 포함)
        """
        evaluation = ShadowEvaluation(
            evaluation_id=uuid4().hex[:12],
            rollout_id=rollout_id,
            status=EvaluationStatus.PENDING,
            created_at=utc_now(),
            config_type=config_type,
            baseline_config=baseline_config,
            candidate_config=candidate_config,
            service_name=service_name,
            time_window_hours=time_window_hours,
            region=region,
        )
        self._evaluations[evaluation.evaluation_id] = evaluation

        from baldur.adapters.celery.tasks.config_shadow import (
            run_shadow_evaluation,
        )

        run_shadow_evaluation.delay(
            evaluation_id=evaluation.evaluation_id,
            config_type=config_type,
            baseline_config=baseline_config,
            candidate_config=candidate_config,
            service_name=service_name,
            time_window_hours=time_window_hours,
            region=region,
            rollout_id=rollout_id,
        )

        return evaluation

    def execute_evaluation(self, evaluation_id: str) -> ShadowEvaluation:
        """인프로세스 시뮬레이션 실행. 로컬 dict에서 evaluation을 조회한다."""
        evaluation = self._evaluations.get(evaluation_id)
        if evaluation is None:
            raise ValueError(f"Unknown evaluation_id: {evaluation_id}")
        return self._run_evaluation(evaluation)

    def execute_from_params(
        self,
        evaluation_id: str,
        config_type: str,
        baseline_config: dict[str, Any],
        candidate_config: dict[str, Any],
        service_name: str = "",
        time_window_hours: int = 336,
        region: str = "",
        rollout_id: str | None = None,
    ) -> ShadowEvaluation:
        """Celery 워커에서 호출. 파라미터로부터 evaluation을 생성하고 실행한다."""
        evaluation = ShadowEvaluation(
            evaluation_id=evaluation_id,
            rollout_id=rollout_id,
            status=EvaluationStatus.PENDING,
            created_at=utc_now(),
            config_type=config_type,
            baseline_config=baseline_config,
            candidate_config=candidate_config,
            service_name=service_name,
            time_window_hours=time_window_hours,
            region=region,
        )
        self._evaluations[evaluation_id] = evaluation
        return self._run_evaluation(evaluation)

    def _run_evaluation(self, evaluation: ShadowEvaluation) -> ShadowEvaluation:
        """실제 시뮬레이션 로직. submit/execute/execute_from_params에서 호출."""
        evaluation.status = EvaluationStatus.RUNNING

        try:
            evaluator = self._find_evaluator(evaluation.config_type)
            if evaluator is None:
                evaluation.status = EvaluationStatus.FAILED
                evaluation.error_message = (
                    f"No evaluator for config_type: {evaluation.config_type}"
                )
                return evaluation

            end_time = utc_now()
            start_time = end_time - timedelta(hours=evaluation.time_window_hours)

            query_filter = JournalQueryFilter(
                event_types=evaluator.event_types,
                service_name=evaluation.service_name or None,
                start_time=start_time,
                end_time=end_time,
                region=evaluation.region or None,
            )
            query_result = self._journal_repo.query(query_filter)

            context = EvaluationContext(
                baseline_config=evaluation.baseline_config,
                candidate_config=evaluation.candidate_config,
                events=query_result.entries,
                service_name=evaluation.service_name,
            )
            result = evaluator.evaluate(context)

            evaluation.report = EvaluationReport(
                events_analyzed=len(query_result.entries),
                time_range_start=start_time,
                time_range_end=end_time,
                evaluator_results=[result],
                passed=result.passed,
                confidence_score=result.confidence_score,
                summary=result.details,
                warnings=result.warnings,
            )
            evaluation.status = EvaluationStatus.COMPLETED
            evaluation.completed_at = utc_now()

        except Exception as e:
            evaluation.status = EvaluationStatus.FAILED
            evaluation.error_message = str(e)
            logger.exception(
                "config_shadow.evaluation_failed",
                evaluation_id=evaluation.evaluation_id,
                error=str(e),
            )

        return evaluation

    def get_evaluation(self, evaluation_id: str) -> ShadowEvaluation | None:
        """evaluation_id로 상태를 조회한다. 클라이언트 폴링용."""
        return self._evaluations.get(evaluation_id)

    def compare_candidates(
        self,
        config_type: str,
        baseline_config: dict[str, Any],
        candidates: list[dict[str, Any]],
        service_name: str = "",
        time_window_hours: int = 336,
    ) -> list[ShadowEvaluation]:
        """여러 후보 설정을 baseline과 비교한다."""
        results = []
        for candidate in candidates:
            result = self.submit_evaluation(
                config_type=config_type,
                baseline_config=baseline_config,
                candidate_config=candidate,
                service_name=service_name,
                time_window_hours=time_window_hours,
            )
            results.append(result)
        return results

    def evaluate_for_rollout(
        self,
        rollout_id: str,
        config_type: str,
        baseline_config: dict[str, Any],
        candidate_config: dict[str, Any],
        service_name: str = "",
        time_window_hours: int = 336,
    ) -> ShadowEvaluation:
        """Canary rollout에 연결된 Shadow Evaluation을 실행한다.

        submit_evaluation()과 동일하되 rollout_id를 연결하고 결과를 캐시한다.
        이후 start_rollout()의 _check_shadow_evaluation()에서 조회된다.
        """
        evaluation = self.submit_evaluation(
            config_type=config_type,
            baseline_config=baseline_config,
            candidate_config=candidate_config,
            service_name=service_name,
            time_window_hours=time_window_hours,
            rollout_id=rollout_id,
        )

        self._rollout_evaluations[rollout_id] = evaluation

        return evaluation

    def get_latest_for_rollout(self, rollout_id: str) -> ShadowEvaluation | None:
        """rollout에 연결된 최신 Shadow Evaluation을 반환한다."""
        return self._rollout_evaluations.get(rollout_id)

    def has_rollout_evaluation_trigger(self) -> bool:
        """Return whether a production path populates rollout-linked evaluations.

        The canary shadow gate consults this before honoring a hard block on a
        missing evaluation: while no trigger is wired, requiring an evaluation
        can never be satisfied, so the gate warns-and-skips instead of
        permanently blocking the rollout.

        Returns ``False`` today — no production path calls
        ``evaluate_for_rollout``.
        """
        # TODO(v1.1): remove structural probe once the evaluate_for_rollout trigger is wired
        # Flip to a real "is a production trigger wired?" check when
        # evaluate_for_rollout gains a production caller; co-located here so the
        # auto-yield seam moves with the trigger it describes. See doc 556 / OOS #550.
        return False

    def _find_evaluator(self, config_type: str) -> ConfigEvaluator | None:
        for evaluator in self._evaluators:
            if evaluator.name == config_type:
                return evaluator
        return None
