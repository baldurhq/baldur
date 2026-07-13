"""
Pool Circuit Breaker Settings - Pydantic v2.

Configuration for the Django pool-aware circuit-breaker middleware
(``api/django/pool_circuit_breaker.py``): the fail-fast state-machine
thresholds and the cache-staleness tuning for its background pool-status
refresh.

Environment Variables:
    BALDUR_POOL_CB_FAILURE_THRESHOLD=3
    BALDUR_POOL_CB_SUCCESS_THRESHOLD=2
    BALDUR_POOL_CB_RECOVERY_TIMEOUT=10
    BALDUR_POOL_CB_HALF_OPEN_MAX_REQUESTS=3
    BALDUR_POOL_CB_CACHE_INTERVAL_MS=100
    BALDUR_POOL_CB_STALE_MULTIPLIER=10
    BALDUR_POOL_CB_CRITICAL_STALE_MS=5000
"""

from __future__ import annotations

import warnings

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import IntervalDuration, MediumCount

__all__ = [
    "PoolCircuitBreakerSettings",
    "get_pool_circuit_breaker_settings",
    "reset_pool_circuit_breaker_settings",
]


class PoolCircuitBreakerSettings(BaseSettings):
    """
    Settings for the Django pool-aware circuit-breaker middleware.

    Controls the fail-fast state machine (failure/success thresholds, recovery
    timeout, half-open trial budget) and the cached pool-status staleness tiers
    (refresh interval, stale-warning multiplier, critical-stale safe fallback).
    """

    model_config = make_settings_config("BALDUR_POOL_CB_")

    failure_threshold: MediumCount = Field(
        default=3,
        description="Consecutive pool failures before the circuit opens",
    )
    success_threshold: MediumCount = Field(
        default=2,
        description="Successes required to close the circuit from half-open",
    )
    recovery_timeout: IntervalDuration = Field(
        default=10,
        description="Seconds to wait before probing recovery (open -> half-open)",
    )
    half_open_max_requests: MediumCount = Field(
        default=3,
        description=(
            "Max trial requests admitted while probing recovery in the half-open state"
        ),
    )
    cache_interval_ms: int = Field(
        default=100,
        ge=50,
        le=1000,
        description="Background pool-status cache refresh interval (milliseconds)",
    )
    stale_multiplier: MediumCount = Field(
        default=10,
        description=(
            "Multiple of cache_interval_ms at which a stale-cache warning fires"
        ),
    )
    critical_stale_ms: int = Field(
        default=5000,
        ge=1000,
        le=60000,
        description=(
            "Cache age (milliseconds) beyond which the cache is critically "
            "stale and the middleware safely falls back to CLOSED"
        ),
    )

    @model_validator(mode="after")
    def _warn_unreachable_stale_warning_tier(self) -> PoolCircuitBreakerSettings:
        """Warn when the stale-warning tier can never fire.

        ``get_cached_pool_status`` evaluates the critical-stale branch first, so
        if the stale-warning threshold (``cache_interval_ms * stale_multiplier``)
        reaches ``critical_stale_ms``, the warning tier is unreachable — the
        critical-stale safe fallback always triggers first.
        """
        stale_warning_threshold_ms = self.cache_interval_ms * self.stale_multiplier
        if stale_warning_threshold_ms >= self.critical_stale_ms:
            warnings.warn(
                f"Stale-warning threshold ({stale_warning_threshold_ms} ms = "
                f"cache_interval_ms {self.cache_interval_ms} * stale_multiplier "
                f"{self.stale_multiplier}) is at or beyond critical_stale_ms "
                f"({self.critical_stale_ms} ms); the stale-warning tier will "
                f"never fire because the critical-stale fallback triggers first.",
                UserWarning,
                stacklevel=2,
            )
        return self


def get_pool_circuit_breaker_settings() -> PoolCircuitBreakerSettings:
    from baldur.settings.root import get_config

    return get_config().adapters.pool_circuit_breaker


def reset_pool_circuit_breaker_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().adapters.__dict__["pool_circuit_breaker"]
    except KeyError:
        pass
