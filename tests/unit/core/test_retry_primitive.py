"""
Core Retry Primitive 단위 테스트.

테스트 대상: core/retry.py
- RetryConfig: 기본값, 설정 검증
- RetryContext: 컨텍스트 구조
- RetryOutcome: 결과 구조
- retry_with_backoff(): 재시도 로직, 콜백, 비재시도 예외
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from baldur.core.backoff import BackoffStrategy, ExponentialBackoff
from baldur.core.retry import (
    RetryConfig,
    RetryContext,
    RetryOutcome,
    retry_with_backoff,
)

# =============================================================================
# RetryConfig — 계약 검증
# =============================================================================


class TestRetryConfigContract:
    """RetryConfig 기본값 계약 검증."""

    def test_max_retries_default_is_three(self):
        """max_retries 기본값: 3."""
        config = RetryConfig()
        assert config.max_retries == 3

    def test_retryable_exceptions_default_is_exception(self):
        """retryable_exceptions 기본값: (Exception,)."""
        config = RetryConfig()
        assert config.retryable_exceptions == (Exception,)

    def test_context_name_default_is_empty_string(self):
        """context_name 기본값: 빈 문자열."""
        config = RetryConfig()
        assert config.context_name == ""

    def test_on_retry_default_is_none(self):
        """on_retry 기본값: None."""
        config = RetryConfig()
        assert config.on_retry is None

    def test_on_exhausted_default_is_none(self):
        """on_exhausted 기본값: None."""
        config = RetryConfig()
        assert config.on_exhausted is None

    def test_backoff_default_is_exponential(self):
        """backoff 기본값은 ExponentialBackoff 인스턴스."""
        config = RetryConfig()
        assert isinstance(config.backoff, ExponentialBackoff)


# =============================================================================
# RetryContext — 계약 검증
# =============================================================================


class TestRetryContextContract:
    """RetryContext 구조 계약 검증."""

    def test_metric_labels_default_is_empty_dict(self):
        """metric_labels 기본값: 빈 딕셔너리."""
        ctx = RetryContext(
            func_name="test", attempt=0, max_retries=3, wait_time=0.0, elapsed_total=0.0
        )
        assert ctx.metric_labels == {}

    def test_trace_id_default_is_none(self):
        """trace_id 기본값: None."""
        ctx = RetryContext(
            func_name="test", attempt=0, max_retries=3, wait_time=0.0, elapsed_total=0.0
        )
        assert ctx.trace_id is None


# =============================================================================
# RetryOutcome — 계약 검증
# =============================================================================


class TestRetryOutcomeContract:
    """RetryOutcome 구조 계약 검증."""

    def test_result_default_is_none(self):
        """result 기본값: None."""
        outcome = RetryOutcome(success=False)
        assert outcome.result is None

    def test_exception_default_is_none(self):
        """exception 기본값: None."""
        outcome = RetryOutcome(success=True)
        assert outcome.exception is None

    def test_attempts_default_is_zero(self):
        """attempts 기본값: 0."""
        outcome = RetryOutcome(success=True)
        assert outcome.attempts == 0

    def test_total_wait_seconds_default_is_zero(self):
        """total_wait_seconds 기본값: 0.0."""
        outcome = RetryOutcome(success=True)
        assert outcome.total_wait_seconds == 0.0


# =============================================================================
# retry_with_backoff — 동작 검증
# =============================================================================


class TestRetryWithBackoffBehavior:
    """retry_with_backoff 함수 동작 검증."""

    def test_success_on_first_attempt_returns_result(self):
        """첫 번째 시도 성공 시 결과를 반환한다."""
        func = MagicMock(return_value="ok")
        config = RetryConfig(max_retries=3)

        outcome = retry_with_backoff(func, config)

        assert outcome.success is True
        assert outcome.result == "ok"
        assert outcome.attempts == 1
        assert func.call_count == 1

    def test_success_after_retries_returns_result(self):
        """실패 후 재시도 성공 시 결과를 반환한다."""
        func = MagicMock(side_effect=[ValueError("fail"), "ok"])
        config = RetryConfig(max_retries=3)

        with patch("baldur.core.retry.time.sleep"):
            outcome = retry_with_backoff(func, config)

        assert outcome.success is True
        assert outcome.result == "ok"
        assert outcome.attempts == 2

    def test_all_retries_exhausted_returns_failure(self):
        """모든 재시도 소진 시 실패를 반환한다."""
        error = ValueError("persistent failure")
        func = MagicMock(side_effect=error)
        config = RetryConfig(max_retries=3)

        with patch("baldur.core.retry.time.sleep"):
            outcome = retry_with_backoff(func, config)

        assert outcome.success is False
        assert outcome.exception is error
        assert outcome.attempts == config.max_retries

    def test_non_retryable_exception_fails_immediately(self):
        """retryable_exceptions에 해당하지 않는 예외는 즉시 실패한다."""
        func = MagicMock(side_effect=TypeError("wrong type"))
        config = RetryConfig(
            max_retries=5,
            retryable_exceptions=(ValueError,),
        )

        outcome = retry_with_backoff(func, config)

        assert outcome.success is False
        assert isinstance(outcome.exception, TypeError)
        assert outcome.attempts == 1
        assert func.call_count == 1

    def test_passes_args_and_kwargs_to_func(self):
        """func에 args, kwargs를 전달한다."""
        func = MagicMock(return_value="result")
        config = RetryConfig(max_retries=1)

        retry_with_backoff(func, config, "arg1", "arg2", key="value")

        func.assert_called_once_with("arg1", "arg2", key="value")

    def test_on_retry_callback_invoked_on_failure(self):
        """실패 시 on_retry 콜백이 호출된다."""
        on_retry = MagicMock()
        func = MagicMock(side_effect=[ValueError("fail"), "ok"])
        config = RetryConfig(max_retries=3, on_retry=on_retry)

        with patch("baldur.core.retry.time.sleep"):
            retry_with_backoff(func, config)

        assert on_retry.call_count == 1
        ctx_arg = on_retry.call_args[0][0]
        exc_arg = on_retry.call_args[0][1]
        assert isinstance(ctx_arg, RetryContext)
        assert isinstance(exc_arg, ValueError)

    def test_on_exhausted_callback_invoked_when_all_retries_fail(self):
        """모든 재시도 소진 시 on_exhausted 콜백이 호출된다."""
        on_exhausted = MagicMock()
        error = ValueError("always fails")
        func = MagicMock(side_effect=error)
        config = RetryConfig(max_retries=2, on_exhausted=on_exhausted)

        with patch("baldur.core.retry.time.sleep"):
            retry_with_backoff(func, config)

        on_exhausted.assert_called_once()
        ctx_arg = on_exhausted.call_args[0][0]
        exc_arg = on_exhausted.call_args[0][1]
        assert isinstance(ctx_arg, RetryContext)
        assert exc_arg is error

    def test_on_exhausted_not_called_on_success(self):
        """성공 시 on_exhausted 콜백이 호출되지 않는다."""
        on_exhausted = MagicMock()
        func = MagicMock(return_value="ok")
        config = RetryConfig(max_retries=3, on_exhausted=on_exhausted)

        retry_with_backoff(func, config)

        on_exhausted.assert_not_called()

    def test_context_name_used_as_func_name_in_context(self):
        """context_name이 RetryContext.func_name으로 사용된다."""
        on_retry = MagicMock()
        func = MagicMock(side_effect=[ValueError("fail"), "ok"])
        config = RetryConfig(max_retries=3, context_name="payment", on_retry=on_retry)

        with patch("baldur.core.retry.time.sleep"):
            retry_with_backoff(func, config)

        ctx_arg = on_retry.call_args[0][0]
        assert ctx_arg.func_name == "payment"

    def test_func_name_fallback_to_callable_name(self):
        """context_name이 비어있으면 func.__name__을 사용한다."""
        on_retry = MagicMock()

        def my_function():
            raise ValueError("fail")

        call_count = 0

        def tracked_func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ValueError("fail")
            return "ok"

        tracked_func.__name__ = "my_function"
        config = RetryConfig(max_retries=3, on_retry=on_retry)

        with patch("baldur.core.retry.time.sleep"):
            retry_with_backoff(tracked_func, config)

        ctx_arg = on_retry.call_args[0][0]
        assert ctx_arg.func_name == "my_function"

    def test_sleep_called_between_retries(self):
        """재시도 사이에 sleep이 호출된다."""
        func = MagicMock(side_effect=[ValueError("fail"), ValueError("fail"), "ok"])
        config = RetryConfig(max_retries=3)

        with patch("baldur.core.retry.time.sleep") as mock_sleep:
            retry_with_backoff(func, config)

        assert mock_sleep.call_count == 2

    def test_no_sleep_on_last_failed_attempt(self):
        """마지막 실패 시도 후에는 sleep이 호출되지 않는다."""
        func = MagicMock(side_effect=ValueError("fail"))
        config = RetryConfig(max_retries=2)

        with patch("baldur.core.retry.time.sleep") as mock_sleep:
            retry_with_backoff(func, config)

        # max_retries=2, sleep should be called only between attempt 1 and 2
        assert mock_sleep.call_count == 1

    def test_total_wait_seconds_accumulated(self):
        """total_wait_seconds가 누적된다."""
        func = MagicMock(side_effect=ValueError("fail"))
        backoff = MagicMock(spec=ExponentialBackoff)
        backoff.calculate.return_value = 1.5
        config = RetryConfig(max_retries=3, backoff=backoff)

        with patch("baldur.core.retry.time.sleep"):
            outcome = retry_with_backoff(func, config)

        # 2 waits (between attempt 1-2 and 2-3), each 1.5s
        assert outcome.total_wait_seconds == pytest.approx(3.0)

    def test_max_retries_one_means_single_attempt(self):
        """max_retries=1이면 한 번만 시도한다."""
        func = MagicMock(side_effect=ValueError("fail"))
        config = RetryConfig(max_retries=1)

        outcome = retry_with_backoff(func, config)

        assert outcome.success is False
        assert outcome.attempts == 1
        assert func.call_count == 1


# =============================================================================
# retry_with_backoff — wait-duration wiring verification
# =============================================================================


class TestRetryWithBackoffWaitDurationBehavior:
    """Verify the backoff-calculated wait value is actually wired through to
    time.sleep, the RetryContext payload, and the outcome — not merely that
    sleep was called the right number of times.
    """

    def test_sleep_receives_each_backoff_calculated_wait_value(self):
        """time.sleep receives the exact per-attempt backoff value, in order."""
        # Given a deterministic backoff returning distinct values per attempt
        func = MagicMock(side_effect=ValueError("fail"))
        backoff = MagicMock(spec=BackoffStrategy)
        backoff.calculate.side_effect = [3.0, 5.0]
        config = RetryConfig(max_retries=3, backoff=backoff)

        # When all attempts fail
        with patch("baldur.core.retry.time.sleep") as mock_sleep:
            retry_with_backoff(func, config)

        # Then sleep got the calculated values (not just the right count)
        assert mock_sleep.call_args_list == [call(3.0), call(5.0)]
        # and calculate was queried with the 0-based attempt index
        assert backoff.calculate.call_args_list == [call(0), call(1)]

    def test_on_retry_context_carries_wait_time_and_elapsed_total(self):
        """RetryContext given to on_retry carries the wait and cumulative elapsed."""
        # Given distinct per-attempt waits
        func = MagicMock(side_effect=ValueError("fail"))
        backoff = MagicMock(spec=BackoffStrategy)
        backoff.calculate.side_effect = [3.0, 5.0]
        on_retry = MagicMock()
        config = RetryConfig(max_retries=3, backoff=backoff, on_retry=on_retry)

        # When all attempts fail
        with patch("baldur.core.retry.time.sleep"):
            retry_with_backoff(func, config)

        # Then each context reflects that attempt's wait and the running total.
        # The final attempt does not back off, so its wait is 0.0.
        contexts = [c.args[0] for c in on_retry.call_args_list]
        assert [ctx.wait_time for ctx in contexts] == [3.0, 5.0, 0.0]
        assert [ctx.elapsed_total for ctx in contexts] == [3.0, 8.0, 8.0]

    def test_on_exhausted_context_carries_zero_wait_and_total_elapsed(self):
        """RetryContext given to on_exhausted has wait_time 0.0 and full elapsed."""
        # Given distinct per-attempt waits
        func = MagicMock(side_effect=ValueError("fail"))
        backoff = MagicMock(spec=BackoffStrategy)
        backoff.calculate.side_effect = [3.0, 5.0]
        on_exhausted = MagicMock()
        config = RetryConfig(max_retries=3, backoff=backoff, on_exhausted=on_exhausted)

        # When all attempts are exhausted
        with patch("baldur.core.retry.time.sleep"):
            retry_with_backoff(func, config)

        # Then the exhausted context reports no pending wait but the full total
        ctx = on_exhausted.call_args.args[0]
        assert ctx.wait_time == 0.0
        assert ctx.elapsed_total == 8.0

    def test_total_wait_seconds_preserved_on_non_retryable_after_retry(self):
        """A non-retryable failure after a retry still reports accumulated wait."""
        # Given one retryable failure (which accrues wait) then a fatal one
        func = MagicMock(side_effect=[ValueError("retry me"), TypeError("fatal")])
        backoff = MagicMock(spec=BackoffStrategy)
        backoff.calculate.side_effect = [4.0]
        config = RetryConfig(
            max_retries=5,
            backoff=backoff,
            retryable_exceptions=(ValueError,),
        )

        # When the non-retryable exception aborts the loop
        with patch("baldur.core.retry.time.sleep"):
            outcome = retry_with_backoff(func, config)

        # Then the accumulated wait so far is preserved on the outcome
        assert outcome.success is False
        assert isinstance(outcome.exception, TypeError)
        assert outcome.total_wait_seconds == 4.0


# =============================================================================
# retry_with_backoff — 멱등성 검증
# =============================================================================


class TestRetryWithBackoffIdempotencyBehavior:
    """retry_with_backoff 멱등성 검증."""

    def test_same_input_produces_same_success_outcome(self):
        """동일 입력에 대해 동일한 성공 결과를 반환한다."""
        func = MagicMock(return_value=42)
        config = RetryConfig(max_retries=3)

        outcome1 = retry_with_backoff(func, config)
        outcome2 = retry_with_backoff(func, config)

        assert outcome1.success == outcome2.success
        assert outcome1.result == outcome2.result


# =============================================================================
# retry_with_backoff — 데이터 불변성 검증
# =============================================================================


class TestRetryWithBackoffImmutabilityBehavior:
    """retry_with_backoff 입력 데이터 불변성 검증."""

    def test_config_not_mutated_after_call(self):
        """retry_with_backoff 호출 후 config가 변경되지 않는다."""
        config = RetryConfig(max_retries=3, context_name="test")
        original_max = config.max_retries
        original_name = config.context_name

        func = MagicMock(return_value="ok")
        retry_with_backoff(func, config)

        assert config.max_retries == original_max
        assert config.context_name == original_name
