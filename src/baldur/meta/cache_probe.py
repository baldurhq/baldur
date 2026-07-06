"""
PrecomputedCacheProbe — monitor cache worker health for MetaWatchdog.

Two-source judgment:
  Source A: Worker state — running + compute functions registered
  Source B: Cache staleness — last refresh within multiplier × interval

Reference: 411 ED1
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import structlog

from baldur.meta.health_probe import HealthProbe, HealthStatus, ProbeResult
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = ["PrecomputedCacheProbe"]


class PrecomputedCacheProbe(HealthProbe):
    """Health probe for PrecomputedCache worker."""

    @property
    def component_name(self) -> str:
        return "precomputed_cache"

    def probe(self) -> ProbeResult:  # noqa: C901, PLR0912, PLR0915
        start = time.time()
        try:
            from baldur.services.precomputed_cache.worker import (
                get_precomputed_cache_worker,
            )

            worker = get_precomputed_cache_worker()
            health = worker.get_passive_health()

            from baldur.meta.config import get_meta_watchdog_settings

            settings = get_meta_watchdog_settings()
            multiplier = settings.probe_cache_staleness_multiplier

            running: bool = health["running"]
            registered_keys: list[str] = health["registered_keys"]
            last_refresh_at_str: str | None = health["last_refresh_at"]
            started_at_str: str | None = health["started_at"]
            refresh_interval: float = health["refresh_interval_seconds"]
            effective_interval: float = health.get(
                "effective_interval_seconds", refresh_interval
            )

            staleness_ratio: float | None = None
            staleness_threshold = multiplier * effective_interval

            # Parse timestamps
            now = utc_now()
            last_refresh_at: datetime | None = None
            started_at: datetime | None = None
            if last_refresh_at_str:
                last_refresh_at = datetime.fromisoformat(last_refresh_at_str)
            if started_at_str:
                started_at = datetime.fromisoformat(started_at_str)

            # Calculate staleness ratio if last refresh exists
            if last_refresh_at is not None:
                elapsed = (now - last_refresh_at).total_seconds()
                staleness_ratio = (
                    elapsed / effective_interval if effective_interval > 0 else None
                )

            # Judgment
            status: HealthStatus
            reason: str = ""

            if not running:
                status = HealthStatus.UNHEALTHY
                reason = "Cache worker not running"
            elif last_refresh_at is None:
                # Cold start: never refreshed yet
                if started_at is not None:
                    started_elapsed = (now - started_at).total_seconds()
                    if started_elapsed > staleness_threshold:
                        status = HealthStatus.UNHEALTHY
                        reason = "First refresh overdue — possible worker deadlock"
                    else:
                        status = HealthStatus.UNKNOWN
                        reason = "Worker started but first refresh not yet completed"
                else:
                    status = HealthStatus.UNKNOWN
                    reason = "Worker started but first refresh not yet completed"
            elif not registered_keys:
                status = HealthStatus.DEGRADED
                reason = "Worker running but no compute functions registered"
            elif staleness_ratio is not None and staleness_ratio > multiplier:
                status = HealthStatus.DEGRADED
                reason = (
                    f"Cache stale: last refresh {staleness_ratio:.1f}× interval ago"
                )
            else:
                status = HealthStatus.HEALTHY

            details: dict[str, Any] = {
                "running": running,
                "registered_keys": registered_keys,
                "last_refresh_at": last_refresh_at_str,
                "started_at": started_at_str,
                "refresh_interval_seconds": refresh_interval,
                "staleness_ratio": staleness_ratio,
            }

            return ProbeResult(
                component=self.component_name,
                status=status,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                reason=reason,
                details=details,
            )

        except ImportError:
            return ProbeResult(
                component=self.component_name,
                status=HealthStatus.UNKNOWN,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                error="precomputed_cache module not available",
            )
        except Exception as e:
            return ProbeResult(
                component=self.component_name,
                status=HealthStatus.UNKNOWN,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                error=str(e),
            )
