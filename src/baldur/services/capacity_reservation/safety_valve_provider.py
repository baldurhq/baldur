"""SafetyValveMetricsProvider implementation backed by existing OSS metrics.

The capacity-reservation safety valve needs a CPU-usage and an error-rate
signal to decide the hard-limit CRITICAL override. Rather than introduce a new
metrics collector, this provider reads two sources that already ship and run in
the OSS core:

- CPU from the background :class:`SystemMetricsCache` (psutil-backed, ~0ms
  lock-free read).
- Error rate from the system-wide aggregate circuit-breaker failure fraction.

This is the same source pair PRO's recovery gate uses for its live stability
check.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()

__all__ = ["SystemMetricsSafetyValveProvider"]


class SystemMetricsSafetyValveProvider:
    """Feed the capacity-reservation safety valve from live OSS metrics.

    :meth:`get_cpu_usage` **raises** when the metrics cache is not running or its
    sample is stale, so :meth:`PreWarmer.check_safety_valve` degrades to
    no-override on its except-branch — an unreadable CPU signal must never
    trigger a CRITICAL transition (fail-safe direction).
    """

    def get_cpu_usage(self) -> float:
        """Current CPU usage as a 0.0-1.0 fraction.

        Raises:
            RuntimeError: when the metrics cache is not running or its latest
                sample is stale (age beyond ``max_age_seconds``).
        """
        # In-function imports keep the source getters as monkeypatch seams.
        from baldur.services.system_metrics_cache import get_system_metrics_cache

        cache = get_system_metrics_cache()
        if not cache.is_running():
            raise RuntimeError("System metrics unavailable: cache not running")

        metrics = cache.get_metrics()
        if metrics.source == "stale":
            raise RuntimeError(
                "System metrics unavailable: sample stale (age > max_age_seconds)"
            )

        # cpu_percent is 0-100; the safety-valve threshold is a 0.0-1.0 fraction.
        return metrics.cpu_percent / 100.0

    def get_error_rate(self) -> float:
        """Current system-wide error rate as a 0.0-1.0 fraction."""
        from baldur.services.circuit_breaker import get_circuit_breaker_service

        return get_circuit_breaker_service().get_aggregate_failure_rate()
