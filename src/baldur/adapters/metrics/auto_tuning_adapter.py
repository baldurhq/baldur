"""
Auto Tuning Metrics Adapters - metric adapters for autonomous tuning.

Metric collection adapters used by RuntimeFeedbackLoop.

Provided adapters:
- InternalMetricsAdapter: internal metrics backed by DB/cache
- PrometheusMetricsAdapter: Prometheus integration
- MockMetricsAdapter: for testing
"""

from __future__ import annotations

from typing import Protocol

import structlog

logger = structlog.get_logger()


class AutoTuningMetricsAdapter(Protocol):
    """Metric adapter protocol for autonomous tuning."""

    def fetch_current_metrics(self) -> dict[str, float]:
        """
        Collect current metrics.

        Returns:
            Metrics dictionary:
            - error_rate: error rate (0.0 ~ 1.0)
            - p99_latency_ms: P99 latency (ms)
            - retry_exhausted_rate: retry exhaustion rate
            - retry_collision_rate: retry collision rate
            - throttle_rate: throttling ratio
            - throughput_rps: throughput (requests/sec)
            - sample_count: number of samples
        """
        ...


class InternalMetricsAdapter:
    """
    Internal metrics adapter.

    Collects metrics from a DB or cache.
    Can operate without depending on any external system.
    """

    def __init__(
        self,
        cache_provider=None,
        db_provider=None,
        metrics_prefix: str = "baldur",
    ):
        """
        Args:
            cache_provider: cache provider such as Redis
            db_provider: DB access provider
            metrics_prefix: metric key prefix
        """
        self.cache_provider = cache_provider
        self.db_provider = db_provider
        self.metrics_prefix = metrics_prefix

        # Internal metric store (used when no cache is available)
        self._internal_metrics: dict[str, float] = {}
        self._sample_counts: dict[str, int] = {}

    def fetch_current_metrics(self) -> dict[str, float]:
        """Collect current metrics."""
        metrics = {
            "error_rate": self._get_error_rate(),
            "p99_latency_ms": self._get_p99_latency(),
            "retry_exhausted_rate": self._get_retry_exhausted_rate(),
            "retry_collision_rate": self._get_retry_collision_rate(),
            "throttle_rate": self._get_throttle_rate(),
            "throughput_rps": self._get_throughput(),
            "sample_count": self._get_sample_count(),
        }

        logger.debug(
            "internal_metrics.fetched",
            metrics=metrics,
        )
        return metrics

    def record_metric(self, name: str, value: float):
        """Record a metric (called externally)."""
        self._internal_metrics[name] = value
        self._sample_counts[name] = self._sample_counts.get(name, 0) + 1

        if self.cache_provider:
            try:
                key = f"{self.metrics_prefix}:{name}"
                self.cache_provider.set(key, value)
            except Exception as e:
                logger.debug(
                    "internal_metrics.cache_set_failed",
                    error=e,
                )

    def _get_metric(self, name: str, default: float = 0.0) -> float:
        """Look up a metric value."""
        # Try the cache first
        if self.cache_provider:
            try:
                key = f"{self.metrics_prefix}:{name}"
                value = self.cache_provider.get(key)
                if value is not None:
                    return float(value)
            except Exception:
                pass

        # Fall back to the internal store
        return self._internal_metrics.get(name, default)

    def _get_error_rate(self) -> float:
        return self._get_metric("error_rate", 0.01)

    def _get_p99_latency(self) -> float:
        return self._get_metric("p99_latency_ms", 200.0)

    def _get_retry_exhausted_rate(self) -> float:
        return self._get_metric("retry_exhausted_rate", 0.02)

    def _get_retry_collision_rate(self) -> float:
        return self._get_metric("retry_collision_rate", 0.01)

    def _get_throttle_rate(self) -> float:
        return self._get_metric("throttle_rate", 0.005)

    def _get_throughput(self) -> float:
        return self._get_metric("throughput_rps", 100.0)

    def _get_sample_count(self) -> int:
        return sum(self._sample_counts.values()) or 10


class PrometheusMetricsAdapter:
    """
    Prometheus metrics adapter.

    Queries metrics from Prometheus for use in autonomous tuning.
    """

    def __init__(
        self,
        prometheus_url: str = "http://localhost:9090",
        timeout_seconds: int = 5,
        job_name: str = "baldur",
    ):
        """
        Args:
            prometheus_url: Prometheus server URL
            timeout_seconds: request timeout
            job_name: metric job label
        """
        self.prometheus_url = prometheus_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.job_name = job_name

    def fetch_current_metrics(self) -> dict[str, float]:
        """Query metrics from Prometheus."""
        metrics = {}

        # Error rate query
        metrics["error_rate"] = self._query_metric(
            f'sum(rate(http_requests_total{{job="{self.job_name}",status=~"5.."}}[5m])) / '
            f'sum(rate(http_requests_total{{job="{self.job_name}"}}[5m]))',
            default=0.01,
        )

        # P99 latency query
        metrics["p99_latency_ms"] = self._query_metric(
            f'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{{job="{self.job_name}"}}[5m])) by (le)) * 1000',
            default=200.0,
        )

        # Retry exhaustion rate
        metrics["retry_exhausted_rate"] = self._query_metric(
            f'sum(rate(retry_exhausted_total{{job="{self.job_name}"}}[5m])) / '
            f'sum(rate(retry_attempts_total{{job="{self.job_name}"}}[5m]))',
            default=0.02,
        )

        # Retry collision rate
        metrics["retry_collision_rate"] = self._query_metric(
            f'sum(rate(retry_collision_total{{job="{self.job_name}"}}[5m])) / '
            f'sum(rate(retry_attempts_total{{job="{self.job_name}"}}[5m]))',
            default=0.01,
        )

        # Throttling ratio
        metrics["throttle_rate"] = self._query_metric(
            f'sum(rate(rate_limited_total{{job="{self.job_name}"}}[5m])) / '
            f'sum(rate(http_requests_total{{job="{self.job_name}"}}[5m]))',
            default=0.005,
        )

        # Throughput
        metrics["throughput_rps"] = self._query_metric(
            f'sum(rate(http_requests_total{{job="{self.job_name}"}}[5m]))',
            default=100.0,
        )

        # Sample count
        metrics["sample_count"] = self._query_metric(
            f'sum(http_requests_total{{job="{self.job_name}"}})', default=1000
        )

        logger.debug(
            "prometheus_metrics.fetched",
            metrics=metrics,
        )
        return metrics

    def _query_metric(self, query: str, default: float = 0.0) -> float:
        """Execute a Prometheus query."""
        try:
            import urllib.parse
            import urllib.request

            from baldur.utils.http import safe_urlopen
            from baldur.utils.serialization import fast_loads

            url = f"{self.prometheus_url}/api/v1/query"
            params = urllib.parse.urlencode({"query": query})
            full_url = f"{url}?{params}"

            req = urllib.request.Request(full_url)
            with safe_urlopen(req, timeout=self.timeout_seconds) as response:
                data = fast_loads(response.read())

            if data.get("status") == "success":
                result = data.get("data", {}).get("result", [])
                if result:
                    value = result[0].get("value", [None, None])[1]
                    if value is not None and value != "NaN":
                        return float(value)

            return default
        except Exception as e:
            logger.debug(
                "prometheus_metrics.query_failed",
                error=e,
            )
            return default


class MockMetricsAdapter:
    """
    Mock metrics adapter (for testing).

    Lets tests set metric values directly.
    """

    def __init__(self, initial_metrics: dict[str, float] | None = None):
        self.metrics = initial_metrics or {
            "error_rate": 0.02,
            "p99_latency_ms": 150.0,
            "retry_exhausted_rate": 0.03,
            "retry_collision_rate": 0.01,
            "throttle_rate": 0.005,
            "throughput_rps": 500.0,
            "sample_count": 1000,
        }

    def fetch_current_metrics(self) -> dict[str, float]:
        """Return the mock metrics."""
        return self.metrics.copy()

    def set_metrics(self, metrics: dict[str, float]):
        """Set metrics."""
        self.metrics.update(metrics)

    def set_metric(self, name: str, value: float):
        """Set a single metric."""
        self.metrics[name] = value

    def simulate_degradation(self, level: str = "minor"):
        """Simulate a degradation scenario."""
        if level == "minor":
            self.metrics["error_rate"] = 0.06
            self.metrics["p99_latency_ms"] = 3500
        elif level == "major":
            self.metrics["error_rate"] = 0.15
            self.metrics["p99_latency_ms"] = 6000
        elif level == "critical":
            self.metrics["error_rate"] = 0.35
            self.metrics["p99_latency_ms"] = 12000

    def reset(self):
        """Reset to default values."""
        self.metrics = {
            "error_rate": 0.02,
            "p99_latency_ms": 150.0,
            "retry_exhausted_rate": 0.03,
            "retry_collision_rate": 0.01,
            "throttle_rate": 0.005,
            "throughput_rps": 500.0,
            "sample_count": 1000,
        }


__all__ = [
    "AutoTuningMetricsAdapter",
    "InternalMetricsAdapter",
    "PrometheusMetricsAdapter",
    "MockMetricsAdapter",
]
