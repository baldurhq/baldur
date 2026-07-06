"""
Success Handler — record circuit breaker success and success metrics on task completion.
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.adapters.celery.integrations.cb_recorder import (
    CircuitBreakerRecorder,
)
from baldur.adapters.celery.integrations.metric_recorder import (
    MetricRecorder,
)
from baldur.adapters.celery.signal_config import (
    SignalHooksSettings,
    extract_service_name,
)

__all__ = ["SuccessHandler"]

logger = structlog.get_logger()


class SuccessHandler:
    """Handle Celery task success signals."""

    def __init__(self, config: SignalHooksSettings) -> None:
        self._config = config
        self._cb = CircuitBreakerRecorder()
        self._metrics = MetricRecorder(config)

    def handle(
        self,
        sender: Any = None,
        result: Any = None,
        **kw: Any,
    ) -> None:
        """
        Handle Celery task success signal.

        Records success in CB (for half-open -> closed transition) and updates metrics.
        """
        if not self._config.enabled:
            return

        task_name = sender.name if sender else "unknown"

        # Skip excluded tasks
        if task_name in self._config.excluded_tasks:
            return

        try:
            service_name = extract_service_name(task_name, self._config)

            # Circuit Breaker: Record success
            if self._config.cb_enabled:
                self._cb.record_success(service_name, task_name)

            # Metrics: Record success
            if self._config.metrics_enabled:
                self._metrics.record_success(service_name, task_name)

        except Exception as e:
            logger.exception(
                "baldur_signal.success_handler_error",
                error=e,
            )
