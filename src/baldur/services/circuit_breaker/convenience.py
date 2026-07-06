"""
Circuit Breaker Convenience Functions

Module-level convenience functions for common circuit breaker operations.
These provide a simpler API for common use cases.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from baldur.utils.singleton import make_singleton_factory

from .config import CircuitBreakerResult
from .service import CircuitBreakerService

if TYPE_CHECKING:
    pass


# =============================================================================
# Module-level convenience functions
# =============================================================================
#
# 450 Phase 4: the circuit-breaker convenience singleton now lives on the
# active ``BaldurRuntime`` via ``make_singleton_factory`` — same delegation
# path as the 67 other singletons migrated in Phase 2. Resetting the runtime
# (or calling ``reset_circuit_breaker_service()``) drops this instance
# atomically.

_factory_triple = make_singleton_factory(
    "circuit_breaker_service",
    CircuitBreakerService,
)
_get_fn, _configure_fn, _reset_fn = _factory_triple


def get_circuit_breaker_service() -> CircuitBreakerService:
    """Return the runtime-scoped ``CircuitBreakerService`` singleton.

    Delegates to the active :class:`~baldur.runtime.BaldurRuntime` —
    test isolation, ``copy_context()`` scoping, and runtime
    swap-in work transparently through the runtime's singleton store.

    Explicit-``def`` wrapper (instead of tuple-unpacking the
    ``make_singleton_factory`` return value directly) so the symbol is
    statically discoverable by docs tooling (mkdocstrings/griffe) and carries
    a Public-surface docstring per the reference page contract.
    """
    return _get_fn()


def configure_circuit_breaker_service(service: CircuitBreakerService) -> None:
    """Install ``service`` as the active singleton.

    Overrides whatever the default factory would have produced. Calls after
    this point see ``service`` until ``reset_circuit_breaker_service()`` runs.
    """
    _configure_fn(service)


def reset_circuit_breaker_service() -> None:
    """Drop the current singleton.

    The next ``get_circuit_breaker_service()`` call constructs a fresh
    instance via the registered factory.
    """
    _reset_fn()


def should_allow_request(service_name: str) -> bool:
    """
    Convenience function to check if requests should be allowed.

    Args:
        service_name: Name of the external service

    Returns:
        True if requests should be allowed
    """
    return get_circuit_breaker_service().should_allow(service_name)


def force_open_circuit(
    service_name: str,
    reason: str = "",
) -> CircuitBreakerResult:
    """
    Convenience function to force open a circuit breaker.

    Actor information is read from ActorContext (set by Django middleware
    or Celery task signal handlers).

    Args:
        service_name: Name of the external service
        reason: Reason for opening

    Returns:
        CircuitBreakerResult with operation outcome
    """
    return get_circuit_breaker_service().force_open(
        service_name=service_name,
        reason=reason,
    )


def force_close_circuit(
    service_name: str,
    reason: str = "",
    trigger_replay: bool = False,
) -> CircuitBreakerResult:
    """
    Convenience function to force close a circuit breaker.

    Actor information is read from ActorContext (set by Django middleware
    or Celery task signal handlers).

    Args:
        service_name: Name of the external service
        reason: Reason for closing
        trigger_replay: Whether to trigger conditional replay

    Returns:
        CircuitBreakerResult with operation outcome
    """
    return get_circuit_breaker_service().force_close(
        service_name=service_name,
        reason=reason,
        trigger_replay=trigger_replay,
    )


def record_rate_limit(service_name: str) -> CircuitBreakerResult | None:
    """
    Convenience function to record a 429 rate limit response.

    Call this when receiving a 429 response from an external service.
    If a rate limit cascade is detected, the circuit breaker will auto-open.

    Args:
        service_name: Name of the external service

    Returns:
        CircuitBreakerResult if circuit was opened, None otherwise
    """
    return get_circuit_breaker_service().record_rate_limit_response(service_name)


def should_allow_with_protection(service_name: str) -> tuple[bool, float]:
    """
    Convenience function to check if request should be allowed with self-DDoS protection.

    Args:
        service_name: Name of the external service

    Returns:
        Tuple of (should_allow, suggested_backoff_seconds)
    """
    return get_circuit_breaker_service().should_allow_with_ddos_protection(service_name)


def get_protection_status(service_name: str) -> dict[str, Any]:
    """
    Convenience function to get comprehensive protection status.

    Args:
        service_name: Name of the external service

    Returns:
        Dictionary with protection status details
    """
    return get_circuit_breaker_service().get_protection_status(service_name)
