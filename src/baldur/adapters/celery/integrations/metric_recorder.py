"""
Metric Recorder — record failure, success, and retry metrics.

Wraps lazy imports to baldur.services.metrics so the signal handler
layer never crashes due to missing optional dependencies.
"""

from __future__ import annotations

import structlog

from baldur.adapters.celery.signal_config import (
    SignalHooksSettings,
    extract_domain_from_task_name,
)

__all__ = ["MetricRecorder"]

logger = structlog.get_logger()


class MetricRecorder:
    """Record Celery task metrics for the baldur system."""

    def __init__(self, config: SignalHooksSettings) -> None:
        self._config = config

    def record_failure(self, domain: str, task_name: str, exception: Exception) -> None:
        """Record failure metrics."""
        try:
            from baldur.services.metrics.recorders import record_retry_attempt

            record_retry_attempt(
                domain=domain,
                attempt_count=1,
                outcome="failure",
            )
        except ImportError:
            pass
        except Exception as e:
            logger.debug(
                "baldur_metrics.record_failed",
                error=e,
            )

    def record_success(self, service_name: str, task_name: str) -> None:
        """Record success metrics."""
        try:
            from baldur.services.metrics.recorders import record_retry_attempt

            domain = extract_domain_from_task_name(task_name, self._config)
            record_retry_attempt(
                domain=domain,
                attempt_count=1,
                outcome="success",
            )
        except ImportError:
            pass
        except Exception as e:
            logger.debug(
                "baldur_metrics.record_failed",
                error=e,
            )

    def record_retry(self, domain: str, task_name: str) -> None:
        """Record retry attempt metrics."""
        try:
            from baldur.services.metrics.recorders import record_retry_attempt

            record_retry_attempt(
                domain=domain,
                attempt_count=1,
                outcome="retry",
            )
        except ImportError:
            pass
        except Exception as e:
            logger.debug(
                "baldur_metrics.record_failed",
                error=e,
            )
