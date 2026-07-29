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

    def record_failure(
        self,
        domain: str,
        task_name: str,
        exception: Exception,
        attempt_count: int = 1,
    ) -> None:
        """Record failure metrics.

        Args:
            domain: Business domain the task belongs to
            task_name: Celery task name
            exception: The exception that terminated the task
            attempt_count: Attempts this task took to reach its terminal
                failure (``request.retries + 1``); 1 when unknown
        """
        try:
            from baldur.metrics.registry import register_domain
            from baldur.services.metrics.recorders import record_task_attempt

            # Register where the value is consumed as a metric domain — not
            # inside ``extract_domain_from_task_name``, which is reused as a
            # circuit-breaker service-name fallback. Registering inside the
            # recorder method covers every caller of it.
            register_domain(domain)
            record_task_attempt(
                domain=domain,
                attempt_count=attempt_count,
                outcome="failure",
            )
        except ImportError:
            pass
        except Exception as e:
            logger.debug(
                "baldur_metrics.record_failed",
                error=e,
            )

    def record_success(
        self, service_name: str, task_name: str, attempt_count: int = 1
    ) -> None:
        """Record success metrics.

        Args:
            service_name: Resolved service name (unused by the metric itself)
            task_name: Celery task name the domain is derived from
            attempt_count: Attempts this task took to succeed
                (``request.retries + 1``); 1 when unknown
        """
        try:
            from baldur.metrics.registry import register_domain
            from baldur.services.metrics.recorders import record_task_attempt

            domain = extract_domain_from_task_name(task_name, self._config)
            register_domain(domain)
            record_task_attempt(
                domain=domain,
                attempt_count=attempt_count,
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
        """Record a task retry signal.

        A retry signal is non-terminal, so it goes to its own counter — never
        to the terminal-outcome counter, and never as an observation into the
        attempts histogram (whose contract is attempts-before-resolution).
        """
        try:
            from baldur.metrics.registry import register_domain
            from baldur.services.metrics.recorders import record_retry_marker

            register_domain(domain)
            record_retry_marker(domain=domain)
        except ImportError:
            pass
        except Exception as e:
            logger.debug(
                "baldur_metrics.record_failed",
                error=e,
            )
