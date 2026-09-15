"""The breaker stage decides the 429 cascade once, after it records the call.

793 D4. Under ``protect(..., retry=...)`` the retry stage is composed inside
the breaker stage: every attempt's 429 is counted as it is seen, and the
cascade is evaluated exactly once per protected call by the breaker, after it
has classified and recorded the whole call — whatever the final outcome was.
Deciding after the record means the evidence pair a cascade trip carries into
its OPEN event holds the tripping call: never ``(0, 0)``, never one short.

Verification techniques applied:
- Idempotency: exactly one evaluation per call through the real composition,
  including a call whose 429 attempts end in a 5xx
- Boundary: a scope with no 429 is not evaluated; no scope is not evaluated
- Evidence: the OPEN event's ``window_*`` pair includes the tripping call
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.protect_facade import protect, reset_protect_caches
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.convenience import (
    configure_circuit_breaker_service,
    reset_circuit_breaker_service,
)
from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy
from baldur.services.circuit_breaker.rate_limit_observation import (
    OutboundObservationScope,
)
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.policy import RetryPolicy
from tests.factories import InMemoryRateLimitTracker

SERVICE = "cascade-api"
MAX_ATTEMPTS = 3
_TRACKER_SITES = (
    "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
    "baldur.services.circuit_breaker.rate_limit_tracker.get_rate_limit_tracker",
)


class ThrottledError(Exception):
    """A client that raises on 429."""

    def __init__(self):
        super().__init__("HTTP 429 Too Many Requests")


class UpstreamError(Exception):
    """A 5xx-shaped failure that is not a rate limit."""


def _config(cascade_threshold: int) -> CircuitBreakerConfig:
    # The count / rate triggers are out of reach; only the cascade can trip.
    return CircuitBreakerConfig(
        enabled=True,
        failure_threshold=100,
        minimum_calls=1000,
        rate_limit_cascade_threshold=cascade_threshold,
        rate_limit_cascade_window_seconds=60,
        rate_limit_cascade_rate=10.0,
        rate_limit_cascade_minimum_calls=1,
    )


@pytest.fixture
def tracker():
    """A real in-memory tracker at both getter sites the fan-out reaches."""
    tracker = InMemoryRateLimitTracker()
    with (
        patch(_TRACKER_SITES[0], return_value=tracker),
        patch(_TRACKER_SITES[1], return_value=tracker),
    ):
        yield tracker


def _install_service(cascade_threshold: int) -> CircuitBreakerService:
    """The runtime singleton ``protect()``'s breaker stage records on."""
    service = CircuitBreakerService(
        config=_config(cascade_threshold),
        repository=InMemoryCircuitBreakerStateRepository(),
    )
    configure_circuit_breaker_service(service)
    return service


@pytest.fixture
def _clean_protect():
    reset_protect_caches()
    yield
    reset_protect_caches()
    reset_circuit_breaker_service()


def _retry() -> RetryPolicy:
    """A retry stage that never sleeps and never coordinates a cooldown."""
    return RetryPolicy(
        config=RetryPolicyConfig(max_attempts=MAX_ATTEMPTS, rate_limit_aware=False),
        sleeper=lambda seconds: None,
    )


def _outcomes(*errors):
    """A function that raises each error in turn, then returns ``"ok"``."""
    queue = list(errors)

    def _fn():
        if queue:
            raise queue.pop(0)
        return "ok"

    return _fn


@pytest.mark.usefixtures("_clean_protect", "tracker")
class TestCascadeDecidedAfterRecordBehavior:
    """One evaluation per protected call, after the record, with the call in it."""

    @pytest.mark.parametrize(
        ("errors", "raises"),
        [
            pytest.param(
                (ThrottledError(), ThrottledError(), ThrottledError()),
                ThrottledError,
                id="every_attempt_429",
            ),
            pytest.param(
                (ThrottledError(), ThrottledError(), UpstreamError()),
                UpstreamError,
                id="429_attempts_ending_in_5xx",
            ),
            pytest.param(
                (ThrottledError(),),
                None,
                id="429_then_success",
            ),
        ],
    )
    def test_exactly_one_cascade_evaluation_per_protected_call(self, errors, raises):
        """Idempotency: the inner stage counts, the breaker decides once."""
        service = _install_service(cascade_threshold=100)

        with (
            patch.object(
                service,
                "evaluate_rate_limit_cascade",
                wraps=service.evaluate_rate_limit_cascade,
            ) as evaluate,
            patch.object(
                service,
                "record_rate_limit_observation",
                wraps=service.record_rate_limit_observation,
            ) as observe,
        ):
            if raises is None:
                protect(
                    SERVICE, _outcomes(*errors), circuit_breaker=True, retry=_retry()
                )
            else:
                with pytest.raises(raises):
                    protect(
                        SERVICE,
                        _outcomes(*errors),
                        circuit_breaker=True,
                        retry=_retry(),
                    )

        evaluate.assert_called_once_with(SERVICE)
        assert observe.call_count == sum(isinstance(e, ThrottledError) for e in errors)

    def test_call_with_no_429_is_not_evaluated(self):
        """Boundary: ``scope.rate_limited == 0`` -> no evaluation."""
        service = _install_service(cascade_threshold=1)

        with patch.object(
            service,
            "evaluate_rate_limit_cascade",
            wraps=service.evaluate_rate_limit_cascade,
        ) as evaluate:
            with pytest.raises(UpstreamError):
                protect(
                    SERVICE,
                    _outcomes(UpstreamError(), UpstreamError(), UpstreamError()),
                    circuit_breaker=True,
                    retry=_retry(),
                )

        evaluate.assert_not_called()

    def test_no_scope_is_not_evaluated(self):
        """Boundary: a caller with no observation scope evaluates nothing here."""
        service = _install_service(cascade_threshold=1)
        policy = CircuitBreakerPolicy(service_name=SERVICE, cb_service=service)

        with patch.object(
            service,
            "evaluate_rate_limit_cascade",
            wraps=service.evaluate_rate_limit_cascade,
        ) as evaluate:
            policy._evaluate_cascade_after_record(None, service)

        evaluate.assert_not_called()

    def test_scope_with_a_429_is_evaluated_once(self):
        service = _install_service(cascade_threshold=100)
        policy = CircuitBreakerPolicy(service_name=SERVICE, cb_service=service)
        scope = OutboundObservationScope(SERVICE, service=service)
        scope.rate_limited = 2

        with patch.object(
            service,
            "evaluate_rate_limit_cascade",
            wraps=service.evaluate_rate_limit_cascade,
        ) as evaluate:
            policy._evaluate_cascade_after_record(scope, service)

        evaluate.assert_called_once_with(SERVICE)

    def test_evaluation_failure_is_fail_open(self):
        service = _install_service(cascade_threshold=100)
        policy = CircuitBreakerPolicy(service_name=SERVICE, cb_service=service)
        scope = OutboundObservationScope(SERVICE, service=service)
        scope.rate_limited = 1

        with patch.object(
            service,
            "evaluate_rate_limit_cascade",
            side_effect=RuntimeError("tracker down"),
        ):
            policy._evaluate_cascade_after_record(scope, service)  # must not raise

    def test_cascade_trip_event_carries_the_tripping_call(self):
        """Evidence: the OPEN event's pair includes the call that tripped it.

        Two healthy calls precede the storm so the pair is distinguishable from
        ``(0, 0)`` and from a pair one call short.
        """
        service = _install_service(cascade_threshold=1)
        for _ in range(2):
            protect(SERVICE, lambda: "ok", circuit_breaker=True, retry=_retry())

        with (
            patch.object(service, "_emit_event") as emit,
            patch.object(service, "_log_circuit_open_audit"),
            patch.object(service, "_apply_burn_rate_multiplier"),
            pytest.raises(ThrottledError),
        ):
            protect(
                SERVICE,
                _outcomes(ThrottledError(), ThrottledError(), ThrottledError()),
                circuit_breaker=True,
                retry=_retry(),
            )

        opened = [
            call.kwargs["data"]
            for call in emit.call_args_list
            if call.kwargs["data"].get("trigger") == "rate_limit_cascade"
        ]
        assert len(opened) == 1
        # Two successes, then the one failed call the breaker recorded before
        # it decided: (1, 3), never (0, 2) or (0, 0).
        assert (
            opened[0]["window_failure_count"],
            opened[0]["window_total_calls"],
        ) == (1, 3)
        assert service.get_state(SERVICE) == "open"
