"""
Layered Configuration Provider.

Configuration priority (lowest → highest):
1. Hard-coded defaults (Pydantic Field default)
2. Static ENV (.env, environment variables)
3. Dynamic DB/Redis (RuntimeConfigManager)
4. Request-scoped override (per-request context)
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any, TypeVar

import structlog
from pydantic_settings import BaseSettings

logger = structlog.get_logger()

T = TypeVar("T", bound=BaseSettings)

# Hot-path snapshot TTL for get_layered_settings_cached (686 D3). Deliberately a
# module constant, NOT a BALDUR_* field: a new settings field would itself need a
# consumer audit, and the value bounds staleness below the affected domains' own
# default apply delays (idempotency DELAYED 30s, security 60s). Do not "harden"
# the cache below with a lock — see get_layered_settings_cached's docstring.
LAYERED_SETTINGS_CACHE_TTL_SECONDS = 30.0

# =============================================================================
# Level 4: Request-scoped overrides using contextvars
# =============================================================================
_request_overrides: ContextVar[dict[str, dict[str, Any]]] = ContextVar(
    "baldur_request_config_overrides",
    default={},  # noqa: B039
)


def set_request_override(config_type: str, overrides: dict[str, Any]) -> None:
    """
    Set request-scoped configuration overrides.

    Applies temporary settings valid only within the current request/context.
    Used from Django middleware or an async context.

    Args:
        config_type: Configuration type (e.g., "circuit_breaker", "retry")
        overrides: Dictionary of fields to override

    Example:
        set_request_override("circuit_breaker", {"failure_threshold": 10})
    """
    current = _request_overrides.get().copy()
    current[config_type] = overrides
    _request_overrides.set(current)
    logger.debug(
        "layered_provider.request_override_set",
        config_type=config_type,
        overrides=overrides,
    )


def get_request_override(config_type: str) -> dict[str, Any]:
    """
    Get the request-scoped overrides of the current context.

    Args:
        config_type: Configuration type

    Returns:
        Override dictionary (empty dict if none)
    """
    return _request_overrides.get().get(config_type, {})


def clear_request_overrides() -> None:
    """
    Clear all request-scoped overrides.

    Called from Django middleware's process_response for cleanup.
    """
    _request_overrides.set({})
    logger.debug("layered_provider.request_overrides_cleared")


def get_all_request_overrides() -> dict[str, dict[str, Any]]:
    """
    Get all request-scoped overrides.

    Returns:
        {config_type: {field: value}} dictionary
    """
    return _request_overrides.get().copy()


# =============================================================================
# Layered Settings Provider
# =============================================================================


def get_layered_settings(
    settings_class: type[T],
    config_type: str,
    include_runtime: bool = True,
    include_request: bool = True,
) -> T:
    """
    Load layered settings.

    Priority: Hard-coded < ENV < DB/Redis < Request Override

    Args:
        settings_class: Pydantic Settings class
        config_type: RuntimeConfigManager config type (e.g., "circuit_breaker")
        include_runtime: Whether to include Level 3 (DB/Redis)
        include_request: Whether to include Level 4 (Request override)

    Returns:
        Merged Settings instance

    Example:
        from baldur.settings import CircuitBreakerSettings

        # Apply the full layer stack
        settings = get_layered_settings(CircuitBreakerSettings, "circuit_breaker")

        # Up to ENV only (for tests)
        settings = get_layered_settings(
            CircuitBreakerSettings, "circuit_breaker",
            include_runtime=False, include_request=False
        )
    """
    # Level 1 + 2: Pydantic defaults + ENV (handled automatically by BaseSettings)
    base_settings = settings_class()
    base_dict = base_settings.model_dump()

    # Level 3: DB/Redis (RuntimeConfigManager)
    if include_runtime:
        try:
            from baldur.factory.registry import ProviderRegistry

            manager = ProviderRegistry.runtime_config_manager.safe_get()
            if manager is None:
                raise RuntimeError("baldur_pro RuntimeConfigManager not registered")
            runtime_config = manager._get_config(config_type)

            # Merge only valid fields
            valid_fields = set(base_dict.keys())
            for key, value in runtime_config.items():
                if key in valid_fields:
                    base_dict[key] = value

            logger.debug(
                "layered_provider.merged_runtime_config",
                config_type=config_type,
            )
        except Exception as e:
            # Graceful fallback - works without a RuntimeConfigManager
            logger.debug(
                "layered_provider.runtime_config_available",
                error=e,
            )

    # Level 4: Request-scoped override
    if include_request:
        request_overrides = get_request_override(config_type)
        if request_overrides:
            valid_fields = set(base_dict.keys())
            for key, value in request_overrides.items():
                if key in valid_fields:
                    base_dict[key] = value
            logger.debug(
                "layered_provider.applied_request_overrides",
                config_type=config_type,
                override_keys=list(request_overrides.keys()),
            )

    # Build a new instance from the merged values
    return settings_class.model_validate(base_dict)


# =============================================================================
# Hot-path cached layered snapshot (686 D3)
# =============================================================================
#
# Per-process TTL cache keyed by config_type. Request-rate consumers (idempotency
# gate/decorator/policies, HTTP-metrics record-gates) cannot pay ~3 Pydantic
# validations per call at the 500-5K RPS PRO baseline; a 30s snapshot bounds
# staleness below the affected domains' own default apply delays.

# {config_type: (settings_value, monotonic_expiry)} — see the module TTL comment.
_layered_settings_cache: dict[str, tuple[Any, float]] = {}


def get_layered_settings_cached(
    settings_class: type[T],
    config_type: str,
) -> T:
    """
    Load layered settings via a per-process TTL snapshot (hot-path variant).

    Same result as :func:`get_layered_settings` (full layering, manager overlay,
    env fallback) but caches the merged instance per ``config_type`` for
    ``LAYERED_SETTINGS_CACHE_TTL_SECONDS``. Use only at request-rate sites where a
    direct layered read's per-call Pydantic-validation cost is an unacceptable
    per-op tax; low/mid-cadence sites call :func:`get_layered_settings` directly.

    The cache is intentionally lock-free. The underlying hot read is in-process
    (the manager returns its per-process cache with no backend round-trip), and
    get/set of the ``{config_type: (value, expiry)}`` entry is GIL-atomic, so the
    worst case under concurrency is a rare redundant recompute, never corruption
    or a stale write. A lock (sync or asyncio) is deliberately NOT added: at the
    hot path it would introduce contention worse than the recompute it prevents,
    there is no backend to stampede, and the sync path has no await point where an
    event loop could interleave a partial update. Do not add one.
    """
    now = time.monotonic()
    entry = _layered_settings_cache.get(config_type)
    if entry is not None and entry[1] > now:
        return entry[0]

    value = get_layered_settings(settings_class, config_type)
    _layered_settings_cache[config_type] = (
        value,
        now + LAYERED_SETTINGS_CACHE_TTL_SECONDS,
    )
    return value


def reset_layered_settings_cached() -> None:
    """Clear the hot-path layered snapshot cache (get/reset pair; testing)."""
    _layered_settings_cache.clear()


def detect_config_source(
    settings_class: type[T],
    config_type: str,
    field_name: str,
) -> str:
    """
    Detect where a configuration value came from.

    Args:
        settings_class: Pydantic Settings class
        config_type: Configuration type
        field_name: Field name

    Returns:
        Source string: "DEFAULT", "ENV", "RUNTIME", "REQUEST"
    """
    import os

    # Level 4: Request override
    request_overrides = get_request_override(config_type)
    if field_name in request_overrides:
        return "REQUEST"

    # Level 3: Runtime (DB/Redis)
    try:
        from baldur.factory.registry import ProviderRegistry

        manager = ProviderRegistry.runtime_config_manager.safe_get()
        if manager is None:
            raise RuntimeError("baldur_pro RuntimeConfigManager not registered")
        # PRO impl exposes a per-section `_cache`; duck-type so the Protocol
        # stays free of private cache details.
        cache = getattr(manager, "_cache", {})
        runtime_config = cache.get(config_type, {}) if isinstance(cache, dict) else {}
        if field_name in runtime_config:
            return "RUNTIME"
    except Exception:
        pass

    # Level 2: Environment variable
    # Check the Pydantic BaseSettings env_prefix
    model_config = getattr(settings_class, "model_config", {})
    env_prefix = model_config.get("env_prefix", "BALDUR_")
    env_key = f"{env_prefix}{field_name.upper()}"
    if env_key in os.environ:
        return "ENV"

    # Level 1: Default
    return "DEFAULT"


def get_config_with_sources(
    settings_class: type[T],
    config_type: str,
) -> dict[str, dict[str, Any]]:
    """
    Return every configuration value together with its source.

    Args:
        settings_class: Pydantic Settings class
        config_type: Configuration type

    Returns:
        {field_name: {"value": ..., "source": ...}} dictionary

    Example:
        >>> info = get_config_with_sources(CircuitBreakerSettings, "circuit_breaker")
        >>> info["failure_threshold"]
        {"value": 5, "source": "DEFAULT"}
    """
    settings = get_layered_settings(settings_class, config_type)
    result = {}

    for field_name in settings.model_fields:
        value = getattr(settings, field_name)
        source = detect_config_source(settings_class, config_type, field_name)
        result[field_name] = {
            "value": value,
            "source": source,
        }

    return result


# =============================================================================
# Context Manager for Request Override
# =============================================================================


class RequestOverrideContext:
    """
    Context manager for request overrides.

    Overrides are cleaned up automatically when the with block exits.

    Example:
        with RequestOverrideContext("circuit_breaker", {"failure_threshold": 10}):
            # Inside this block failure_threshold is 10
            settings = get_layered_settings(CircuitBreakerSettings, "circuit_breaker")
            assert settings.failure_threshold == 10
        # Restored to the original value after the block exits
    """

    def __init__(self, config_type: str, overrides: dict[str, Any]):
        self.config_type = config_type
        self.overrides = overrides
        self._previous: dict[str, dict[str, Any]] | None = None

    def __enter__(self) -> RequestOverrideContext:
        self._previous = get_all_request_overrides()
        set_request_override(self.config_type, self.overrides)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # Restore the previous state
        if self._previous is not None:
            _request_overrides.set(self._previous)
        else:
            clear_request_overrides()
        return


# =============================================================================
# Convenience functions for common config types
# =============================================================================


def get_circuit_breaker_layered():
    """Load CircuitBreakerSettings through the layered provider."""
    from baldur.settings.circuit_breaker import CircuitBreakerSettings

    return get_layered_settings(CircuitBreakerSettings, "circuit_breaker")


def get_retry_layered():
    """Load RetrySettings through the layered provider."""
    from baldur.settings.retry import RetrySettings

    return get_layered_settings(RetrySettings, "retry")


def get_dlq_layered():
    """Load DLQSettings through the layered provider."""
    from baldur.settings.dlq import DLQSettings

    return get_layered_settings(DLQSettings, "dlq")


def get_rate_limit_layered():
    """Load RateLimitSettings through the layered provider."""
    from baldur.settings.rate_limit import RateLimitSettings

    return get_layered_settings(RateLimitSettings, "rate_limit")
