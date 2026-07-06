"""Centralized DI fallback resolution for services.

Implements the 3-tier FallbackPolicy:
- ALLOW: Silent fallback to in-memory adapter (dev/test)
- WARN_AND_ALLOW: Fallback with warning log + Prometheus metric (staging)
- FAIL_FAST: Raise RuntimeError for K8s pod restart (production)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import structlog

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
        RuntimeError: If policy is FAIL_FAST and ProviderRegistry is unavailable.
    """
    try:
        return registry_method()
    except (ImportError, ValueError) as exc:
        from baldur.settings import FallbackPolicy, get_config

        policy = get_config().fallback_policy

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
            )
            _inc_fallback_metric(service_name, fallback_name)

        return instance


def _inc_fallback_metric(service_name: str, adapter_name: str) -> None:
    """Increment di_fallback_total Prometheus counter."""
    try:
        from baldur.metrics.prometheus import get_metrics

        metrics = get_metrics()
        if hasattr(metrics, "di_fallback_total"):
            metrics.di_fallback_total.labels(
                service=service_name,
                adapter=adapter_name,
            ).inc()
    except Exception:
        pass
