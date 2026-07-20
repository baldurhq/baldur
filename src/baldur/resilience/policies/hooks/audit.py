"""
Audit Hook — record the pipeline execution result to the audit log.

Registered as a PolicyComposer Hook to audit-log the whole-pipeline result.
Events internal to an individual Policy are not observed (2-layer Hook
structure).

Fail-open principle: if the audit service fails to import or call, it is only
logged and business logic is unaffected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from baldur.interfaces.resilience_policy import PolicyResult

if TYPE_CHECKING:
    from baldur.interfaces.resilience_policy import PolicyContext

logger = structlog.get_logger()


class AuditHook:
    """Audit logging hook.

    Observes only the end-to-end pipeline result. Events internal to an
    individual Policy (per-attempt retry events, etc.) are handled by the Policy
    and are not propagated to this Hook.
    """

    def on_execute(
        self, policy_name: str, attempt: int, context: PolicyContext | None = None
    ) -> None:
        """Called when execution starts."""
        logger.debug(
            "policy_pipeline.execution_started",
            policy_name=policy_name,
            attempt=attempt,
        )

    def on_success(
        self,
        policy_name: str,
        result: PolicyResult,
        context: PolicyContext | None = None,
    ) -> None:
        """Called when the pipeline succeeds."""
        logger.info(
            "policy_pipeline.execution_succeeded",
            executed_policies=result.executed_policies,
            total_attempts=result.total_attempts,
            duration_ms=result.total_duration_ms,
        )

    def on_failure(
        self,
        policy_name: str,
        error: Exception,
        attempt: int,
        context: PolicyContext | None = None,
    ) -> None:
        """Called when the pipeline fails."""
        logger.warning(
            "policy_pipeline.execution_failed",
            policy_name=policy_name,
            error=str(error),
            total_attempts=attempt,
        )

    def on_retry(
        self,
        policy_name: str,
        attempt: int,
        delay: float,
        context: PolicyContext | None = None,
    ) -> None:
        """Called when a retry is scheduled."""
        logger.info(
            "policy_pipeline.retry_scheduled",
            policy_name=policy_name,
            attempt=attempt,
            delay_seconds=delay,
        )

    def on_reject(
        self, guard_name: str, reason: str, context: PolicyContext | None = None
    ) -> None:
        """Called when the pipeline is rejected."""
        logger.warning(
            "policy_pipeline.execution_rejected",
            guard_name=guard_name,
            reason=reason,
        )
