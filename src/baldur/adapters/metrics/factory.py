"""
Metric Adapter Factory.

Creates the appropriate metric source adapter based on configuration.
"""

from __future__ import annotations

import os
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
    """Create Redis-based adapter."""
    try:
        import redis as redis_lib

        from baldur.adapters.metrics.redis_adapter import RedisMetricSourceAdapter

        redis_url = os.environ.get("BALDUR_REDIS_URL", "redis://localhost:6379/0")
        settings = get_metrics_settings()
        prefix = settings.redis_prefix

        client = redis_lib.from_url(redis_url, decode_responses=True)
        # Test connection
        client.ping()

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
