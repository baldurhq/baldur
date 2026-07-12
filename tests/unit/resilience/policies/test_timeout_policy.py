"""
TimeoutPolicy / AsyncTimeoutPolicy unit tests (#449).

Test targets:
- resilience/policies/timeout.py (TimeoutPolicy, AsyncTimeoutPolicy)
- core/exceptions.py (TimeoutPolicyError)

UNIT_TEST_GUIDELINES.md compliance:
- Contract verification: hardcoded expected values (init boundary, name, extra_context)
- Behavior verification: source references (PolicyOutcome, execute flow)
- conftest.py: single-file fixtures → inline (§5.1)
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from baldur.core.exceptions import TimeoutPolicyError
from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.resilience.policies.timeout import AsyncTimeoutPolicy, TimeoutPolicy

# =============================================================================
# Fixtures — single-file only (§5.1)
# =============================================================================


@pytest.fixture
def sync_policy():
    """TimeoutPolicy with 5s timeout."""
    return TimeoutPolicy(timeout_seconds=5.0)


@pytest.fixture
def async_policy():
    """AsyncTimeoutPolicy with 5s timeout."""
    return AsyncTimeoutPolicy(timeout_seconds=5.0)


# =============================================================================
# TimeoutPolicyError Contract
# =============================================================================


class TestTimeoutPolicyErrorContract:
    """TimeoutPolicyError design contract verification."""

    def test_timeout_seconds_attribute_stored(self):
        """timeout_seconds attribute preserves the given value."""
        err = TimeoutPolicyError(10.5)
        assert err.timeout_seconds == 10.5

    def test_default_message_format(self):
        """Default message: 'Call timed out after {n}s'."""
        err = TimeoutPolicyError(30.0)
        assert str(err) == "Call timed out after 30.0s"

    def test_custom_message_overrides_default(self):
        """Custom message overrides the default format."""
        err = TimeoutPolicyError(5.0, message="custom timeout")
        assert str(err) == "custom timeout"

    def test_extra_context_returns_timeout_seconds(self):
        """extra_context() returns dict with timeout_seconds key."""
        err = TimeoutPolicyError(7.5)
        ctx = err.extra_context()
        assert ctx == {"timeout_seconds": 7.5}


# =============================================================================
# TimeoutPolicy Contract
# =============================================================================


class TestTimeoutPolicyContract:
    """TimeoutPolicy init / name contract verification."""

    def test_name_returns_timeout(self, sync_policy):
        """Policy name is 'timeout'."""
        assert sync_policy.name == "timeout"

    @pytest.mark.parametrize(
        "value",
        [0, -1, -0.001],
        ids=["zero", "negative_int", "negative_float"],
    )
    def test_init_rejects_non_positive_timeout(self, value):
        """timeout_seconds <= 0 raises ValueError."""
        with pytest.raises(ValueError, match="must be > 0"):
            TimeoutPolicy(timeout_seconds=value)

    def test_init_accepts_positive_float(self):
        """Smallest positive float (0.001) is accepted."""
        policy = TimeoutPolicy(timeout_seconds=0.001)
        assert policy._timeout_seconds == 0.001


# =============================================================================
# TimeoutPolicy Behavior
# =============================================================================


class TestTimeoutPolicyBehavior:
    """TimeoutPolicy.execute() behavior verification."""

    def test_execute_success_returns_value_and_success_outcome(self, sync_policy):
        """Successful fn returns PolicyResult with value and SUCCESS outcome."""
        result = sync_policy.execute(lambda: "hello")

        assert result.value == "hello"
        assert result.outcome == PolicyOutcome.SUCCESS
        assert "timeout" in result.executed_policies

    def test_execute_timeout_returns_timeout_outcome(self):
        """fn exceeding timeout returns PolicyResult(outcome=TIMEOUT, error=TimeoutPolicyError).

        Per ResiliencePolicy Protocol (interfaces/resilience_policy.py:194-232):
        policy-defined outcomes are wrapped in PolicyResult, not raised. Same
        pattern as BulkheadPolicy.BulkheadTimeoutError handling.
        """
        policy = TimeoutPolicy(timeout_seconds=0.1)
        blocker = threading.Event()

        def slow_fn():
            blocker.wait(timeout=5.0)
            return "never"

        result = policy.execute(slow_fn)

        assert result.outcome == PolicyOutcome.TIMEOUT
        assert result.value is None
        assert isinstance(result.error, TimeoutPolicyError)
        assert result.error.timeout_seconds == 0.1
        assert result.metadata == {"timeout_seconds": 0.1}
        assert "timeout" in result.executed_policies
        blocker.set()

    def test_execute_business_exception_propagates(self, sync_policy):
        """Business exception from fn propagates unmodified (not wrapped in PolicyResult)."""

        def failing_fn():
            raise ValueError("business error")

        with pytest.raises(ValueError, match="business error"):
            sync_policy.execute(failing_fn)

    def test_execute_user_timeout_error_propagates_unmodified(self, sync_policy):
        """fn raising stdlib TimeoutError is a business exception, not a policy timeout.

        On Python >= 3.11 ``concurrent.futures.TimeoutError`` aliases builtin
        ``TimeoutError``, so without completion-state disambiguation an
        instantly-raised user TimeoutError was misreported as
        ``TimeoutPolicyError("Call timed out after 5.0s")``.
        """
        user_error = TimeoutError("upstream deadline from fn")

        def failing_fn():
            raise user_error

        with pytest.raises(TimeoutError) as exc_info:
            sync_policy.execute(failing_fn)
        assert exc_info.value is user_error
        assert not isinstance(exc_info.value, TimeoutPolicyError)

    def test_execute_passes_args_and_kwargs(self, sync_policy):
        """Arguments and keyword arguments are forwarded to fn."""

        def fn_with_args(a, b, key=None):
            return f"{a}-{b}-{key}"

        result = sync_policy.execute(fn_with_args, 1, 2, key="three")
        assert result.value == "1-2-three"

    def test_execute_cancels_future_on_timeout(self):
        """On TIMEOUT, ``future.cancel()`` is called so a slow inner fn does not
        block the shared executor worker after the caller already gave up.

        The executor itself is process-shared (see TestTimeoutPolicySharedExecutor)
        so the per-call cleanup is now ``future.cancel()``, not ``executor.shutdown``.
        """
        from concurrent.futures import TimeoutError as FuturesTimeoutError
        from unittest.mock import patch

        policy = TimeoutPolicy(timeout_seconds=0.05)
        TimeoutPolicy.shutdown_executor()  # ensure clean classvar

        with patch.object(TimeoutPolicy, "_get_executor") as mock_get_executor:
            executor = MagicMock()
            future = MagicMock()
            future.result.side_effect = FuturesTimeoutError()
            executor.submit.return_value = future
            mock_get_executor.return_value = executor

            result = policy.execute(lambda: None)

        assert result.outcome == PolicyOutcome.TIMEOUT
        future.cancel.assert_called_once()
        # Per-call executor.shutdown is INTENTIONALLY absent post-#481 —
        # the executor is process-shared and lifecycle is owned by
        # TimeoutPolicy.shutdown_executor() / reset_protect_caches().
        executor.shutdown.assert_not_called()

    def test_execute_propagates_contextvars_to_worker_thread(self, sync_policy):
        """ContextVar set in calling thread is visible inside fn running on worker thread.

        Why this matters: structlog binding (merge_contextvars), deadline ContextVar,
        cell/actor context all rely on contextvars. Without copy_context() the worker
        thread sees empty contextvars and observability/tracing breaks.

        Pattern reference: src/baldur_pro/services/bulkhead/threadpool.py:173-186
        uses contextvars.copy_context() + ctx.run(fn) for the same reason.
        """
        # Given — a ContextVar bound in the calling thread
        import contextvars

        var: contextvars.ContextVar[str] = contextvars.ContextVar(
            "timeout_policy_test_var", default="default"
        )
        var.set("propagated")

        def read_var() -> str:
            return var.get()

        # When — fn runs inside the worker thread via TimeoutPolicy
        result = sync_policy.execute(read_var)

        # Then — worker thread saw the calling thread's ContextVar value
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "propagated"


# =============================================================================
# TimeoutPolicy Shared Executor (#481 DEC-1)
# =============================================================================


class TestTimeoutPolicySharedExecutor:
    """TimeoutPolicy class-level shared ThreadPoolExecutor (DCL singleton).

    Per #481 DEC-1, ``TimeoutPolicy._get_executor()`` returns a
    process-shared executor mirroring
    ``baldur_pro.services.hedging.executor.HedgingExecutor._get_executor``.
    These tests pin the new contract.
    """

    def setup_method(self) -> None:
        """Each test starts with a clean classvar so prior-test state cannot leak."""
        TimeoutPolicy.shutdown_executor()

    def teardown_method(self) -> None:
        """Drain any executor a test left running so the next test is isolated."""
        TimeoutPolicy.shutdown_executor()

    def test_executor_reused_across_calls(self):
        """``_get_executor()`` returns the same instance across N calls."""
        first = TimeoutPolicy._get_executor()
        second = TimeoutPolicy._get_executor()
        third = TimeoutPolicy._get_executor()
        assert first is second is third

    def test_execute_uses_shared_executor(self):
        """``policy.execute()`` reuses the cached classvar across calls."""
        policy = TimeoutPolicy(timeout_seconds=5.0)

        # Trigger lazy construction.
        policy.execute(lambda: 1)
        executor_after_first = TimeoutPolicy._executor
        assert executor_after_first is not None

        policy.execute(lambda: 2)
        executor_after_second = TimeoutPolicy._executor
        # Same object, NOT a fresh per-call ThreadPoolExecutor.
        assert executor_after_first is executor_after_second

    def test_dcl_first_call_race_constructs_once(self):
        """Concurrent first-call from N threads triggers exactly 1 constructor.

        DCL pattern from ``hedging/executor.py:72-86`` (#479-hardened by
        ``b80ba463``): unlocked fast path + locked second-check ensures only
        the first arriving thread builds; late arrivals see the cached
        instance through the second classvar read inside the lock.
        """
        from unittest.mock import patch

        construct_count = 0
        original_cls = ThreadPoolExecutor

        def counting_constructor(*args: object, **kwargs: object) -> object:
            nonlocal construct_count
            construct_count += 1
            return original_cls(*args, **kwargs)

        n_threads = 8
        barrier = threading.Barrier(n_threads)
        instances: list[object] = []
        instances_lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            inst = TimeoutPolicy._get_executor()
            with instances_lock:
                instances.append(inst)

        with patch(
            "baldur.resilience.policies.timeout.ThreadPoolExecutor",
            side_effect=counting_constructor,
        ):
            threads = [threading.Thread(target=worker) for _ in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)

        assert construct_count == 1
        assert len(instances) == n_threads
        assert all(inst is instances[0] for inst in instances)

    def test_shutdown_executor_clears_classvar(self):
        """``shutdown_executor()`` drains and nulls the classvar."""
        TimeoutPolicy._get_executor()
        assert TimeoutPolicy._executor is not None

        TimeoutPolicy.shutdown_executor()
        assert TimeoutPolicy._executor is None

    def test_shutdown_executor_idempotent_when_uninitialized(self):
        """Calling shutdown without a prior _get_executor is a no-op (no error)."""
        assert TimeoutPolicy._executor is None
        TimeoutPolicy.shutdown_executor()  # must not raise
        assert TimeoutPolicy._executor is None

    def test_post_shutdown_get_executor_rebuilds(self):
        """After shutdown, the next ``_get_executor()`` returns a NEW instance."""
        first = TimeoutPolicy._get_executor()
        TimeoutPolicy.shutdown_executor()
        second = TimeoutPolicy._get_executor()

        assert first is not second
        assert TimeoutPolicy._executor is second

    def test_executor_uses_settings_max_workers(self):
        """The executor's ``_max_workers`` matches
        ``ProtectSettings.default_timeout_executor_workers``.

        Settings is a singleton — reading once at lazy construction time is
        the documented contract (#481 DEC-4). Subsequent settings changes
        require ``reset_protect_caches()``, which forwards to
        ``shutdown_executor()`` and forces rebuild.
        """
        from baldur.settings.protect import (
            get_protect_settings,
            reset_protect_settings,
        )

        reset_protect_settings()
        try:
            expected = get_protect_settings().default_timeout_executor_workers
            executor = TimeoutPolicy._get_executor()
            assert executor._max_workers == expected
        finally:
            reset_protect_settings()

    def test_reset_protect_caches_drains_executor(self):
        """``reset_protect_caches()`` calls ``TimeoutPolicy.shutdown_executor()``
        so a single call invalidates every piece of process-local
        protect()-related state.
        """
        from baldur.protect_facade import reset_protect_caches

        TimeoutPolicy._get_executor()
        assert TimeoutPolicy._executor is not None

        reset_protect_caches()
        assert TimeoutPolicy._executor is None


# =============================================================================
# AsyncTimeoutPolicy Contract
# =============================================================================


class TestAsyncTimeoutPolicyContract:
    """AsyncTimeoutPolicy init / name contract verification."""

    def test_name_returns_timeout(self, async_policy):
        """Policy name is 'timeout'."""
        assert async_policy.name == "timeout"

    @pytest.mark.parametrize(
        "value",
        [0, -1, -0.001],
        ids=["zero", "negative_int", "negative_float"],
    )
    def test_init_rejects_non_positive_timeout(self, value):
        """timeout_seconds <= 0 raises ValueError."""
        with pytest.raises(ValueError, match="must be > 0"):
            AsyncTimeoutPolicy(timeout_seconds=value)

    def test_init_accepts_positive_float(self):
        """Smallest positive float (0.001) is accepted."""
        policy = AsyncTimeoutPolicy(timeout_seconds=0.001)
        assert policy._timeout_seconds == 0.001


# =============================================================================
# AsyncTimeoutPolicy Behavior
# =============================================================================


class TestAsyncTimeoutPolicyBehavior:
    """AsyncTimeoutPolicy.execute() behavior verification."""

    @pytest.mark.asyncio
    async def test_execute_success_returns_value_and_success_outcome(
        self, async_policy
    ):
        """Successful coroutine returns PolicyResult with value and SUCCESS."""

        async def ok_fn():
            return "async_hello"

        result = await async_policy.execute(ok_fn)

        assert result.value == "async_hello"
        assert result.outcome == PolicyOutcome.SUCCESS
        assert "timeout" in result.executed_policies

    @pytest.mark.asyncio
    async def test_execute_timeout_returns_timeout_outcome(self):
        """Coroutine exceeding timeout returns PolicyResult(outcome=TIMEOUT, error=...).

        Mirrors the sync TimeoutPolicy behavior — outcome wrapped in PolicyResult
        per Protocol contract, not raised.
        """
        policy = AsyncTimeoutPolicy(timeout_seconds=0.05)

        async def slow_fn():
            await asyncio.sleep(10)
            return "never"

        result = await policy.execute(slow_fn)

        assert result.outcome == PolicyOutcome.TIMEOUT
        assert result.value is None
        assert isinstance(result.error, TimeoutPolicyError)
        assert result.error.timeout_seconds == 0.05
        assert result.metadata == {"timeout_seconds": 0.05}
        assert "timeout" in result.executed_policies

    @pytest.mark.asyncio
    async def test_execute_business_exception_propagates(self, async_policy):
        """Business exception from coroutine propagates unmodified."""

        async def failing_fn():
            raise ValueError("async business error")

        with pytest.raises(ValueError, match="async business error"):
            await async_policy.execute(failing_fn)

    @pytest.mark.asyncio
    async def test_execute_user_timeout_error_propagates_unmodified(self, async_policy):
        """Coroutine raising stdlib TimeoutError is a business exception.

        On Python >= 3.11 ``asyncio.TimeoutError`` aliases builtin
        ``TimeoutError``, so without task-state disambiguation an
        instantly-raised user TimeoutError was misreported as a policy
        timeout. A real policy timeout leaves the inner task cancelled;
        a coroutine-raised one leaves it finished with that exception.
        """
        user_error = TimeoutError("upstream deadline from coroutine")

        async def failing_fn():
            raise user_error

        with pytest.raises(TimeoutError) as exc_info:
            await async_policy.execute(failing_fn)
        assert exc_info.value is user_error
        assert not isinstance(exc_info.value, TimeoutPolicyError)

    @pytest.mark.asyncio
    async def test_execute_cancelled_error_propagates(self, async_policy):
        """asyncio.CancelledError propagates without conversion to TimeoutPolicyError."""

        async def cancelled_fn():
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await async_policy.execute(cancelled_fn)

    @pytest.mark.asyncio
    async def test_execute_passes_args_and_kwargs(self, async_policy):
        """Arguments and keyword arguments are forwarded to async fn."""

        async def fn_with_args(a, b, key=None):
            return f"{a}-{b}-{key}"

        result = await async_policy.execute(fn_with_args, 1, 2, key="three")
        assert result.value == "1-2-three"
