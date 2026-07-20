"""
Air-Gap Adapter Factory.

Creates the appropriate Air-Gap storage adapter based on configuration.
Default is NullAirGapAdapter (disabled).

Configuration:
    BALDUR_AIRGAP_ENABLED: "true" or "false" (default: "false")
    BALDUR_AIRGAP_REDIS_URL: Redis connection URL
    BALDUR_AIRGAP_PREFIX: Key prefix (default: "sh:airgap:")
    BALDUR_AIRGAP_TTL: Default TTL in seconds (default: 3600)
"""

from __future__ import annotations

import os

import structlog

from baldur.adapters.airgap.base import AirGapStorageAdapter
from baldur.adapters.airgap.null_adapter import NullAirGapAdapter
from baldur.utils.singleton import make_singleton_factory

logger = structlog.get_logger()


def _create_airgap_adapter() -> AirGapStorageAdapter:
    enabled = os.environ.get("BALDUR_AIRGAP_ENABLED", "false").lower() == "true"
    if enabled:
        adapter = _create_redis_adapter()
        if adapter is None:
            logger.warning("airgap.redis_adapter_creation_failed")
            return NullAirGapAdapter()
        return adapter
    logger.info("air_gap.disabled_null_adapter_fallback")
    return NullAirGapAdapter()


get_airgap_adapter, configure_airgap_adapter, reset_airgap_adapter = (
    make_singleton_factory("airgap_adapter", _create_airgap_adapter)
)


def _create_redis_adapter() -> AirGapStorageAdapter | None:
    """Create Redis-based Air-Gap adapter."""
    redis_url = os.environ.get("BALDUR_AIRGAP_REDIS_URL")

    if not redis_url:
        # Try to read it from Django settings
        redis_url = _get_redis_url_from_django()

    if not redis_url:
        logger.error("airgap.redis_url_not_configured")
        return None

    try:
        from baldur.adapters.redis.connection_factory import (
            get_redis_connection_factory,
        )

        client = get_redis_connection_factory().create(redis_url)
        client.ping()  # Connection test

        prefix = os.environ.get("BALDUR_AIRGAP_PREFIX", "sh:airgap:")
        ttl_str = os.environ.get("BALDUR_AIRGAP_TTL", "3600")
        ttl = int(ttl_str) if ttl_str else 3600

        from baldur.adapters.airgap.redis_adapter import RedisAirGapAdapter

        adapter = RedisAirGapAdapter(client, prefix=prefix, default_ttl=ttl)
        logger.info(
            "air_gap.redisairgapadapter_created",
            redis_url=redis_url,
        )
        return adapter

    except ImportError:
        logger.exception("air_gap.redis_package_installed")
        return None
    except Exception as e:
        logger.exception(
            "air_gap.create_redis_adapter_failed",
            error=e,
        )
        return None


def _get_redis_url_from_django() -> str | None:
    """Try to get Redis URL from Django settings."""
    try:
        from django.conf import settings

        # Try the various setting names
        for attr in ["REDIS_URL", "CACHES", "CELERY_BROKER_URL"]:
            if hasattr(settings, attr):
                value = getattr(settings, attr)

                if attr == "REDIS_URL" and isinstance(value, str):
                    return value

                if attr == "CACHES" and isinstance(value, dict):
                    default_cache = value.get("default", {})
                    location = default_cache.get("LOCATION")
                    if location and "redis" in str(location):
                        return location if isinstance(location, str) else location[0]

                if (
                    attr == "CELERY_BROKER_URL"
                    and isinstance(value, str)
                    and "redis" in value
                ):
                    return value

        return None

    except Exception:
        return None


__all__ = [
    "get_airgap_adapter",
    "configure_airgap_adapter",
    "reset_airgap_adapter",
]
