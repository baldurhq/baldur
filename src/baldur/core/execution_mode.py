"""
Execution Mode Configuration for Shadow/Evaluation Mode Support.

Provides centralized control over whether actions are executed or only logged.

Usage:
    from baldur.core.execution_mode import ExecutionMode, get_execution_mode

    mode = get_execution_mode()
    if mode.is_active:
        # Execute the real action
    else:
        # Log only

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

    # Execute real actions (production default)
    ACTIVE = "active"

    # Log decisions only, do not execute actions (observe mode)
    SHADOW = "shadow"

    # Log decisions + validation, do not execute actions (evaluation mode)
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


def _read_switches() -> tuple[bool, bool]:
    """Read System Control's ``(enabled, dry_run)`` from one copy read.

    The copy read itself never raises and does no store I/O. The import is
    deliberately kept inside the function body: ``system_control`` pulls in the
    audit pipeline and the state backend, which would form an import-time cycle
    if imported at module scope. A def-body import is excluded from the
    first-party import-time graph by construction. A failed import (early init)
    reads as enabled and live, the defaults every process starts from.
    """
    # def-body lazy import — excluded from the G40 import-cycle graph
    try:
        from baldur.services.system_control import get_system_control

        return get_system_control().switches()
    except Exception:
        return True, False


def _resolve_mode() -> tuple[ExecutionMode, str]:
    """Resolve the effective execution mode and the precedence rung that set it.

    Single observe-only resolver. Precedence:

    1. Kill switch (System Control) — while an operator has pulled it, every
       automatic intervention steps aside exactly where dry-run holds it back.
       Above the programmatic override: a code hook must not defeat the
       operator's brake.
    2. Programmatic override (``set_execution_mode``) — test / advanced hook,
       can force-execute over an on dry-run toggle.
    3. Runtime dry-run toggle (System Control) — monotonic toward observe-only:
       forces ``shadow`` only when the env mode would otherwise execute. An
       already-observe-only env posture (``shadow`` / ``evaluation``) is kept
       as-is, so ``evaluation`` retains ``validate_only=True``.
    4. ``BALDUR_EXECUTION_MODE`` env — the deployment-time posture.

    Returns:
        ``(mode, source)`` where source is one of ``"kill_switch"`` /
        ``"override"`` / ``"runtime_toggle"`` / ``"env"`` — which rung resolved
        the mode. Used by the would-have log so an operator can tell a console
        toggle from a deployment-posture env var.
    """
    enabled, dry_run = _read_switches()
    if not enabled:
        return ExecutionMode.shadow(), "kill_switch"

    if _override_mode is not None:
        return _override_mode, "override"

    env_mode = _get_mode_from_env()
    if env_mode.should_execute and dry_run:
        return ExecutionMode.shadow(), "runtime_toggle"
    return env_mode, "env"


def get_execution_mode() -> ExecutionMode:
    """
    Get the current execution mode — the single observe-only source of truth.

    Resolves the operator's kill switch, the deployment-time env posture and
    the runtime dry-run toggle through one function. Precedence: kill switch >
    programmatic override > runtime dry-run toggle > ``BALDUR_EXECUTION_MODE``
    env. While the kill switch is pulled this reports ``shadow``: Baldur's
    automatic interventions step aside everywhere dry-run holds them back. The
    toggle is monotonic toward observe-only: it can force observe-only over an
    executing env posture, never the reverse.

    Returns:
        Current ExecutionMode configuration
    """
    return _resolve_mode()[0]


def resolve_execution_mode() -> tuple[ExecutionMode, str]:
    """The current execution mode and the rung that set it.

    The source is one of ``"kill_switch"`` / ``"override"`` /
    ``"runtime_toggle"`` / ``"env"``.
    """
    return _resolve_mode()


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

    On a dry-run / shadow / evaluation suppression this emits both halves of the
    would-have contract in one place so every site logs identically:

    - the fixed-field decision record
      (``log_intervention_evaluated(allowed=False, POLICY_CONSTRAINT_ACTIVE)``),
      mirroring the action executor's log-only path; and
    - the per-site structured log carrying the suppressed action's identity and
      the ``mode_source`` field (the half the fixed-field record cannot express).

    A suppression by the kill switch emits neither: the would-have timeline is
    dry-run's product, and an operator who pulls the brake during an incident
    needs reach, not a per-call record at request rate. System Control logs
    once per process when its copy of the switch changes.

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
    if source == "kill_switch":
        return True

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
    "resolve_execution_mode",
    "set_execution_mode",
    "clear_execution_mode_override",
]
