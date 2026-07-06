"""
Integration test: Resilience policy composition (#418).

Verifies end-to-end behavior of standard_pipeline with CB-open fast-fail
and fallback activation. Mock-based (no Docker required).

Scenarios (stub-based):
1. CB open → single attempt → FAILURE (no fallback)
2. CB open + fallback_default → single attempt → SUCCESS_WITH_FALLBACK
3. Transient failures → retry up to max → FAILURE

Scenarios (real CircuitBreakerPolicy + InMemory repository):
4. CB disabled → passthrough, normal retry
5. CB failure accumulation → OPEN → fast-fail (1 attempt)
6. CB OPEN + fallback → fast-fail → SUCCESS_WITH_FALLBACK
7. Mixed: transient failures exhaust retries while CB stays closed
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.core.exceptions import CircuitBreakerError
from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.resilience.policies.presets import standard_pipeline


class TestCBOpenFastFailComposition:
    """standard_pipeline CB-open fast-fail integration (#418 P0-1 + P0-2)."""

    def test_cb_open_fast_fail_via_standard_pipeline(self):
        """CB open → single attempt, no retry, returns FAILURE.

        Integration path:
        1. standard_pipeline("svc") creates compose(Retry, CB)
        2. CB.execute() raises CircuitBreakerError
        3. Retry catches CB error → non_retryable → break (1 attempt)
        4. Composer returns PolicyResult(FAILURE)
        """
        from baldur.interfaces.resilience_policy import (
            ResiliencePolicy,
        )

        class ForcedOpenCB(ResiliencePolicy):
            @property
            def name(self):
                return "circuit_breaker"

            def execute(self, func, *args, context=None, **kwargs):
                raise CircuitBreakerError("OPEN")

        pipeline = standard_pipeline("test_svc", max_retries=5)

        # Mock guards/hooks/sinks to isolate composition behavior
        pipeline._guards.clear()
        pipeline._hooks.clear()
        pipeline._sinks.clear()

        # Replace CB with forced-open stub
        pipeline._policies[-1] = ForcedOpenCB()

        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=MagicMock(),
        ):
            result = pipeline.execute(lambda: "should_not_reach")

        assert result.outcome == PolicyOutcome.FAILURE
        assert isinstance(result.error, CircuitBreakerError)

    def test_standard_pipeline_fallback_on_cb_open(self):
        """CB open + fallback_default → single attempt → SUCCESS_WITH_FALLBACK.

        Integration path:
        1. standard_pipeline with fallback_default creates compose(Fallback, Retry, CB)
        2. CB raises CircuitBreakerError
        3. Retry catches → non_retryable → break → raises error
        4. Fallback catches → returns degraded value → SUCCESS_WITH_FALLBACK
        """
        from baldur.interfaces.resilience_policy import (
            ResiliencePolicy,
        )

        class ForcedOpenCB(ResiliencePolicy):
            @property
            def name(self):
                return "circuit_breaker"

            def execute(self, func, *args, context=None, **kwargs):
                raise CircuitBreakerError("OPEN")

        fallback_value = {"status": "degraded"}
        pipeline = standard_pipeline(
            "test_svc",
            max_retries=5,
            fallback_default=fallback_value,
        )

        # Clear infra concerns
        pipeline._guards.clear()
        pipeline._hooks.clear()
        pipeline._sinks.clear()

        # Replace CB with forced-open stub
        pipeline._policies[-1] = ForcedOpenCB()

        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=MagicMock(),
        ):
            result = pipeline.execute(lambda: "should_not_reach")

        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        assert result.value == fallback_value

    def test_transient_failure_retries_then_fails(self):
        """Transient errors trigger retries up to max_attempts → FAILURE."""
        from baldur.interfaces.resilience_policy import ResiliencePolicy

        call_count = 0

        def transient_fail():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("timeout")

        # CB that passes through to actual func
        class PassthroughCB(ResiliencePolicy):
            @property
            def name(self):
                return "circuit_breaker"

            def execute(self, func, *args, context=None, **kwargs):
                from baldur.interfaces.resilience_policy import (
                    PolicyOutcome as PO,
                )
                from baldur.interfaces.resilience_policy import (
                    PolicyResult as PR,
                )

                try:
                    val = func(*args, **kwargs)
                    return PR(value=val, outcome=PO.SUCCESS)
                except Exception:
                    raise

        pipeline = standard_pipeline("test_svc", max_retries=3)
        pipeline._guards.clear()
        pipeline._hooks.clear()
        pipeline._sinks.clear()
        pipeline._policies[-1] = PassthroughCB()

        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=MagicMock(),
        ):
            result = pipeline.execute(transient_fail)

        assert result.outcome == PolicyOutcome.FAILURE
        assert isinstance(result.error, ConnectionError)
        assert call_count == 3


# =============================================================================
# Real CircuitBreakerPolicy + InMemory repository integration
# =============================================================================


def _make_real_cb_pipeline(
    service_name: str = "test_svc",
    max_retries: int = 3,
    cb_failure_threshold: int = 3,
    cb_minimum_calls: int = 1,
    fallback_default: object = None,
):
    """Build standard_pipeline with a REAL CircuitBreakerPolicy backed by InMemory repo.

    Returns (pipeline, cb_service) so tests can inspect CB state.
    """
    from baldur.adapters.memory.circuit_breaker import (
        InMemoryCircuitBreakerStateRepository,
    )
    from baldur.services.circuit_breaker.config import CircuitBreakerConfig
    from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy
    from baldur.services.circuit_breaker.service import CircuitBreakerService

    cb_config = CircuitBreakerConfig(
        enabled=True,
        failure_threshold=cb_failure_threshold,
        minimum_calls=cb_minimum_calls,
        recovery_timeout=60,
        failure_rate_threshold=0,  # use count-based only
    )
    repo = InMemoryCircuitBreakerStateRepository()
    cb_service = CircuitBreakerService(config=cb_config, repository=repo)

    cb_policy = CircuitBreakerPolicy(
        service_name=service_name,
        cb_service=cb_service,
        hooks=[],  # disable hooks for isolation
    )

    pipeline = standard_pipeline(
        service_name,
        max_retries=max_retries,
        fallback_default=fallback_default,
    )

    # Replace auto-created CB with our configured real CB
    pipeline._policies[-1] = cb_policy

    # Clear infra guards/hooks/sinks
    pipeline._guards.clear()
    pipeline._hooks.clear()
    pipeline._sinks.clear()

    return pipeline, cb_service


class TestRealCBPolicyCompositionBehavior:
    """Real CB + Retry + Fallback composition with InMemory repository (#418)."""

    def test_cb_disabled_passthrough_allows_normal_retry(self):
        """CB enabled=False → passthrough, retry works normally on transient errors.

        Verifies that when CB is disabled (default config), the CB policy
        is transparent and does not interfere with retry behavior.
        """
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        # Given — CB disabled
        cb_config = CircuitBreakerConfig(enabled=False)
        cb_service = CircuitBreakerService(config=cb_config)
        cb_policy = CircuitBreakerPolicy(
            service_name="test_svc",
            cb_service=cb_service,
            hooks=[],
        )

        call_count = 0

        def transient_fail():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("timeout")

        pipeline = standard_pipeline("test_svc", max_retries=3)
        pipeline._policies[-1] = cb_policy
        pipeline._guards.clear()
        pipeline._hooks.clear()
        pipeline._sinks.clear()

        # When
        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=MagicMock(),
        ):
            result = pipeline.execute(transient_fail)

        # Then — all 3 retries executed, CB did not interfere
        assert result.outcome == PolicyOutcome.FAILURE
        assert isinstance(result.error, ConnectionError)
        assert call_count == 3

    def test_cb_opens_after_threshold_failures_then_fast_fails(self):
        """Failures accumulate → CB opens → next request fast-fails (1 attempt).

        Integration path:
        1. First pipeline: 3 transient failures → CB records 3 failures → CB OPEN;
           attempt 4 hits the now-open CB → ``CircuitBreakerOpenError``.
        2. Second pipeline.execute(): CB OPEN from the start → fast-fail.
        3. Retry sees non_retryable → break immediately (1 attempt).
        4. Result on both executes: REJECTED with ``CircuitBreakerOpenError`` —
           the outer composer dispatches via ``PolicyRejectedException`` because
           ``CircuitBreakerOpenError`` multi-inherits it (OOS #466 F10 fix).
        """
        from baldur.services.circuit_breaker.exceptions import (
            CircuitBreakerOpenError,
        )

        # Given — CB with threshold=3, minimum_calls=1
        pipeline, cb_service = _make_real_cb_pipeline(
            max_retries=5,
            cb_failure_threshold=3,
            cb_minimum_calls=1,
        )

        call_count = 0

        def transient_fail():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("downstream timeout")

        # When — first execute: failures accumulate, CB opens
        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=MagicMock(),
        ):
            result1 = pipeline.execute(transient_fail)

        # Then — CB should be OPEN after threshold failures, last error is the
        # CB rejection on the post-trip attempt → REJECTED outcome.
        assert result1.outcome == PolicyOutcome.REJECTED
        assert isinstance(result1.error, CircuitBreakerOpenError)
        cb_state = cb_service.get_state("test_svc")
        assert cb_state == "open", (
            f"Expected CB OPEN after {call_count} failures, got {cb_state}"
        )

        # When — second execute: CB is OPEN → fast-fail
        call_count_before = call_count
        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=MagicMock(),
        ):
            result2 = pipeline.execute(lambda: "should_not_reach")

        # Then — no additional func calls, fast-fail via CB → REJECTED outcome
        assert call_count == call_count_before, (
            "Function should not be called when CB is OPEN"
        )
        assert result2.outcome == PolicyOutcome.REJECTED
        assert isinstance(result2.error, (CircuitBreakerError, CircuitBreakerOpenError))

    def test_cb_open_with_fallback_returns_degraded_value(self):
        """CB opens → fast-fail → Fallback provides degraded response.

        End-to-end integration:
        1. Force CB OPEN via failure accumulation
        2. Next request: CB rejects → Retry non_retryable → Fallback activates
        3. Result: SUCCESS_WITH_FALLBACK with degraded value
        """

        degraded_value = {"status": "degraded", "source": "fallback"}

        # Given — CB with low threshold + fallback configured
        pipeline, cb_service = _make_real_cb_pipeline(
            max_retries=5,
            cb_failure_threshold=2,
            cb_minimum_calls=1,
            fallback_default=degraded_value,
        )

        def always_fail():
            raise ConnectionError("downstream unavailable")

        # When — accumulate failures to open CB
        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=MagicMock(),
        ):
            pipeline.execute(always_fail)

        # Verify CB is OPEN
        assert cb_service.get_state("test_svc") == "open"

        # When — next request hits open CB → fallback activates
        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=MagicMock(),
        ):
            result = pipeline.execute(lambda: "should_not_reach")

        # Then — fallback provided degraded value
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        assert result.value == degraded_value

    def test_transient_failures_below_threshold_keep_cb_closed(self):
        """Failures below CB threshold → CB stays closed, normal retry behavior.

        With max_retries=2 and cb_failure_threshold=5, the retry exhausts
        before CB opens. CB remains CLOSED throughout.
        """
        # Given — high CB threshold, low retry count
        pipeline, cb_service = _make_real_cb_pipeline(
            max_retries=2,
            cb_failure_threshold=5,
            cb_minimum_calls=1,
        )

        call_count = 0

        def transient_fail():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("timeout")

        # When
        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=MagicMock(),
        ):
            result = pipeline.execute(transient_fail)

        # Then — retries exhausted, but CB still closed
        assert result.outcome == PolicyOutcome.FAILURE
        assert call_count == 2
        assert cb_service.get_state("test_svc") == "closed"

    def test_success_after_failures_keeps_cb_closed(self):
        """Partial failures followed by success → CB records success, stays closed."""
        # Given — CB with threshold=5
        pipeline, cb_service = _make_real_cb_pipeline(
            max_retries=5,
            cb_failure_threshold=5,
            cb_minimum_calls=1,
        )

        call_count = 0

        def fail_then_succeed():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ConnectionError("transient")
            return "success"

        # When
        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=MagicMock(),
        ):
            result = pipeline.execute(fail_then_succeed)

        # Then — succeeded on 3rd attempt, CB still closed
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "success"
        assert call_count == 3
        assert cb_service.get_state("test_svc") == "closed"
