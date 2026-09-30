"""Centralized DI fallback resolution for services.

When a service's ProviderRegistry lookup fails to construct its adapter
(``ImportError`` / ``ValueError``), the 3-tier FallbackPolicy decides what
happens:

- ALLOW: fall back to the in-memory adapter silently
- WARN_AND_ALLOW: fall back with a WARNING log + the ``di_fallback_total``
  counter
- FAIL_FAST: raise RuntimeError

The effective policy is the operator's ``FALLBACK_POLICY`` (the root setting
reads the unprefixed variable) when it is set, else ``WARN_AND_ALLOW`` in
production and ``ALLOW`` elsewhere. Production does not default to FAIL_FAST:
the circuit breaker's repository lookup runs on the ``protect()`` call path,
so a construction error would reach the caller, and the breaker is
fail-open. Production refuses an unconstructible selected backend at boot
instead (``baldur.init()`` constructs it); what remains here is announced.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

import structlog

if TYPE_CHECKING:
    from baldur.settings import FallbackPolicy

logger = structlog.get_logger(__name__)

T = TypeVar("T")


def resolve_with_fallback(
    registry_method: Callable[[], T],
    fallback_class: Callable[[], T],
    service_name: str,
) -> T:
    """Resolve an adapter via ProviderRegistry with policy-based fallback.

    Args:
        registry_method: Callable that returns the adapter from ProviderRegistry.
        fallback_class: Class or zero-arg factory used as fallback. Typed as
            Callable[[], T] (not ``type[T]``) so callers may pass a class whose
            constructor returns a *subtype* of T without tripping mypy's
            invariant ``type[T]`` checks (e.g. registry returns the broad
            Protocol, fallback_class returns the concrete InMemory impl).
        service_name: Name of the calling service (for logging/metrics).

    Returns:
        Adapter instance from ProviderRegistry, or fallback instance.

    Raises:
        RuntimeError: If the effective policy is FAIL_FAST and the adapter
            cannot be constructed.
    """
    try:
        return registry_method()
    except (ImportError, ValueError) as exc:
        from baldur.settings import FallbackPolicy

        policy = _effective_fallback_policy()

        if policy == FallbackPolicy.FAIL_FAST:
            raise RuntimeError(
                f"ProviderRegistry unavailable in production: {exc}"
            ) from exc

        instance = fallback_class()
        fallback_name = getattr(fallback_class, "__name__", repr(fallback_class))

        if policy == FallbackPolicy.WARN_AND_ALLOW:
            logger.warning(
                "service.fallback_adapter",
                adapter=fallback_name,
                service=service_name,
                error=str(exc),
            )
            _inc_fallback_metric(service_name, fallback_name)

        return instance


def _effective_fallback_policy() -> FallbackPolicy:
    """The operator's policy when set, else the environment's default.

    ``fallback_policy`` counts as set when it is in the root settings'
    ``model_fields_set`` — an env-sourced ``FALLBACK_POLICY`` is, the field
    default is not. Unset, production announces every fallback
    (WARN_AND_ALLOW) and other environments stay silent (ALLOW).
    """
    from baldur.runtime import is_production
    from baldur.settings import FallbackPolicy, get_config

    config = get_config()
    if "fallback_policy" in config.model_fields_set:
        return config.fallback_policy
    if is_production():
        return FallbackPolicy.WARN_AND_ALLOW
    return FallbackPolicy.ALLOW


def _inc_fallback_metric(service_name: str, adapter_name: str) -> None:
    """Increment the ``di_fallback_total`` counter through the metrics facade."""
    try:
        from baldur.metrics.prometheus import get_metrics

        get_metrics().record_di_fallback(service_name, adapter_name)
    except Exception as e:
        logger.debug("service.fallback_metric_record_failed", error=str(e))
