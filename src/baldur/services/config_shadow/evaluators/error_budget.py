"""
Error Budget Evaluator.

Error Budget Calculator의 burn rate 로직을 활용하여
설정 변경이 예산 소모에 미치는 영향을 시뮬레이션한다.
"""

from __future__ import annotations

from typing import Any

from baldur.interfaces.event_journal import JournalEntry
from baldur.services.config_shadow.models import (
    BudgetSimulationResult,
    EvaluationContext,
    EvaluatorResult,
)


class ErrorBudgetEvaluator:
    """Error Budget 설정 변경 효과 시뮬레이터."""

    @property
    def name(self) -> str:
        return "error_budget"

    @property
    def event_types(self) -> list[str]:
        return ["error_budget_critical"]

    def evaluate(self, context: EvaluationContext) -> EvaluatorResult:
        events = context.events
        baseline_config = context.baseline_config
        candidate_config = context.candidate_config

        baseline_budget = self._simulate(events, baseline_config)
        candidate_budget = self._simulate(events, candidate_config)

        delta_drain = (
            candidate_budget.total_drain_percent - baseline_budget.total_drain_percent
        )
        delta_critical_episodes = (
            candidate_budget.critical_episodes - baseline_budget.critical_episodes
        )

        return EvaluatorResult(
            evaluator_name=self.name,
            passed=self._check_pass_criteria(baseline_budget, candidate_budget),
            confidence_score=self._calculate_confidence(events),
            baseline_metrics={
                "total_drain_percent": baseline_budget.total_drain_percent,
                "critical_episodes": baseline_budget.critical_episodes,
                "max_burn_rate_1h": baseline_budget.max_burn_rate_1h,
            },
            candidate_metrics={
                "total_drain_percent": candidate_budget.total_drain_percent,
                "critical_episodes": candidate_budget.critical_episodes,
                "max_burn_rate_1h": candidate_budget.max_burn_rate_1h,
            },
            delta={
                "drain_percent_delta": delta_drain,
                "critical_episodes_delta": delta_critical_episodes,
            },
        )

    def _simulate(
        self,
        events: list[JournalEntry],
        config: dict[str, Any],
    ) -> BudgetSimulationResult:
        """Error Budget 이벤트를 기반으로 예산 소모를 시뮬레이션한다."""
        critical_threshold = config.get("critical_threshold_percent", 10)
        burn_rate_fast_critical = config.get("burn_rate_fast_critical", 14.4)
        total_drain = 0.0
        critical_episodes = 0
        max_burn_rate_1h = 0.0

        for event in events:
            if event.event_type == "error_budget_critical":
                budget_pct = event.context.get("budget_remaining_percent", 100)
                burn_rate = event.context.get("burn_rate_1h", 0)

                if (
                    budget_pct < critical_threshold
                    or burn_rate >= burn_rate_fast_critical
                ):
                    critical_episodes += 1

                max_burn_rate_1h = max(max_burn_rate_1h, burn_rate)
                total_drain += event.context.get("drain_amount", 0)

        return BudgetSimulationResult(
            total_drain_percent=total_drain,
            critical_episodes=critical_episodes,
            max_burn_rate_1h=max_burn_rate_1h,
        )

    def _check_pass_criteria(
        self,
        baseline: BudgetSimulationResult,
        candidate: BudgetSimulationResult,
    ) -> bool:
        if candidate.critical_episodes > baseline.critical_episodes:
            return False

        if baseline.total_drain_percent > 0:
            drain_ratio = candidate.total_drain_percent / baseline.total_drain_percent
            if drain_ratio > 1.5:
                return False

        return True

    def _calculate_confidence(self, events: list[JournalEntry]) -> float:
        """이벤트 충분성 기반 신뢰도 계산."""
        budget_events = [e for e in events if e.event_type == "error_budget_critical"]

        if len(budget_events) < 5:
            return 0.2
        if len(budget_events) < 20:
            return 0.5
        if len(budget_events) < 50:
            return 0.8
        return 0.95
