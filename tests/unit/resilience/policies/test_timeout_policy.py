"""
TimeoutPolicy / AsyncTimeoutPolicy unit tests (#449).

Test targets:
- resilience/policies/timeout.py (TimeoutPolicy, AsyncTimeoutPolicy)
- core/exceptions.py (TimeoutPolicyError)

UNIT_TEST_GUIDELINES.md compliance:
- Contract verification: hardcoded expected values (init boundary, name, extra_context)
- Behavior verification: source references (PolicyOutcome, execute flow)
- conftest.py: single-file fixtures → inline (§5.1)

810 D1: a wait cut short by anything but the function's own exception (a soft
time limit, a gevent timeout, ``KeyboardInterrupt``) records running work as
the call's own and re-raises the interruption unchanged — branch outcomes over
a real future handed back by a stub executor (running, finished in the
instant, never started) and the function's own exception on the real executor.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from unittest.mock import MagicMock, call, patch

import pytest

from baldur.core.abandoned_work import WorkSummary, close_work_scope, open_work_scope
from baldur.core.exceptions import TimeoutPolicyError
from baldur.interfaces.resilience_policy import PolicyContext, PolicyOutcome
from baldur.resilience.policies.timeout import AsyncTimeoutPolicy, TimeoutPolicy
from tests.factories.interruptions import (
    INTERRUPTION_IDS,
    INTERRUPTIONS,
    GeventTimeout,
    SoftTimeLimitExceeded,
)

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
        policy = TimeoutPolicy(timeout_seconds=0.05)
        TimeoutPolicy.shutdown_executor()  # ensure clean classvar

        with patch.object(TimeoutPolicy, "_get_executor") as mock_get_executor:
            executor = MagicMock()
            future = MagicMock()
            future.result.side_effect = FuturesTimeoutError()
            future.done.return_value = False
            # The task never started: the cancel succeeds (805 D9 — a running
            # task's failed cancel is covered by
            # TestTimeoutPolicyAbandonedRecordBehavior).
            future.cancel.return_value = True
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
# TimeoutPolicy — abandoned work record (805 D9)
# =============================================================================

# Upper bound on any wait the test expects to end.
_WAIT_S = 5.0
# A timeout that fires while the task is already running: an idle worker picks
# the task up long before it, so the cancel finds it running.
_RUNNING_TIMEOUT_S = 1.0


def _wait_for(predicate, timeout: float = _WAIT_S) -> bool:
    """Wait for a done-callback another thread runs."""
    poll = threading.Event()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        poll.wait(0.002)
    return bool(predicate())


class _OneWorkerTimeoutPolicy(TimeoutPolicy):
    """A TimeoutPolicy with its own executor slot."""

    _executor = None


class TestTimeoutPolicyAbandonedRecordBehavior:
    """805 D9: a timeout records its future as abandoned work only when the
    cancel fails (the task is running), with the PolicyContext it ran with."""

    @pytest.mark.parametrize(
        ("cancelled", "recorded"),
        [(True, False), (False, True)],
        ids=["cancel_succeeds_unstarted", "cancel_fails_running"],
    )
    def test_timeout_records_future_only_when_cancel_fails(self, cancelled, recorded):
        # Given — a timed-out future whose cancel answers ``cancelled``.
        policy = TimeoutPolicy(timeout_seconds=0.05)
        context = PolicyContext(order_id="o-1")
        future = MagicMock(spec=Future)
        future.result.side_effect = FuturesTimeoutError()
        future.done.return_value = False
        future.cancel.return_value = cancelled
        executor = MagicMock(spec=ThreadPoolExecutor)
        executor.submit.return_value = future

        # When
        with (
            patch.object(TimeoutPolicy, "_get_executor", return_value=executor),
            patch(
                "baldur.resilience.policies.timeout.record_abandoned", autospec=True
            ) as record,
        ):
            result = policy.execute(lambda: None, context=context)

        # Then
        assert result.outcome == PolicyOutcome.TIMEOUT
        assert record.call_args_list == (
            [call(future, origin=context)] if recorded else []
        )

    def test_running_work_is_held_as_own_work_of_its_context(self):
        """The scope opened with the same context holds the task as its own."""
        # Given
        context = PolicyContext(order_id="o-1")
        scope, token = open_work_scope(origin=context)
        started, release = threading.Event(), threading.Event()

        def _slow() -> str:
            started.set()
            release.wait(_WAIT_S)
            return "done"

        settled: list[WorkSummary] = []
        try:
            # When — the timeout fires while the work runs.
            result = TimeoutPolicy(timeout_seconds=_RUNNING_TIMEOUT_S).execute(
                _slow, context=context
            )
            held = scope.running_count
            at_close = close_work_scope(scope, token, settled.append)
        finally:
            release.set()

        # Then — held until it ended, then settled as own work that returned.
        assert result.outcome == PolicyOutcome.TIMEOUT
        assert started.is_set()
        assert held == 1
        assert at_close is None
        assert _wait_for(lambda: settled)
        assert settled == [WorkSummary(own_finished=True, own_failed=False)]

    def test_unstarted_work_is_cancelled_and_not_recorded(self):
        # Given — the policy's only worker busy, so the call waits in the queue.
        executor = ThreadPoolExecutor(max_workers=1)
        busy = threading.Event()
        blocker = executor.submit(busy.wait, _WAIT_S)
        ran = {"n": 0}
        scope, token = open_work_scope(origin=None)
        try:
            with patch.object(_OneWorkerTimeoutPolicy, "_executor", executor):
                # When
                result = _OneWorkerTimeoutPolicy(timeout_seconds=0.05).execute(
                    lambda: ran.__setitem__("n", 1)
                )
            held = scope.running_count
        finally:
            summary = close_work_scope(scope, token)
            busy.set()
            blocker.result(_WAIT_S)
            executor.shutdown(wait=True)

        # Then
        assert result.outcome == PolicyOutcome.TIMEOUT
        assert held == 0
        assert summary == WorkSummary()
        assert ran["n"] == 0


# =============================================================================
# TimeoutPolicy — an interrupted wait records the work like a timeout (810 D1)
# =============================================================================


def _interrupted_future(
    interruption: BaseException, *, started: bool = True, outcome: str | None = None
) -> Future:
    """A real future whose ``result()`` raises ``interruption`` instead of waiting.

    ``started`` puts it in the running state (its cancel then fails, as for
    work a pool worker picked up); ``outcome`` finishes it first — the work
    ended in the very instant the interruption arrived.
    """
    future: Future = Future()
    if started:
        future.set_running_or_notify_cancel()
    if outcome == "returned":
        future.set_result("charged")
    elif outcome == "raised":
        future.set_exception(ConnectionError("gateway reset"))

    def _interrupted(timeout=None):
        raise interruption

    future.result = _interrupted  # type: ignore[method-assign]
    return future


def _execute_with_future(policy: TimeoutPolicy, future: Future, context):
    """Run ``policy.execute`` with the shared executor handing back ``future``."""
    executor = MagicMock(spec=ThreadPoolExecutor)
    executor.submit.return_value = future
    with patch.object(TimeoutPolicy, "_get_executor", return_value=executor):
        return policy.execute(lambda: None, context=context)


class TestTimeoutPolicyInterruptedWaitBehavior:
    """810 D1: a wait cut short by anything other than the function's own
    exception records the work as the call's own, like its own timeout, and
    lets the interruption propagate unchanged."""

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            ("returned", WorkSummary(own_finished=True, own_failed=False)),
            ("raised", WorkSummary(own_finished=True, own_failed=True)),
        ],
        ids=["work_returned", "work_raised"],
    )
    @pytest.mark.parametrize("kind", INTERRUPTIONS, ids=INTERRUPTION_IDS)
    def test_interrupted_wait_on_running_work_holds_scope_as_own_work(
        self, kind, outcome, expected
    ):
        # Given — a keyed call's scope, and work its wait was cut short on.
        context = PolicyContext(order_id="o-1")
        interruption = kind()
        future = _interrupted_future(interruption)
        scope, token = open_work_scope(origin=context)
        settled: list[WorkSummary] = []

        # When
        with pytest.raises(kind) as raised:
            _execute_with_future(TimeoutPolicy(timeout_seconds=5.0), future, context)
        held = scope.running_count
        at_close = close_work_scope(scope, token, settled.append)
        if outcome == "returned":
            future.set_result("charged")
        else:
            future.set_exception(ConnectionError("gateway reset"))

        # Then — re-raised as it came, held while running, then own work.
        assert raised.value is interruption
        assert held == 1
        assert at_close is None
        assert settled == [expected]

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            ("returned", WorkSummary(own_finished=True, own_failed=False)),
            ("raised", WorkSummary(own_finished=True, own_failed=True)),
        ],
        ids=["work_returned", "work_raised"],
    )
    def test_interruption_as_the_work_finishes_folds_at_once_with_its_outcome(
        self, outcome, expected
    ):
        """No ``done()`` precondition: a function that just returned is not lost."""
        # Given — the work finished in the instant the interruption arrived.
        context = PolicyContext(order_id="o-1")
        interruption = SoftTimeLimitExceeded()
        future = _interrupted_future(interruption, outcome=outcome)
        scope, token = open_work_scope(origin=context)

        # When
        with pytest.raises(SoftTimeLimitExceeded):
            _execute_with_future(TimeoutPolicy(timeout_seconds=5.0), future, context)
        held = scope.running_count
        summary = close_work_scope(scope, token)

        # Then — recorded and folded at once with its real outcome.
        assert held == 0
        assert summary == expected

    @pytest.mark.parametrize("kind", INTERRUPTIONS, ids=INTERRUPTION_IDS)
    def test_interrupted_wait_before_work_started_cancels_it_and_records_nothing(
        self, kind
    ):
        # Given — the work is still queued when the wait is interrupted.
        context = PolicyContext(order_id="o-1")
        interruption = kind()
        future = _interrupted_future(interruption, started=False)
        scope, token = open_work_scope(origin=context)

        # When
        with pytest.raises(kind) as raised:
            _execute_with_future(TimeoutPolicy(timeout_seconds=5.0), future, context)
        summary = close_work_scope(scope, token)

        # Then — cancelled, so it never runs and nothing holds the scope.
        assert raised.value is interruption
        assert future.cancelled()
        assert summary == WorkSummary()

    @pytest.mark.parametrize(
        "own",
        [
            ValueError("declined"),
            SoftTimeLimitExceeded("raised by the function itself"),
            GeventTimeout(),
        ],
        ids=["exception", "interruption_shaped_exception", "base_exception"],
    )
    def test_function_own_exception_propagates_and_records_nothing(self, own):
        """Only the caught object being the future's own makes it the function's."""
        # Given — a function that raises on its own, run on the real executor.
        context = PolicyContext(order_id="o-1")
        scope, token = open_work_scope(origin=context)

        def _raises():
            raise own

        # When
        with pytest.raises(type(own)) as raised:
            TimeoutPolicy(timeout_seconds=_WAIT_S).execute(_raises, context=context)
        summary = close_work_scope(scope, token)

        # Then — unchanged, and no piece was recorded (nothing to fold).
        assert raised.value is own
        assert summary == WorkSummary()


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
