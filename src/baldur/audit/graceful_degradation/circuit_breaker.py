"""
Circuit Breaker for Hash Chain Operations.

Prevents cascading failures by stopping requests to a failing
Redis instance and allowing recovery time.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.resilience.circuit_breaker import CircuitBreakerBase

from .enums import CircuitState, HashChainCircuitBreakerConfig

if TYPE_CHECKING:
    from .degradation_manager import HashChainDegradationManager


logger = structlog.get_logger()


class HashChainCircuitBreaker(CircuitBreakerBase):
    """
    Circuit breaker for hash chain Redis operations (Sync).

    .. note::
        **Tier**: Dormant (compliance-grade enhancement, no standalone demand)
        **Status**: Not auto-wired. Available for custom integration engagements.
        PRO Audit (full) uses the basic file-based ``HashChainManager`` in
        ``audit/integrity/`` instead.

    Prevents cascading failures by stopping requests to a failing
    Redis instance and allowing recovery time.

    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Failures exceeded threshold, requests rejected
    - HALF_OPEN: Testing if Redis has recovered

    Usage:
        cb = HashChainCircuitBreaker()

        if cb.can_execute():
            try:
                result = redis_operation()
                cb.record_success()
            except Exception as e:
                cb.record_failure()
                raise
        else:
            result = fallback_operation()
    """

    def __init__(
        self,
        name: str = "hash_chain_redis",
        config: HashChainCircuitBreakerConfig | None = None,
        degradation_manager: HashChainDegradationManager | None = None,
    ):
        cfg = config or HashChainCircuitBreakerConfig()
        super().__init__(
            name=name,
            failure_threshold=cfg.failure_threshold,
            success_threshold=cfg.success_threshold,
            timeout_seconds=cfg.recovery_timeout_seconds,
        )
        self._config = cfg
        self._degradation_manager = degradation_manager
        # DR-5: Sync-only lock
        self._lock = threading.RLock()

        # Half-open request limiting
        self._half_open_requests = 0
        self._half_open_max_requests = cfg.half_open_requests

    # DR-5: Concurrency control — threading.RLock wrapping via
    # `_drains_state_changes` decorator (454 D4): wraps the body in
    # `with self._lock:` and drains `_on_state_changed` hooks after release.

    @property
    @CircuitBreakerBase._drains_state_changes
    def state(self) -> CircuitState:
        """Get current circuit state."""
        self._check_timeout_impl()
        return self._state

    @CircuitBreakerBase._drains_state_changes
    def can_execute(self) -> bool:
        return self._can_execute_impl()

    @CircuitBreakerBase._drains_state_changes
    def record_success(self) -> None:
        self._record_success_impl()

    @CircuitBreakerBase._drains_state_changes
    def record_failure(self, error: Exception | None = None) -> None:
        self._record_failure_impl()
        # The `degradation_manager.on_redis_failure` call remains inside the
        # held lock — separate hook-under-lock OOS (graceful_degradation has
        # different fix shape than the pure-observability `_on_state_changed`).
        if self._degradation_manager and self._state == CircuitState.OPEN:
            self._degradation_manager.on_redis_failure(error)

    @CircuitBreakerBase._drains_state_changes
    def force_open(self) -> None:
        """Force circuit to OPEN state."""
        self._transition_to(CircuitState.OPEN)

    @CircuitBreakerBase._drains_state_changes
    def force_closed(self) -> None:
        """Force circuit to CLOSED state."""
        self._transition_to(CircuitState.CLOSED)
        self._half_open_requests = 0

    def _on_open(self) -> None:
        """Notify degradation manager on OPEN (already called within lock)."""
        # Note: degradation_manager notification for record_failure is handled
        # in record_failure() itself to pass the error argument.

    def _on_close(self) -> None:
        if self._degradation_manager:
            self._degradation_manager.on_redis_recovery()

    def _can_attempt_half_open(self) -> bool:
        if self._half_open_requests < self._half_open_max_requests:
            self._half_open_requests += 1
            return True
        return False

    def _transition_to(self, new_state: CircuitState) -> None:
        super()._transition_to(new_state)
        if new_state == CircuitState.HALF_OPEN:
            self._half_open_requests = 0
        elif new_state == CircuitState.CLOSED:
            self._half_open_requests = 0
            self._on_close()

    # DR-6: Observability hook — AuditMetrics integration
    def _on_state_changed(self, old: CircuitState, new: CircuitState) -> None:
        from baldur.audit.resilience.metrics import AuditMetrics

        AuditMetrics.get_instance().set_circuit_state("redis_hashchain", new.value)

    def get_stats(self) -> dict[str, Any]:
        with self._lock:
            stats = super().get_stats()
            stats["config"] = {
                "failure_threshold": self._config.failure_threshold,
                "recovery_timeout_seconds": self._config.recovery_timeout_seconds,
                "half_open_requests": self._config.half_open_requests,
                "success_threshold": self._config.success_threshold,
            }
            return stats


__all__ = ["HashChainCircuitBreaker"]
