"""
Protection Mixin for Circuit Breaker Service

Provides rate limit cascade detection and self-DDoS protection functionality.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from baldur.core.execution_mode import intervention_suppressed

from .rate_limit_tracker import get_rate_limit_tracker

if TYPE_CHECKING:
    from collections.abc import Callable

    from baldur.services.circuit_breaker import CircuitBreakerResult

    from .config import CircuitBreakerConfig

logger = structlog.get_logger()

# Defensive floor for adaptive backoff. Unreachable at defaults (min possible
# delay = base x (1 - jitter_factor) = 0.75s), but preserves the historical
# 0.1s lower bound if an operator sets base/jitter to near-zero values.
_MIN_BACKOFF_SECONDS = 0.1


class ProtectionMixin:
    """
    Mixin class providing protection functionality for CircuitBreakerService.

    Includes:
    - Rate limit cascade detection
    - Self-DDoS protection
    - Adaptive backoff calculation
    """

    if TYPE_CHECKING:
        # Host contract — supplied by CircuitBreakerService (the composing
        # class). is_enabled is a property on the host; should_allow/
        # get_state/force_open are methods. Declared here so type checkers
        # see Mixin self.X access; runtime resolution flows through MRO.
        config: CircuitBreakerConfig
        is_enabled: bool
        should_allow: Callable[..., bool]
        get_state: Callable[..., str]
        force_open: Callable[..., CircuitBreakerResult]

    # =========================================================================
    # Rate Limit Cascade Detection
    # =========================================================================

    def record_rate_limit_response(
        self, service_name: str
    ) -> CircuitBreakerResult | None:
        """
        Record a 429 rate limit response and check for cascade.

        Call this method when receiving a 429 response from an external service.
        If a rate limit cascade is detected (too many 429s in a short window),
        the circuit breaker will automatically open to prevent self-DDoS.

        Args:
            service_name: Name of the external service

        Returns:
            CircuitBreakerResult if circuit was opened, None otherwise
        """
        if not self.is_enabled:
            return None

        tracker = get_rate_limit_tracker()
        tracker.record_rate_limit(service_name)
        # A 429 implies a request was made — record it so the hybrid cascade
        # rate calculation works even when self-DDoS protection is disabled.
        tracker.record_request(service_name)

        # Hybrid cascade condition: absolute floor AND minimum sample AND rate threshold
        window = self.config.rate_limit_cascade_window_seconds
        rate_limit_count = tracker.get_rate_limit_count(service_name, window)
        total_requests = tracker.get_request_count(service_name, window)

        cascade_detected = (
            rate_limit_count >= self.config.rate_limit_cascade_threshold
            and total_requests >= self.config.rate_limit_cascade_minimum_calls
            and (rate_limit_count / total_requests)
            >= (self.config.rate_limit_cascade_rate / 100)
        )

        if cascade_detected:
            rate_percent = rate_limit_count / total_requests * 100
            logger.warning(
                "circuit_breaker.rate_limit_cascade_detected",
                service_name=service_name,
                rate_limit_count=rate_limit_count,
                total_requests=total_requests,
                rate_percent=round(rate_percent, 2),
                window_seconds=window,
            )

            # Observe-only (dry-run / shadow / evaluation): suppress the
            # automatic 429 force-open. The gate sits at this call site, NOT
            # inside force_open, so the manual force path stays live. The 429
            # tracking above is observation and still runs.
            if intervention_suppressed(
                service_name=service_name,
                action="rate_limit_force_open",
                rate_limit_count=rate_limit_count,
                total_requests=total_requests,
            ):
                return None

            # Auto-open circuit breaker
            result = self.force_open(
                service_name=service_name,
                reason=f"Rate limit cascade detected ({rate_limit_count}/{total_requests} "
                f"= {rate_percent:.1f}% in {window}s)",
            )

            if result.success:
                tracker.increment_backoff(service_name)
                logger.warning(
                    "circuit_breaker.auto_opened_circuit_due",
                    service_name=service_name,
                )

            return result

        return None

    def check_rate_limit_cascade(self, service_name: str) -> bool:
        """
        Check if a rate limit cascade is occurring for a service.

        Args:
            service_name: Name of the external service

        Returns:
            True if cascade is detected, False otherwise
        """
        tracker = get_rate_limit_tracker()
        window = self.config.rate_limit_cascade_window_seconds
        rate_limit_count = tracker.get_rate_limit_count(service_name, window)
        total_requests = tracker.get_request_count(service_name, window)

        return (
            rate_limit_count >= self.config.rate_limit_cascade_threshold
            and total_requests >= self.config.rate_limit_cascade_minimum_calls
            and (rate_limit_count / total_requests)
            >= (self.config.rate_limit_cascade_rate / 100)
        )

    # =========================================================================
    # Self-DDoS Protection
    # =========================================================================

    def should_allow_with_ddos_protection(
        self, service_name: str
    ) -> tuple[bool, float]:
        """
        Check if request should be allowed with self-DDoS protection.

        This method combines circuit breaker check with self-DDoS protection.
        If the request rate is too high, it returns a suggested backoff delay.

        Args:
            service_name: Name of the external service

        Returns:
            Tuple of (should_allow, suggested_backoff_seconds)
            - If should_allow is False, the request should be blocked
            - suggested_backoff_seconds indicates how long to wait before retry
        """
        # First, check standard circuit breaker
        if not self.should_allow(service_name):
            backoff = self.calculate_adaptive_backoff(service_name)
            return False, backoff

        # Check self-DDoS protection
        if not self.config.self_ddos_protection_enabled:
            return True, 0.0

        tracker = get_rate_limit_tracker()
        tracker.record_request(service_name)

        request_count = tracker.get_request_count(
            service_name, self.config.self_ddos_window_seconds
        )
        rps = request_count / self.config.self_ddos_window_seconds

        if rps > self.config.self_ddos_rps_limit:
            backoff = self.calculate_adaptive_backoff(service_name)
            logger.warning(
                "circuit_breaker.self_ddos_protection_triggered",
                service_name=service_name,
                current_rps=round(rps, 1),
                rps_limit=self.config.self_ddos_rps_limit,
                window_seconds=self.config.self_ddos_window_seconds,
                backoff=backoff,
            )
            return True, backoff  # Allow but suggest delay

        return True, 0.0

    def calculate_adaptive_backoff(self, service_name: str) -> float:
        """
        Calculate adaptive backoff delay based on current conditions.

        Uses exponential backoff with jitter to prevent thundering herd.

        Args:
            service_name: Name of the external service

        Returns:
            Backoff delay in seconds
        """
        from baldur.core.backoff import ExponentialBackoff

        tracker = get_rate_limit_tracker()
        backoff_level = tracker.get_backoff_level(service_name)

        # Reuse the settings-backed exponential-backoff-with-jitter strategy
        # instead of an inline reimplementation. ExponentialBackoff.calculate
        # uses attempt-1 as the exponent, so pass backoff_level + 1 to keep the
        # historical base x multiplier^level growth.
        strategy = ExponentialBackoff(
            base_delay=self.config.self_ddos_backoff_base_seconds,
            max_delay=self.config.self_ddos_backoff_max_seconds,
            multiplier=self.config.self_ddos_backoff_multiplier,
            jitter=True,
            jitter_factor=self.config.self_ddos_backoff_jitter_factor,
        )
        return max(_MIN_BACKOFF_SECONDS, strategy.calculate(backoff_level + 1))

    def reset_backoff(self, service_name: str) -> None:
        """
        Reset backoff level for a service after successful recovery.

        Call this after a service has recovered to reset adaptive backoff.

        Args:
            service_name: Name of the external service
        """
        tracker = get_rate_limit_tracker()
        tracker.reset_backoff(service_name)
        logger.info(
            "circuit_breaker.reset_backoff_level",
            service_name=service_name,
        )

    def is_self_ddos_detected(self, service_name: str) -> bool:
        """
        Check if self-DDoS conditions are detected for a service.

        Args:
            service_name: Name of the external service

        Returns:
            True if self-DDoS is detected, False otherwise
        """
        if not self.config.self_ddos_protection_enabled:
            return False

        tracker = get_rate_limit_tracker()
        request_count = tracker.get_request_count(
            service_name, self.config.self_ddos_window_seconds
        )
        rps = request_count / self.config.self_ddos_window_seconds
        return rps > self.config.self_ddos_rps_limit

    def get_protection_status(self, service_name: str) -> dict[str, Any]:
        """
        Get comprehensive protection status for a service.

        Returns:
            Dictionary with protection status details
        """
        tracker = get_rate_limit_tracker()

        cascade_window = self.config.rate_limit_cascade_window_seconds
        rate_limit_count = tracker.get_rate_limit_count(service_name, cascade_window)
        total_requests = tracker.get_request_count(service_name, cascade_window)

        ddos_window = self.config.self_ddos_window_seconds
        request_count = tracker.get_request_count(service_name, ddos_window)

        return {
            "service_name": service_name,
            "circuit_state": self.get_state(service_name),
            "circuit_breaker_enabled": self.is_enabled,
            "rate_limit_cascade": {
                "detected": self.check_rate_limit_cascade(service_name),
                "count_in_window": rate_limit_count,
                "total_requests_in_window": total_requests,
                "rate_percent": (
                    (rate_limit_count / total_requests * 100)
                    if total_requests > 0
                    else 0.0
                ),
                "threshold": self.config.rate_limit_cascade_threshold,
                "rate_threshold_percent": self.config.rate_limit_cascade_rate,
                "minimum_calls": self.config.rate_limit_cascade_minimum_calls,
                "window_seconds": cascade_window,
            },
            "self_ddos_protection": {
                "enabled": self.config.self_ddos_protection_enabled,
                "detected": self.is_self_ddos_detected(service_name),
                "request_count_in_window": request_count,
                "current_rps": request_count / ddos_window,
                "rps_limit": self.config.self_ddos_rps_limit,
                "window_seconds": ddos_window,
            },
            "backoff": {
                "current_level": tracker.get_backoff_level(service_name),
                "suggested_delay_seconds": self.calculate_adaptive_backoff(
                    service_name
                ),
            },
        }
