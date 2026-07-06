"""
Failure Handler — orchestrate circuit breaker, DLQ, metrics, and forensics on task failure.
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.adapters.celery.integrations.cb_recorder import (
    CircuitBreakerRecorder,
)
from baldur.adapters.celery.integrations.dlq_recorder import DLQRecorder
from baldur.adapters.celery.integrations.forensic_capture import (
    ForensicCapture,
)
from baldur.adapters.celery.integrations.metric_recorder import (
    MetricRecorder,
)
from baldur.adapters.celery.signal_config import (
    SignalHooksSettings,
    extract_domain_from_task_name,
    extract_service_name,
)
from baldur.core.execution_mode import intervention_suppressed

__all__ = ["FailureHandler"]

logger = structlog.get_logger()


class FailureHandler:
    """Handle Celery task failure signals."""

    def __init__(self, config: SignalHooksSettings) -> None:
        self._config = config
        self._cb = CircuitBreakerRecorder()
        self._dlq = DLQRecorder()
        self._metrics = MetricRecorder(config)
        self._forensics = ForensicCapture()

    # ------------------------------------------------------------------
    # Public API (matches Celery signal signature)
    # ------------------------------------------------------------------

    def handle(
        self,
        sender: Any = None,
        task_id: str | None = None,
        exception: Exception | None = None,
        args: tuple | None = None,
        kwargs: dict | None = None,
        traceback: Any = None,
        einfo: Any = None,
        **kw: Any,
    ) -> None:
        """
        Handle Celery task failure signal.

        Called when a task raises an exception and fails.
        Records failure in CB, stores to DLQ, captures forensics, updates metrics.
        """
        if not self._config.enabled:
            return

        task_name = sender.name if sender else "unknown"

        # Skip excluded tasks
        if task_name in self._config.excluded_tasks:
            return

        logger.info(
            "baldur_signal.task_failed",
            task_name=task_name,
            task_id=task_id,
            exception_type=type(exception).__name__ if exception else None,
            error=exception,
        )

        try:
            self._handle_internal(sender, task_id, exception, args, kwargs, einfo)
        except Exception as e:
            # Never let signal handler crash affect task execution
            logger.exception(
                "baldur_signal.failure_handler_error",
                error=e,
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _handle_internal(
        self,
        sender: Any,
        task_id: str | None,
        exception: Exception | None,
        args: tuple | None,
        kwargs: dict | None,
        einfo: Any,
    ) -> None:
        """Internal handler — separated for complexity reduction."""
        task_name = sender.name if sender else "unknown"
        domain = extract_domain_from_task_name(task_name, self._config)
        service_name = extract_service_name(task_name, self._config, exception)

        # Observe-only (dry-run / shadow / evaluation): suppress the two
        # state-mutating interventions (CB record + DLQ store). Metrics and
        # forensics (steps 3-4) are observation and still run.
        observe_only = intervention_suppressed(
            service_name=service_name,
            action="celery_failure_intervention",
            task_name=task_name,
            domain=domain,
        )

        # 1. Circuit Breaker: Record failure
        if self._config.cb_enabled and exception is not None and not observe_only:
            self._cb.record_failure(service_name, task_name, exception)

        # 2. DLQ: Store failed operation (if max retries exceeded)
        if (
            self._config.dlq_enabled
            and _should_store_to_dlq(sender)
            and not observe_only
        ):
            self._dlq.store(
                domain=domain,
                task_name=task_name,
                task_id=task_id or "",
                exception=exception or Exception("unknown"),
                args=args,
                kwargs=kwargs,
                einfo=einfo,
            )

        # 3. Metrics: Record failure
        if self._config.metrics_enabled and exception is not None:
            self._metrics.record_failure(domain, task_name, exception)

        # 4. Forensics: Capture context
        if self._config.forensics_enabled and exception is not None:
            self._forensics.capture(
                task_name=task_name,
                task_id=task_id or "",
                exception=exception,
                args=args,
                kwargs=kwargs,
                einfo=einfo,
            )


def _should_store_to_dlq(sender: Any) -> bool:
    """Determine if failed task should be stored to DLQ."""
    request = sender.request if sender else None
    retries = getattr(request, "retries", 0) if request else 0
    max_retries = getattr(sender, "max_retries", None) if sender else None

    # Store to DLQ if:
    # 1. max_retries is None or 0 (no retry configured)
    # 2. retries >= max_retries (all retries exhausted)
    return max_retries is None or max_retries == 0 or retries >= max_retries
