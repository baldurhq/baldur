"""
Execution Mode Configuration for Shadow/Evaluation Mode Support.

Provides centralized control over whether actions are executed or only logged.

Usage:
    from baldur.core.execution_mode import ExecutionMode, get_execution_mode

    mode = get_execution_mode()
    if mode.is_active:
        # 실제 액션 실행
    else:
        # 로깅만

Environment Variable:
    BALDUR_EXECUTION_MODE: "active" | "shadow" | "evaluation"
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Any

import structlog

from baldur.core.decision_logger import ReasonCode, log_intervention_evaluated

logger = structlog.get_logger()


class ExecutionModeType(str, Enum):
    """Execution mode types."""

    # 실제 액션 실행 (프로덕션 기본값)
    ACTIVE = "active"

    # 결정만 로깅, 액션 실행 안함 (관찰 모드)
    SHADOW = "shadow"

    # 결정 + 검증 로깅, 액션 실행 안함 (평가 모드)
    EVALUATION = "evaluation"


@dataclass(frozen=True)
class ExecutionMode:
    """
    Execution mode configuration.

    Attributes:
        mode: Current execution mode
        log_decisions: Whether to log all decisions
        execute_actions: Whether to actually execute actions
        validate_only: Whether to validate without execution
    """

    mode: ExecutionModeType
    log_decisions: bool = True
    execute_actions: bool = True
    validate_only: bool = False

    @property
    def is_active(self) -> bool:
        """Check if in active (production) mode."""
        return self.mode == ExecutionModeType.ACTIVE

    @property
    def is_shadow(self) -> bool:
        """Check if in shadow (observe-only) mode."""
        return self.mode == ExecutionModeType.SHADOW

    @property
    def is_evaluation(self) -> bool:
        """Check if in evaluation mode."""
        return self.mode == ExecutionModeType.EVALUATION

    @property
    def should_execute(self) -> bool:
        """Check if actions should be executed."""
        return self.execute_actions and self.is_active

    @property
    def is_dry_run(self) -> bool:
        """Check if this is a dry-run (no side effects)."""
        return not self.execute_actions

    @classmethod
    def active(cls) -> ExecutionMode:
        """Create active mode configuration."""
        return cls(
            mode=ExecutionModeType.ACTIVE,
            log_decisions=True,
            execute_actions=True,
            validate_only=False,
        )

    @classmethod
    def shadow(cls) -> ExecutionMode:
        """Create shadow mode configuration."""
        return cls(
            mode=ExecutionModeType.SHADOW,
            log_decisions=True,
            execute_actions=False,
            validate_only=False,
        )

    @classmethod
    def evaluation(cls) -> ExecutionMode:
        """Create evaluation mode configuration."""
        return cls(
            mode=ExecutionModeType.EVALUATION,
            log_decisions=True,
            execute_actions=False,
            validate_only=True,
        )


# =============================================================================
# Global Mode Access
# =============================================================================

_override_mode: ExecutionMode | None = None


def set_execution_mode(mode: ExecutionMode) -> None:
    """
    Override the execution mode programmatically.

    Useful for testing or temporary mode changes.

    Args:
        mode: ExecutionMode to set
    """
    global _override_mode
    _override_mode = mode


def clear_execution_mode_override() -> None:
    """Clear any programmatic mode override."""
    global _override_mode
    _override_mode = None


@lru_cache(maxsize=1)
def _get_mode_from_env() -> ExecutionMode:
    """Load execution mode from environment variable."""
    mode_str = os.environ.get("BALDUR_EXECUTION_MODE", "active").lower()

    if mode_str == "shadow":
        return ExecutionMode.shadow()
    if mode_str == "evaluation":
        return ExecutionMode.evaluation()
    return ExecutionMode.active()


def _is_runtime_dry_run() -> bool:
    """Read the System Control runtime dry-run toggle, fail-safe.

    The import is deliberately kept inside the function body: ``system_control``
    pulls in the audit pipeline and the state backend, which would form an
    import-time cycle if imported at module scope. A def-body import is excluded
    from the first-party import-time graph by construction.

    Any failure (import cycle, early init, backend error) falls back to "not
    dry-run" so the toggle never disables healing on error — consistent with the
    kill switch assuming the system is enabled when its state cannot be read.
    """
    # def-body lazy import — excluded from the G40 import-cycle graph
    try:
        from baldur.services.system_control import is_dry_run

        return is_dry_run()
    except Exception:
        return False


def _resolve_mode() -> tuple[ExecutionMode, str]:
    """Resolve the effective execution mode and the precedence rung that set it.

    Single observe-only resolver. Precedence:

    1. Programmatic override (``set_execution_mode``) — test / advanced hook,
       wins absolutely (can force-execute over an on toggle).
    2. Runtime dry-run toggle (System Control) — monotonic toward observe-only:
       forces ``shadow`` only when the env mode would otherwise execute. An
       already-observe-only env posture (``shadow`` / ``evaluation``) is kept
       as-is, so ``evaluation`` retains ``validate_only=True``.
    3. ``BALDUR_EXECUTION_MODE`` env — the deployment-time posture.

    Returns:
        ``(mode, source)`` where source is one of ``"override"`` /
        ``"runtime_toggle"`` / ``"env"`` — which rung resolved the mode. Used by
        the would-have log so an operator can tell a console toggle from a
        deployment-posture env var.
    """
    if _override_mode is not None:
        return _override_mode, "override"

    env_mode = _get_mode_from_env()
    if env_mode.should_execute and _is_runtime_dry_run():
        return ExecutionMode.shadow(), "runtime_toggle"
    return env_mode, "env"


def get_execution_mode() -> ExecutionMode:
    """
    Get the current execution mode — the single observe-only source of truth.

    Resolves the deployment-time env posture and the runtime dry-run toggle
    through one function. Precedence: programmatic override > runtime dry-run
    toggle > ``BALDUR_EXECUTION_MODE`` env. The toggle is monotonic toward
    observe-only: it can force observe-only over an executing env posture, never
    the reverse.

    Returns:
        Current ExecutionMode configuration
    """
    return _resolve_mode()[0]


def intervention_suppressed(
    service_name: str,
    action: str,
    **would_have: Any,
) -> bool:
    """Guard predicate: is this automatic intervention suppressed (observe-only)?

    Returns ``True`` when the resolved execution mode is observe-only
    (``should_execute`` is ``False``). The caller MUST then skip its
    state-mutating side-effect, run its observe-only branch, and return the
    site-appropriate value — this is a guard predicate, not a control-flow
    router; the caller still owns the branch after it returns.

    On the suppressed path this emits both halves of the would-have contract in
    one place so every site logs identically:

    - the fixed-field decision record
      (``log_intervention_evaluated(allowed=False, POLICY_CONSTRAINT_ACTIVE)``),
      mirroring the action executor's log-only path; and
    - the per-site structured log carrying the suppressed action's identity and
      the ``mode_source`` field (the half the fixed-field record cannot express).

    Args:
        service_name: Affected service identifier (decision-record field).
        action: The suppressed intervention's identity (e.g. ``"retry"``,
            ``"dlq_store"``, ``"circuit_breaker_reject"``) for the would-have log.
        **would_have: Site-specific context describing what would have happened.

    Returns:
        ``True`` if the side-effect must be skipped (observe-only), ``False`` to
        proceed with the real intervention.
    """
    mode, source = _resolve_mode()
    if mode.should_execute:
        return False

    log_intervention_evaluated(
        service_name=service_name,
        allowed=False,
        reason=ReasonCode.POLICY_CONSTRAINT_ACTIVE,
    )
    logger.info(
        "execution_mode.intervention_suppressed",
        service_name=service_name,
        action=action,
        mode=mode.mode.value,
        mode_source=source,
        **would_have,
    )
    return True


__all__ = [
    "ExecutionModeType",
    "ExecutionMode",
    "get_execution_mode",
    "intervention_suppressed",
    "set_execution_mode",
    "clear_execution_mode_override",
]
