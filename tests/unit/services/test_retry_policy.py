"""
RetryPolicy 순수 재시도 정책 단위 테스트.

테스트 대상: services/retry_handler/policy.py
- 핵심 재시도 루프 (성공, 실패, 재시도, 예외 분류)
- Collaborator 주입: sleeper, retry_budget, rate_limit_coordinator, backoff
- 429 rate limit 감지
- PolicyContext 전달
"""

from __future__ import annotations

from unittest.mock import MagicMock

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
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.policy import RetryPolicy
from baldur.services.retry_handler.rate_limit_detection import (  # noqa: F401
    RATE_LIMIT_INDICATORS,
)

# =============================================================================
# RetryPolicy — 계약 검증
# =============================================================================


class TestRetryPolicyContract:
    """RetryPolicy 고정 식별자 및 결과 구조 검증."""

    def test_retry_policy_is_resilience_policy(self):
        """RetryPolicy는 ResiliencePolicy Protocol과 isinstance 호환이다."""
        policy = RetryPolicy(config=RetryPolicyConfig(max_attempts=1))
        assert isinstance(policy, ResiliencePolicy)

    def test_name_is_retry(self):
        """RetryPolicy.name은 'retry'이다."""
        policy = RetryPolicy(config=RetryPolicyConfig(max_attempts=1))
        assert policy.name == "retry"

    def test_rate_limit_indicators_contain_expected_keywords(self):
        """RATE_LIMIT_INDICATORS에 429, rate limit, throttle 등이 포함된다."""
        assert "429" in RATE_LIMIT_INDICATORS
        assert "rate limit" in RATE_LIMIT_INDICATORS
        assert "throttle" in RATE_LIMIT_INDICATORS
        assert "too many requests" in RATE_LIMIT_INDICATORS

    def test_success_result_has_retry_in_executed_policies(self):
        """성공 결과의 executed_policies에 'retry'가 포함된다."""
        policy = RetryPolicy(config=RetryPolicyConfig(max_attempts=1))
        result = policy.execute(lambda: "ok")
        assert "retry" in result.executed_policies

    def test_failure_metadata_contains_should_dlq(self):
        """실패 결과의 metadata에 should_dlq 키가 포함된다."""
        policy = RetryPolicy(config=RetryPolicyConfig(max_attempts=1, enable_dlq=True))
        result = policy.execute(lambda: (_ for _ in ()).throw(Exception("fail")))
        assert "should_dlq" in result.metadata

    def test_failure_metadata_contains_domain(self):
        """실패 결과의 metadata에 domain이 포함된다."""
        policy = RetryPolicy(config=RetryPolicyConfig(max_attempts=1, domain="payment"))
        result = policy.execute(lambda: (_ for _ in ()).throw(Exception("fail")))
        assert result.metadata["domain"] == "payment"


# =============================================================================
# RetryPolicy — 핵심 재시도 동작
# =============================================================================


class TestRetryPolicyExecuteBehavior:
    """RetryPolicy.execute() 핵심 재시도 동작 검증."""

    def test_success_first_attempt(self):
        """첫 시도에서 성공하면 SUCCESS를 반환한다."""
        policy = RetryPolicy(config=RetryPolicyConfig(max_attempts=3))
        result = policy.execute(lambda: "ok")
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"
        assert result.total_attempts == 1

    def test_success_after_retry(self):
        """첫 시도 실패 후 두 번째 시도에서 성공한다."""
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
        """모든 시도 소진 시 FAILURE를 반환한다."""
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
        """non_retryable_exceptions에 해당하면 즉시 중단된다."""
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
        """retryable_exceptions에 해당하는 예외만 재시도를 트리거한다."""
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
        """retryable_exceptions에 없는 예외는 재시도하지 않는다."""
        config = RetryPolicyConfig(
            max_attempts=3, retryable_exceptions=(ConnectionError,)
        )
        policy = RetryPolicy(config=config)
        result = policy.execute(lambda: (_ for _ in ()).throw(TypeError("wrong type")))
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.total_attempts == 1

    def test_retry_history_records_all_failed_attempts(self):
        """retry_history에 모든 실패 시도가 기록된다."""
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
        """should_dlq 플래그는 config.enable_dlq 값을 반영한다."""
        for enable_dlq in (True, False):
            policy = RetryPolicy(
                config=RetryPolicyConfig(max_attempts=1, enable_dlq=enable_dlq)
            )
            result = policy.execute(lambda: (_ for _ in ()).throw(Exception("fail")))
            assert result.metadata["should_dlq"] is enable_dlq

    def test_max_attempts_in_failure_metadata(self):
        """실패 metadata에 max_attempts가 포함된다."""
        config = RetryPolicyConfig(max_attempts=5)
        policy = RetryPolicy(
            config=config, backoff=ConstantBackoff(delay=0.0), sleeper=lambda _: None
        )
        result = policy.execute(lambda: (_ for _ in ()).throw(Exception("fail")))
        assert result.metadata["max_attempts"] == config.max_attempts


# =============================================================================
# RetryPolicy — Sleeper 대기 함수 주입
# =============================================================================


class TestRetryPolicySleeperBehavior:
    """RetryPolicy sleeper 대기 함수 주입 동작 검증."""

    def test_sleeper_none_skips_sleep(self):
        """sleeper=None이면 sleep을 수행하지 않는다."""
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=2),
            backoff=ConstantBackoff(delay=1.0),
            sleeper=None,
        )
        result = policy.execute(lambda: (_ for _ in ()).throw(Exception("fail")))
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.total_attempts == 2

    def test_sleeper_called_with_backoff_delay(self):
        """sleeper가 제공되면 backoff 지연값으로 호출된다."""
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
        """delay가 0이면 sleeper를 호출하지 않는다."""
        mock_sleeper = MagicMock()
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=2),
            backoff=ConstantBackoff(delay=0.0),
            sleeper=mock_sleeper,
        )
        policy.execute(lambda: (_ for _ in ()).throw(Exception("fail")))
        mock_sleeper.assert_not_called()

    def test_sleeper_not_called_on_first_attempt_success(self):
        """첫 시도 성공 시 sleeper가 호출되지 않는다."""
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
    """RetryPolicy retry_budget Collaborator 동작 검증."""

    def test_record_request_called_per_attempt(self):
        """retry_budget.record_request()가 매 시도마다 호출된다."""
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
        """retry_budget.should_allow_retry()가 False면 루프를 중단한다."""
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
        """retry_budget=None이면 budget 체크 없이 모든 시도를 수행한다."""
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
    """RetryPolicy rate_limit_coordinator Collaborator 동작 검증."""

    def test_wait_if_needed_called_before_execution(self):
        """rate_limit_coordinator.wait_if_needed()가 함수 실행 전에 호출된다."""
        mock_coord = MagicMock()
        mock_coord.wait_if_needed.return_value = MagicMock(waited=False)

        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=1, domain="payment"),
            rate_limit_coordinator=mock_coord,
        )
        policy.execute(lambda: "ok")
        mock_coord.wait_if_needed.assert_called_once_with("payment")

    def test_on_success_called_after_success(self):
        """성공 시 rate_limit_coordinator.on_success()가 호출된다."""
        mock_coord = MagicMock()
        mock_coord.wait_if_needed.return_value = MagicMock(waited=False)

        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=1, domain="payment"),
            rate_limit_coordinator=mock_coord,
        )
        policy.execute(lambda: "ok")
        mock_coord.on_success.assert_called_once_with("payment")

    def test_coordinator_none_skips_rate_limit(self):
        """rate_limit_coordinator=None이면 rate limit 체크 없이 실행된다."""
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
    """RetryPolicy backoff Collaborator 동작 검증."""

    def test_default_backoff_is_exponential(self):
        """backoff=None이면 ExponentialBackoff가 기본 생성된다."""
        policy = RetryPolicy(config=RetryPolicyConfig(max_attempts=1))
        assert isinstance(policy._backoff, ExponentialBackoff)

    def test_default_backoff_uses_config_values(self):
        """기본 ExponentialBackoff는 config의 backoff 설정을 사용한다."""
        config = RetryPolicyConfig(backoff_base=10, backoff_max=300, jitter_percent=50)
        policy = RetryPolicy(config=config)
        backoff = policy._backoff
        assert backoff.base_delay == config.backoff_base
        assert backoff.max_delay == config.backoff_max
        assert backoff.jitter_factor == config.jitter_percent / 100.0

    def test_custom_backoff_strategy_injected(self):
        """커스텀 BackoffStrategy가 주입되면 그것을 사용한다."""
        custom_backoff = LinearBackoff(base_delay=1.0, increment=2.0)
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=1), backoff=custom_backoff
        )
        assert policy._backoff is custom_backoff

    def test_backoff_calculate_receives_context(self):
        """backoff.calculate()가 PolicyContext와 함께 호출된다."""
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
# RetryPolicy — 429 Rate Limit 감지
# =============================================================================


class TestRetryPolicyDetectRateLimitBehavior:
    """RetryPolicy._detect_rate_limit() 정적 메서드 동작 검증."""

    def test_detect_429_in_message(self):
        """에러 메시지에 429가 포함되면 True를 반환한다."""
        is_rate, _ = RetryPolicy._detect_rate_limit(
            Exception("HTTP 429 Too Many Requests")
        )
        assert is_rate is True

    def test_detect_rate_limit_keyword(self):
        """'rate limit' 키워드가 있으면 True를 반환한다."""
        is_rate, _ = RetryPolicy._detect_rate_limit(Exception("Rate limit exceeded"))
        assert is_rate is True

    def test_detect_throttle_keyword(self):
        """'throttle' 키워드가 있으면 True를 반환한다."""
        is_rate, _ = RetryPolicy._detect_rate_limit(Exception("Request throttled"))
        assert is_rate is True

    def test_normal_error_not_detected(self):
        """일반 에러는 rate limit으로 감지되지 않는다."""
        is_rate, _ = RetryPolicy._detect_rate_limit(ValueError("Invalid input"))
        assert is_rate is False

    def test_extract_retry_after_attribute(self):
        """예외에 retry_after 속성이 있으면 추출한다."""
        err = Exception("429")
        err.retry_after = 30.0
        is_rate, retry_after = RetryPolicy._detect_rate_limit(err)
        assert is_rate is True
        assert retry_after == 30.0

    def test_extract_retry_after_from_response_headers(self):
        """예외.response.headers['Retry-After']에서 값을 추출한다."""
        err = Exception("429")
        mock_response = MagicMock()
        mock_response.headers = {"Retry-After": "60"}
        err.response = mock_response
        is_rate, retry_after = RetryPolicy._detect_rate_limit(err)
        assert is_rate is True
        assert retry_after == 60.0

    def test_no_retry_after_returns_none(self):
        """retry_after 정보가 없으면 None을 반환한다."""
        is_rate, retry_after = RetryPolicy._detect_rate_limit(Exception("429 error"))
        assert is_rate is True
        assert retry_after is None


# =============================================================================
# RetryPolicy — PolicyContext 전달
# =============================================================================


class TestRetryPolicyContextBehavior:
    """RetryPolicy.execute()에 PolicyContext 전달 동작 검증."""

    def test_execute_accepts_context_parameter(self):
        """execute()가 context 파라미터를 수용한다."""
        ctx = PolicyContext(order_id="ORD-123", tier_id="critical")
        policy = RetryPolicy(config=RetryPolicyConfig(max_attempts=1))
        result = policy.execute(lambda: "ok", context=ctx)
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_execute_works_without_context(self):
        """context=None이어도 정상 동작한다."""
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
