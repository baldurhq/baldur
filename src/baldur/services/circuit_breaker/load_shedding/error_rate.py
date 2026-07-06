"""
Error Rate Provider for Load Shedding.

Interface that tracks and provides per-service error rates.
The default implementation is memory-based; in real environments it integrates
with a metrics system.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()


class ErrorRateProvider:
    """
    Interface that provides per-service error rates.

    The default implementation is memory-based. In real environments it
    integrates with a metrics system.
    """

    def __init__(self):
        self._error_rates: dict[str, float] = {}
        self._success_counts: dict[str, int] = {}
        self._failure_counts: dict[str, int] = {}

    def get_error_rate(self, service_id: str) -> float:
        """
        Look up the service's current error rate (0~100%).

        Args:
            service_id: Service ID

        Returns:
            Error rate (0~100)
        """
        return self._error_rates.get(service_id, 0.0)

    def set_error_rate(self, service_id: str, error_rate: float) -> None:
        """
        Set the service's error rate (for tests).

        Args:
            service_id: Service ID
            error_rate: Error rate (0~100)
        """
        if not (0.0 <= error_rate <= 100.0):
            raise ValueError(f"error_rate must be between 0 and 100, got {error_rate}")
        self._error_rates[service_id] = error_rate

    def record_success(self, service_id: str) -> None:
        """Record a success."""
        self._success_counts[service_id] = self._success_counts.get(service_id, 0) + 1
        self._update_error_rate(service_id)

    def record_failure(self, service_id: str) -> None:
        """Record a failure."""
        self._failure_counts[service_id] = self._failure_counts.get(service_id, 0) + 1
        self._update_error_rate(service_id)

    def _update_error_rate(self, service_id: str) -> None:
        """Recompute the error rate."""
        success = self._success_counts.get(service_id, 0)
        failure = self._failure_counts.get(service_id, 0)
        total = success + failure
        if total > 0:
            self._error_rates[service_id] = (failure / total) * 100.0

    def reset(self, service_id: str | None = None) -> None:
        """Reset error rates."""
        if service_id:
            self._error_rates.pop(service_id, None)
            self._success_counts.pop(service_id, None)
            self._failure_counts.pop(service_id, None)
        else:
            self._error_rates.clear()
            self._success_counts.clear()
            self._failure_counts.clear()
