"""Point-in-time system snapshot for postmortem timelines.

``collect_system_snapshot()`` reads CPU and memory (from the background system
metrics cache when it runs, otherwise directly through psutil), the active
database connection count, the error-budget burn rate and the request counter.
It imports no web framework, so a Celery worker or an admin handler can take a
snapshot on any install.
"""

from __future__ import annotations

from typing import Any

import psutil
import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = ["collect_system_snapshot"]


def collect_system_snapshot() -> dict[str, Any]:  # noqa: C901, PLR0912
    """Collect a system snapshot (CPU, memory, connections, error/request rate).

    Collects the system state to embed in a postmortem timeline snapshot.

    Returns:
        System snapshot dictionary:
        - timestamp: capture time
        - cpu_percent: CPU utilization
        - memory_percent: memory utilization
        - memory_used_mb: used memory (MB)
        - memory_available_mb: available memory (MB)
        - db_active_connections: number of active DB connections
        - error_rate: error rate (when available)
        - request_rate: request rate (when available)
    """
    try:
        # Read CPU/memory from the cache (~0ms); fall back to direct measurement
        # (100ms) when the cache is not running.
        try:
            from baldur.services.system_metrics_cache import (
                get_system_metrics_cache,
            )

            cache = get_system_metrics_cache()
            if cache.is_running():
                metrics = cache.get_metrics()
                snapshot = {
                    "timestamp": utc_now().isoformat(),
                    "cpu_percent": metrics.cpu_percent,
                    "memory_percent": metrics.memory_percent,
                    "memory_used_mb": metrics.memory_used_mb,
                    "memory_available_mb": metrics.memory_available_mb,
                    "metrics_source": metrics.source,
                }
            else:
                raise RuntimeError("Cache not running")
        except Exception:
            # Fallback: direct measurement (preserves the previous behavior)
            cpu_percent = psutil.cpu_percent(interval=0.1)
            memory = psutil.virtual_memory()
            snapshot = {
                "timestamp": utc_now().isoformat(),
                "cpu_percent": round(cpu_percent, 1),
                "memory_percent": round(memory.percent, 1),
                "memory_used_mb": round(memory.used / (1024 * 1024), 1),
                "memory_available_mb": round(memory.available / (1024 * 1024), 1),
                "metrics_source": "direct",
            }

        # DB active connection count via the pg_admin registry surface.
        try:
            from baldur.factory import ProviderRegistry

            pg_admin = ProviderRegistry.pg_admin.get()
            if pg_admin.is_available():
                snapshot["db_active_connections"] = (
                    pg_admin.get_active_connection_count()
                )
            else:
                snapshot["db_active_connections"] = None
        except Exception:
            snapshot["db_active_connections"] = None

        # Read the error rate from the error budget — live data only. With the
        # feature off or unwired the raw getter returns a simulated healthy
        # status, which a postmortem would record as fact.
        try:
            from baldur_pro.services.error_budget import get_live_budget_status

            budget_status = get_live_budget_status()
            if budget_status is not None:
                snapshot["error_rate"] = float(budget_status.burn_rate_1h)
                snapshot["remaining_budget_percent"] = float(
                    budget_status.budget_remaining_percent
                )
            else:
                snapshot["error_rate"] = None
        except Exception:
            snapshot["error_rate"] = None

        # Read the request rate from the metric adapter
        try:
            from baldur.adapters.metrics import get_metric_adapter

            adapter = get_metric_adapter()
            # Try to read the request counter from the MetricSourceAdapter
            if hasattr(adapter, "get_counter_value"):
                request_counter = adapter.get_counter_value(
                    "baldur_http_requests_total"
                )
                if request_counter is not None:
                    snapshot["request_rate"] = request_counter
            else:
                snapshot["request_rate"] = None
        except Exception:
            snapshot["request_rate"] = None

        return snapshot
    except Exception as e:
        logger.warning(
            "system_snapshot.collection_failed",
            error=e,
        )
        # The snapshot is returned in an HTTP response body, so the exception
        # text stays server-side: it can carry adapter internals and connection
        # strings, and the caller only needs to know the snapshot is missing.
        # The log line above keeps the detail for whoever is debugging.
        return {
            "timestamp": utc_now().isoformat(),
            "error": "snapshot_collection_failed",
        }
