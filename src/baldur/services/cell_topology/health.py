"""
Cell Health Aggregator — per-cell health aggregation.

Uses the Prometheus API as the global SSOT and collects
BulkheadRegistry / CB state callbacks to compute per-cell health.
Only a single leader in the cluster performs aggregation via
LeaderScheduler.

Dependencies:
- CellRegistry: health-update target (registry.py update_health_score)
- BulkheadRegistry: bulkhead utilization lookup (bulkhead/registry.py get_all_states)
- LeaderScheduler: single-leader execution guarantee (coordination/scheduler.py)
- EWMAForecaster: fallback error rate + score smoothing (time_series.py)
- prometheus_client: metric exposure (optional)
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import structlog

from baldur.utils.http import safe_urlopen

if TYPE_CHECKING:
    from baldur.core.time_series import (
        EWMAForecaster,
    )

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Prometheus API configuration — defaults from CellTopologySettings (339)
# ---------------------------------------------------------------------------


@dataclass
class CellHealthSnapshot:
    """Snapshot of a cell's health state."""

    cell_id: str
    health_score: float  # final EWMA-smoothed score
    raw_health_score: float = 0.0  # pre-smoothing raw score (debugging / audit)
    error_rate: float = 0.0
    latency_p99: float = 0.0
    bulkhead_utilization: float = 0.0
    cb_open_ratio: float = 0.0
    source: str = "prometheus"  # "prometheus" | "ewma_fallback"
    timestamp: float = field(default_factory=time.time)


class CellHealthAggregator:
    """
    Per-cell health aggregation.

    Only the single leader worker in the cluster runs aggregation via
    LeaderScheduler. Every worker's record_request() writes to Prometheus
    Counter/Histogram, and the leader's aggregate_all() queries globally
    summed metrics from the Prometheus API.

    Usage (LeaderScheduler decorator)::

        from baldur.coordination.scheduler import get_leader_scheduler

        scheduler = get_leader_scheduler("cell-health-aggregator")

        @scheduler.job(interval_seconds=10)
        def cell_health_aggregation():
            aggregator = get_cell_health_aggregator()
            aggregator.aggregate_all()

    Usage (manual)::

        aggregator = CellHealthAggregator()
        aggregator.record_request(cell_id, success=True, latency=0.05)
        snapshot = aggregator.get_snapshot("cell-3")
    """

    def __init__(
        self,
        settings: Any = None,
        prometheus_url: str | None = None,
    ):
        from baldur.settings.cell_topology import get_cell_topology_settings

        self._settings = settings or get_cell_topology_settings()
        self._lock = threading.RLock()
        self._snapshots: dict[str, CellHealthSnapshot] = {}

        # Prometheus API endpoint (precedence: settings -> ctor arg -> default)
        self._prometheus_url = (
            prometheus_url
            or getattr(self._settings, "prometheus_url", None)
            or "http://localhost:9090"
        )
        self._prometheus_consecutive_failures = 0
        self._last_prometheus_failure_time: float = 0.0

        # EWMA fallback — error rate / latency tracking (worker-local, O(1) memory)
        self._error_rate_ewma: dict[str, EWMAForecaster] = {}
        self._latency_ewma: dict[str, EWMAForecaster] = {}
        self._local_request_counts: dict[str, int] = {}  # fallback total request count

        # Health score smoothing — EWMA applied over the raw score
        self._health_ewma: dict[str, EWMAForecaster] = {}

        # Leader handoff detection
        self._leader_since: float | None = None

        # Prometheus metrics (optional)
        self._metrics = self._init_metrics()

    def _init_metrics(self) -> dict[str, Any] | None:
        """Initialize Prometheus metrics — includes the Raw/Smoothed dual gauge."""
        if not self._settings.metrics_enabled:
            return None
        try:
            from prometheus_client import Counter, Gauge, Histogram

            return {
                "health_score": Gauge(
                    "baldur_cell_health_score",
                    "Cell health score after EWMA smoothing (0.0~1.0)",
                    ["cell_id"],
                ),
                "health_score_raw": Gauge(
                    "baldur_cell_health_score_raw",
                    "Cell health score before EWMA smoothing (0.0~1.0)",
                    ["cell_id"],
                ),
                "request_total": Counter(
                    "baldur_cell_requests_total",
                    "Total requests per cell",
                    ["cell_id", "status"],
                ),
                "request_duration": Histogram(
                    "baldur_cell_request_duration_seconds",
                    "Request duration per cell",
                    ["cell_id"],
                    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
                ),
                "bulkhead_utilization": Gauge(
                    "baldur_cell_bulkhead_utilization",
                    "Bulkhead utilization ratio per cell",
                    ["cell_id"],
                ),
                "health_data_source": Gauge(
                    "baldur_cell_health_data_source",
                    "Health data source (1=prometheus, 0=ewma_fallback)",
                    ["cell_id"],
                ),
            }
        except ImportError:
            logger.debug("metrics.collection_disabled")
            return None

    # =========================================================================
    # record_request() — called from every worker
    # =========================================================================

    def record_request(self, cell_id: str, success: bool, latency: float) -> None:
        """
        Record a request outcome.

        Called from CellTaggingMiddleware or a view. Writes to the
        Prometheus Counter/Histogram (for global aggregation) and also
        updates the local EWMA (for fallback).

        Args:
            cell_id: cell identifier
            success: whether the request succeeded
            latency: response time in seconds
        """
        # 1) Prometheus metrics — for global aggregation (always recorded)
        if self._metrics:
            status = "success" if success else "error"
            self._metrics["request_total"].labels(cell_id=cell_id, status=status).inc()
            self._metrics["request_duration"].labels(cell_id=cell_id).observe(latency)

        # 2) Local EWMA — for fallback (O(1) memory)
        with self._lock:
            if cell_id not in self._error_rate_ewma:
                from baldur.core.time_series import (
                    EWMAForecaster,
                )

                self._error_rate_ewma[cell_id] = EWMAForecaster(
                    alpha=self._settings.health_ewma_alpha
                )
                self._latency_ewma[cell_id] = EWMAForecaster(
                    alpha=self._settings.health_ewma_alpha
                )

            self._error_rate_ewma[cell_id].update(0.0 if success else 1.0)
            self._latency_ewma[cell_id].update(latency)
            self._local_request_counts[cell_id] = (
                self._local_request_counts.get(cell_id, 0) + 1
            )

    # =========================================================================
    # Prometheus API queries — leader worker only
    # =========================================================================

    def _fetch_prometheus_metrics(
        self, cell_id: str
    ) -> tuple[float, float, int] | None:
        """
        Query global error rate and P99 latency from the Prometheus HTTP API.

        timeout=3s prevents aggregate_all from blocking. After 3
        consecutive failures the aggregator switches to fallback mode.

        Returns:
            (error_rate, latency_p99, total_requests), or None on failure
        """
        # Mini circuit breaker (including half-open):
        # after the consecutive-failure threshold, stay in fallback mode and
        # attempt a single probe once the retry window elapses.
        if (
            self._prometheus_consecutive_failures
            >= self._settings.prometheus_max_consecutive_failures
        ):
            elapsed_since_failure = (
                time.monotonic() - self._last_prometheus_failure_time
            )
            if elapsed_since_failure < self._settings.prometheus_retry_after_seconds:
                return None
            logger.info(
                "cell_health_aggregator.prometheus_halfopen_probe",
                elapsed_seconds=round(elapsed_since_failure, 1),
            )

        try:
            base_url = f"{self._prometheus_url}/api/v1/query"
            timeout = self._settings.prometheus_timeout_seconds

            # Error rate query
            error_data = self._prometheus_instant_query(
                base_url,
                (
                    f"sum(rate(baldur_cell_requests_total"
                    f'{{cell_id="{cell_id}",status="error"}}[5m]))'
                ),
                timeout,
            )
            total_data = self._prometheus_instant_query(
                base_url,
                (f'sum(rate(baldur_cell_requests_total{{cell_id="{cell_id}"}}[5m]))'),
                timeout,
            )
            p99_data = self._prometheus_instant_query(
                base_url,
                (
                    f"histogram_quantile(0.99, sum(rate("
                    f"baldur_cell_request_duration_seconds_bucket"
                    f'{{cell_id="{cell_id}"}}[5m])) by (le))'
                ),
                timeout,
            )

            error_rate_val = self._parse_prometheus_scalar(error_data)
            total_rate_val = self._parse_prometheus_scalar(total_data)
            p99_val = self._parse_prometheus_scalar(p99_data)

            error_rate = (
                (error_rate_val / total_rate_val) if total_rate_val > 0 else 0.0
            )
            # total_requests estimate (5-minute rate x 300 seconds)
            total_requests = int(total_rate_val * 300)

            self._prometheus_consecutive_failures = 0
            return error_rate, p99_val, total_requests

        except Exception as e:
            self._prometheus_consecutive_failures += 1
            self._last_prometheus_failure_time = time.monotonic()
            logger.warning(
                "cell_health_aggregator.prometheus_api_failed",
                cell_id=cell_id,
                consecutive_failures=self._prometheus_consecutive_failures,
                error=str(e),
            )
            return None

    @staticmethod
    def _prometheus_instant_query(
        base_url: str, query: str, timeout: float
    ) -> dict[str, Any]:
        """Execute a Prometheus instant query and return the parsed JSON body.

        Routes through ``safe_urlopen`` (stdlib urllib with an http(s) scheme
        allowlist) so cell-health Prometheus probes honor the framework SSRF
        guard instead of issuing raw outbound requests.
        """
        url = f"{base_url}?{urlencode({'query': query})}"
        with safe_urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())

    @staticmethod
    def _parse_prometheus_scalar(data: dict[str, Any]) -> float:
        """Extract a scalar value from a Prometheus instant query body."""
        if data.get("status") == "success":
            result = data.get("data", {}).get("result", [])
            if result:
                return float(result[0]["value"][1])
        return 0.0

    # =========================================================================
    # compute_health() — health computation
    # =========================================================================

    def compute_health(self, cell_id: str) -> float:
        """
        Compute the health of a cell.

        Primary: query global metrics from the Prometheus API.
        Fallback: local EWMA (when Prometheus is unavailable).

        Args:
            cell_id: cell identifier

        Returns:
            EWMA-smoothed health score (0.0~1.0)
        """
        # 1) Error rate & latency P99 — two-tier source
        prom_result = self._fetch_prometheus_metrics(cell_id)
        if prom_result is not None:
            error_rate, latency_p99, total_requests = prom_result
            source = "prometheus"
        else:
            # Fallback: local EWMA
            with self._lock:
                ewma = self._error_rate_ewma.get(cell_id)
                error_rate = ewma.get_smoothed() if ewma else 0.0  # type: ignore[assignment]
                lat_ewma = self._latency_ewma.get(cell_id)
                latency_p99 = lat_ewma.get_smoothed() if lat_ewma else 0.0  # type: ignore[assignment]
                total_requests = self._local_request_counts.get(cell_id, 0)
            source = "ewma_fallback"

        # get_smoothed() may return None, so guarantee defaults
        if error_rate is None:
            error_rate = 0.0
        if latency_p99 is None:
            latency_p99 = 0.0

        # 2) Bulkhead utilization (local memory)
        bulkhead_util = self._get_bulkhead_utilization(cell_id)

        # 3) CB open ratio (callback-based counter)
        cb_open_ratio = self._get_cb_open_ratio(cell_id)

        # 4) Normalization + minimum-sample-size guard
        if total_requests < self._settings.health_min_samples_for_penalty:
            error_norm = 0.0  # insufficient samples -> error-rate penalty waived
        else:
            error_norm = min(error_rate / self._settings.health_max_error_rate, 1.0)
        latency_norm = min(latency_p99 / self._settings.health_max_latency_p99, 1.0)

        # 5) Weighted sum -> raw health score
        penalty = (
            error_norm * self._settings.health_weight_error_rate
            + latency_norm * self._settings.health_weight_latency
            + bulkhead_util * self._settings.health_weight_bulkhead
            + cb_open_ratio * self._settings.health_weight_cb_open
        )
        raw_health = max(0.0, 1.0 - penalty)

        # 6) EWMA smoothing
        if cell_id not in self._health_ewma:
            from baldur.core.time_series import (
                EWMAForecaster,
            )

            self._health_ewma[cell_id] = EWMAForecaster(
                alpha=self._settings.health_ewma_alpha
            )
        smoothed_health = self._health_ewma[cell_id].update(raw_health)

        # 7) Persist the snapshot — record both raw and smoothed
        self._snapshots[cell_id] = CellHealthSnapshot(
            cell_id=cell_id,
            health_score=smoothed_health,
            raw_health_score=raw_health,
            error_rate=error_rate,
            latency_p99=latency_p99,
            bulkhead_utilization=bulkhead_util,
            cb_open_ratio=cb_open_ratio,
            source=source,
        )

        # 8) Update Prometheus gauges — expose both raw and smoothed
        if self._metrics:
            self._metrics["health_score"].labels(cell_id=cell_id).set(smoothed_health)
            self._metrics["health_score_raw"].labels(cell_id=cell_id).set(raw_health)
            self._metrics["bulkhead_utilization"].labels(cell_id=cell_id).set(
                bulkhead_util
            )
            self._metrics["health_data_source"].labels(cell_id=cell_id).set(
                1.0 if source == "prometheus" else 0.0
            )

        return smoothed_health

    def _get_bulkhead_utilization(self, cell_id: str) -> float:
        """
        Query the cell's bulkhead utilization from BulkheadRegistry.

        Uses BulkheadRegistry.get_all_states() to read from local memory.
        """
        try:
            from baldur_pro.services.bulkhead.registry import (
                get_bulkhead_registry,
            )

            registry = get_bulkhead_registry()
            all_states = registry.get_all_states()
            state = all_states.get(cell_id)
            if state:
                max_c = state.max_concurrent or 1
                active = state.active_count or 0
                return min(active / max_c, 1.0)
        except ImportError:
            pass
        except Exception as e:
            logger.warning(
                "cell_health_aggregator.bulkhead_query_failed",
                cell_id=cell_id,
                error=str(e),
            )
        return 0.0

    def _get_cb_open_ratio(self, cell_id: str) -> float:
        """
        Query the cell's CB OPEN ratio via composite keys.

        Parses cell_id directly out of the CB service_name, therefore:
        - no dependency on the assigned_services TTL (race condition removed)
        - manual control (force_open/force_close) is also detected
        - no metadata contention (each cell owns physically separate CBs)
        """
        try:
            from baldur.core.cb_namespace import (
                parse_composite_cb_name,
            )
            from baldur.services.circuit_breaker import (
                get_circuit_breaker_service,
            )

            cb_service = get_circuit_breaker_service()
            all_states = cb_service.get_all_states()

            open_count = 0
            total = 0
            for state in all_states:
                _, state_cell_id = parse_composite_cb_name(
                    state.get("service_name", "")
                )
                if state_cell_id == cell_id:
                    total += 1
                    if state.get("state") == "open":
                        open_count += 1

            return (open_count / total) if total > 0 else 0.0
        except Exception as e:
            logger.warning(
                "cell_health_aggregator.cb_open_ratio_query_failed",
                cell_id=cell_id,
                error=str(e),
            )
            return 0.0

    # =========================================================================
    # Snapshot queries
    # =========================================================================

    def get_snapshot(self, cell_id: str) -> CellHealthSnapshot | None:
        """Return the most recent health snapshot."""
        return self._snapshots.get(cell_id)

    def get_all_snapshots(self) -> dict[str, CellHealthSnapshot]:
        """Return health snapshots for all cells."""
        return dict(self._snapshots)

    # =========================================================================
    # aggregate_all() — called from LeaderScheduler
    # =========================================================================

    def aggregate_all(self) -> None:
        """
        Compute health for all cells and refresh CellRegistry.

        Called periodically by the single leader worker in the cluster
        via LeaderScheduler's @scheduler.job().
        """
        if self._leader_since is not None:
            elapsed = time.monotonic() - self._leader_since
            warmup_window = self._settings.health_check_interval_seconds * 2
            if elapsed < warmup_window:
                logger.info(
                    "cell_health_aggregator.leader_warmup",
                    elapsed_seconds=round(elapsed, 1),
                    warmup_window_seconds=warmup_window,
                )

        try:
            from baldur.services.cell_topology import get_cell_registry

            registry = get_cell_registry()
            for cell_id in registry.get_all_cells():
                score = self.compute_health(cell_id)
                registry.update_health_score(cell_id, score)

            # Evaluate the evacuation policy after health refresh completes
            self._evaluate_evacuation_policy(registry)
        except Exception as e:
            logger.exception(
                "cell_health_aggregator.aggregate_all_failed",
                error=e,
            )

    def _evaluate_evacuation_policy(self, registry: object) -> None:
        """Evaluate the cell evacuation policy against refreshed health scores.

        Returns immediately when the evacuation_enabled toggle is off.
        Protected by an independent try/except so an evacuation-policy
        failure never affects the health-collection loop.
        """
        if not self._settings.evacuation_enabled:
            return

        try:
            from baldur.services.cell_topology.policy import (
                get_cell_evacuation_policy,
            )

            policy = get_cell_evacuation_policy()
            all_cells = registry.get_all_cells()  # type: ignore[attr-defined]
            for cell_id, cell_info in all_cells.items():
                policy.evaluate(cell_id, cell_info.health_score)
        except Exception as e:
            logger.exception(
                "cell_health_aggregator.evacuation_policy_failed",
                error=e,
            )

    def on_become_leader(self) -> None:
        """Called on leader transition — records the warmup start time."""
        self._leader_since = time.monotonic()
        logger.info("cell_health_aggregator.became_leader_ewma_fallback")

    def on_lose_leader(self) -> None:
        """Called on leadership loss."""
        self._leader_since = None
        logger.info("cell_health_aggregator.lost_leadership")


# =============================================================================
# LeaderScheduler integration — entry points
# =============================================================================

_aggregator: CellHealthAggregator | None = None
_aggregator_lock = threading.Lock()


def get_cell_health_aggregator() -> CellHealthAggregator:
    """Return the CellHealthAggregator singleton."""
    global _aggregator
    if _aggregator is None:
        with _aggregator_lock:
            if _aggregator is None:
                _aggregator = CellHealthAggregator()
    return _aggregator


def setup_cell_health_scheduler() -> None:
    """
    Register the LeaderScheduler-based health aggregation loop.

    Called from AppConfig.ready() or startup.

    Pattern references:
    - LeaderScheduler (coordination/scheduler.py)
    - DLQConsumerCoordinator (coordination/dlq_consumer.py)
    """
    from baldur.settings.cell_topology import get_cell_topology_settings

    settings = get_cell_topology_settings()
    if not settings.enabled:
        return

    from baldur.coordination.scheduler import get_leader_scheduler

    aggregator = get_cell_health_aggregator()
    scheduler = get_leader_scheduler("cell-health-aggregator")

    # Detect leader-transition events — record warmup context
    scheduler.register_leader_callbacks(
        on_become=aggregator.on_become_leader,
        on_lose=aggregator.on_lose_leader,
    )

    @scheduler.job(
        interval_seconds=settings.health_check_interval_seconds,
        name="cell-health-aggregation",
    )
    def _aggregate():
        aggregator.aggregate_all()

    scheduler.start()
    logger.info(
        "cell_health_aggregator.scheduler_registered",
        interval_seconds=settings.health_check_interval_seconds,
    )


def reset_cell_health_aggregator() -> None:
    """Reset the singleton (test utility)."""
    global _aggregator
    with _aggregator_lock:
        _aggregator = None
