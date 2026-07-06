"""
Circuit Breaker Configuration and Types

Contains configuration dataclass, state constants, and result types
for circuit breaker operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

# =============================================================================
# Circuit Breaker State Enum
# =============================================================================
# Canonical source: CircuitBreakerStateEnum(str, Enum) in interfaces/repositories.py
# Alias kept for backward compatibility — zero consumer-code changes
from baldur.interfaces.repositories import (
    CircuitBreakerStateEnum as CircuitState,  # noqa: F401
)
from baldur.settings import get_config

if TYPE_CHECKING:
    from baldur.interfaces.repositories import CircuitBreakerStateData

__all__ = [
    "CircuitState",
    "CircuitBreakerConfig",
    "CircuitBreakerDecision",
    "CircuitBreakerFallbackResult",
    "CircuitBreakerResult",
]

# =============================================================================
# Configuration
# =============================================================================


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker operations."""

    enabled: bool = False
    failure_threshold: int = 5
    recovery_timeout: int = 60  # seconds
    success_threshold: int = 2

    # Minimum calls before CB can open (prevents false positives with low traffic)
    # If total calls < minimum_calls, CB will not open even if failure_threshold is met
    minimum_calls: int = 10  # At least 10 calls before CB can trigger

    # Sliding window for rate-based threshold (used when failure_rate_threshold > 0)
    sliding_window_size: int = 100  # Number of calls to track
    failure_rate_threshold: float = 50.0  # percentage — CB Opens when error rate exceeds 50% (OR'd with count-based)

    # Fallback strategy when CB is open
    # Options: "cache" (default), "block", "dlq", "default_response"
    fallback_strategy: str = "cache"
    fallback_cache_ttl_seconds: int = 300  # 5 minutes cache TTL for stale data

    # Error Budget integration - burn rate multiplier when CB is open
    # When CB opens, burn rate is multiplied by this factor
    cb_open_burn_rate_multiplier: float = 10.0
    # Base error budget minutes consumed per CB trip (before multiplier)
    cb_open_base_consumption_minutes: float = 1.0

    # Governance parameters
    manual_override_ttl_minutes: int = 90  # Default 90 min, max recommended 180
    half_open_max_calls: int = (
        3  # Max trial calls admitted while probing recovery in half-open state
    )
    max_pending_duration_hours: int = 4  # SLA for pending DLQ items
    max_retry_lifetime_hours: int = 24  # Max time to attempt retries

    # Rate limit cascade detection settings
    rate_limit_cascade_threshold: int = 10  # Number of 429s in window to trigger CB
    rate_limit_cascade_window_seconds: int = 60  # Time window for cascade detection
    rate_limit_cascade_rate: float = 10.0  # 429 rate (%) to trigger cascade
    rate_limit_cascade_minimum_calls: int = (
        20  # Minimum requests before rate evaluation
    )

    # Self-DDoS protection settings
    self_ddos_protection_enabled: bool = True
    self_ddos_rps_limit: int = 200  # Per-service RPS cap for DDoS detection
    self_ddos_window_seconds: int = 10  # Time window for self-DDoS detection
    self_ddos_backoff_multiplier: float = 2.0  # Exponential backoff multiplier
    self_ddos_backoff_base_seconds: float = 1.0  # Base delay for adaptive backoff
    self_ddos_backoff_max_seconds: float = 60.0  # Max delay cap for adaptive backoff
    self_ddos_backoff_jitter_factor: float = 0.25  # Jitter factor for adaptive backoff

    # Distributed rate limit tracking
    rate_limit_distributed: bool = False  # Enable Redis L2 backend

    @classmethod
    def from_settings(cls) -> CircuitBreakerConfig:
        """Load configuration from RuntimeConfigManager (preferred) or core config."""
        # Try RuntimeConfigManager first (runtime-configurable)
        try:
            from baldur.factory.registry import ProviderRegistry

            manager = ProviderRegistry.runtime_config_manager.safe_get()
            if manager is None:
                raise RuntimeError("baldur_pro RuntimeConfigManager not registered")
            runtime_config = manager.get_circuit_breaker_config()

            return cls(
                enabled=runtime_config.get("enabled", True),
                failure_threshold=runtime_config.get("failure_threshold", 5),
                recovery_timeout=runtime_config.get("recovery_timeout", 60),
                success_threshold=runtime_config.get("success_threshold", 2),
                minimum_calls=runtime_config.get("minimum_calls", 10),
                sliding_window_size=runtime_config.get("sliding_window_size", 100),
                failure_rate_threshold=runtime_config.get(
                    "failure_rate_threshold", 50.0
                ),
                fallback_strategy=runtime_config.get("fallback_strategy", "cache"),
                fallback_cache_ttl_seconds=runtime_config.get(
                    "fallback_cache_ttl_seconds", 300
                ),
                cb_open_burn_rate_multiplier=runtime_config.get(
                    "cb_open_burn_rate_multiplier", 10.0
                ),
                cb_open_base_consumption_minutes=runtime_config.get(
                    "cb_open_base_consumption_minutes", 1.0
                ),
                manual_override_ttl_minutes=runtime_config.get(
                    "manual_override_ttl_minutes", 90
                ),
                half_open_max_calls=runtime_config.get("half_open_max_calls", 3),
                max_pending_duration_hours=runtime_config.get(
                    "max_pending_duration_hours", 4
                ),
                max_retry_lifetime_hours=runtime_config.get(
                    "max_retry_lifetime_hours", 24
                ),
                rate_limit_cascade_threshold=runtime_config.get(
                    "rate_limit_cascade_threshold", 10
                ),
                rate_limit_cascade_window_seconds=runtime_config.get(
                    "rate_limit_cascade_window_seconds", 60
                ),
                rate_limit_cascade_rate=runtime_config.get(
                    "rate_limit_cascade_rate", 10.0
                ),
                rate_limit_cascade_minimum_calls=runtime_config.get(
                    "rate_limit_cascade_minimum_calls", 20
                ),
                self_ddos_protection_enabled=runtime_config.get(
                    "self_ddos_protection_enabled", True
                ),
                self_ddos_rps_limit=runtime_config.get("self_ddos_rps_limit", 200),
                self_ddos_window_seconds=runtime_config.get(
                    "self_ddos_window_seconds", 10
                ),
                self_ddos_backoff_multiplier=runtime_config.get(
                    "self_ddos_backoff_multiplier", 2.0
                ),
                self_ddos_backoff_base_seconds=runtime_config.get(
                    "self_ddos_backoff_base_seconds", 1.0
                ),
                self_ddos_backoff_max_seconds=runtime_config.get(
                    "self_ddos_backoff_max_seconds", 60.0
                ),
                self_ddos_backoff_jitter_factor=runtime_config.get(
                    "self_ddos_backoff_jitter_factor", 0.25
                ),
                rate_limit_distributed=runtime_config.get(
                    "rate_limit_distributed", False
                ),
            )
        except Exception:
            pass  # Fall through to static config

        # Fallback to static core config
        cb_settings = get_config().core.circuit_breaker
        return cls(
            enabled=cb_settings.enabled,
            failure_threshold=cb_settings.failure_threshold,
            recovery_timeout=cb_settings.recovery_timeout,
            success_threshold=cb_settings.success_threshold,
            minimum_calls=getattr(cb_settings, "minimum_calls", 10),
            sliding_window_size=getattr(cb_settings, "sliding_window_size", 100),
            failure_rate_threshold=getattr(cb_settings, "failure_rate_threshold", 50.0),
            fallback_strategy=getattr(cb_settings, "fallback_strategy", "cache"),
            fallback_cache_ttl_seconds=getattr(
                cb_settings, "fallback_cache_ttl_seconds", 300
            ),
            cb_open_burn_rate_multiplier=getattr(
                cb_settings, "cb_open_burn_rate_multiplier", 10.0
            ),
            cb_open_base_consumption_minutes=getattr(
                cb_settings, "cb_open_base_consumption_minutes", 1.0
            ),
            manual_override_ttl_minutes=getattr(
                cb_settings, "manual_override_ttl_minutes", 90
            ),
            half_open_max_calls=getattr(cb_settings, "half_open_max_calls", 3),
            max_pending_duration_hours=getattr(
                cb_settings, "max_pending_duration_hours", 4
            ),
            max_retry_lifetime_hours=getattr(
                cb_settings, "max_retry_lifetime_hours", 24
            ),
            rate_limit_cascade_threshold=cb_settings.rate_limit_cascade_threshold,
            rate_limit_cascade_window_seconds=cb_settings.rate_limit_cascade_window_seconds,
            rate_limit_cascade_rate=cb_settings.rate_limit_cascade_rate,
            rate_limit_cascade_minimum_calls=cb_settings.rate_limit_cascade_minimum_calls,
            self_ddos_protection_enabled=cb_settings.self_ddos_protection_enabled,
            self_ddos_rps_limit=cb_settings.self_ddos_rps_limit,
            self_ddos_window_seconds=cb_settings.self_ddos_window_seconds,
            self_ddos_backoff_multiplier=cb_settings.self_ddos_backoff_multiplier,
            self_ddos_backoff_base_seconds=cb_settings.self_ddos_backoff_base_seconds,
            self_ddos_backoff_max_seconds=cb_settings.self_ddos_backoff_max_seconds,
            self_ddos_backoff_jitter_factor=cb_settings.self_ddos_backoff_jitter_factor,
            rate_limit_distributed=cb_settings.rate_limit_distributed,
        )


# =============================================================================
# Companion-API Decision Type
# =============================================================================


@dataclass(frozen=True, slots=True)
class CircuitBreakerDecision:
    """Decision pair returned by ``CircuitBreakerService.should_allow_with_state``.

    Pairs the bool admit decision with the resolved state object so callers
    can branch on ``allowed`` and read ``state`` without re-fetching from the
    repository (closes the redundant ``get_or_create_state`` lookup that
    Cat 7A.3 microbenchmarks identified on the CB reject hot path).

    ``frozen=True, slots=True`` keeps allocation cost identical to a tuple
    while preserving named-attribute access at call sites.
    """

    allowed: bool
    state: CircuitBreakerStateData


# =============================================================================
# Fallback Result Types
# =============================================================================


@dataclass
class CircuitBreakerFallbackResult:
    """Result when circuit breaker provides a fallback response."""

    allowed: bool  # Whether the request should proceed
    fallback_used: bool = False  # Whether a fallback was used
    fallback_type: str = ""  # "cache", "dlq", "default", "none"
    fallback_data: Any = None  # Cached data or default response
    message: str = ""

    @classmethod
    def allow(cls) -> CircuitBreakerFallbackResult:
        """Request allowed to proceed normally."""
        return cls(allowed=True, fallback_used=False)

    @classmethod
    def block(
        cls, message: str = "Circuit breaker is open"
    ) -> CircuitBreakerFallbackResult:
        """Request blocked with no fallback."""
        return cls(allowed=False, fallback_used=False, message=message)

    @classmethod
    def from_cache(
        cls, data: Any, message: str = "Stale data from cache"
    ) -> CircuitBreakerFallbackResult:
        """Request served from cache (stale data)."""
        return cls(
            allowed=False,
            fallback_used=True,
            fallback_type="cache",
            fallback_data=data,
            message=message,
        )

    @classmethod
    def to_dlq(
        cls, message: str = "Request queued for later retry"
    ) -> CircuitBreakerFallbackResult:
        """Request queued to DLQ for later processing."""
        return cls(
            allowed=False,
            fallback_used=True,
            fallback_type="dlq",
            message=message,
        )

    @classmethod
    def default_response(
        cls, data: Any, message: str = "Default fallback response"
    ) -> CircuitBreakerFallbackResult:
        """Request served with a default/static response."""
        return cls(
            allowed=False,
            fallback_used=True,
            fallback_type="default",
            fallback_data=data,
            message=message,
        )


# =============================================================================
# Circuit Breaker Result
# =============================================================================


@dataclass
class CircuitBreakerResult:
    """Result of a circuit breaker operation."""

    success: bool
    service_name: str
    previous_state: str = ""
    new_state: str = ""
    message: str = ""
    error: str | None = None

    @classmethod
    def succeeded(
        cls,
        service_name: str,
        previous_state: str,
        new_state: str,
        message: str = "",
    ) -> CircuitBreakerResult:
        """Factory for successful operation."""
        return cls(
            success=True,
            service_name=service_name,
            previous_state=previous_state,
            new_state=new_state,
            message=message,
        )

    @classmethod
    def failed(cls, service_name: str, error: str) -> CircuitBreakerResult:
        """Factory for failed operation."""
        return cls(
            success=False,
            service_name=service_name,
            error=error,
        )
