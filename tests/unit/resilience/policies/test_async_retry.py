"""
AsyncRetryPolicy / async_retry_policy 단위 테스트 (#333).

테스트 대상:
- resilience/policies/async_retry.py (AsyncRetryPolicy, async_retry_policy)

Note: the ``@retried_async`` decorator was removed in 670 (superseded by the
unified ``@retry``). Its dual-dispatch replacement is covered by
test_retry_decorator.py.

UNIT_TEST_GUIDELINES.md 준수:
- 계약 검증(Contract): 하드코딩 기대값 (name, __all__, 기본값)
- 동작 검증(Behavior): 소스 참조 (PolicyOutcome, BackoffStrategy)
- conftest.py 배치: 1개 파일 전용 fixture → 파일 내부 (§5.1)
- Mock autospec: autospec=True 사용 (§6.2)
- 시간 의존성: asyncio.sleep 모킹 (§6.3)

검증 기법:
- §8.2 예외/엣지 케이스 — CancelledError, BaseException, non-retryable
- §8.4 부수효과 — structlog 로깅 이벤트
- §8.5 의존성 상호작용 — backoff.calculate, asyncio.sleep 호출 검증
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.core.backoff import (
    BackoffStrategy,
    ConstantBackoff,
    ExponentialBackoff,
)
from baldur.interfaces.resilience_policy import (
    AsyncResiliencePolicy,
    PolicyContext,
    PolicyOutcome,
)
from baldur.resilience.policies.async_retry import (
    AsyncRetryPolicy,
    async_retry_policy,
)

# =============================================================================
# 계약 검증 — AsyncRetryPolicy 인터페이스 / 기본값
# =============================================================================


class TestAsyncRetryPolicyContract:
    """AsyncRetryPolicy 고정 식별자 및 기본값 계약 검증."""

    def test_name_is_retry(self):
        """name property는 'retry'이다."""
        policy = AsyncRetryPolicy()
        assert policy.name == "retry"

    def test_default_max_retries_is_three(self):
        """기본 max_retries는 3이다."""
        policy = AsyncRetryPolicy()
        assert policy._max_retries == 3

    def test_default_backoff_is_exponential(self):
        """기본 backoff는 ExponentialBackoff이다."""
        policy = AsyncRetryPolicy()
        assert isinstance(policy._backoff, ExponentialBackoff)

    def test_default_retryable_exceptions_is_exception(self):
        """기본 retryable_exceptions는 (Exception,)이다."""
        policy = AsyncRetryPolicy()
        assert policy._retryable_exceptions == (Exception,)

    def test_satisfies_async_resilience_protocol(self):
        """AsyncResiliencePolicy Protocol을 만족한다."""
        policy = AsyncRetryPolicy()
        assert isinstance(policy, AsyncResiliencePolicy)

    @pytest.mark.asyncio
    async def test_success_result_has_retry_in_executed_policies(self):
        """성공 결과의 executed_policies에 'retry'가 포함된다."""

        async def ok():
            return "ok"

        policy = AsyncRetryPolicy()
        result = await policy.execute(ok)
        assert "retry" in result.executed_policies

    def test_module_all_contains_three_exports(self):
        """__all__은 정확히 3개 항목을 포함한다."""
        import baldur.resilience.policies.async_retry as mod

        assert len(mod.__all__) == 3
        assert "AsyncRetryPolicy" in mod.__all__
        assert "async_retry_policy" in mod.__all__
        assert "retry" in mod.__all__


# =============================================================================
# 동작 검증 — AsyncRetryPolicy.execute() 기본 동작
# =============================================================================


class TestAsyncRetryPolicyExecuteBehavior:
    """AsyncRetryPolicy.execute() 기본 동작 검증."""

    @pytest.mark.asyncio
    async def test_success_on_first_attempt_returns_value(self):
        """첫 시도 성공 시 값과 attempts=1을 반환한다."""

        async def ok():
            return 42

        policy = AsyncRetryPolicy()
        result = await policy.execute(ok)

        assert result.success is True
        assert result.value == 42
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.total_attempts == 1

    @pytest.mark.asyncio
    async def test_success_after_retries_returns_correct_attempts(self):
        """N회 실패 후 성공 시 올바른 attempts를 반환한다."""
        call_count = 0

        async def fail_then_succeed():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("transient")
            return "recovered"

        policy = AsyncRetryPolicy(max_retries=5)
        with patch(
            "baldur.resilience.policies.async_retry.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            result = await policy.execute(fail_then_succeed)

        assert result.success is True
        assert result.value == "recovered"
        assert result.total_attempts == 3

    @pytest.mark.asyncio
    async def test_exhausted_returns_failure_with_last_error(self):
        """모든 재시도 소진 시 FAILURE outcome과 마지막 에러를 반환한다."""

        async def always_fail():
            raise ConnectionError("down")

        policy = AsyncRetryPolicy(max_retries=2)
        with patch(
            "baldur.resilience.policies.async_retry.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            result = await policy.execute(always_fail)

        assert result.success is False
        assert result.outcome == PolicyOutcome.FAILURE
        assert isinstance(result.error, ConnectionError)
        assert result.total_attempts == 3  # 1 initial + 2 retries

    @pytest.mark.asyncio
    async def test_non_retryable_exception_not_retried(self):
        """Non-retryable exception returns FAILURE immediately (D14 convergence)."""
        call_count = 0

        async def raise_value_error():
            nonlocal call_count
            call_count += 1
            raise ValueError("bad input")

        policy = AsyncRetryPolicy(
            max_retries=3,
            retryable_exceptions=(ConnectionError,),
        )

        # D14: now returns PolicyResult(FAILURE) instead of propagating raw exception
        result = await policy.execute(raise_value_error)
        assert result.outcome == PolicyOutcome.FAILURE
        assert isinstance(result.error, ValueError)
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_sync_func_wrapped_in_to_thread(self):
        """sync 함수가 asyncio.to_thread()로 실행된다."""

        def sync_func():
            return "sync_result"

        policy = AsyncRetryPolicy()
        with patch(
            "baldur.resilience.policies.async_retry.asyncio.to_thread",
            new_callable=AsyncMock,
            return_value="sync_result",
        ) as mock_to_thread:
            result = await policy.execute(sync_func)

        assert result.value == "sync_result"
        mock_to_thread.assert_called_once_with(sync_func)

    @pytest.mark.asyncio
    async def test_args_and_kwargs_forwarded_to_async_func(self):
        """위치/키워드 인수가 async 함수에 전달된다."""
        received_args = {}

        async def capture(a, b, key=None):
            received_args["a"] = a
            received_args["b"] = b
            received_args["key"] = key
            return "ok"

        policy = AsyncRetryPolicy()
        await policy.execute(capture, 1, 2, key="val")

        assert received_args == {"a": 1, "b": 2, "key": "val"}

    @pytest.mark.asyncio
    async def test_args_and_kwargs_forwarded_to_sync_func(self):
        """위치/키워드 인수가 sync 함수에 to_thread로 전달된다."""

        def sync_capture(a, b, key=None):
            return (a, b, key)

        policy = AsyncRetryPolicy()
        with patch(
            "baldur.resilience.policies.async_retry.asyncio.to_thread",
            new_callable=AsyncMock,
            return_value=(1, 2, "val"),
        ) as mock_to_thread:
            result = await policy.execute(sync_capture, 1, 2, key="val")

        mock_to_thread.assert_called_once_with(sync_capture, 1, 2, key="val")
        assert result.value == (1, 2, "val")


# =============================================================================
# 동작 검증 — Backoff delay 및 Jitter
# =============================================================================


class TestAsyncRetryPolicyBackoffBehavior:
    """AsyncRetryPolicy backoff delay 적용 검증."""

    @pytest.mark.asyncio
    async def test_backoff_delay_applied_between_retries(self):
        """재시도 간 backoff 지연이 asyncio.sleep으로 적용된다."""
        call_count = 0

        async def fail_twice():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ConnectionError("fail")
            return "ok"

        backoff = ConstantBackoff(delay=1.5, jitter=False)
        policy = AsyncRetryPolicy(max_retries=3, backoff=backoff)

        with patch(
            "baldur.resilience.policies.async_retry.asyncio.sleep",
            new_callable=AsyncMock,
        ) as mock_sleep:
            result = await policy.execute(fail_twice)

        assert result.success is True
        assert mock_sleep.call_count == 2
        for call in mock_sleep.call_args_list:
            assert call.args[0] == pytest.approx(1.5)

    @pytest.mark.asyncio
    async def test_backoff_calculate_called_with_attempt_and_context(self):
        """backoff.calculate(attempt, context=context) 호출을 검증한다."""

        async def always_fail():
            raise ConnectionError("fail")

        mock_backoff = MagicMock(spec=BackoffStrategy)
        mock_backoff.calculate.return_value = 0.01
        ctx = PolicyContext(domain="test")

        policy = AsyncRetryPolicy(max_retries=1, backoff=mock_backoff)
        with patch(
            "baldur.resilience.policies.async_retry.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            await policy.execute(always_fail, context=ctx)

        # attempt=0 (first failure), context should have retry_attempt=1
        mock_backoff.calculate.assert_called_once()
        call_args = mock_backoff.calculate.call_args
        assert call_args.args[0] == 0  # attempt index
        assert call_args.kwargs["context"] is not None
        assert call_args.kwargs["context"].extra["retry_attempt"] == 1

    @pytest.mark.asyncio
    async def test_jitter_controlled_by_backoff_strategy(self):
        """Jitter 활성화/비활성화는 BackoffStrategy 파라미터로 제어된다."""
        # jitter=False → delay가 정확히 일정
        backoff_no_jitter = ConstantBackoff(delay=1.0, jitter=False)
        delays_no_jitter = [backoff_no_jitter.calculate(i) for i in range(5)]
        assert all(d == pytest.approx(1.0) for d in delays_no_jitter)

        # jitter=True → delay가 변동 (확률적이지만 100% 동일하지 않을 것)
        backoff_with_jitter = ConstantBackoff(delay=1.0, jitter=True, jitter_factor=0.5)
        delays_with_jitter = [backoff_with_jitter.calculate(i) for i in range(20)]
        # 20회 중 최소 1번은 정확히 1.0이 아닐 것 (확률적 보장)
        assert not all(d == 1.0 for d in delays_with_jitter)

    @pytest.mark.asyncio
    async def test_no_sleep_on_final_failure(self):
        """마지막 실패(소진) 시에는 asyncio.sleep이 호출되지 않는다."""

        async def always_fail():
            raise ConnectionError("fail")

        policy = AsyncRetryPolicy(max_retries=2)
        with patch(
            "baldur.resilience.policies.async_retry.asyncio.sleep",
            new_callable=AsyncMock,
        ) as mock_sleep:
            await policy.execute(always_fail)

        # max_retries=2 → 3 attempts total → 2 sleeps (between 1-2, 2-3)
        assert mock_sleep.call_count == 2


# =============================================================================
# 동작 검증 — CancelledError 방어
# =============================================================================


class TestAsyncRetryCancellationBehavior:
    """asyncio.CancelledError 방어 로직 검증."""

    @pytest.mark.asyncio
    async def test_cancelled_error_not_retried(self):
        """CancelledError는 재시도하지 않고 즉시 전파한다."""
        policy = AsyncRetryPolicy(max_retries=3)

        async def raise_cancelled():
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await policy.execute(raise_cancelled)

    @pytest.mark.asyncio
    async def test_cancelled_error_with_broad_retryable(self):
        """retryable_exceptions=(Exception,)이어도 CancelledError는 통과한다."""
        policy = AsyncRetryPolicy(max_retries=3, retryable_exceptions=(Exception,))

        async def raise_cancelled():
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await policy.execute(raise_cancelled)

    @pytest.mark.asyncio
    async def test_concurrent_retries_with_cancellation(self):
        """여러 태스크가 동시에 재시도 중일 때 일부 취소된다."""
        policy = AsyncRetryPolicy(max_retries=5)
        call_count = 0

        async def slow_fail():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0)  # yield control without real delay
            raise ConnectionError("timeout")

        with patch(
            "baldur.resilience.policies.async_retry.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            tasks = [asyncio.create_task(policy.execute(slow_fail)) for _ in range(5)]
            await asyncio.sleep(0)  # yield to let tasks start
            tasks[0].cancel()

            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert isinstance(results[0], asyncio.CancelledError)

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_not_retried(self):
        """KeyboardInterrupt는 재시도하지 않고 즉시 전파한다."""
        policy = AsyncRetryPolicy(retryable_exceptions=(Exception,))

        async def raise_keyboard_interrupt():
            raise KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            await policy.execute(raise_keyboard_interrupt)

    @pytest.mark.asyncio
    async def test_cancelled_error_consumes_no_retry_attempt(self):
        """A cancelled call runs the func exactly once — cancellation consumes no retry.

        Pins the ``except Exception`` boundary (670 D2): CancelledError is a
        BaseException that escapes the generic handler, so it is neither retried
        nor recorded — the func is invoked exactly once before propagating.
        """
        calls = {"n": 0}

        async def raise_cancelled():
            calls["n"] += 1
            raise asyncio.CancelledError()

        policy = AsyncRetryPolicy(max_retries=5, retryable_exceptions=(Exception,))
        with pytest.raises(asyncio.CancelledError):
            await policy.execute(raise_cancelled)

        assert calls["n"] == 1


# =============================================================================
# 동작 검증 — PolicyContext 전파
# =============================================================================


class TestAsyncRetryContextPropagationBehavior:
    """PolicyContext 상태 전파 검증."""

    @pytest.mark.asyncio
    async def test_context_extra_updated_with_retry_attempt(self):
        """재시도 시 context.extra에 retry_attempt이 기록된다."""
        captured_contexts = []

        mock_backoff = MagicMock(spec=BackoffStrategy)
        mock_backoff.calculate.return_value = 0.0

        def capture_context(attempt, context=None):
            if context is not None:
                captured_contexts.append(context)
            return 0.0

        mock_backoff.calculate.side_effect = capture_context

        call_count = 0

        async def fail_then_succeed():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("fail")
            return "ok"

        ctx = PolicyContext(domain="test")
        policy = AsyncRetryPolicy(max_retries=5, backoff=mock_backoff)
        with patch(
            "baldur.resilience.policies.async_retry.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            await policy.execute(fail_then_succeed, context=ctx)

        # 2 failures → 2 backoff.calculate calls
        assert len(captured_contexts) == 2
        assert captured_contexts[0].extra["retry_attempt"] == 1
        assert captured_contexts[1].extra["retry_attempt"] == 2

    @pytest.mark.asyncio
    async def test_context_extra_updated_with_retry_last_error(self):
        """재시도 시 context.extra에 retry_last_error가 기록된다."""
        captured_contexts = []

        mock_backoff = MagicMock(spec=BackoffStrategy)

        def capture_context(attempt, context=None):
            if context is not None:
                captured_contexts.append(context)
            return 0.0

        mock_backoff.calculate.side_effect = capture_context

        call_count = 0

        async def fail_once():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("first error")
            return "ok"

        ctx = PolicyContext(domain="test")
        policy = AsyncRetryPolicy(max_retries=3, backoff=mock_backoff)
        with patch(
            "baldur.resilience.policies.async_retry.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            await policy.execute(fail_once, context=ctx)

        assert len(captured_contexts) == 1
        assert captured_contexts[0].extra["retry_last_error"] == "first error"

    @pytest.mark.asyncio
    async def test_none_context_handled_safely(self):
        """context=None 시 에러 없이 재시도가 동작한다."""

        async def always_fail():
            raise ConnectionError("fail")

        policy = AsyncRetryPolicy(max_retries=1)
        with patch(
            "baldur.resilience.policies.async_retry.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            result = await policy.execute(always_fail, context=None)

        assert result.success is False
        assert result.outcome == PolicyOutcome.FAILURE

    @pytest.mark.asyncio
    async def test_original_context_not_mutated(self):
        """원본 PolicyContext는 변경되지 않는다 (frozen + CoW)."""

        async def always_fail():
            raise ConnectionError("fail")

        ctx = PolicyContext(domain="test", extra={"original": True})
        policy = AsyncRetryPolicy(max_retries=1)
        with patch(
            "baldur.resilience.policies.async_retry.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            await policy.execute(always_fail, context=ctx)

        # frozen dataclass → with_updates는 새 인스턴스를 생성
        assert ctx.extra == {"original": True}
        assert "retry_attempt" not in ctx.extra


# =============================================================================
# 동작 검증 — 로깅 메타데이터
# =============================================================================


class TestAsyncRetryLoggingBehavior:
    """AsyncRetryPolicy structlog 로깅 메타데이터 검증.

    structlog.testing.capture_logs()를 사용하여 로그를 캡처한다.
    capsys는 configure_structlog() 호출 후 stdlib 라우팅 시 캡처 불가.
    """

    @pytest.mark.asyncio
    async def test_attempt_failed_log_includes_func_name(self):
        """retry.async_attempt_failed 이벤트에 func 메타데이터가 포함된다."""

        async def my_async_function():
            raise ConnectionError("fail")

        policy = AsyncRetryPolicy(max_retries=1)
        with (
            capture_logs() as cap_logs,
            patch(
                "baldur.resilience.policies.async_retry.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await policy.execute(my_async_function)

        events = [e["event"] for e in cap_logs]
        assert "retry.async_attempt_failed" in events
        assert any("my_async_function" in e.get("func", "") for e in cap_logs)

    @pytest.mark.asyncio
    async def test_exhausted_log_includes_func_name(self):
        """retry.async_exhausted 이벤트에 func 메타데이터가 포함된다."""

        async def fetch_data():
            raise ConnectionError("fail")

        policy = AsyncRetryPolicy(max_retries=1)
        with (
            capture_logs() as cap_logs,
            patch(
                "baldur.resilience.policies.async_retry.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await policy.execute(fetch_data)

        events = [e["event"] for e in cap_logs]
        assert "retry.async_exhausted" in events
        assert any("fetch_data" in e.get("func", "") for e in cap_logs)

    @pytest.mark.asyncio
    async def test_qualname_preferred_over_name(self):
        """__qualname__이 __name__보다 우선 사용된다."""

        class MyService:
            async def fetch(self):
                raise ConnectionError("fail")

        svc = MyService()
        policy = AsyncRetryPolicy(max_retries=1)
        with (
            capture_logs() as cap_logs,
            patch(
                "baldur.resilience.policies.async_retry.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await policy.execute(svc.fetch)

        assert any("MyService.fetch" in str(e.get("func", "")) for e in cap_logs)


# =============================================================================
# 동작 검증 — AsyncPolicyComposer 통합
# =============================================================================


class TestAsyncRetryComposerIntegrationBehavior:
    """AsyncRetryPolicy가 AsyncPolicyComposer 체인에서 동작하는지 검증."""

    @pytest.mark.asyncio
    async def test_composes_with_async_policy_composer(self):
        """AsyncPolicyComposer 체인에서 정상 동작한다."""
        from baldur.resilience.policies.composer import AsyncPolicyComposer

        call_count = 0

        async def fail_then_succeed():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("transient")
            return "composed_result"

        composer = AsyncPolicyComposer()
        retry = AsyncRetryPolicy(max_retries=3)
        composer.add(retry)

        with patch(
            "baldur.resilience.policies.async_retry.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            result = await composer.execute(fail_then_succeed)

        assert result.success is True
        assert result.value == "composed_result"


# =============================================================================
# 계약 검증 — async_retry_policy 팩토리
# =============================================================================


class TestAsyncRetryPolicyFactoryContract:
    """async_retry_policy 팩토리 함수 계약 검증."""

    def test_returns_async_retry_policy_instance(self):
        """반환 타입이 AsyncRetryPolicy이다."""
        policy = async_retry_policy()
        assert isinstance(policy, AsyncRetryPolicy)

    def test_parameters_forwarded(self):
        """파라미터가 AsyncRetryPolicy에 전달된다."""
        backoff = ConstantBackoff(delay=2.0)
        policy = async_retry_policy(
            max_retries=5,
            backoff=backoff,
            retryable_exceptions=(ConnectionError, TimeoutError),
        )
        assert policy._max_retries == 5
        assert policy._backoff is backoff
        assert policy._retryable_exceptions == (ConnectionError, TimeoutError)


# =============================================================================
# 계약 검증 — max_retries 입력 검증 (#2)
# =============================================================================


class TestAsyncRetryPolicyInputValidation:
    """AsyncRetryPolicy 생성자 입력 검증."""

    def test_negative_max_retries_raises_value_error(self):
        """max_retries가 음수이면 ValueError가 발생한다."""
        with pytest.raises(ValueError, match="max_retries must be >= 0"):
            AsyncRetryPolicy(max_retries=-1)

    def test_negative_large_max_retries_raises_value_error(self):
        """큰 음수 max_retries도 ValueError가 발생한다."""
        with pytest.raises(ValueError, match="max_retries must be >= 0"):
            AsyncRetryPolicy(max_retries=-100)

    def test_zero_max_retries_accepted(self):
        """max_retries=0은 허용된다 (재시도 없이 1회 실행)."""
        policy = AsyncRetryPolicy(max_retries=0)
        assert policy._max_retries == 0

    @pytest.mark.asyncio
    async def test_zero_retries_executes_once(self):
        """max_retries=0 시 함수가 정확히 1회 실행된다."""
        call_count = 0

        async def counting_func():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("fail")

        policy = AsyncRetryPolicy(max_retries=0)
        result = await policy.execute(counting_func)

        assert call_count == 1
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.total_attempts == 1

    def test_factory_negative_max_retries_raises(self):
        """async_retry_policy 팩토리도 음수 max_retries에 ValueError를 발생시킨다."""
        with pytest.raises(ValueError, match="max_retries must be >= 0"):
            async_retry_policy(max_retries=-1)


# =============================================================================
# 동작 검증 — functools.partial 언래핑 (#4)
# =============================================================================


class TestAsyncRetryPartialUnwrapBehavior:
    """functools.partial로 래핑된 함수의 async 감지 검증."""

    @pytest.mark.asyncio
    async def test_partial_wrapped_async_func_detected_as_async(self):
        """functools.partial(async_func, ...)이 async로 감지된다."""
        import functools

        async def async_add(a, b):
            return a + b

        partial_fn = functools.partial(async_add, 1)
        policy = AsyncRetryPolicy()
        result = await policy.execute(partial_fn, 2)

        assert result.success is True
        assert result.value == 3

    @pytest.mark.asyncio
    async def test_partial_wrapped_sync_func_uses_to_thread(self):
        """functools.partial(sync_func, ...)이 to_thread로 실행된다."""
        import functools

        def sync_add(a, b):
            return a + b

        partial_fn = functools.partial(sync_add, 10)
        policy = AsyncRetryPolicy()

        with patch(
            "baldur.resilience.policies.async_retry.asyncio.to_thread",
            new_callable=AsyncMock,
            return_value=30,
        ) as mock_to_thread:
            result = await policy.execute(partial_fn, 20)

        assert result.value == 30
        mock_to_thread.assert_called_once_with(partial_fn, 20)

    @pytest.mark.asyncio
    async def test_nested_partial_async_func_detected(self):
        """이중 partial 래핑된 async 함수도 감지된다."""
        import functools

        async def async_compute(a, b, c):
            return a + b + c

        partial_1 = functools.partial(async_compute, 1)
        partial_2 = functools.partial(partial_1, 2)
        policy = AsyncRetryPolicy()
        result = await policy.execute(partial_2, 3)

        assert result.success is True
        assert result.value == 6


# =============================================================================
# Behavior — AsyncRetryPolicy CB deference (#418 P0-5)
# =============================================================================


class TestAsyncRetryPolicyCBDeferenceP0_5Behavior:
    """AsyncRetryPolicy CB-open fast-fail behavior (#418 P0-5)."""

    @pytest.mark.asyncio
    async def test_async_retry_skips_cb_open_error(self):
        """Async: CB OPEN → retry exits immediately, total_attempts=1."""
        from baldur.core.exceptions import CircuitBreakerError

        call_count = 0

        async def raise_cb_open():
            nonlocal call_count
            call_count += 1
            raise CircuitBreakerError("CB is OPEN")

        policy = AsyncRetryPolicy(max_retries=5)
        result = await policy.execute(raise_cb_open)

        assert result.outcome == PolicyOutcome.FAILURE
        assert isinstance(result.error, CircuitBreakerError)
        assert result.total_attempts == 1
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_async_retry_custom_non_retryable(self):
        """Custom non_retryable_exceptions overrides default."""
        call_count = 0

        async def raise_value_error():
            nonlocal call_count
            call_count += 1
            raise ValueError("bad")

        policy = AsyncRetryPolicy(
            max_retries=5,
            non_retryable_exceptions=(ValueError,),
        )
        result = await policy.execute(raise_value_error)

        assert result.outcome == PolicyOutcome.FAILURE
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_async_retry_none_non_retryable_uses_default(self):
        """non_retryable_exceptions=None falls back to default (CircuitBreakerError)."""
        from baldur.core.exceptions import CircuitBreakerError

        policy = AsyncRetryPolicy(max_retries=3, non_retryable_exceptions=None)
        assert CircuitBreakerError in policy._non_retryable

    @pytest.mark.asyncio
    async def test_async_retry_empty_non_retryable_allows_cb_retry(self):
        """non_retryable_exceptions=() opt-out allows CB error retry."""
        from baldur.core.exceptions import CircuitBreakerError

        call_count = 0

        async def raise_cb():
            nonlocal call_count
            call_count += 1
            raise CircuitBreakerError("OPEN")

        policy = AsyncRetryPolicy(max_retries=2, non_retryable_exceptions=())
        result = await policy.execute(raise_cb)

        assert result.outcome == PolicyOutcome.FAILURE
        assert call_count == 3  # 1 initial + 2 retries


# =============================================================================
# Contract — from_policy_config mapping (670 D1)
# =============================================================================


class TestAsyncRetryFromPolicyConfigContract:
    """AsyncRetryPolicy.from_policy_config mirrors the sync RetryPolicy mapping."""

    @pytest.mark.parametrize(
        ("max_attempts", "expected_max_retries"),
        [
            (1, 0),  # off-by-one: 1 total attempt → 0 additional retries
            (3, 2),
            (5, 4),
            (0, 0),  # max(0 - 1, 0) floors at 0
        ],
    )
    def test_max_attempts_maps_to_max_retries_off_by_one(
        self, max_attempts, expected_max_retries
    ):
        """max_retries = max(cfg.max_attempts - 1, 0) — the load-bearing off-by-one."""
        from baldur.services.retry_handler.models import RetryPolicyConfig

        cfg = RetryPolicyConfig(max_attempts=max_attempts, domain="d")
        policy = AsyncRetryPolicy.from_policy_config(cfg)

        assert policy._max_retries == expected_max_retries

    def test_config_fields_forwarded_to_policy(self):
        """enable_dlq / domain / retryable / non_retryable are forwarded verbatim."""
        from baldur.services.retry_handler.models import RetryPolicyConfig

        cfg = RetryPolicyConfig(
            max_attempts=4,
            enable_dlq=True,
            domain="payment",
            retryable_exceptions=(ConnectionError,),
            non_retryable_exceptions=(ValueError,),
        )
        policy = AsyncRetryPolicy.from_policy_config(cfg)

        assert policy._enable_dlq is True
        assert policy._domain == "payment"
        assert policy._retryable_exceptions == (ConnectionError,)
        assert policy._non_retryable == (ValueError,)

    def test_backoff_derived_from_config_delays_and_jitter(self):
        """A None backoff derives an ExponentialBackoff from the config fields."""
        from baldur.services.retry_handler.models import RetryPolicyConfig

        cfg = RetryPolicyConfig(
            max_attempts=3,
            backoff_base=2,
            backoff_max=30,
            jitter_percent=50,
            domain="d",
        )
        policy = AsyncRetryPolicy.from_policy_config(cfg)

        assert isinstance(policy._backoff, ExponentialBackoff)
        assert policy._backoff.base_delay == 2
        assert policy._backoff.max_delay == 30
        # jitter_percent (50) is mapped to a 0–1 jitter_factor (0.5).
        assert policy._backoff.jitter_factor == pytest.approx(0.5)

    def test_explicit_backoff_overrides_config_derivation(self):
        """An explicit backoff argument wins over the config-derived one."""
        from baldur.services.retry_handler.models import RetryPolicyConfig

        cfg = RetryPolicyConfig(max_attempts=3, backoff_base=2, domain="d")
        custom = ConstantBackoff(delay=1.5, jitter=False)
        policy = AsyncRetryPolicy.from_policy_config(cfg, backoff=custom)

        assert policy._backoff is custom


# =============================================================================
# Behavior — should_dlq arming on exhaustion (670 D1)
# =============================================================================


class TestAsyncRetryDLQArmingBehavior:
    """The exhaustion FAILURE result arms DLQ metadata mirroring sync RetryPolicy."""

    @pytest.mark.asyncio
    async def test_exhaustion_arms_should_dlq_metadata(self):
        """enable_dlq=True → FAILURE metadata carries should_dlq / domain / max_attempts."""

        async def always_fail():
            raise ConnectionError("down")

        policy = AsyncRetryPolicy(max_retries=1, enable_dlq=True, domain="payment")
        with patch(
            "baldur.resilience.policies.async_retry.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            result = await policy.execute(always_fail)

        assert result.outcome == PolicyOutcome.FAILURE
        assert result.metadata["should_dlq"] is True
        assert result.metadata["domain"] == "payment"
        # max_retries=1 → 2 total attempts.
        assert result.metadata["max_attempts"] == 2
        assert len(result.metadata["retry_history"]) == 2

    @pytest.mark.asyncio
    async def test_exhaustion_should_dlq_false_when_dlq_disabled(self):
        """A bare policy (enable_dlq=False) leaves should_dlq False → sink skips."""

        async def always_fail():
            raise ConnectionError("down")

        policy = AsyncRetryPolicy(max_retries=1, enable_dlq=False, domain="payment")
        with patch(
            "baldur.resilience.policies.async_retry.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            result = await policy.execute(always_fail)

        assert result.metadata["should_dlq"] is False


# =============================================================================
# Behavior — observe-only single-attempt guard (670 D1)
# =============================================================================


class TestAsyncRetryObserveOnlyBehavior:
    """Observe-only (dry-run / shadow) suppresses the retry intervention."""

    def teardown_method(self):
        from baldur.core.execution_mode import clear_execution_mode_override

        clear_execution_mode_override()

    @pytest.mark.asyncio
    async def test_observe_only_runs_single_attempt_without_should_dlq(self):
        """Shadow mode → the business call runs exactly once, no re-execution, no DLQ arm."""
        from baldur.core.execution_mode import ExecutionMode, set_execution_mode

        calls = {"n": 0}

        async def always_fail():
            calls["n"] += 1
            raise ConnectionError("down")

        policy = AsyncRetryPolicy(max_retries=3, enable_dlq=True, domain="obs")
        set_execution_mode(ExecutionMode.shadow())
        result = await policy.execute(always_fail)

        assert calls["n"] == 1  # single attempt — retry suppressed
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.total_attempts == 1
        # Observe-only must NOT arm DLQ (the single-attempt result has no metadata).
        assert "should_dlq" not in (result.metadata or {})

    @pytest.mark.asyncio
    async def test_observe_only_consults_intervention_suppressed(self):
        """The suppression decision goes through intervention_suppressed with domain/action."""
        calls = {"n": 0}

        async def always_fail():
            calls["n"] += 1
            raise ConnectionError("down")

        policy = AsyncRetryPolicy(max_retries=3, domain="obs")
        with patch(
            "baldur.resilience.policies.async_retry.intervention_suppressed",
            autospec=True,
            return_value=True,
        ) as mock_suppressed:
            result = await policy.execute(always_fail)

        mock_suppressed.assert_called_once()
        assert mock_suppressed.call_args.kwargs["service_name"] == "obs"
        assert mock_suppressed.call_args.kwargs["action"] == "retry"
        assert mock_suppressed.call_args.kwargs["max_attempts"] == 4
        assert calls["n"] == 1  # suppressed → single attempt
        assert result.total_attempts == 1
