"""
Circuit Breaker Configuration and Types

Contains configuration dataclass, state constants, and result types
for circuit breaker operations.

The configuration a default-constructed :class:`CircuitBreakerService` reads is
process-shared: :func:`current_circuit_breaker_config` owns the single instance
and :func:`invalidate_circuit_breaker_config` swaps in a rebuilt one. Services
therefore never own (and never rebuild) a config of their own, which keeps the
config-source lookup off every request path and lets one invalidation reach
every default-config instance at once.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

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
    from datetime import datetime

    from baldur.interfaces.repositories import CircuitBreakerStateData

logger = structlog.get_logger()

__all__ = [
    "CircuitState",
    "CircuitBreakerConfig",
    "CircuitBreakerDecision",
    "CircuitBreakerFallbackResult",
    "CircuitBreakerResult",
    "current_circuit_breaker_config",
    "invalidate_circuit_breaker_config",
    "reset_circuit_breaker_config",
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

    # Calls the outcome window must hold before the rate trigger is evaluated
    # (prevents false positives with low traffic). Gates the rate trigger only —
    # the consecutive-failure count trigger is traffic-independent.
    minimum_calls: int = 10

    # Outcome window for the rate-based trigger (used when failure_rate_threshold > 0)
    sliding_window_size: int = 100  # Recent CLOSED calls the rate is computed over
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
    # Seconds after which a HALF_OPEN window at its call limit is treated as
    # stuck (the worker holding the trial slot died) and auto-reset on the next
    # slot acquisition. Matches the settings default.
    half_open_stuck_timeout_seconds: int = 60
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
        """Load configuration from RuntimeConfigManager (preferred) or core config.

        This is the single admission point for every circuit-breaker consumer:
        both source branches funnel through :func:`_admit_config_values`, so a
        value that would disable protection cannot reach the protection logic
        no matter which source it came from.
        """
        # Try RuntimeConfigManager first (runtime-configurable)
        try:
            from baldur.factory.registry import ProviderRegistry

            manager = ProviderRegistry.runtime_config_manager.safe_get()
            if manager is None:
                raise RuntimeError("baldur_pro RuntimeConfigManager not registered")
            runtime_config = manager.get_circuit_breaker_config()

            return cls._admitted(
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
                half_open_stuck_timeout_seconds=runtime_config.get(
                    "half_open_stuck_timeout_seconds", 60
                ),
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
        return cls._admitted(
            enabled=cb_settings.enabled,
            failure_threshold=cb_settings.failure_threshold,
            recovery_timeout=cb_settings.recovery_timeout,
            success_threshold=cb_settings.success_threshold,
            minimum_calls=cb_settings.minimum_calls,
            sliding_window_size=cb_settings.sliding_window_size,
            failure_rate_threshold=cb_settings.failure_rate_threshold,
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
            half_open_stuck_timeout_seconds=getattr(
                cb_settings, "half_open_stuck_timeout_seconds", 60
            ),
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

    @classmethod
    def _admitted(cls, **values: Any) -> CircuitBreakerConfig:
        """Build the config from ``values`` after the admission clamp."""
        return cls(**_admit_config_values(values))


# =============================================================================
# Config Admission — derived bounds
# =============================================================================
#
# A value stored through the runtime-config write path runs no Pydantic
# validator, so a window/count field can hold a 0 (or an unreachably large
# number) that silently disables a protection trigger on every worker that
# builds a config from it. The clamp below is the last gate before such a value
# reaches the protection logic.
#
# The bounds are DERIVED from CircuitBreakerSettings' own field declarations,
# never authored here: the range an operator can already set through
# BALDUR_CB_* is exactly the range admitted, so the clamp cannot narrow a
# legal configuration. A field that declares no bound is passed through
# unchanged, and a stored out-of-range value is still stored and still shown in
# the console — it simply cannot reach the breaker.

# Clamps already reported, so a repeated rebuild of the same out-of-range value
# costs one WARNING per (field, stored, applied) triple per process rather than
# one per config build.
_clamp_warned: set[tuple[str, Any, Any]] = set()
_clamp_warned_lock = threading.Lock()


def _declared_bounds() -> dict[str, tuple[Any, Any, Any]]:
    """Map each bounded settings field to ``(lower, upper, python_type)``.

    Only inclusive bounds (``ge`` / ``le``) are resolved. An exclusive bound
    (``gt`` / ``lt``) names no admissible value of its own, so a field that
    declares one is left unclamped rather than clamped to a value the settings
    layer would itself reject. No circuit-breaker field declares one today.
    """
    from annotated_types import Ge, Le

    from baldur.settings.circuit_breaker import CircuitBreakerSettings

    bounds: dict[str, tuple[Any, Any, Any]] = {}
    for name, field in CircuitBreakerSettings.model_fields.items():
        lower = upper = None
        for constraint in field.metadata:
            if isinstance(constraint, Ge):
                lower = constraint.ge
            elif isinstance(constraint, Le):
                upper = constraint.le
        if lower is None and upper is None:
            continue
        bounds[name] = (lower, upper, field.annotation)
    return bounds


def _admit_config_values(values: dict[str, Any]) -> dict[str, Any]:
    """Clamp every bounded numeric value into its declared range.

    Returns a new dict; the input is not mutated. Also emits the report-only
    cross-field warning for ``minimum_calls > sliding_window_size``, which
    makes the failure-rate trigger unreachable — the settings layer warns about
    that combination but the runtime-config write path never runs its
    validators.
    """
    admitted = dict(values)

    for name, (lower, upper, python_type) in _declared_bounds().items():
        if name not in admitted:
            continue
        stored = admitted[name]
        # bool is an int subclass; a flag has no range to clamp into.
        if isinstance(stored, bool) or not isinstance(stored, (int, float)):
            continue

        applied = stored
        if lower is not None and applied < lower:
            applied = lower
        if upper is not None and applied > upper:
            applied = upper
        if applied == stored:
            continue

        if python_type is int:
            applied = int(applied)
        elif python_type is float:
            applied = float(applied)

        admitted[name] = applied
        _warn_clamped(name, stored, applied)

    minimum_calls = admitted.get("minimum_calls")
    window_size = admitted.get("sliding_window_size")
    if (
        isinstance(minimum_calls, int)
        and isinstance(window_size, int)
        and minimum_calls > window_size
    ):
        logger.warning(
            "circuit_breaker.config_rate_trigger_unreachable",
            minimum_calls=minimum_calls,
            sliding_window_size=window_size,
            remedy=(
                "the outcome window never holds more calls than "
                "sliding_window_size, so the failure-rate trigger is never "
                "evaluated: lower minimum_calls or raise sliding_window_size"
            ),
        )

    return admitted


def _warn_clamped(field_name: str, stored: Any, applied: Any) -> None:
    """Report one admission clamp, at most once per distinct triple."""
    key = (field_name, stored, applied)
    with _clamp_warned_lock:
        if key in _clamp_warned:
            return
        _clamp_warned.add(key)

    logger.warning(
        "circuit_breaker.config_value_clamped",
        field=field_name,
        stored_value=stored,
        applied_value=applied,
        remedy=(
            "the stored value is outside the range this field declares; the "
            "breaker runs the clamped value until the stored one is corrected"
        ),
    )


# =============================================================================
# Process-Shared Config Holder
# =============================================================================

_current_config: CircuitBreakerConfig | None = None
_current_config_lock = threading.Lock()


def current_circuit_breaker_config() -> CircuitBreakerConfig:
    """Return the process-shared circuit-breaker configuration.

    Every default-constructed service reads this, so a single invalidation
    reaches all of them. ``baldur.init()`` seeds the holder, which is what keeps
    the build (and the config-source lock it takes) off the first request; the
    lazy build below is the fallback for a process that never calls ``init()``.
    """
    global _current_config

    config = _current_config
    if config is not None:
        return config

    with _current_config_lock:
        if _current_config is None:
            _current_config = CircuitBreakerConfig.from_settings()
        return _current_config


def invalidate_circuit_breaker_config() -> CircuitBreakerConfig | None:
    """Rebuild the shared configuration and swap it in, on the calling thread.

    Eager by design: a lazy rebuild would move the config-source read onto
    whichever request thread happens to read first, where it can block behind an
    administrative write. Returns the configuration now in force — the previous
    one when the rebuild failed, so a transient source failure never leaves the
    process without a configuration.
    """
    global _current_config

    try:
        rebuilt = CircuitBreakerConfig.from_settings()
    except Exception as e:
        logger.warning(
            "circuit_breaker.config_rebuild_failed",
            error=str(e),
        )
        return _current_config

    with _current_config_lock:
        _current_config = rebuilt
    return rebuilt


def reset_circuit_breaker_config() -> None:
    """Drop the shared configuration — test isolation only.

    The next read rebuilds it lazily. Production code invalidates (which
    rebuilds eagerly) rather than resetting.
    """
    global _current_config

    with _current_config_lock:
        _current_config = None
    with _clamp_warned_lock:
        _clamp_warned.clear()


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
    expires_at: datetime | None = None
    """When the manual override this operation created lifts.

    Read back from storage after the write, so a caller reporting an expiry to
    an operator reports the one that is actually stored rather than recomputing
    it. ``None`` means either no manual override was created or the read-back
    was unavailable — in both cases the caller must not promise an expiry.
    """

    @classmethod
    def succeeded(
        cls,
        service_name: str,
        previous_state: str,
        new_state: str,
        message: str = "",
        expires_at: datetime | None = None,
    ) -> CircuitBreakerResult:
        """Factory for successful operation."""
        return cls(
            success=True,
            service_name=service_name,
            previous_state=previous_state,
            new_state=new_state,
            message=message,
            expires_at=expires_at,
        )

    @classmethod
    def failed(cls, service_name: str, error: str) -> CircuitBreakerResult:
        """Factory for failed operation."""
        return cls(
            success=False,
            service_name=service_name,
            error=error,
        )
