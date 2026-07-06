"""
Config Shadow Evaluators.

ConfigEvaluator Protocol 및 구현체.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from baldur.services.config_shadow.models import EvaluationContext, EvaluatorResult


@runtime_checkable
class ConfigEvaluator(Protocol):
    """설정 변경 효과를 시뮬레이션하는 Evaluator 프로토콜."""

    @property
    def name(self) -> str:
        """Evaluator 이름 (예: "circuit_breaker")."""
        ...

    @property
    def event_types(self) -> list[str]:
        """이 Evaluator가 처리하는 이벤트 타입 리스트."""
        ...

    def evaluate(self, context: EvaluationContext) -> EvaluatorResult:
        """EvaluationContext를 기반으로 baseline과 candidate 설정을 비교 평가한다.

        Shadow Evaluator는 context.events를 사용하고,
        Live Evaluator는 context.time_window_seconds + context.*_labels를 사용한다.

        Returns:
            비교 결과
        """
        ...
