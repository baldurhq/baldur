"""
DLQ Replay Execution Mixin.

Provides the single-entry replay-execution primitive (``_execute_replay``)
and the replay-exhausted metric emission (``_emit_replay_exhausted``) shared by
every replay caller: the OSS single-entry ``retry_entry`` / ``force_redrive_entry``
and the PRO batch / throttle-aware replay overlays (which reach these via MRO).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from baldur.audit.trace import extract_origin_trace

if TYPE_CHECKING:
    from baldur.interfaces.repositories import FailedOperationData

logger = structlog.get_logger()

# Existing ``replay_type`` label vocabulary ("single"/"conditional"/"batch") —
# the operator surface replays one entry per call, so no new label value enters
# the published attempts family.
REPLAY_TYPE_SINGLE = "single"

__all__ = ["REPLAY_TYPE_SINGLE", "ReplayExecutionMixin"]


class ReplayExecutionMixin:
    """Mixin providing the shared single-entry replay-execution primitive."""

    def _execute_replay(self, entry: FailedOperationData) -> bool:
        """
        Execute replay for a single DLQ entry using registered handler.

        This is the convergence point of the whole operator replay surface
        (single-entry retry, force-redrive, batch and throttle-aware replay), so
        it is where those replays enter the replay attempt/outcome metrics — the
        replay service records its own stack separately.

        Args:
            entry: The failed operation entry to replay

        Returns:
            True if replay succeeded, False otherwise
        """
        import time

        start = time.monotonic()
        # Whether the registered handler was actually invoked. A gate refusal
        # costs microseconds; observing it in the replay-duration histogram
        # would mix non-events with second-scale replays and drag the reported
        # quantile toward zero. A refusal that is nonetheless slow — one gate is
        # customer code and may do I/O — stays visible in the timing carried on
        # its own blocked WARNING.
        handler_ran = False
        try:
            from baldur.metrics.event_handlers import ReplayEventHandler
            from baldur.observability import span_with_link
            from baldur.services.replay_service import get_replay_handler
            from baldur.services.replay_service.handlers import _truncate_gate

            # #502 D7: framework-side gate — runs before customer can_replay
            # so handlers stay clean of truncation logic.
            gate_allowed, gate_reason = _truncate_gate(entry)
            if not gate_allowed:
                logger.warning(
                    "dlq.replay_blocked",
                    dlq_entry_id=entry.id,
                    reason=gate_reason,
                    duration_ms=(time.monotonic() - start) * 1000,
                )
                return False

            handler = get_replay_handler(entry.domain)

            # Check if replay is allowed
            can_replay, reason = handler.can_replay(entry)
            if not can_replay:
                logger.warning(
                    "dlq.replay_blocked",
                    dlq_entry_id=entry.id,
                    reason=reason,
                    duration_ms=(time.monotonic() - start) * 1000,
                )
                return False

            # 679 D5: centralized origin-trace span link — this is the single
            # point every replay caller converges on (replay / retry_entry /
            # force_redrive / throttle-aware). No-op when OTEL is off or the
            # origin full ids are absent, so unlinked entries create no span.
            origin = extract_origin_trace(entry.metadata)

            # Both events are emitted, never one alone: outcomes without their
            # attempt would push an operator's success-rate panel above 1.
            # Gate-blocked exits above emit neither, so attempts never count an
            # entry whose handler was not reached.
            ReplayEventHandler.on_replay_started(entry.domain, REPLAY_TYPE_SINGLE)
            replay_start = time.monotonic()
            succeeded = False

            # Execute replay. One completion site, reached by the handler's
            # return AND by its crash, so the two counters cannot come apart.
            try:
                with span_with_link(
                    "dlq.replay",
                    origin["origin_trace_id_full"],
                    origin["origin_span_id"],
                    attributes={
                        "baldur.dlq.id": str(entry.id),
                        "baldur.dlq.origin_trace_id": origin["origin_trace_id"] or "",
                    },
                ):
                    handler_ran = True
                    result = handler.replay(entry)
                succeeded = result.success
            finally:
                ReplayEventHandler.on_replay_completed(
                    entry.domain, succeeded, time.monotonic() - replay_start
                )
            return succeeded
        finally:
            if handler_ran:
                duration = time.monotonic() - start
                try:
                    from baldur.metrics.prometheus import get_metrics

                    metrics = get_metrics()
                    if metrics and hasattr(metrics, "dlq"):
                        metrics.dlq.record_replay_duration(entry.domain, duration)
                except Exception:
                    pass

    def _emit_replay_exhausted(self, entry: FailedOperationData) -> None:
        """Emit the replay-exhausted metric when a replay reached the cap.

        Called from the operator replay failure branches with the acquired
        entry (whose retry_count was already incremented by
        ``try_acquire_for_replay``). Emits only when the just-completed
        attempt was the terminal one — the same condition ``complete_replay``
        uses to set REQUIRES_REVIEW. Fail-open: a metrics error never affects
        the replay outcome.
        """
        if entry.retry_count < entry.max_retries:
            return
        try:
            from baldur.metrics.prometheus import get_metrics

            metrics = get_metrics()
            if metrics and hasattr(metrics, "dlq"):
                metrics.dlq.record_replay_exhausted(entry.domain)
                # D7: a force-redriven entry (operator-asserted fix) that still
                # re-converges to REQUIRES_REVIEW is strictly more severe than
                # ordinary exhaustion — emit the escalated signal + WARNING.
                metadata = entry.metadata or {}
                if metadata.get("force_redrive_count", 0) > 0:
                    metrics.dlq.record_force_redrive_exhausted(entry.domain)
                    logger.warning(
                        "dlq.force_redrive_exhausted",
                        record_pk=entry.id,
                        entry_domain=entry.domain,
                        force_redrive_count=metadata.get("force_redrive_count"),
                    )
        except Exception:
            pass
