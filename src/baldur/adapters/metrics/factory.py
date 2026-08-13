"""
Metric Adapter Factory.

Creates the appropriate metric source adapter based on configuration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from baldur.adapters.metrics.base import (
    MetricSourceAdapter,
    NullMetricSourceAdapter,
)
from baldur.settings.metrics import get_metrics_settings
from baldur.utils.singleton import make_singleton_factory

if TYPE_CHECKING:
    pass

logger = structlog.get_logger()


def _create_metric_adapter() -> MetricSourceAdapter:
    settings = get_metrics_settings()
    adapter_type = settings.adapter_type

    if adapter_type == "redis":
        return _create_redis_adapter()
    if adapter_type == "django":
        return _create_django_adapter()
    logger.info("metric_adapter.null_adapter_fallback")
    return NullMetricSourceAdapter()


get_metric_adapter, configure_metric_adapter, reset_metric_adapter = (
    make_singleton_factory("metric_adapter", _create_metric_adapter)
)


def _create_redis_adapter() -> MetricSourceAdapter:
    """Create Redis-based adapter.

    The client comes from the shared connection factory rather than a direct
    ``redis.from_url``: the factory is what applies the configured socket
    timeouts, injects credentials from settings instead of the URL, and
    resolves Sentinel/Cluster URLs. Built directly, the verification ping below
    carried no connect timeout at all and blocked on the OS TCP timeout —
    tens of seconds — whenever the host was unreachable rather than refusing.
    """
    try:
        from baldur.adapters.metrics.redis_adapter import RedisMetricSourceAdapter
        from baldur.adapters.redis.connection_factory import (
            get_redis_connection_factory,
        )
        from baldur.settings.redis import get_redis_settings

        # Same value the previous env read resolved to — RedisSettings binds
        # BALDUR_REDIS_URL with the identical default — now read through the
        # settings layer like every other Redis consumer.
        redis_url = get_redis_settings().url
        settings = get_metrics_settings()
        prefix = settings.redis_prefix

        factory = get_redis_connection_factory()
        # Bounded admission probe on a throwaway client. The adapter built
        # below is memoized by the metric-adapter singleton for the process
        # lifetime, so the shared client keeps its data-path timeouts. The
        # probe needs no decode_responses — it only pings.
        factory.probe(redis_url)
        client = factory.create(redis_url, decode_responses=True)

        logger.info(
            "metric_adapter.redis_adapter_connected",
            redis_url=redis_url,
        )
        return RedisMetricSourceAdapter(redis_client=client, prefix=prefix)

    except ImportError:
        logger.warning("metric_adapter.redis_package_installed_falling")
        return NullMetricSourceAdapter()
    except Exception as e:
        logger.warning(
            "metric_adapter.redis_connection_failed_falling",
            error=e,
        )
        return NullMetricSourceAdapter()


def _create_django_adapter() -> MetricSourceAdapter:
    """Create Django ORM-based adapter."""
    try:
        from baldur.adapters.metrics.django_adapter import (
            DjangoMetricSourceAdapter,
        )

        # Django model must be set via configure_metric_adapter() by the user.
        # Create an empty adapter without model binding here.
        logger.info("metric_adapter.django_adapter_created_without")
        return DjangoMetricSourceAdapter()

    except ImportError as e:
        logger.warning(
            "metric_adapter.django_available",
            error=e,
        )
        return NullMetricSourceAdapter()


__all__ = [
    "get_metric_adapter",
    "configure_metric_adapter",
    "reset_metric_adapter",
]
