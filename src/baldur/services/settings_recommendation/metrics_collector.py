"""Cluster-wide metrics aggregation for Leader Pod.

Primary: Prometheus PromQL (avg/p99 across all pods)
Fallback: Local MetricsAdapter (single pod, degraded accuracy)

Mini Circuit Breaker pattern (same as CellHealthAggregator):
- N consecutive failures → Prometheus blocked
- retry_after_seconds later → half-open probe → success → recovered
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

__all__ = ["ClusterMetricsCollector"]

_URLLIB3_AVAILABLE = False
try:
    import urllib3

    _URLLIB3_AVAILABLE = True
except ImportError:
    pass


class MetricsAdapterProtocol(Protocol):
    """Local metrics adapter interface for fallback."""

    def fetch_current_metrics(self) -> dict[str, float]: ...


class ClusterMetricsCollector:
    """Cluster-wide metrics aggregation for Leader Pod.

    Primary: Prometheus PromQL (avg/p99 across all pods)
    Fallback: Local MetricsAdapter (single pod, degraded accuracy)

    Mini Circuit Breaker:
    - max_consecutive_failures consecutive failures → Prometheus blocked
    - retry_after_seconds later → half-open probe → success → recovered
    """

    PROMQL_QUERIES: dict[str, str] = {
        "error_rate": "avg(baldur_error_rate)",
        "p99_latency_ms": (
            "histogram_quantile(0.99, "
            "sum(rate(baldur_request_duration_seconds_bucket[5m])) by (le)"
            ") * 1000"
        ),
        "retry_exhaustion_rate": "avg(baldur_retry_exhaustion_rate)",
        "throttle_rate": "avg(baldur_throttle_rate)",
        "cb_open_ratio": "avg(baldur_cb_open_ratio)",
    }

    REQUIRED_METRIC_KEYS: frozenset[str] = frozenset(PROMQL_QUERIES.keys())

    def __init__(
        self,
        prometheus_url: str | None = None,
        fallback_adapter: Any | None = None,
        timeout_seconds: float = 3.0,
        max_consecutive_failures: int = 3,
        retry_after_seconds: float = 60.0,
    ) -> None:
        self._prometheus_url = prometheus_url
        self._fallback_adapter = fallback_adapter
        self._timeout = timeout_seconds
        self._max_failures = max_consecutive_failures
        self._retry_after = retry_after_seconds

        # Mini circuit breaker state
        self._consecutive_failures = 0
        self._circuit_open_since: float | None = None

    def collect(self) -> dict[str, float]:
        """Collect aggregated metrics across all pods.

        Returns dict with all REQUIRED_METRIC_KEYS; missing keys filled with NaN.
        """
        raw: dict[str, float] = {}

        if self._should_use_prometheus():
            try:
                raw = self._fetch_prometheus_metrics()
                self._consecutive_failures = 0
                self._circuit_open_since = None
            except Exception:
                self._record_failure()
                logger.debug("metrics_collector.prometheus_failed", exc_info=True)

        # Fallback: local metrics (single pod, degraded accuracy)
        if not raw and self._fallback_adapter:
            try:
                raw = self._fallback_adapter.fetch_current_metrics()
            except Exception:
                logger.debug("metrics_collector.fallback_failed", exc_info=True)

        # Normalize: missing keys → NaN
        return {k: raw.get(k, float("nan")) for k in self.REQUIRED_METRIC_KEYS}

    def _should_use_prometheus(self) -> bool:
        """Check if Prometheus is available (mini circuit breaker)."""
        if not self._prometheus_url:
            return False
        if self._circuit_open_since is not None:
            elapsed = time.monotonic() - self._circuit_open_since
            if elapsed < self._retry_after:
                return False
            # Half-open: try once
        return True

    def _record_failure(self) -> None:
        """Record Prometheus failure and open circuit if threshold exceeded."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._max_failures:
            self._circuit_open_since = time.monotonic()
            logger.warning(
                "metrics_collector.prometheus_blocked",
                extra={
                    "failures": self._consecutive_failures,
                    "retry_after": self._retry_after,
                },
            )

    def _fetch_prometheus_metrics(self) -> dict[str, float]:
        """Fetch metrics from Prometheus HTTP API."""
        if not _URLLIB3_AVAILABLE or not self._prometheus_url:
            return {}

        http = urllib3.PoolManager(timeout=self._timeout)
        result: dict[str, float] = {}

        for metric_name, query in self.PROMQL_QUERIES.items():
            try:
                url = f"{self._prometheus_url}/api/v1/query"
                resp = http.request("GET", url, fields={"query": query})
                if resp.status == 200:
                    import json

                    data = json.loads(resp.data.decode("utf-8"))
                    results = data.get("data", {}).get("result", [])
                    if results:
                        value = float(results[0]["value"][1])
                        result[metric_name] = value
            except Exception:
                logger.debug(
                    "metrics_collector.query_failed",
                    extra={"metric": metric_name},
                )

        return result
