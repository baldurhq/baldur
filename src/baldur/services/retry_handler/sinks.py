"""
Retry Policy Sinks — DLQ (Dead Letter Queue) final-failure handling.

Sink implementation that stores a final failure to the DLQ after all Policies
are exhausted.
should_dlq-flag-based Dumb Sink: RetryPolicy decides whether to store.
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.core.execution_mode import intervention_suppressed
from baldur.dlq.helpers import store_to_dlq
from baldur.interfaces.resilience_policy import PolicyContext, PolicyResult

logger = structlog.get_logger()


class DLQSink:
    """
    Sink that stores a final failure to the DLQ (Dead Letter Queue).

    Checks only the PolicyResult.metadata["should_dlq"] flag:
    stores if True, skips if False (Dumb Sink pattern).
    RetryPolicy marks the store decision via config.enable_dlq.

    Stateless — shared-singleton-safe. No ``__init__``, no instance
    attributes; every method either reads ``policy_result.metadata`` or
    delegates to the ``baldur.dlq.helpers.store_to_dlq`` helper.
    ``baldur.protect_facade`` reuses a single ``_DLQ_SINK`` module-level instance
    across all cached/slow-path composers (#499 D1). Adding instance state
    here would silently break that singleton — keep state at module scope
    or refactor the singleton accordingly.
    """

    def handle_failure(
        self,
        error: Exception,
        context: PolicyContext | None,
        policy_result: PolicyResult,
    ) -> str | None:
        """
        Store a final failure to the DLQ.

        Args:
            error: Final-failure exception
            context: PolicyContext (order_id, user_id, etc.)
            policy_result: Whole-pipeline result

        Returns:
            DLQ record ID string, or None (when not stored)
        """
        if not policy_result.metadata.get("should_dlq", False):
            return None

        # Observe-only (dry-run / shadow / evaluation): suppress the DLQ write,
        # log the would-store decision, and return None as if nothing stored.
        domain = policy_result.metadata.get("domain", "default")
        if intervention_suppressed(
            service_name=domain,
            action="dlq_store",
            error_type=type(error).__name__ if error else "Unknown",
        ):
            return None

        return self._store_to_dlq(error, context, policy_result)

    @staticmethod
    def _build_dlq_metadata(policy_result: PolicyResult) -> tuple[dict[str, Any], str]:
        """Build the metadata and domain for DLQ storage."""
        domain = policy_result.metadata.get("domain", "default")
        metadata: dict[str, Any] = {
            "retry_history": policy_result.metadata.get("retry_history", []),
            "max_attempts": policy_result.metadata.get("max_attempts"),
            "domain": domain,
            "final_attempt": policy_result.total_attempts,
            "executed_policies": policy_result.executed_policies,
            # Exhaustion cause (max_attempts / retry_budget / non_retryable /
            # max_elapsed / deadline) — passed through so DLQ triage can tell an
            # attempt-exhaustion apart from a budget/deadline break.
            "reason": policy_result.metadata.get("reason"),
        }
        return metadata, domain

    @staticmethod
    def _extract_context_fields(context: PolicyContext | None) -> dict[str, Any]:
        """Extract business identifiers and payload data from a PolicyContext.

        ``user_id`` precedence (#504 D10): the named ``PolicyContext.user_id``
        field wins when set; legacy direct callers that populate
        ``extra["user_id"]`` still work as a fallback. The named field is the
        contract documented at ``interfaces/resilience_policy.py``.
        """
        extra = context.extra if context and context.extra else {}
        if context is not None and context.user_id is not None:
            user_id_raw: Any = context.user_id
        else:
            user_id_raw = extra.get("user_id")
        return {
            "entity_id": context.order_id if context else None,
            "user_id": int(user_id_raw) if user_id_raw is not None else None,
            "snapshot_data": extra.get("snapshot_data", {}),
            "request_data": extra.get("request_data", {}),
            "response_data": extra.get("response_data", {}),
        }

    def _store_to_dlq(
        self,
        error: Exception,
        context: PolicyContext | None,
        policy_result: PolicyResult,
    ) -> str | None:
        """Call the DLQ service to store the failure."""
        try:
            error_type = type(error).__name__ if error else "Unknown"
            metadata, domain = self._build_dlq_metadata(policy_result)
            ctx_fields = self._extract_context_fields(context)

            result = store_to_dlq(
                domain=domain,
                failure_type=f"MAX_RETRIES_{error_type.upper()}",
                entity_id=ctx_fields["entity_id"],
                user_id=ctx_fields["user_id"],
                error_code=error_type,
                error_message=str(error)[:1000] if error else "",
                snapshot_data=ctx_fields["snapshot_data"],
                request_data=ctx_fields["request_data"],
                response_data=ctx_fields["response_data"],
                metadata=metadata,
                next_action_hint="Review error and retry if transient",
                recommended_action="manual_check",
            )

            if result is None:
                return None

            if result.success:
                logger.info(
                    "dlq_sink.created_dlq_entry",
                    result=result.dlq_id,
                )
                return str(result.dlq_id) if result.dlq_id is not None else None
            logger.error(
                "dlq_sink.create_dlq_entry_failed",
                result=result.error,
            )
            return None

        except Exception as dlq_error:
            logger.exception(
                "dlq_sink.create_dlq_entry_failed",
                dlq_error=dlq_error,
            )
            return None
