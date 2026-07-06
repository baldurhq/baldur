"""
Retry Handler — record retry metrics on task retry.
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.adapters.celery.integrations.metric_recorder import (
    MetricRecorder,
)
from baldur.adapters.celery.signal_config import (
    SignalHooksSettings,
    extract_domain_from_task_name,
)

__all__ = ["RetryHandler"]

logger = structlog.get_logger()


class RetryHandler:
    """Handle Celery task retry signals."""

    def __init__(self, config: SignalHooksSettings) -> None:
        self._config = config
        self._metrics = MetricRecorder(config)

    def handle(
        self,
        sender: Any = None,
        reason: Any = None,
        einfo: Any = None,
        **kw: Any,
    ) -> None:
        """
        Handle Celery task retry signal.

        Tracks retry attempts for metrics.
        """
        if not self._config.enabled or not self._config.metrics_enabled:
            return

        task_name = sender.name if sender else "unknown"

        if task_name in self._config.excluded_tasks:
            return

        try:
            domain = extract_domain_from_task_name(task_name, self._config)
            self._metrics.record_retry(domain, task_name)
        except Exception as e:
            logger.exception(
                "baldur_signal.retry_handler_error",
                error=e,
            )
