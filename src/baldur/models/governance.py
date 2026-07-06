"""
Governance Domain Types.

Shared governance result types used by governance checks, replay service,
and other OSS modules that need to inspect governance decisions.

Keeping these in models/ breaks the OSS → PRO/ENT dependency for basic
type definitions (same pattern as models/emergency.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from baldur.core.serializable import SerializableMixin


class BlockReason(str, Enum):
    """Reason why automation was blocked."""

    KILL_SWITCH = "kill_switch"
    """Kill Switch is active."""

    EMERGENCY_MODE = "emergency_mode"
    """Emergency mode is active (LEVEL_2+)."""

    ERROR_BUDGET = "error_budget"
    """Error budget exhausted."""

    RATE_LIMITED = "rate_limited"
    """Rate limit exceeded."""

    MANUALLY_BLOCKED = "manually_blocked"
    """Manually blocked by administrator."""

    SHADOW_EVALUATION_FAILED = "shadow_evaluation_failed"
    """Shadow Evaluation simulation result: ineligible."""


@dataclass
class GovernanceCheckResult(SerializableMixin):
    """Result of a governance check."""

    allowed: bool
    """Whether execution is allowed."""

    block_reason: BlockReason | None = None
    """Reason for blocking, if blocked."""

    block_message: str = ""
    """Human-readable message."""

    # Detail fields (for debugging/logging)
    emergency_level: str = "UNKNOWN"
    error_budget_percent: float = 100.0
    threshold_percent: float = 0.0

    @classmethod
    def allowed_result(cls) -> GovernanceCheckResult:
        """Factory for an allowed result."""
        return cls(allowed=True)

    @classmethod
    def blocked_by_kill_switch(cls) -> GovernanceCheckResult:
        """Factory for a kill-switch-blocked result."""
        return cls(
            allowed=False,
            block_reason=BlockReason.KILL_SWITCH,
            block_message="Kill Switch is active: baldur system is disabled",
        )

    @classmethod
    def blocked_by_emergency(
        cls,
        level_name: str,
        message: str = "",
        hours_remaining: float | None = None,
    ) -> GovernanceCheckResult:
        """Factory for an emergency-mode-blocked result."""
        if not message:
            if hours_remaining is not None:
                message = (
                    f"Emergency mode {level_name} is active. "
                    f"Auto-restore in ~{hours_remaining:.1f}h."
                )
            else:
                message = f"Emergency mode {level_name} is active"
        return cls(
            allowed=False,
            block_reason=BlockReason.EMERGENCY_MODE,
            block_message=message,
            emergency_level=level_name,
        )

    @classmethod
    def blocked_by_shadow_evaluation(
        cls,
        evaluation_id: str,
        summary: str,
        confidence_score: float,
    ) -> GovernanceCheckResult:
        """Factory for a shadow-evaluation-failed result."""
        return cls(
            allowed=False,
            block_reason=BlockReason.SHADOW_EVALUATION_FAILED,
            block_message=(
                f"Shadow evaluation failed (id={evaluation_id}, "
                f"confidence={confidence_score:.2f}): {summary}"
            ),
        )

    @classmethod
    def blocked_by_error_budget(
        cls,
        budget_percent: float,
        threshold_percent: float,
    ) -> GovernanceCheckResult:
        """Factory for an error-budget-blocked result."""
        return cls(
            allowed=False,
            block_reason=BlockReason.ERROR_BUDGET,
            block_message=(
                f"Error budget critically low ({budget_percent:.1f}%, "
                f"threshold: {threshold_percent:.1f}%): manual mode enforced"
            ),
            error_budget_percent=budget_percent,
            threshold_percent=threshold_percent,
        )

    @classmethod
    def from_pipeline_result(cls, pipeline_result) -> GovernanceCheckResult:
        """Map a SafetyCheckPipeline PipelineResult to GovernanceCheckResult."""
        if pipeline_result.passed:
            return cls.allowed_result()

        failure = pipeline_result.first_failure
        if failure is None:
            return cls.allowed_result()

        name = failure.check_name
        meta = failure.metadata

        if name == "kill_switch":
            return cls.blocked_by_kill_switch()

        if name == "emergency_mode":
            return cls.blocked_by_emergency(
                level_name=meta.get("emergency_level", "UNKNOWN"),
                message=failure.reason,
                hours_remaining=meta.get("hours_remaining"),
            )

        if name in ("error_budget_gate", "error_budget"):
            return cls.blocked_by_error_budget(
                budget_percent=meta.get("error_budget_percent", 0.0),
                threshold_percent=meta.get("threshold_percent", 0.0),
            )

        return cls(
            allowed=False,
            block_reason=BlockReason.MANUALLY_BLOCKED,
            block_message=failure.reason,
        )

    # to_dict() inherited — Mixin handles Optional[Enum] .value automatically
