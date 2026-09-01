"""
Retry Policy Sinks — DLQ (Dead Letter Queue) terminal-failure handling.

Sink implementation that stores a terminal failure to the DLQ. Two terminals
reach it, each with its own store shape:

- retry exhaustion — the call ran and kept failing; RetryPolicy decides whether
  to store via the ``should_dlq`` flag (Dumb Sink pattern).
- open-circuit rejection — the call never ran because its breaker was OPEN.
  The composer delivers it only when armed for open-circuit capture, and this
  sink gates it on ``DLQSettings.open_circuit_capture_enabled``.
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.core.execution_mode import intervention_suppressed
from baldur.dlq.helpers import store_to_dlq
from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
)
from baldur.models.dlq import OPEN_CIRCUIT_FAILURE_TYPE, POLICY_CHAIN_CAPTURE_SOURCE

logger = structlog.get_logger()


class DLQSink:
    """
    Sink that stores a terminal failure to the DLQ (Dead Letter Queue).

    On the retry-exhaustion terminal it checks only the
    PolicyResult.metadata["should_dlq"] flag: stores if True, skips if False
    (Dumb Sink pattern). RetryPolicy marks the store decision via
    config.enable_dlq.

    On the open-circuit rejection terminal there is no such flag — the call
    never ran — so the store is gated on
    ``DLQSettings.open_circuit_capture_enabled`` instead, and only reaches this
    sink at all on a composer armed for open-circuit capture.

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
        Store a terminal failure to the DLQ.

        Args:
            error: Terminal exception — a retry-exhausted failure, or the
                ``CircuitBreakerOpenError`` of a rejected call
            context: PolicyContext (order_id, user_id, etc.)
            policy_result: Whole-pipeline result

        Returns:
            DLQ record ID string, or None (when not stored)
        """
        if policy_result.outcome == PolicyOutcome.REJECTED:
            return self._handle_open_circuit_rejection(error, context, policy_result)

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

    def _handle_open_circuit_rejection(
        self,
        error: Exception,
        context: PolicyContext | None,
        policy_result: PolicyResult,
    ) -> str | None:
        """Park a call an OPEN circuit rejected, so recovery can replay it.

        The rejected call never ran, so there is no retry history and no
        ``should_dlq`` verdict to consult — the chain metadata carries only the
        rejecting breaker's own keys. The stored domain is that breaker's name;
        the on-recovery sweep joins on it, which is why the entry is stamped
        with the policy-chain source: an entry stored under a path-inferred
        domain names a different circuit and must not be swept on this one.

        Fails open end to end. A settings read that raises skips the capture,
        never the rejection, and a store that fails is logged and swallowed —
        the caller still receives the original ``CircuitBreakerOpenError``.
        """
        from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError

        if not isinstance(error, CircuitBreakerOpenError):
            # Bulkhead-full and guard vetoes are rejections too; which of those
            # represent parkable work is a separate decision.
            return None

        try:
            from baldur.settings.dlq import get_dlq_settings

            if not get_dlq_settings().open_circuit_capture_enabled:
                return None
        except Exception as settings_error:
            logger.debug(
                "dlq_sink.open_circuit_capture_skipped",
                error=str(settings_error),
            )
            return None

        service_name = str(
            policy_result.metadata.get("service_name") or error.service_name
        )

        if intervention_suppressed(
            service_name=service_name,
            action="dlq_store",
            error_type=type(error).__name__,
        ):
            return None

        ctx_fields = self._extract_context_fields(context)
        try:
            result = store_to_dlq(
                domain=service_name,
                failure_type=OPEN_CIRCUIT_FAILURE_TYPE,
                entity_id=ctx_fields["entity_id"],
                user_id=ctx_fields["user_id"],
                error_code=type(error).__name__,
                error_message=str(error)[:1000],
                snapshot_data=ctx_fields["snapshot_data"],
                request_data=ctx_fields["request_data"],
                response_data=ctx_fields["response_data"],
                metadata={
                    "source": POLICY_CHAIN_CAPTURE_SOURCE,
                    "service_name": service_name,
                    "circuit_state": str(policy_result.metadata.get("state", "")),
                    "executed_policies": policy_result.executed_policies,
                },
                next_action_hint="Replayed automatically when the circuit closes",
                recommended_action="replay",
            )
        except Exception as dlq_error:
            logger.exception(
                "dlq_sink.create_dlq_entry_failed",
                dlq_error=dlq_error,
            )
            return None

        if not result.success:
            logger.error(
                "dlq_sink.create_dlq_entry_failed",
                result=result.error,
            )
            return None

        dlq_id = str(result.dlq_id) if result.dlq_id is not None else None
        # Mark the exception instance, not the id: the async outbox acks before
        # an id exists, so a later capture layer testing id-truthiness would
        # store the same rejection a second time.
        error.mark_dlq_capture_dispatched(dlq_id)
        logger.info(
            "dlq_sink.open_circuit_entry_created",
            healing_domain=service_name,
            result=dlq_id,
        )
        return dlq_id

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
