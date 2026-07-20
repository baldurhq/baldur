"""
Config Shadow Evaluators.

The ConfigEvaluator protocol and its implementations.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from baldur.services.config_shadow.models import EvaluationContext, EvaluatorResult


@runtime_checkable
class ConfigEvaluator(Protocol):
    """Protocol for evaluators that simulate the effect of a config change."""

    @property
    def name(self) -> str:
        """Evaluator name (e.g. "circuit_breaker")."""
        ...

    @property
    def event_types(self) -> list[str]:
        """Event types this evaluator handles."""
        ...

    def evaluate(self, context: EvaluationContext) -> EvaluatorResult:
        """Compare the baseline and candidate configs using an EvaluationContext.

        The Shadow evaluator uses context.events; the Live evaluator uses
        context.time_window_seconds + context.*_labels.

        Returns:
            The comparison result
        """
        ...
