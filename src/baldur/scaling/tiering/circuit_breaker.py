"""
Tiering Circuit Breaker.

Meta circuit breaker for the tiering engine itself.
When RegEx evaluation is slow or failing, bypass tiering and use static fallback.
"""

from __future__ import annotations

import threading
import time

import structlog

from baldur.interfaces.repositories import CircuitBreakerStateEnum

logger = structlog.get_logger()


class TieringCircuitBreaker:
    """
    Circuit Breaker for Tiering Engine itself.

    When RegEx evaluation is slow or failing, bypass tiering
    and use static fallback. Prevents tiering from becoming
    a performance bottleneck.

    Reference: Envoy Proxy routing bypass pattern
    """

    FAILURE_THRESHOLD = 5  # 5 consecutive failures → OPEN
    TIMEOUT_MS = 50  # 50ms → count as slow
    SLOW_THRESHOLD = 10  # 10 slow responses → OPEN
    HALF_OPEN_DELAY_SEC = 30  # 30s before trying again

    _instance: TieringCircuitBreaker | None = None
    _lock = threading.Lock()

    def __new__(cls) -> TieringCircuitBreaker:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._init()
                    cls._instance = instance
        return cls._instance

    def _init(self):
        """Initialize circuit breaker state."""
        self._state: str = CircuitBreakerStateEnum.CLOSED.value
        self._failure_count = 0
        self._slow_count = 0
        self._last_failure_time: float | None = None
        self._state_lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        """Check if circuit is open (tiering should be bypassed)."""
        with self._state_lock:
            if self._state == CircuitBreakerStateEnum.OPEN.value:
                if (
                    self._last_failure_time
                    and time.time() - self._last_failure_time > self.HALF_OPEN_DELAY_SEC
                ):
                    self._state = CircuitBreakerStateEnum.HALF_OPEN.value
                    logger.info("tiering_cb.transitioning")
                    return False
                return True
            return False

    @property
    def state(self) -> str:
        """Get current circuit breaker state."""
        with self._state_lock:
            return self._state

    def record_success(self, latency_ms: float):
        """Record successful tiering evaluation."""
        with self._state_lock:
            if self._state == CircuitBreakerStateEnum.HALF_OPEN.value:
                self._state = CircuitBreakerStateEnum.CLOSED.value
                logger.info("tiering_cb.closed_recovered")

            self._failure_count = 0

            if latency_ms > self.TIMEOUT_MS:
                self._slow_count += 1
                if self._slow_count >= self.SLOW_THRESHOLD:
                    self._trip(
                        f"slow_responses ({self._slow_count} > {self.SLOW_THRESHOLD})"
                    )
            else:
                self._slow_count = 0

    def record_failure(self, error: Exception):
        """Record failed tiering evaluation."""
        with self._state_lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._failure_count >= self.FAILURE_THRESHOLD:
                self._trip(f"failures: {error}")

    def _trip(self, reason: str):
        """Open the circuit breaker."""
        self._state = CircuitBreakerStateEnum.OPEN.value
        self._last_failure_time = time.time()
        logger.critical(
            "tiering_cb.open",
            reason=reason,
        )
        self._log_shadow_audit(reason)

    def _log_shadow_audit(self, reason: str):
        """Log circuit breaker trip to audit."""
        try:
            from baldur.audit import log_config_change

            log_config_change(
                config_type="tiering_circuit_breaker",
                config_key="circuit_state",
                old_value=CircuitBreakerStateEnum.CLOSED.value,
                new_value={
                    "state": CircuitBreakerStateEnum.OPEN.value,
                    "reason": reason,
                    "failure_count": self._failure_count,
                    "slow_count": self._slow_count,
                    "severity": "critical",
                    "tag": "TIERING_CB_OPEN",
                },
                user="system",
            )
        except Exception as e:
            logger.exception(
                "tiering_cb.shadow_audit_failed",
                error=e,
            )

    def reset(self):
        """Reset circuit breaker (for testing)."""
        with self._state_lock:
            self._state = CircuitBreakerStateEnum.CLOSED.value
            self._failure_count = 0
            self._slow_count = 0
            self._last_failure_time = None


def get_tiering_circuit_breaker() -> TieringCircuitBreaker:
    """Get singleton TieringCircuitBreaker instance."""
    return TieringCircuitBreaker()
