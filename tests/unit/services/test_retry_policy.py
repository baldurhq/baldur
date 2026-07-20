"""
Unit tests for the pure RetryPolicy retry policy.

Target: services/retry_handler/policy.py
- Core retry loop (success, failure, retry, exception classification)
- Collaborator injection: sleeper, retry_budget, rate_limit_coordinator, backoff
- 429 rate limit detection
- PolicyContext propagation
"""

from __future__ import annotations

from unittest.mock import MagicMock

from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage
from baldur.core.backoff import (
    BackoffStrategy,
    ConstantBackoff,
    ExponentialBackoff,
    LinearBackoff,
)
from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    ResiliencePolicy,
)
from baldur.services.rate_limit_coordinator import (
    RateLimitCoordinator,
    RateLimitDeferredError,
)
from baldur.services.rate_limit_coordinator.models import RateLimitResult
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.policy import RetryPolicy
from baldur.services.retry_handler.rate_limit_detection import (  # noqa: F401
    RATE_LIMIT_INDICATORS,
)

# =============================================================================
# RetryPolicy — Contract
# =============================================================================


class TestRetryPolicyContract:
    """RetryPolicy fixed identifiers and result structure."""

    def test_retry_policy_is_resilience_policy(self):
        """RetryPolicy is isinstance-compatible with the ResiliencePolicy Protocol."""
        policy = RetryPolicy(config=RetryPolicyConfig(max_attempts=1))
        assert isinstance(policy, ResiliencePolicy)

    def test_name_is_retry(self):
        """RetryPolicy.name is 'retry'."""
        policy = RetryPolicy(config=RetryPolicyConfig(max_attempts=1))
        assert policy.name == "retry"

    def test_rate_limit_indicators_contain_expected_keywords(self):
        """RATE_LIMIT_INDICATORS contains 429, rate limit, throttle and friends."""
        assert "429" in RATE_LIMIT_INDICATORS
        assert "rate limit" in RATE_LIMIT_INDICATORS
        assert "throttle" in RATE_LIMIT_INDICATORS
        assert "too many requests" in RATE_LIMIT_INDICATORS

    def test_success_result_has_retry_in_executed_policies(self):
        """A successful result's executed_policies contains 'retry'."""
        policy = RetryPolicy(config=RetryPolicyConfig(max_attempts=1))
        result = policy.execute(lambda: "ok")
        assert "retry" in result.executed_policies

    def test_failure_metadata_contains_should_dlq(self):
        """A failure result's metadata contains the should_dlq key."""
        policy = RetryPolicy(config=RetryPolicyConfig(max_attempts=1, enable_dlq=True))
        result = policy.execute(lambda: (_ for _ in ()).throw(Exception("fail")))
        assert "should_dlq" in result.metadata

    def test_failure_metadata_contains_domain(self):
        """A failure result's metadata contains the domain."""
        policy = RetryPolicy(config=RetryPolicyConfig(max_attempts=1, domain="payment"))
        result = policy.execute(lambda: (_ for _ in ()).throw(Exception("fail")))
        assert result.metadata["domain"] == "payment"


# =============================================================================
# RetryPolicy — Core retry behavior
# =============================================================================


class TestRetryPolicyExecuteBehavior:
    """RetryPolicy.execute() core retry behavior."""

    def test_success_first_attempt(self):
        """Success on the first attempt returns SUCCESS."""
        policy = RetryPolicy(config=RetryPolicyConfig(max_attempts=3))
        result = policy.execute(lambda: "ok")
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"
        assert result.total_attempts == 1

    def test_success_after_retry(self):
        """A first-attempt failure is followed by a successful second attempt."""
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=3),
            backoff=ConstantBackoff(delay=0.0),
            sleeper=lambda _: None,
        )
        attempts = [0]

        def flaky():
            attempts[0] += 1
            if attempts[0] == 1:
                raise ConnectionError("temporary")
            return "recovered"

        result = policy.execute(flaky)
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "recovered"
        assert result.total_attempts == 2

    def test_all_attempts_exhausted_returns_failure(self):
        """Exhausting every attempt returns FAILURE."""
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=3),
            backoff=ConstantBackoff(delay=0.0),
            sleeper=lambda _: None,
        )
        result = policy.execute(lambda: (_ for _ in ()).throw(ConnectionError("fail")))
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.total_attempts == 3
        assert isinstance(result.error, ConnectionError)

    def test_non_retryable_exception_stops_immediately(self):
        """A non_retryable_exceptions match stops the loop immediately."""
        config = RetryPolicyConfig(
            max_attempts=5,
            retryable_exceptions=(Exception,),
            non_retryable_exceptions=(ValueError,),
        )
        policy = RetryPolicy(config=config)
        result = policy.execute(lambda: (_ for _ in ()).throw(ValueError("bad")))
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.total_attempts == 1
        assert isinstance(result.error, ValueError)

    def test_retryable_exception_triggers_retry(self):
        """Only exceptions in retryable_exceptions trigger a retry."""
        policy = RetryPolicy(
            config=RetryPolicyConfig(
                max_attempts=3, retryable_exceptions=(ConnectionError,)
            ),
            backoff=ConstantBackoff(delay=0.0),
            sleeper=lambda _: None,
        )
        attempts = [0]

        def flaky():
            attempts[0] += 1
            if attempts[0] < 3:
                raise ConnectionError("retry me")
            return "done"

        result = policy.execute(flaky)
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.total_attempts == 3

    def test_non_matching_exception_stops_retry(self):
        """Exceptions outside retryable_exceptions are not retried."""
        config = RetryPolicyConfig(
            max_attempts=3, retryable_exceptions=(ConnectionError,)
        )
        policy = RetryPolicy(config=config)
        result = policy.execute(lambda: (_ for _ in ()).throw(TypeError("wrong type")))
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.total_attempts == 1

    def test_retry_history_records_all_failed_attempts(self):
        """Every failed attempt is recorded in retry_history."""
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=3),
            backoff=ConstantBackoff(delay=0.0),
            sleeper=lambda _: None,
        )
        result = policy.execute(lambda: (_ for _ in ()).throw(ConnectionError("fail")))
        history = result.metadata["retry_history"]
        assert len(history) == 3
        for i, entry in enumerate(history, 1):
            assert entry["attempt"] == i
            assert entry["error_type"] == "ConnectionError"

    def test_should_dlq_flag_reflects_enable_dlq_config(self):
        """The should_dlq flag reflects config.enable_dlq."""
        for enable_dlq in (True, False):
            policy = RetryPolicy(
                config=RetryPolicyConfig(max_attempts=1, enable_dlq=enable_dlq)
            )
            result = policy.execute(lambda: (_ for _ in ()).throw(Exception("fail")))
            assert result.metadata["should_dlq"] is enable_dlq

    def test_max_attempts_in_failure_metadata(self):
        """Failure metadata contains max_attempts."""
        config = RetryPolicyConfig(max_attempts=5)
        policy = RetryPolicy(
            config=config, backoff=ConstantBackoff(delay=0.0), sleeper=lambda _: None
        )
        result = policy.execute(lambda: (_ for _ in ()).throw(Exception("fail")))
        assert result.metadata["max_attempts"] == config.max_attempts


# =============================================================================
# RetryPolicy — Sleeper injection
# =============================================================================


class TestRetryPolicySleeperBehavior:
    """RetryPolicy sleeper injection behavior."""

    def test_sleeper_none_skips_sleep(self):
        """sleeper=None performs no sleep."""
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=2),
            backoff=ConstantBackoff(delay=1.0),
            sleeper=None,
        )
        result = policy.execute(lambda: (_ for _ in ()).throw(Exception("fail")))
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.total_attempts == 2

    def test_sleeper_called_with_backoff_delay(self):
        """A provided sleeper is called with the backoff delay."""
        mock_sleeper = MagicMock()
        delay_value = 2.5
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=2),
            backoff=ConstantBackoff(delay=delay_value),
            sleeper=mock_sleeper,
        )
        policy.execute(lambda: (_ for _ in ()).throw(Exception("fail")))
        mock_sleeper.assert_called_once_with(delay_value)

    def test_sleeper_not_called_on_zero_delay(self):
        """A zero delay does not call the sleeper."""
        mock_sleeper = MagicMock()
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=2),
            backoff=ConstantBackoff(delay=0.0),
            sleeper=mock_sleeper,
        )
        policy.execute(lambda: (_ for _ in ()).throw(Exception("fail")))
        mock_sleeper.assert_not_called()

    def test_sleeper_not_called_on_first_attempt_success(self):
        """The sleeper is not called when the first attempt succeeds."""
        mock_sleeper = MagicMock()
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=3), sleeper=mock_sleeper
        )
        policy.execute(lambda: "ok")
        mock_sleeper.assert_not_called()


# =============================================================================
# RetryPolicy — AdaptiveRetryBudget Collaborator
# =============================================================================


class TestRetryPolicyRetryBudgetBehavior:
    """RetryPolicy retry_budget collaborator behavior."""

    def test_record_request_called_per_attempt(self):
        """retry_budget.record_request() is called on every attempt."""
        mock_budget = MagicMock()
        mock_budget.should_allow_retry.return_value = True
        mock_budget.get_stats.return_value = {}

        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=3),
            backoff=ConstantBackoff(delay=0.0),
            retry_budget=mock_budget,
            sleeper=lambda _: None,
        )
        policy.execute(lambda: (_ for _ in ()).throw(Exception("fail")))

        assert mock_budget.record_request.call_count == 3
        mock_budget.record_request.assert_any_call(is_retry=False)
        mock_budget.record_request.assert_any_call(is_retry=True)

    def test_budget_exhaustion_breaks_loop(self):
        """A False retry_budget.should_allow_retry() stops the loop."""
        mock_budget = MagicMock()
        mock_budget.should_allow_retry.return_value = False
        mock_budget.get_stats.return_value = {"ratio": 0.9}

        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=5),
            backoff=ConstantBackoff(delay=0.0),
            retry_budget=mock_budget,
            sleeper=lambda _: None,
        )
        result = policy.execute(lambda: (_ for _ in ()).throw(Exception("fail")))
        assert result.total_attempts == 2
        assert result.outcome == PolicyOutcome.FAILURE

    def test_budget_none_allows_all_attempts(self):
        """retry_budget=None runs every attempt with no budget check."""
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=3),
            backoff=ConstantBackoff(delay=0.0),
            retry_budget=None,
            sleeper=lambda _: None,
        )
        result = policy.execute(lambda: (_ for _ in ()).throw(Exception("fail")))
        assert result.total_attempts == 3


# =============================================================================
# RetryPolicy — RateLimitCoordinator Collaborator
# =============================================================================


class TestRetryPolicyRateLimitCoordinatorBehavior:
    """RetryPolicy rate_limit_coordinator collaborator behavior."""

    def test_wait_if_needed_called_before_execution(self):
        """rate_limit_coordinator.wait_if_needed() runs before the function."""
        mock_coord = MagicMock()
        # Real result object, not a MagicMock: an auto-generated ``.deferred``
        # attribute is truthy and would defer before any attempt runs.
        mock_coord.wait_if_needed.return_value = RateLimitResult(waited=False)

        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=1, domain="payment"),
            rate_limit_coordinator=mock_coord,
        )
        policy.execute(lambda: "ok")
        # No budget configured -> unbounded wait bound is forwarded as None.
        mock_coord.wait_if_needed.assert_called_once_with("payment", max_wait=None)

    def test_on_success_skipped_when_no_rate_limit_signal(self):
        """A clean success owes no on_success — it costs a storage round trip.

        ``on_success`` reads state and, when a counter is standing, writes a
        reset. Neither is owed by a call that never observed a rate-limit
        signal, so the clean path stays at one storage read per attempt (the
        pre-attempt cooldown consult).
        """
        mock_coord = MagicMock()
        mock_coord.wait_if_needed.return_value = RateLimitResult(waited=False)

        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=1, domain="payment"),
            rate_limit_coordinator=mock_coord,
        )
        policy.execute(lambda: "ok")
        mock_coord.on_success.assert_not_called()

    def test_on_success_called_after_success_following_a_wait(self):
        """A success that followed an honored cooldown does call on_success."""
        mock_coord = MagicMock(spec=RateLimitCoordinator)
        mock_coord.wait_if_needed.return_value = RateLimitResult(
            waited=True, wait_time=0.01, was_rate_limited=True
        )

        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=1, domain="payment"),
            rate_limit_coordinator=mock_coord,
        )
        policy.execute(lambda: "ok")
        mock_coord.on_success.assert_called_once_with("payment")

    def test_coordinator_none_skips_rate_limit(self):
        """With rate_limit_coordinator=None the rate-limit check is skipped."""
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=1),
            rate_limit_coordinator=None,
        )
        result = policy.execute(lambda: "ok")
        assert result.outcome == PolicyOutcome.SUCCESS


# =============================================================================
# RetryPolicy — BackoffStrategy Collaborator
# =============================================================================


class TestRetryPolicyBackoffBehavior:
    """RetryPolicy backoff collaborator behavior."""

    def test_default_backoff_is_exponential(self):
        """backoff=None constructs an ExponentialBackoff by default."""
        policy = RetryPolicy(config=RetryPolicyConfig(max_attempts=1))
        assert isinstance(policy._backoff, ExponentialBackoff)

    def test_default_backoff_uses_config_values(self):
        """The default ExponentialBackoff uses the config's backoff settings."""
        config = RetryPolicyConfig(backoff_base=10, backoff_max=300, jitter_percent=50)
        policy = RetryPolicy(config=config)
        backoff = policy._backoff
        assert backoff.base_delay == config.backoff_base
        assert backoff.max_delay == config.backoff_max
        assert backoff.jitter_factor == config.jitter_percent / 100.0

    def test_custom_backoff_strategy_injected(self):
        """An injected custom BackoffStrategy is the one used."""
        custom_backoff = LinearBackoff(base_delay=1.0, increment=2.0)
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=1), backoff=custom_backoff
        )
        assert policy._backoff is custom_backoff

    def test_backoff_calculate_receives_context(self):
        """backoff.calculate() is called with the PolicyContext."""
        mock_backoff = MagicMock(spec=BackoffStrategy)
        mock_backoff.calculate.return_value = 1.0
        ctx = PolicyContext(tier_id="critical", domain="payment")

        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=2),
            backoff=mock_backoff,
            sleeper=lambda _: None,
        )
        policy.execute(lambda: (_ for _ in ()).throw(Exception("fail")), context=ctx)
        mock_backoff.calculate.assert_called_once_with(1, context=ctx)


# =============================================================================
# RetryPolicy — 429 rate limit detection
# =============================================================================


class TestRetryPolicyDetectRateLimitBehavior:
    """RetryPolicy._detect_rate_limit() static method behavior."""

    def test_detect_429_in_message(self):
        """An error message containing 429 returns True."""
        is_rate, _ = RetryPolicy._detect_rate_limit(
            Exception("HTTP 429 Too Many Requests")
        )
        assert is_rate is True

    def test_detect_rate_limit_keyword(self):
        """The 'rate limit' keyword returns True."""
        is_rate, _ = RetryPolicy._detect_rate_limit(Exception("Rate limit exceeded"))
        assert is_rate is True

    def test_detect_throttle_keyword(self):
        """The 'throttle' keyword returns True."""
        is_rate, _ = RetryPolicy._detect_rate_limit(Exception("Request throttled"))
        assert is_rate is True

    def test_normal_error_not_detected(self):
        """An ordinary error is not detected as a rate limit."""
        is_rate, _ = RetryPolicy._detect_rate_limit(ValueError("Invalid input"))
        assert is_rate is False

    def test_extract_retry_after_attribute(self):
        """A retry_after attribute on the exception is extracted."""
        err = Exception("429")
        err.retry_after = 30.0
        is_rate, retry_after = RetryPolicy._detect_rate_limit(err)
        assert is_rate is True
        assert retry_after == 30.0

    def test_extract_retry_after_from_response_headers(self):
        """The value is extracted from exception.response.headers['Retry-After']."""
        err = Exception("429")
        mock_response = MagicMock()
        mock_response.headers = {"Retry-After": "60"}
        err.response = mock_response
        is_rate, retry_after = RetryPolicy._detect_rate_limit(err)
        assert is_rate is True
        assert retry_after == 60.0

    def test_no_retry_after_returns_none(self):
        """Absent retry_after information returns None."""
        is_rate, retry_after = RetryPolicy._detect_rate_limit(Exception("429 error"))
        assert is_rate is True
        assert retry_after is None


# =============================================================================
# RetryPolicy — PolicyContext propagation
# =============================================================================


class TestRetryPolicyContextBehavior:
    """PolicyContext propagation into RetryPolicy.execute()."""

    def test_execute_accepts_context_parameter(self):
        """execute() accepts a context parameter."""
        ctx = PolicyContext(order_id="ORD-123", tier_id="critical")
        policy = RetryPolicy(config=RetryPolicyConfig(max_attempts=1))
        result = policy.execute(lambda: "ok", context=ctx)
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_execute_works_without_context(self):
        """context=None still works."""
        policy = RetryPolicy(config=RetryPolicyConfig(max_attempts=1))
        result = policy.execute(lambda: "ok")
        assert result.outcome == PolicyOutcome.SUCCESS


# =============================================================================
# Behavior — CB deference (#418 P0-1)
# =============================================================================


class TestRetryPolicyCBDeferenceP0_1Behavior:
    """RetryPolicy CB-open fast-fail behavior (#418 P0-1)."""

    def test_retry_skips_cb_open_error(self):
        """CB OPEN raised in 1st attempt → retry exits immediately, total_attempts=1."""
        from baldur.core.exceptions import CircuitBreakerError

        call_count = 0

        def raise_cb_open():
            nonlocal call_count
            call_count += 1
            raise CircuitBreakerError("CB is OPEN")

        config = RetryPolicyConfig(max_attempts=5)
        policy = RetryPolicy(config=config)
        result = policy.execute(raise_cb_open)

        assert result.outcome == PolicyOutcome.FAILURE
        assert isinstance(result.error, CircuitBreakerError)
        assert result.total_attempts == 1
        assert call_count == 1

    def test_retry_skips_cb_transition_error(self):
        """CircuitBreakerTransitionError (subclass) also stops retry immediately."""
        from baldur.core.exceptions import CircuitBreakerTransitionError

        call_count = 0

        def raise_cb_transition():
            nonlocal call_count
            call_count += 1
            raise CircuitBreakerTransitionError("transition failed")

        config = RetryPolicyConfig(max_attempts=3)
        policy = RetryPolicy(config=config)
        result = policy.execute(raise_cb_transition)

        assert result.outcome == PolicyOutcome.FAILURE
        assert call_count == 1

    def test_retry_explicit_empty_non_retryable_allows_cb_retry(self):
        """Explicit non_retryable_exceptions=() opt-out allows CB error retry."""
        from baldur.core.exceptions import CircuitBreakerError

        call_count = 0

        def raise_cb_open():
            nonlocal call_count
            call_count += 1
            raise CircuitBreakerError("CB is OPEN")

        config = RetryPolicyConfig(max_attempts=3, non_retryable_exceptions=())
        policy = RetryPolicy(
            config=config,
            backoff=ConstantBackoff(delay=0.0),
            sleeper=lambda _: None,
        )
        result = policy.execute(raise_cb_open)

        assert result.outcome == PolicyOutcome.FAILURE
        assert call_count == 3


# =============================================================================
# Behavior — retry.exhausted EventBus emission (#418 P0-3)
# =============================================================================


class TestRetryPolicyExhaustedEventP0_3Behavior:
    """RetryPolicy exhaustion event emission behavior (#418 P0-3)."""

    def test_retry_exhausted_emits_event(self):
        """Exhaustion emits RETRY_EXHAUSTED event with expected data fields."""
        from unittest.mock import patch

        mock_bus = MagicMock()
        config = RetryPolicyConfig(max_attempts=2, domain="payments")
        policy = RetryPolicy(
            config=config,
            backoff=ConstantBackoff(delay=0.0),
            sleeper=lambda _: None,
        )

        with patch(
            "baldur.services.retry_handler.policy.get_event_bus",
            return_value=mock_bus,
            create=True,
        ):
            with patch(
                "baldur.services.retry_handler.policy.EventType",
                create=True,
            ):
                # Patch the lazy imports inside _emit_exhausted_event

                with patch(
                    "baldur.services.event_bus.get_event_bus",
                    return_value=mock_bus,
                ):
                    result = policy.execute(
                        lambda: (_ for _ in ()).throw(ConnectionError("timeout"))
                    )

        assert result.outcome == PolicyOutcome.FAILURE
        assert mock_bus.emit.call_count == 1
        call_kwargs = mock_bus.emit.call_args
        event_data = call_kwargs.kwargs.get("data") or call_kwargs[1].get("data")
        assert event_data["domain"] == "payments"
        assert event_data["max_attempts"] == 2
        assert event_data["final_error_type"] == "ConnectionError"
        assert event_data["attempts"] == 2
        assert "retry_history_length" in event_data

    def test_retry_exhausted_event_bus_unavailable_returns_failure(self):
        """EventBus ImportError → retry still returns FAILURE (fail-open)."""
        from unittest.mock import patch

        config = RetryPolicyConfig(max_attempts=1)
        policy = RetryPolicy(config=config)

        # Patch get_event_bus to raise ImportError inside _emit_exhausted_event
        with patch(
            "baldur.services.event_bus.get_event_bus",
            side_effect=ImportError("no event bus"),
        ):
            result = policy.execute(lambda: (_ for _ in ()).throw(ValueError("fail")))

        # Despite emission error, PolicyResult is returned
        assert result.outcome == PolicyOutcome.FAILURE

    def test_retry_exhausted_event_includes_context_identifiers(self):
        """Event payload includes order_id, user_id, trace_id from PolicyContext."""
        from unittest.mock import patch

        mock_bus = MagicMock()
        config = RetryPolicyConfig(max_attempts=1, domain="orders")
        policy = RetryPolicy(config=config)
        ctx = PolicyContext(order_id="ORD-123", user_id="USR-456", trace_id="abc-trace")

        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=mock_bus,
        ):
            policy.execute(
                lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                context=ctx,
            )

        event_data = mock_bus.emit.call_args.kwargs.get(
            "data"
        ) or mock_bus.emit.call_args[1].get("data")
        assert event_data["order_id"] == "ORD-123"
        assert event_data["user_id"] == "USR-456"
        assert event_data["trace_id"] == "abc-trace"

    def test_retry_exhausted_event_cb_fast_fail_has_attempts_1(self):
        """CB fast-fail emits RETRY_EXHAUSTED with attempts=1 (D13)."""
        from unittest.mock import patch

        from baldur.core.exceptions import CircuitBreakerError

        mock_bus = MagicMock()
        config = RetryPolicyConfig(max_attempts=5)
        policy = RetryPolicy(config=config)

        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=mock_bus,
        ):
            policy.execute(lambda: (_ for _ in ()).throw(CircuitBreakerError("OPEN")))

        event_data = mock_bus.emit.call_args.kwargs.get(
            "data"
        ) or mock_bus.emit.call_args[1].get("data")
        assert event_data["attempts"] == 1
        assert event_data["final_error_type"] == "CircuitBreakerError"


# =============================================================================
# RetryPolicy — Cooldown deferral exit (D2)
# =============================================================================


def _coordinator_with_cooldown(domain: str, seconds: float):
    """Real coordinator over an in-memory store with a shared cooldown installed."""
    import time

    storage = InMemoryRateLimitStorage()
    storage.set_cooldown(domain, time.time() + seconds)
    return RateLimitCoordinator(storage=storage), storage


class TestRetryPolicyCooldownDeferralBehavior:
    """A cooldown longer than the remaining budget defers the attempt, not sleeps it."""

    def test_deferral_exit_reason_and_not_before(self):
        """Over-budget cooldown -> reason=rate_limit_deferred, not_before on metadata."""
        coord, _ = _coordinator_with_cooldown("payment", 300.0)
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=3, domain="payment", max_elapsed=5.0),
            rate_limit_coordinator=coord,
            sleeper=lambda _: None,
        )

        result = policy.execute(lambda: "ok")

        assert result.outcome == PolicyOutcome.FAILURE
        assert result.metadata["reason"] == "rate_limit_deferred"
        assert result.metadata["not_before"] is not None

    def test_first_attempt_deferral_does_not_run_func_and_leaves_history_empty(self):
        """The deferred attempt never calls func; empty history is the discriminator."""
        coord, _ = _coordinator_with_cooldown("payment", 300.0)
        calls = []
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=3, domain="payment", max_elapsed=5.0),
            rate_limit_coordinator=coord,
            sleeper=lambda _: None,
        )

        def func():
            calls.append(1)
            return "ok"

        result = policy.execute(func)

        assert calls == []  # func never ran
        assert result.metadata["retry_history"] == []

    def test_first_attempt_deferral_synthesizes_deferred_error(self):
        """With no prior real error, the loop synthesizes a RateLimitDeferredError."""
        coord, _ = _coordinator_with_cooldown("payment", 300.0)
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=3, domain="payment", max_elapsed=5.0),
            rate_limit_coordinator=coord,
            sleeper=lambda _: None,
        )

        result = policy.execute(lambda: "ok")

        assert type(result.error) is RateLimitDeferredError

    def test_deferral_reason_is_not_exhausted_or_deadline(self):
        """Negative: the deferral outcome is distinct from exhaustion / budget breaks."""
        coord, _ = _coordinator_with_cooldown("payment", 300.0)
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=3, domain="payment", max_elapsed=5.0),
            rate_limit_coordinator=coord,
            sleeper=lambda _: None,
        )

        result = policy.execute(lambda: "ok")

        assert result.metadata["reason"] not in (
            "max_attempts",
            "max_elapsed",
            "deadline",
        )


class TestRetryPolicyDeferralSynthesisPrecedenceBehavior:
    """A rejected result on attempt 1 then a cooldown on attempt 2 yields the deferral error.

    ``last_error is None`` holds on BOTH a first-attempt deferral and a
    result-rejection exit, so the deferral synthesis must be ordered ahead of the
    result-rejection one — otherwise the exhaustion wording leaks onto an exit
    where retries were not exhausted and attempt 2 never ran.
    """

    def test_rejected_then_deferred_yields_deferred_error(self):
        import time

        storage = InMemoryRateLimitStorage()
        coord = RateLimitCoordinator(storage=storage)
        seq = []

        def func():
            seq.append(1)
            if len(seq) == 1:
                # Another worker's 429 installs a shared cooldown between attempts.
                storage.set_cooldown("payment", time.time() + 300.0)
                return "BAD"
            return "ok"

        policy = RetryPolicy(
            config=RetryPolicyConfig(
                max_attempts=3,
                domain="payment",
                max_elapsed=5.0,
                retry_on_result=lambda r: r == "BAD",
            ),
            rate_limit_coordinator=coord,
            sleeper=lambda _: None,
        )

        result = policy.execute(func)

        assert type(result.error) is RateLimitDeferredError
        assert result.metadata["reason"] == "rate_limit_deferred"
        # The rejected value still rides out, and only attempt 1 is recorded.
        assert result.value == "BAD"
        assert len(result.metadata["retry_history"]) == 1

    def test_rejected_then_deferred_message_has_no_exhaustion_wording(self):
        """Negative: the exhaustion message must not leak onto a deferral exit."""
        import time

        storage = InMemoryRateLimitStorage()
        coord = RateLimitCoordinator(storage=storage)
        seq = []

        def func():
            seq.append(1)
            if len(seq) == 1:
                storage.set_cooldown("payment", time.time() + 300.0)
                return "BAD"
            return "ok"

        policy = RetryPolicy(
            config=RetryPolicyConfig(
                max_attempts=3,
                domain="payment",
                max_elapsed=5.0,
                retry_on_result=lambda r: r == "BAD",
            ),
            rate_limit_coordinator=coord,
            sleeper=lambda _: None,
        )

        result = policy.execute(func)

        assert "Retry exhausted" not in str(result.error)


class TestRetryPolicyCoordinatorFailOpenBehavior:
    """A coordinator fault at any of the three loop call sites never changes the result."""

    def _coordinator_mock(self):
        """Spec'd coordinator; wait returns a real idle result (not a truthy mock)."""
        coord = MagicMock(spec=RateLimitCoordinator)
        coord.wait_if_needed.return_value = RateLimitResult(waited=False)
        return coord

    def test_wait_fault_preserves_success(self):
        """Site 1: wait_if_needed raises -> loop proceeds, SUCCESS preserved."""
        coord = self._coordinator_mock()
        coord.wait_if_needed.side_effect = RuntimeError("coordinator down")
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=2, domain="d"),
            rate_limit_coordinator=coord,
            sleeper=lambda _: None,
        )

        result = policy.execute(lambda: "ok")

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"

    def test_on_rate_limited_fault_preserves_business_error(self):
        """Site 2: on_rate_limited raises inside except -> business 429 error preserved."""
        coord = self._coordinator_mock()
        coord.on_rate_limited.side_effect = RuntimeError("coordinator down")
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=1, domain="d"),
            rate_limit_coordinator=coord,
            sleeper=lambda _: None,
        )

        business_error = Exception("429 too many requests")
        result = policy.execute(lambda: (_ for _ in ()).throw(business_error))

        assert result.error is business_error
        assert not isinstance(result.error, RuntimeError)

    def test_on_success_fault_preserves_success(self):
        """Site 3: on_success raises in the else clause -> SUCCESS not destroyed.

        The wait result reports a signal on purpose. ``on_success`` is only owed
        once this call has observed one, so an idle wait result would leave site
        3 unreached and this test asserting nothing — the failing mock would
        never be called at all.
        """
        coord = self._coordinator_mock()
        coord.wait_if_needed.return_value = RateLimitResult(
            waited=True, wait_time=0.01, was_rate_limited=True
        )
        coord.on_success.side_effect = RuntimeError("coordinator down")
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=2, domain="d"),
            rate_limit_coordinator=coord,
            sleeper=lambda _: None,
        )

        result = policy.execute(lambda: "ok")

        coord.on_success.assert_called_once()  # site 3 was actually reached
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"
        assert result.error is None
