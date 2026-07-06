"""
Circuit Breaker Recorder — record task failures/successes for the CB.

Wraps lazy imports to baldur.services.circuit_breaker so the signal
handler layer never crashes due to missing optional dependencies.
"""

from __future__ import annotations

import structlog

__all__ = ["CircuitBreakerRecorder"]

logger = structlog.get_logger()


class CircuitBreakerRecorder:
    """Record circuit breaker state transitions for Celery task signals."""

    def record_failure(
        self, service_name: str, task_name: str, exception: Exception
    ) -> None:
        """Record a task failure in the circuit breaker."""
        try:
            from baldur.services.circuit_breaker.convenience import (
                get_circuit_breaker_service,
            )

            cb_service = get_circuit_breaker_service()
            cb_service.record_failure(service_name=service_name)

            logger.debug(
                "baldur_cb.failure_recorded",
                service_name=service_name,
            )

        except ImportError as e:
            logger.debug(
                "baldur_cb.service_unavailable",
                error=e,
            )
        except Exception as e:
            logger.exception(
                "baldur_cb.record_failed",
                error=e,
            )

    def record_success(self, service_name: str, task_name: str) -> None:
        """Record a task success in the circuit breaker."""
        try:
            from baldur.services.circuit_breaker.convenience import (
                get_circuit_breaker_service,
            )

            cb_service = get_circuit_breaker_service()
            cb_service.record_success(service_name=service_name)

            logger.debug(
                "baldur_cb.success_recorded",
                service_name=service_name,
            )

        except ImportError as e:
            logger.debug(
                "baldur_cb.service_unavailable",
                error=e,
            )
        except Exception as e:
            logger.exception(
                "baldur_cb.record_failed",
                error=e,
            )
