"""Execution Outcome Protocol — common result contract for all execution engines.

Execution Hierarchy:
    PolicyComposer          — Declarative resilience: Guard -> Policy -> Hook -> Sink
                              Independent — does not compose other engines
    ActionExecutor          — Single action + ExecutionMode gate
                              Used by: RunbookExecutor (per-step execution)
    StepExecutionEngine     — Multi-step forward/compensation infrastructure (ABC)
      RunbookExecutor       — Concrete implementation
      (future: SagaExecutor, PipelineExecutor)

All four engines produce results with common semantics (success/failure, execution
status). This protocol captures the minimal shared contract.
"""

from __future__ import annotations

from typing import Protocol

__all__ = ["ExecutionOutcome"]


class ExecutionOutcome(Protocol):
    """Minimal contract for execution results across all engines."""

    @property
    def success(self) -> bool | None: ...

    @property
    def executed(self) -> bool: ...
