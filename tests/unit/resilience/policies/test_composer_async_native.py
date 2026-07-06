"""AsyncPolicyComposer add-time-normalization unit tests (672 D2/D3).

Supersedes ``test_composer_async_offload.py``: the composer now normalizes every
guard/hook/sink to its async Protocol at ``add_*`` time. A native-async channel
is awaited with ZERO thread hop; a sync channel is wrapped in a private
``to_thread``-offload adapter and awaited. ``execute`` dispatches all three
channels uniformly.

This file re-asserts the offload suite's still-valid *behavioral* cases (fail-open
swallow, exactly-once dedup equivalence, guard→hook happens-before ordering,
empty-channel skip, loop-non-blocking probe, ``CancelledError`` escape) against
the add-time-normalized mechanism, and adds the D2/D3 native-vs-offload
distinction. The retired offload cases asserted the composer's *private dispatch
identity* (``_notify_hooks_success`` / ``_process_sinks`` / ``guard.check`` routed
through ``asyncio.to_thread``) — normalization changes that dispatched callable,
so those structural assertions do not carry over.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.core.exceptions import TimeoutPolicyError
from baldur.interfaces.resilience_policy import (
    GuardResult,
    PolicyContext,
    PolicyOutcome,
)
from baldur.resilience.policies.composer import (
    AsyncPolicyComposer,
    _SyncGuardToAsyncAdapter,
    _SyncHookToAsyncAdapter,
    _SyncSinkToAsyncAdapter,
)

# Patch point for the offload adapters' ``asyncio.to_thread`` (module-local).
_TO_THREAD = "baldur.resilience.policies.composer.asyncio.to_thread"

_PROCEED_TIMEOUT = 1.0
_POLL_INTERVAL = 0.005
_POLL_ITERATIONS = 400


async def _async_ok() -> str:
    return "ok"


async def _async_boom() -> str:
    raise ValueError("boom")


# =============================================================================
# Native-async channel stubs (awaited directly — no thread hop)
# =============================================================================


class _NativeAllowGuard:
    name = "idempotency"

    def __init__(self) -> None:
        self.check_count = 0

    async def check(self, context: PolicyContext | None = None) -> GuardResult:
        self.check_count += 1
        return GuardResult(allowed=True)


class _NativeAcquireOnceGuard:
    name = "idempotency"

    def __init__(self) -> None:
        self._acquired = False

    async def check(self, context: PolicyContext | None = None) -> GuardResult:
        if self._acquired:
            return GuardResult(allowed=False, reason="duplicate")
        self._acquired = True
        return GuardResult(allowed=True)


class _NativeAtomicAcquireOnceGuard:
    """Lock-protected acquire-once — models the shipped async idempotency guard's
    atomic dedup gate under concurrent execute()."""

    name = "idempotency"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._acquired = False

    async def check(self, context: PolicyContext | None = None) -> GuardResult:
        with self._lock:
            if self._acquired:
                return GuardResult(allowed=False, reason="duplicate")
            self._acquired = True
            return GuardResult(allowed=True)


class _NativeMarkerGuard:
    name = "marker_guard"
    MARKER_KEY = "_native_marker"
    MARKER_VALUE = "written-by-guard"

    async def check(self, context: PolicyContext | None = None) -> GuardResult:
        if context is not None:
            context.extra[self.MARKER_KEY] = self.MARKER_VALUE
        return GuardResult(allowed=True)


class _NativeMarkerHook:
    def __init__(self) -> None:
        self.observed_marker: str | None = "UNSET"

    async def on_execute(self, *a, **k) -> None:
        pass

    async def on_retry(self, *a, **k) -> None:
        pass

    async def on_success(self, policy_name, result, **kwargs) -> None:
        context = kwargs.get("context")
        self.observed_marker = (
            context.extra.get(_NativeMarkerGuard.MARKER_KEY) if context else None
        )

    async def on_failure(self, *a, **k) -> None:
        pass

    async def on_reject(self, *a, **k) -> None:
        pass


class _NativeRecordingHook:
    def __init__(self) -> None:
        self.success: list = []
        self.failure: list = []

    async def on_execute(self, *a, **k) -> None:
        pass

    async def on_retry(self, *a, **k) -> None:
        pass

    async def on_success(self, policy_name, result, **kwargs) -> None:
        self.success.append((policy_name, result))

    async def on_failure(self, policy_name, error, attempt, **kwargs) -> None:
        self.failure.append((policy_name, error, attempt))

    async def on_reject(self, *a, **k) -> None:
        pass


class _NativeCancelGuard:
    name = "cancel_guard"

    async def check(self, context: PolicyContext | None = None) -> GuardResult:
        raise asyncio.CancelledError()


# =============================================================================
# Sync channel stubs (auto-wrapped + offloaded)
# =============================================================================


class _SyncAllowGuard:
    name = "sync_guard"

    def __init__(self) -> None:
        self.check_count = 0

    def check(self, context: PolicyContext | None = None) -> GuardResult:
        self.check_count += 1
        return GuardResult(allowed=True)


class _SyncRaisingGuard:
    name = "raising_guard"

    def check(self, context: PolicyContext | None = None) -> GuardResult:
        raise RuntimeError("guard boom")


class _SyncBlockingGuard:
    name = "blocking_guard"

    def __init__(self, entered: threading.Event, proceed: threading.Event) -> None:
        self._entered = entered
        self._proceed = proceed
        self.proceed_observed: bool | None = None

    def check(self, context: PolicyContext | None = None) -> GuardResult:
        self._entered.set()
        self.proceed_observed = self._proceed.wait(timeout=_PROCEED_TIMEOUT)
        return GuardResult(allowed=True)


class _SyncRaisingHook:
    def on_execute(self, *a, **k) -> None:
        pass

    def on_retry(self, *a, **k) -> None:
        pass

    def on_success(self, *a, **k) -> None:
        raise RuntimeError("hook boom")

    def on_failure(self, *a, **k) -> None:
        raise RuntimeError("hook boom")

    def on_reject(self, *a, **k) -> None:
        raise RuntimeError("hook boom")


class _SyncRecordingSink:
    def __init__(self, sink_id: str | None = "sink-672") -> None:
        self._sink_id = sink_id
        self.calls: list = []

    def handle_failure(self, error, context, policy_result) -> str | None:
        self.calls.append((error, context, policy_result))
        return self._sink_id


class _SyncRaisingSink:
    def handle_failure(self, error, context, policy_result) -> str | None:
        raise RuntimeError("sink boom")


class _TimeoutRaisingPolicy:
    name = "timeout"

    async def execute(self, func, *args, context=None, **kwargs):
        raise TimeoutPolicyError(3.0)


def _inline_to_thread() -> AsyncMock:
    def _run(fn, /, *args, **kwargs):
        return fn(*args, **kwargs)

    return AsyncMock(side_effect=_run)


@pytest.fixture
def async_composer() -> AsyncPolicyComposer:
    return AsyncPolicyComposer()


# =============================================================================
# D2 — native channels awaited with zero thread hop
# =============================================================================


class TestNativeChannelsNoThreadHop:
    @pytest.mark.asyncio
    async def test_native_guard_awaited_without_to_thread(self, async_composer):
        guard = _NativeAllowGuard()
        async_composer.add_guard(guard)
        assert async_composer._guards[0] is guard  # native pass-through

        mock = _inline_to_thread()
        with patch(_TO_THREAD, new=mock):
            result = await async_composer.execute(_async_ok)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert guard.check_count == 1
        mock.assert_not_called()  # native guard: no thread hop

    @pytest.mark.asyncio
    async def test_native_hook_awaited_without_to_thread(self, async_composer):
        hook = _NativeRecordingHook()
        async_composer.add_hook(hook)

        mock = _inline_to_thread()
        with patch(_TO_THREAD, new=mock):
            result = await async_composer.execute(_async_ok)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert len(hook.success) == 1
        mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_channel_less_pipeline_makes_no_to_thread_call(self, async_composer):
        mock = _inline_to_thread()
        with patch(_TO_THREAD, new=mock):
            result = await async_composer.execute(_async_ok)
        assert result.outcome == PolicyOutcome.SUCCESS
        mock.assert_not_called()


# =============================================================================
# D2 — sync channels auto-wrapped and offloaded via to_thread
# =============================================================================


class TestSyncChannelAutowrap:
    @pytest.mark.asyncio
    async def test_sync_guard_autowrapped_and_offloaded(self, async_composer):
        guard = _SyncAllowGuard()
        async_composer.add_guard(guard)
        assert isinstance(async_composer._guards[0], _SyncGuardToAsyncAdapter)

        mock = _inline_to_thread()
        with patch(_TO_THREAD, new=mock):
            result = await async_composer.execute(_async_ok)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert guard.check_count == 1
        # the sync guard's check was offloaded off the loop
        assert guard.check in [call.args[0] for call in mock.call_args_list]

    @pytest.mark.asyncio
    async def test_sync_guard_autowrap_does_not_block_the_loop(self, async_composer):
        entered = threading.Event()
        proceed = threading.Event()
        guard = _SyncBlockingGuard(entered, proceed)
        async_composer.add_guard(guard)

        exec_task = asyncio.create_task(async_composer.execute(_async_ok))
        try:
            for _ in range(_POLL_ITERATIONS):
                if entered.is_set():
                    break
                await asyncio.sleep(_POLL_INTERVAL)
            entered_ok = entered.is_set()
        finally:
            proceed.set()
            result = await exec_task

        assert entered_ok, "worker thread never entered guard.check()"
        assert guard.proceed_observed is True
        assert result.outcome == PolicyOutcome.SUCCESS

    @pytest.mark.asyncio
    async def test_sync_hook_autowrapped(self, async_composer):
        async_composer.add_hook(_SyncRaisingHook())
        assert isinstance(async_composer._hooks[0], _SyncHookToAsyncAdapter)


# =============================================================================
# D3 — sync DLQ sink is the AsyncFailureSink offload-adapter consumer
# =============================================================================


class TestSinkOffloadAdapter:
    @pytest.mark.asyncio
    async def test_sync_sink_wrapped_in_offload_adapter(self, async_composer):
        sink = _SyncRecordingSink()
        async_composer.add_sink(sink)
        assert isinstance(async_composer._sinks[0], _SyncSinkToAsyncAdapter)

    @pytest.mark.asyncio
    async def test_sink_offload_adapter_fires_on_failure(self, async_composer):
        sink = _SyncRecordingSink(sink_id="sink-abc")
        async_composer.add_sink(sink)

        result = await async_composer.execute(_async_boom)

        assert result.outcome == PolicyOutcome.FAILURE
        assert len(sink.calls) == 1
        assert result.metadata["sink_id"] == "sink-abc"
        assert result.total_duration_ms > 0

    @pytest.mark.asyncio
    async def test_sink_offload_adapter_not_fired_on_reject(self, async_composer):
        class _RejectGuard:
            name = "reject_guard"

            async def check(self, context=None):
                return GuardResult(allowed=False, reason="dup")

        async_composer.add_guard(_RejectGuard())
        sink = _SyncRecordingSink()
        async_composer.add_sink(sink)

        result = await async_composer.execute(_async_ok)
        assert result.outcome == PolicyOutcome.REJECTED
        assert sink.calls == []

    @pytest.mark.asyncio
    async def test_sink_offload_adapter_not_fired_on_timeout(self, async_composer):
        async_composer.add(_TimeoutRaisingPolicy())
        sink = _SyncRecordingSink()
        async_composer.add_sink(sink)

        result = await async_composer.execute(_async_ok)
        assert result.outcome == PolicyOutcome.TIMEOUT
        assert sink.calls == []


# =============================================================================
# Fail-open parity (migrated) — an exception inside any channel is handled
# =============================================================================


class TestFailOpenParity:
    @pytest.mark.asyncio
    async def test_guard_exception_fails_open(self, async_composer):
        async_composer.add_guard(_SyncRaisingGuard())

        with capture_logs() as logs:
            result = await async_composer.execute(_async_ok)

        assert result.outcome == PolicyOutcome.SUCCESS  # fail-open
        events = [
            e for e in logs if e["event"] == "policy_composer.guard_execution_failed"
        ]
        assert len(events) == 1
        assert events[0]["guard_name"] == "raising_guard"
        assert events[0]["mode"] == "fail-open"

    @pytest.mark.asyncio
    async def test_hook_exception_is_swallowed(self, async_composer):
        async_composer.add_hook(_SyncRaisingHook())

        with capture_logs() as logs:
            result = await async_composer.execute(_async_ok)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert any(e["event"] == "hook.failed_fail_open" for e in logs)

    @pytest.mark.asyncio
    async def test_sink_exception_is_swallowed(self, async_composer):
        async_composer.add_sink(_SyncRaisingSink())

        with capture_logs() as logs:
            result = await async_composer.execute(_async_boom)

        assert result.outcome == PolicyOutcome.FAILURE
        assert any(e["event"] == "sink.failed" for e in logs)


# =============================================================================
# CancelledError escape (migrated) — BaseException is not swallowed by fail-open
# =============================================================================


class TestCancellationEscape:
    @pytest.mark.asyncio
    async def test_cancel_at_native_guard_propagates(self, async_composer):
        async_composer.add_guard(_NativeCancelGuard())
        with pytest.raises(asyncio.CancelledError):
            await async_composer.execute(_async_ok)

    @pytest.mark.asyncio
    async def test_cancel_at_offloaded_guard_await_propagates(self, async_composer):
        async_composer.add_guard(_SyncAllowGuard())
        mock = AsyncMock(side_effect=asyncio.CancelledError())
        with patch(_TO_THREAD, new=mock):
            with pytest.raises(asyncio.CancelledError):
                await async_composer.execute(_async_ok)

    @pytest.mark.asyncio
    async def test_plain_exception_at_offloaded_guard_fails_open(self, async_composer):
        async_composer.add_guard(_SyncAllowGuard())
        mock = AsyncMock(side_effect=RuntimeError("dispatch boom"))
        with patch(_TO_THREAD, new=mock):
            with capture_logs() as logs:
                result = await async_composer.execute(_async_ok)
        assert result.outcome == PolicyOutcome.SUCCESS
        assert any(e["event"] == "policy_composer.guard_execution_failed" for e in logs)


# =============================================================================
# Behavior preservation (migrated) — dedup, ordering, concurrent exactly-once
# =============================================================================


class TestBehaviorPreservation:
    @pytest.mark.asyncio
    async def test_exactly_once_double_acquire_across_repeat_calls(
        self, async_composer
    ):
        async_composer.add_guard(_NativeAcquireOnceGuard())

        first = await async_composer.execute(_async_ok)
        second = await async_composer.execute(_async_ok)

        assert first.outcome == PolicyOutcome.SUCCESS
        assert second.outcome == PolicyOutcome.REJECTED
        assert second.metadata["rejected_by"] == "idempotency"

    @pytest.mark.asyncio
    async def test_atomic_guard_preserves_exactly_once_under_concurrent_execute(
        self, async_composer
    ):
        # The native guard is awaited directly; two concurrent execute() calls
        # interleave, so exactly-once rests on the guard's acquire being atomic.
        async_composer.add_guard(_NativeAtomicAcquireOnceGuard())

        first, second = await asyncio.gather(
            async_composer.execute(_async_ok),
            async_composer.execute(_async_ok),
        )

        outcomes = sorted(o.value for o in (first.outcome, second.outcome))
        assert outcomes == [
            PolicyOutcome.REJECTED.value,
            PolicyOutcome.SUCCESS.value,
        ]

    @pytest.mark.asyncio
    async def test_guard_write_visible_to_later_hook_read(self, async_composer):
        hook = _NativeMarkerHook()
        async_composer.add_guard(_NativeMarkerGuard()).add_hook(hook)
        context = PolicyContext(extra={})

        result = await async_composer.execute(_async_ok, context=context)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert hook.observed_marker == _NativeMarkerGuard.MARKER_VALUE
        assert context.extra[_NativeMarkerGuard.MARKER_KEY] == (
            _NativeMarkerGuard.MARKER_VALUE
        )
