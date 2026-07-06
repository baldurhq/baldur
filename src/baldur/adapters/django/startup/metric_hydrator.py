"""
Startup Hydration for Prometheus Gauges.

Schedules gauge hydration with jitter to avoid thundering-herd on
multi-server restarts. The psutil system-metrics cache is started by
``baldur.init()`` (framework-agnostic), not here.
"""

from __future__ import annotations

import random
import threading

import structlog
from django.conf import settings

__all__ = [
    "MetricHydrator",
]

logger = structlog.get_logger()


class MetricHydrator:
    """Startup Hydration for Prometheus Gauges."""

    _hydration_done: bool = False
    _hydration_lock: threading.Lock = threading.Lock()

    @classmethod
    def hydrate(cls) -> None:
        """Schedule gauge hydration with jitter."""
        if not cls._should_hydrate():
            return

        # Jitter calculation (0 ~ max_delay seconds)
        jitter_max = getattr(settings, "BALDUR_SYNC_JITTER_MAX", 60)
        jitter = random.uniform(0, jitter_max)

        # Background hydration (avoid blocking server startup)
        timer = threading.Timer(jitter, cls._hydrate_gauges)
        timer.daemon = True  # exits with main thread
        timer.start()

        logger.info(
            "baldur.gauge_hydration_scheduled",
            jitter=jitter,
            jitter_max=jitter_max,
        )

    @classmethod
    def _should_hydrate(cls) -> bool:
        """Determine whether hydration should execute.

        Prevents duplicate execution and respects settings.

        Returns:
            bool: True if hydration should proceed.
        """
        # Disabled via settings
        if not getattr(settings, "BALDUR_SYNC_ON_STARTUP", True):
            logger.debug("baldur.gauge_hydration_disabled_settings")
            return False

        # Duplicate execution guard
        with cls._hydration_lock:
            if cls._hydration_done:
                logger.debug("baldur.gauge_hydration_already_scheduled")
                return False
            cls._hydration_done = True
            return True

    @classmethod
    def _hydrate_gauges(cls) -> None:
        """Hydrate gauges once.

        Queries Redis for actual values to initialise Prometheus Gauges.
        Since v2.0.0 Redis is the default backend.

        Graceful Degradation:
        - Failure does not block server startup
        - Logs a warning and continues normal operation
        """
        try:
            # Sync gauges via Reconciler
            from baldur.metrics.reconciler import get_reconciler

            reconciler = get_reconciler()
            result = reconciler.sync_all_gauges()

            logger.info(
                "baldur.gauge_hydration_completed",
                dlq_pending_count=len(result.dlq_pending),
                cb_states_count=len(result.circuit_breaker_states),
            )

        except ImportError:
            logger.debug("baldur.reconciler_module_unavailable")
        except Exception as e:
            # Graceful Degradation: failure does not block server startup
            logger.warning(
                "baldur.gauge_hydration_failed",
                error=e,
            )

    @classmethod
    def reset_state(cls) -> None:
        """Reset hydration state (for testing)."""
        with cls._hydration_lock:
            cls._hydration_done = False
