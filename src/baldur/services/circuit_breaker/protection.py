"""
Protection Mixin for Circuit Breaker Service

Provides rate limit cascade detection and self-DDoS protection functionality.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from baldur.core.decision_logger import ReasonCode, log_intervention_evaluated
from baldur.core.execution_mode import intervention_suppressed

from .config import CircuitBreakerResult
from .manual_control import is_manual_pin_active
from .rate_limit_tracker import get_rate_limit_tracker

if TYPE_CHECKING:
    from collections.abc import Callable

    from baldur.interfaces.repositories import (
        CircuitBreakerOpenAttempt,
        CircuitBreakerStateData,
    )

    from .config import CircuitBreakerConfig

logger = structlog.get_logger()

# Defensive floor for adaptive backoff. Unreachable at defaults (min possible
# delay = base x (1 - jitter_factor) = 0.75s), but preserves the historical
# 0.1s lower bound if an operator sets base/jitter to near-zero values.
_MIN_BACKOFF_SECONDS = 0.1

# Label carried by the OPEN event and the audit reason when a 429 storm, rather
# than a failure burst, is what tripped the breaker.
RATE_LIMIT_CASCADE_TRIGGER = "rate_limit_cascade"


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
        # get_state/_trip_circuit_open are methods. Declared here so type
        # checkers see Mixin self.X access; runtime resolution flows through MRO.
        config: CircuitBreakerConfig
        is_enabled: bool
        should_allow: Callable[..., bool]
        get_state: Callable[..., str]
        get_or_create_state: Callable[..., CircuitBreakerStateData]
        get_effective_config: Callable[..., CircuitBreakerConfig]
        get_window_evidence: Callable[..., tuple[int, int]]
        _auto_transition_allowed: Callable[..., bool]
        _trip_circuit_open: Callable[..., CircuitBreakerOpenAttempt]

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

        This is the single writer of the tracker's 429 counter: every
        observation site reaches it through here, so no downstream answer is
        counted twice by two writers disagreeing about who owns it. The
        matching *request* — the cascade rate's denominator — is written by the
        observation site, not here: a site that already counted the call it
        made would otherwise have it counted twice, capping the rate at 50%
        in a pure storm and putting the top half of the setting's range out of
        reach.

        Args:
            service_name: Name of the external service

        Returns:
            CircuitBreakerResult if circuit was opened, None otherwise
        """
        if not self.is_enabled:
            return None

        # Resolve the shared config once — every threshold this decision uses
        # must come from the same snapshot as the window it is measured over,
        # and the same snapshot is what the trip primitive records the decision
        # against. The effective config overrides only the failure-threshold
        # fields, so every cascade field on it equals the base config's.
        cfg = self.get_effective_config(service_name)

        tracker = get_rate_limit_tracker()
        tracker.record_rate_limit(service_name)

        # Hybrid cascade condition: absolute floor AND minimum sample AND rate threshold
        window = cfg.rate_limit_cascade_window_seconds
        rate_limit_count = tracker.get_rate_limit_count(service_name, window)
        total_requests = tracker.get_request_count(service_name, window)

        # ``total_requests > 0`` guards the division, which the minimum-sample
        # term no longer implies: the denominator is the observation site's to
        # write, so a caller that reports a 429 without one leaves it at zero.
        cascade_detected = (
            rate_limit_count >= cfg.rate_limit_cascade_threshold
            and total_requests >= cfg.rate_limit_cascade_minimum_calls
            and total_requests > 0
            and (rate_limit_count / total_requests)
            >= (cfg.rate_limit_cascade_rate / 100)
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

            # An operator's live Block/Allow outranks the automatic verdict.
            # Without this the cascade would replace an Allow with a Block, or
            # restamp a live Block with a TTL nobody typed. Every sibling
            # automatic path already yields to the pin — the record paths skip
            # on it, recovery transitions filter it out, and the expiry sweep
            # leaves a due lift to admission — so this was an asymmetry, not a
            # design. Like the observe-only gate below, it sits at this call
            # site and not inside the trip primitive, which the failure
            # triggers share. Accepted consequence: while an Allow is pinned,
            # traffic
            # keeps flowing to a 429-ing dependency for the pin's lifetime —
            # bounded by its TTL and by the operator's explicit instruction.
            state = self.get_or_create_state(service_name)
            window_failures, window_total = self.get_window_evidence(service_name)
            if is_manual_pin_active(state):
                log_intervention_evaluated(
                    service_name=service_name,
                    allowed=False,
                    reason=ReasonCode.POLICY_CONSTRAINT_ACTIVE,
                )
                logger.warning(
                    "circuit_breaker.rate_limit_force_open_blocked",
                    service_name=service_name,
                    rate_limit_count=rate_limit_count,
                    total_requests=total_requests,
                    rate_percent=round(rate_percent, 2),
                    window_seconds=window,
                )
                return None

            # Observe-only (dry-run / shadow / evaluation): suppress the
            # automatic 429 trip. The gate sits at this call site, NOT inside
            # the shared trip primitive, so the failure triggers keep their own
            # gate. The 429 tracking above is observation and still runs.
            if intervention_suppressed(
                service_name=service_name,
                action="rate_limit_force_open",
                rate_limit_count=rate_limit_count,
                total_requests=total_requests,
            ):
                return None

            # Site F (766 D2): the freeze gate sits at this call site for the
            # same reason the pin and observe-only gates above do — an operator
            # force stays live during a freeze, and only this automatic verdict
            # is withheld. The 429 tracking above is observation and still runs.
            if not self._auto_transition_allowed(service_name, "open"):
                logger.debug(
                    "circuit_breaker.auto_transition_skipped",
                    site="rate_limit_cascade",
                    service_name=service_name,
                    new_state="open",
                )
                return None

            # Auto-open circuit breaker. The cascade is an *automatic* verdict,
            # so it takes the automatic trip primitive every other automatic
            # trigger takes: the row stays operator-free and recovers through
            # recovery_timeout and HALF_OPEN probing, instead of borrowing the
            # manual pin's expiry (which admitted nothing until the TTL was due
            # and ignored recovery entirely).
            attempt = self._trip_circuit_open(
                service_name,
                state,
                {
                    "error_type": RATE_LIMIT_CASCADE_TRIGGER,
                    "rate_limit_count": rate_limit_count,
                    "total_requests": total_requests,
                    "rate_percent": round(rate_percent, 2),
                    "window_seconds": window,
                },
                effective_config=cfg,
                window_failures=window_failures,
                window_total=window_total,
                trigger=RATE_LIMIT_CASCADE_TRIGGER,
            )

            if not attempt.did_open:
                # Already OPEN, HALF_OPEN, or pinned by a racing operator — the
                # 429 stays recorded, but this call performed no transition, so
                # it owes neither the backoff step nor a result.
                return None

            tracker.increment_backoff(service_name)
            logger.warning(
                "circuit_breaker.auto_opened_circuit_due",
                service_name=service_name,
            )
            return CircuitBreakerResult.succeeded(
                service_name=service_name,
                previous_state="closed",
                new_state="open",
                message=(
                    f"Rate limit cascade detected ({rate_limit_count}/{total_requests} "
                    f"= {rate_percent:.1f}% in {window}s)"
                ),
            )

        return None

    def check_rate_limit_cascade(self, service_name: str) -> bool:
        """
        Check if a rate limit cascade is occurring for a service.

        Args:
            service_name: Name of the external service

        Returns:
            True if cascade is detected, False otherwise
        """
        cfg = self.config
        tracker = get_rate_limit_tracker()
        window = cfg.rate_limit_cascade_window_seconds
        rate_limit_count = tracker.get_rate_limit_count(service_name, window)
        total_requests = tracker.get_request_count(service_name, window)

        return (
            rate_limit_count >= cfg.rate_limit_cascade_threshold
            and total_requests >= cfg.rate_limit_cascade_minimum_calls
            and (rate_limit_count / total_requests)
            >= (cfg.rate_limit_cascade_rate / 100)
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

        cfg = self.config

        # Check self-DDoS protection
        if not cfg.self_ddos_protection_enabled:
            return True, 0.0

        tracker = get_rate_limit_tracker()
        tracker.record_request(service_name)

        request_count = tracker.get_request_count(
            service_name, cfg.self_ddos_window_seconds
        )
        rps = request_count / cfg.self_ddos_window_seconds

        if rps > cfg.self_ddos_rps_limit:
            backoff = self.calculate_adaptive_backoff(service_name)
            logger.warning(
                "circuit_breaker.self_ddos_protection_triggered",
                service_name=service_name,
                current_rps=round(rps, 1),
                rps_limit=cfg.self_ddos_rps_limit,
                window_seconds=cfg.self_ddos_window_seconds,
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

        cfg = self.config
        tracker = get_rate_limit_tracker()
        backoff_level = tracker.get_backoff_level(service_name)

        # Reuse the settings-backed exponential-backoff-with-jitter strategy
        # instead of an inline reimplementation. ExponentialBackoff.calculate
        # uses attempt-1 as the exponent, so pass backoff_level + 1 to keep the
        # historical base x multiplier^level growth.
        strategy = ExponentialBackoff(
            base_delay=cfg.self_ddos_backoff_base_seconds,
            max_delay=cfg.self_ddos_backoff_max_seconds,
            multiplier=cfg.self_ddos_backoff_multiplier,
            jitter=True,
            jitter_factor=cfg.self_ddos_backoff_jitter_factor,
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
        cfg = self.config
        if not cfg.self_ddos_protection_enabled:
            return False

        tracker = get_rate_limit_tracker()
        request_count = tracker.get_request_count(
            service_name, cfg.self_ddos_window_seconds
        )
        rps = request_count / cfg.self_ddos_window_seconds
        return rps > cfg.self_ddos_rps_limit

    def get_protection_status(self, service_name: str) -> dict[str, Any]:
        """
        Get comprehensive protection status for a service.

        Returns:
            Dictionary with protection status details
        """
        cfg = self.config
        tracker = get_rate_limit_tracker()

        cascade_window = cfg.rate_limit_cascade_window_seconds
        rate_limit_count = tracker.get_rate_limit_count(service_name, cascade_window)
        total_requests = tracker.get_request_count(service_name, cascade_window)

        ddos_window = cfg.self_ddos_window_seconds
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
                "threshold": cfg.rate_limit_cascade_threshold,
                "rate_threshold_percent": cfg.rate_limit_cascade_rate,
                "minimum_calls": cfg.rate_limit_cascade_minimum_calls,
                "window_seconds": cascade_window,
            },
            "self_ddos_protection": {
                "enabled": cfg.self_ddos_protection_enabled,
                "detected": self.is_self_ddos_detected(service_name),
                "request_count_in_window": request_count,
                "current_rps": request_count / ddos_window,
                "rps_limit": cfg.self_ddos_rps_limit,
                "window_seconds": ddos_window,
            },
            "backoff": {
                "current_level": tracker.get_backoff_level(service_name),
                "suggested_delay_seconds": self.calculate_adaptive_backoff(
                    service_name
                ),
            },
        }
