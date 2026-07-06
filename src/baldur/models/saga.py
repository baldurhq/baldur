"""
Saga Domain Value Types.

OSS-tier value types for Saga step execution results. Runtime-instantiated DTOs
that must be available on OSS-only installs (e.g., runbook primitives that
return StepResult from action handlers).

The full Saga orchestrator and its lifecycle types (SagaStatus, SagaInstance,
SagaDefinition, etc.) live in baldur_pro.services.saga.models and stay PRO-tier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StepResult:
    """SagaStep.execute() / compensate() execution result.

    Returned by Saga step handlers and by runbook action primitives. Carries
    success/failure outcome plus optional data payload (merged into SagaContext
    on execute) and structured error metadata for DLQ classification.
    """

    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    error_code: str | None = None
    retryable: bool = False
    partial_execution: bool = False
    """True when external side effects occurred before failure.

    EXECUTE_FAILED steps with partial_execution=True are included in the
    compensation target list so the orchestrator can clean up the partial state.
    """

    @classmethod
    def succeeded(cls, data: dict[str, Any] | None = None) -> StepResult:
        return cls(success=True, data=data or {})

    @classmethod
    def failed(
        cls, error: str, error_code: str = "", retryable: bool = False
    ) -> StepResult:
        return cls(
            success=False, error=error, error_code=error_code, retryable=retryable
        )

    @classmethod
    def failed_with_side_effect(
        cls,
        error: str,
        data: dict[str, Any] | None = None,
        error_code: str = "",
        retryable: bool = False,
    ) -> StepResult:
        """Failure factory for steps that produced external side effects.

        The orchestrator includes this step in the compensation target list so
        compensate() can clean up partial state. Pass partial execution data via
        the data argument.
        """
        return cls(
            success=False,
            error=error,
            error_code=error_code,
            retryable=retryable,
            data=data or {},
            partial_execution=True,
        )


__all__ = ["StepResult"]
