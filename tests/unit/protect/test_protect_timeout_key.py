"""A timed-out keyed call's key follows the work the timeout abandoned.

End to end through ``protect`` / ``aprotect`` / ``@idempotent`` over the real
in-process ledger:

- While work a timeout could not stop still runs, the claim stays
  ``executing`` and a same-key repeat reads ``ABORT``; when the work ends the
  key follows it — refused (``SKIP``) after the call's own timed-out work
  returned, free after it failed.
- Work cut off before it started (the shared timeout executor saturated) and
  an async timeout (which cancels the coroutine) leave the key free at once.
- A nested timeout whose error a retry replaced, ``@idempotent`` over a
  function that raised after such a timeout, and work abandoned inside
  abandoned work hold the key while that work runs, then release it.
- Nested keyed calls with their own contexts each end with their own outcome,
  also when the inner one is cancelled from outside.
- A late outcome never changes a later claim's record, nor another key claimed
  on a reused ``PolicyContext``.

Verification techniques applied: scenario-style behavior, parametrize over
fallback x work outcome x nesting hop, mark-signal synchronization (the late
mark is awaited, never slept for).
"""

from __future__ import annotations

import asyncio
import functools
import threading
import time
from collections.abc import Callable, Generator
from typing import Any
from unittest.mock import patch

import pytest

from baldur.core.exceptions import (
    AdapterNotFoundError,
    IdempotencyDuplicateError,
    TimeoutPolicyError,
)
from baldur.decorators.idempotent import _reset_fallback_cache, idempotent
from baldur.interfaces.resilience_policy import PolicyContext
from baldur.protect_facade import aprotect, protect, reset_protect_caches
from baldur.resilience.policies.idempotency import (
    _ensure_async_policy_gate,
    _ensure_policy_gate,
)
from baldur.resilience.policies.timeout import TimeoutPolicy
from baldur.services.retry_handler.models import RetryPolicyConfig

# A sync facade timeout that fires while the function is already running: an
# idle shared-executor worker picks the call up long before it, so the cancel
# finds the work running (a future the executor has not started is cancelled
# and never runs).
_SYNC_TIMEOUT_FIRES_S = 1.0
# A timeout meant to cut off work that never starts, or a coroutine.
_TIMEOUT_FIRES_S = 0.05
# Upper bound on any wait for held work to enter, end, or be marked.
_HOLD_S = 5.0
_POLL_S = 0.002

# The facade's own retry / breaker / DLQ stay off so the only stages are the
# ones under test.
_BARE: dict[str, Any] = {"circuit_breaker": False, "retry": False, "dlq": False}

_poll = threading.Event()


def _eventually(predicate: Callable[[], bool], timeout: float = _HOLD_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        _poll.wait(_POLL_S)
    return predicate()


@pytest.fixture(autouse=True)
def _isolate_protect_idempotency():
    """Fresh protect / idempotency / ``@idempotent`` state on the in-process
    ledgers."""
    from baldur.runtime import reset_runtime
    from baldur.settings.idempotency import reset_idempotency_settings
    from baldur.settings.protect import reset_protect_settings

    def _reset() -> None:
        reset_protect_settings()
        reset_idempotency_settings()
        reset_runtime()
        reset_protect_caches()
        _reset_fallback_cache()

    _reset()
    with patch(
        "baldur.factory.registry.ProviderRegistry.get_cache",
        side_effect=AdapterNotFoundError(adapter_type="cache"),
    ):
        yield
    _reset()


class _MarkSignal:
    """Observes every mark the policy layer's sync gate makes."""

    def __init__(self) -> None:
        self.gate = _ensure_policy_gate()
        self.marks: list[tuple[str, str]] = []
        self._event = threading.Event()
        self._wrap("mark_completed", "completed")
        self._wrap("mark_failed", "failed")

    def _wrap(self, method: str, kind: str) -> None:
        real = getattr(self.gate, method)

        def _signalling(key: str, **kwargs: Any) -> None:
            try:
                real(key, **kwargs)
            finally:
                self.marks.append((kind, key))
                self._event.set()

        setattr(self.gate, method, _signalling)

    def arm(self) -> None:
        self._event.clear()

    def wait(self) -> bool:
        return self._event.wait(_HOLD_S)

    def record(self, key: str) -> dict[str, Any] | None:
        return self.gate._cache.get(key)


@pytest.fixture
def marks() -> _MarkSignal:
    """The memoized policy gate the facade will use, with its marks observed."""
    return _MarkSignal()


class _HeldWork:
    """A function body that runs until released, then returns or raises."""

    def __init__(self, outcome: str = "returned") -> None:
        self.outcome = outcome
        self.entered = threading.Event()
        self.release = threading.Event()
        self.exited = threading.Event()
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.calls > 1:
            return "charged-again"
        self.entered.set()
        try:
            self.release.wait(_HOLD_S)
            if self.outcome == "raised":
                raise ConnectionError("gateway reset")
            return "charged"
        finally:
            self.exited.set()


@pytest.fixture
def held() -> Generator[Callable[[str], _HeldWork], None, None]:
    """Factory of held bodies, all released at teardown."""
    made: list[_HeldWork] = []

    def _make(outcome: str = "returned") -> _HeldWork:
        work = _HeldWork(outcome)
        made.append(work)
        return work

    yield _make
    for work in made:
        work.release.set()


def _repeat_decision(call: Callable[[], Any]) -> str:
    """Make a repeat; return its refusal decision, or "ran"."""
    try:
        call()
    except IdempotencyDuplicateError as e:
        return e.decision
    return "ran"


def _repeat_after_release(call: Callable[[], Any]) -> Callable[[], bool]:
    """A probe that retries while the key is held (an ABORT runs nothing)."""

    def _probe() -> bool:
        try:
            call()
        except IdempotencyDuplicateError as e:
            if e.decision != "ABORT":
                raise
            return False
        return True

    return _probe


# =============================================================================
# Sync timeout — held while the work runs, then follows it
# =============================================================================


class TestProtectTimeoutKeyHeldBehavior:
    """SC5: protect(timeout=, idempotency_key=) whose function outlives the timeout."""

    @pytest.mark.parametrize(
        "with_fallback", [False, True], ids=["without_fallback", "with_fallback"]
    )
    @pytest.mark.parametrize(
        ("outcome", "after_end"),
        [("returned", "SKIP"), ("raised", "ran")],
        ids=["work_returned", "work_raised"],
    )
    def test_sync_timeout_holds_key_while_work_runs_then_follows_outcome(
        self, marks, held, with_fallback, outcome, after_end
    ):
        # Given — a charge that keeps running past the facade's timeout.
        work = held(outcome)
        fallback = {"fallback": lambda: "pending"} if with_fallback else {}

        def call() -> Any:
            return protect(
                "svc.tk",
                work,
                timeout=_SYNC_TIMEOUT_FIRES_S,
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                **fallback,
                **_BARE,
            )

        # When — the timeout fires; a repeat arrives while the charge runs.
        if with_fallback:
            assert call() == "pending"
        else:
            with pytest.raises(TimeoutPolicyError):
                call()
        assert work.entered.wait(_HOLD_S)
        during = _repeat_decision(call)
        marks.arm()
        work.release.set()
        assert marks.wait()

        # Then — refused as in flight, then the repeat follows the outcome.
        assert during == "ABORT"
        assert _repeat_decision(call) == after_end
        assert work.calls == (1 if after_end == "SKIP" else 2)


# =============================================================================
# Work that never started, and async timeouts — free at once
# =============================================================================


class TestProtectTimeoutKeyReleasedBehavior:
    """SC6: a keyed call whose work never ran leaves the key free for a retry."""

    @pytest.mark.parametrize(
        "with_fallback", [False, True], ids=["without_fallback", "with_fallback"]
    )
    def test_unstarted_sync_timeout_frees_key_for_immediate_retry(
        self, monkeypatch, with_fallback
    ):
        """The shared timeout executor is saturated: the call never started."""
        from baldur.runtime import reset_runtime
        from baldur.settings.protect import reset_protect_settings

        # Given — one shared timeout worker, kept busy.
        monkeypatch.setenv("BALDUR_PROTECT_DEFAULT_TIMEOUT_EXECUTOR_WORKERS", "1")
        reset_protect_settings()
        reset_runtime()
        reset_protect_caches()
        busy, started = threading.Event(), threading.Event()

        def _occupy() -> None:
            started.set()
            busy.wait(_HOLD_S)

        blocker = TimeoutPolicy._get_executor().submit(_occupy)
        assert started.wait(_HOLD_S)
        calls = {"n": 0}

        def charge() -> str:
            calls["n"] += 1
            return "charged"

        fallback = {"fallback": lambda: "pending"} if with_fallback else {}

        def call(timeout: float) -> Any:
            return protect(
                "svc.unstarted",
                charge,
                timeout=timeout,
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                **fallback,
                **_BARE,
            )

        # When — the call is cut off in the queue; the worker frees; a retry.
        try:
            if with_fallback:
                assert call(_TIMEOUT_FIRES_S) == "pending"
            else:
                with pytest.raises(TimeoutPolicyError):
                    call(_TIMEOUT_FIRES_S)
        finally:
            busy.set()
            blocker.result(_HOLD_S)
        retried = call(_HOLD_S)

        # Then — the retry ran the charge; the first call never did.
        assert retried == "charged"
        assert calls["n"] == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "with_fallback", [False, True], ids=["without_fallback", "with_fallback"]
    )
    async def test_async_timeout_frees_key_for_immediate_retry(self, with_fallback):
        """The timeout cancels the coroutine, so nothing it did keeps running."""
        calls = {"n": 0}

        async def charge() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                await asyncio.Event().wait()  # only the timeout's cancel ends it
            return "charged"

        async def pending() -> str:
            return "pending"

        fallback = {"fallback": pending} if with_fallback else {}

        async def call() -> Any:
            return await aprotect(
                "svc.async-timeout",
                charge,
                timeout=_TIMEOUT_FIRES_S,
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                **fallback,
                **_BARE,
            )

        if with_fallback:
            assert await call() == "pending"
        else:
            with pytest.raises(TimeoutPolicyError):
                await call()
        retried = await call()

        assert retried == "charged"
        assert calls["n"] == 2


# =============================================================================
# Hidden and nested timeouts hold the key while their work runs
# =============================================================================


class TestProtectNestedTimeoutKeyBehavior:
    """A timeout the mark site never sees still holds the key while its work runs."""

    def test_nested_timeout_hidden_by_retry_holds_key_until_inner_work_ends(
        self, marks, held
    ):
        # Given — attempt 1 times out on a nested call whose work keeps
        # running; attempt 2 fails differently, so that error is the call's.
        inner = held("returned")
        attempts = {"n": 0}

        def charge() -> str:
            attempts["n"] += 1
            if attempts["n"] == 1:
                protect("svc.inner", inner, timeout=_SYNC_TIMEOUT_FIRES_S, **_BARE)
            if attempts["n"] == 2:
                raise ValueError("declined")
            return "charged"

        def call() -> Any:
            return protect(
                "svc.outer",
                charge,
                retry=RetryPolicyConfig(
                    domain="svc.outer", max_attempts=2, backoff_base=0
                ),
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                circuit_breaker=False,
                dlq=False,
            )

        with pytest.raises(ValueError, match="declined"):
            call()

        # When — a repeat while the inner work runs; then that work ends.
        during = _repeat_decision(call)
        marks.arm()
        inner.release.set()
        assert marks.wait()

        # Then — held, then released (the call raised): the repeat runs.
        assert during == "ABORT"
        assert marks.marks[-1] == ("failed", "svc.outer:o-1")
        assert call() == "charged"
        assert attempts["n"] == 3

    @pytest.mark.parametrize("outcome", ["returned", "raised"])
    def test_idempotent_over_raise_after_nested_timeout_releases_when_work_ends(
        self, held, outcome
    ):
        """Never SKIP: whatever the abandoned work did, the call raised."""
        work = held(outcome)
        calls = {"n": 0}

        @idempotent(key_fn=lambda order_id: f"decorated:{order_id}")
        def place(order_id: str) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                protect("svc.nested", work, timeout=_SYNC_TIMEOUT_FIRES_S, **_BARE)
            return "placed"

        with pytest.raises(TimeoutPolicyError):
            place("o-1")
        during = _repeat_decision(lambda: place("o-1"))
        work.release.set()

        assert during == "ABORT"
        assert _eventually(_repeat_after_release(lambda: place("o-1")))
        assert calls["n"] == 2

    def test_idempotent_holds_while_work_started_inside_abandoned_step_runs(self, held):
        """A timeout inside the still-running step adds its work to the hold."""
        # Given — the step times out at the outer bound and keeps running;
        # inside it a nested timeout later abandons a sub-step, then the step
        # ends while the sub-step still runs.
        substep = held("returned")
        step_exited = threading.Event()
        calls = {"n": 0}
        step_timeout_s = _SYNC_TIMEOUT_FIRES_S
        substep_timeout_s = step_timeout_s + 1.5

        def step() -> None:
            try:
                protect("svc.substep", substep, timeout=substep_timeout_s, **_BARE)
            finally:
                step_exited.set()

        @idempotent(key_fn=lambda order_id: f"inside:{order_id}")
        def place(order_id: str) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                protect("svc.step", step, timeout=step_timeout_s, **_BARE)
            return "placed"

        with pytest.raises(TimeoutPolicyError):
            place("o-1")

        # When — the step has ended; only the sub-step still runs.
        assert step_exited.wait(_HOLD_S)
        assert substep.entered.is_set()
        during = _repeat_decision(lambda: place("o-1"))
        substep.release.set()

        # Then — held by the sub-step, released once it ends.
        assert during == "ABORT"
        assert calls["n"] == 1
        assert _eventually(_repeat_after_release(lambda: place("o-1")))
        assert calls["n"] == 2


# =============================================================================
# Nested keyed calls with their own contexts
# =============================================================================


class TestProtectNestedKeyedCallsBehavior:
    """Each keyed call ends with its own key marked by its own outcome."""

    @pytest.mark.parametrize(
        ("inner_raises", "inner_status"),
        [(False, "completed"), (True, "failed")],
        ids=["inner_returned", "inner_raised"],
    )
    def test_nested_keyed_calls_each_marked_by_own_outcome(
        self, marks, inner_raises, inner_status
    ):
        def inner() -> str:
            if inner_raises:
                raise ConnectionError("gateway reset")
            return "inner"

        def outer() -> str:
            try:
                protect(
                    "svc.in",
                    inner,
                    idempotency_key="order_id",
                    context=PolicyContext(order_id="i-1"),
                    **_BARE,
                )
            except ConnectionError:
                pass
            return "outer"

        result = protect(
            "svc.out",
            outer,
            idempotency_key="order_id",
            context=PolicyContext(order_id="o-1"),
            **_BARE,
        )

        assert result == "outer"
        assert marks.record("svc.out:o-1")["status"] == "completed"
        assert marks.record("svc.in:i-1")["status"] == inner_status

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("swallowed", "outer_status"),
        [(True, "completed"), (False, "failed")],
        ids=["cancel_swallowed", "cancel_propagated"],
    )
    async def test_inner_cancelled_from_outside_leaves_outer_marked_by_outer_outcome(
        self, swallowed, outer_status
    ):
        """The inner hook never runs; the outer key still ends by its own outcome."""
        gate = _ensure_async_policy_gate()

        async def slow() -> str:
            await asyncio.Event().wait()
            return "never"

        async def outer() -> str:
            try:
                async with asyncio.timeout(_TIMEOUT_FIRES_S):
                    await aprotect(
                        "svc.ain",
                        slow,
                        idempotency_key="order_id",
                        context=PolicyContext(order_id="i-1"),
                        **_BARE,
                    )
            except TimeoutError:
                if not swallowed:
                    raise
            return "outer"

        if swallowed:
            assert (
                await aprotect(
                    "svc.aout",
                    outer,
                    idempotency_key="order_id",
                    context=PolicyContext(order_id="o-1"),
                    **_BARE,
                )
                == "outer"
            )
        else:
            with pytest.raises(TimeoutError):
                await aprotect(
                    "svc.aout",
                    outer,
                    idempotency_key="order_id",
                    context=PolicyContext(order_id="o-1"),
                    **_BARE,
                )

        record = await gate._cache.aget("svc.aout:o-1")
        assert record["status"] == outer_status


# =============================================================================
# Late outcomes never reach another claim or another key
# =============================================================================


class TestProtectLateMarkBehavior:
    """A late mark is scoped to its claim and carries the key read at close."""

    def test_late_success_leaves_later_claim_executing(self, marks, held):
        # Given — call A deferred; its claim went stale and call B took over,
        # B's own work still running too.
        first, second = held("returned"), held("returned")
        works = iter([first, second])

        def call() -> Any:
            return protect(
                "svc.late",
                next(works),
                fallback=lambda: "pending",
                timeout=_SYNC_TIMEOUT_FIRES_S,
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                **_BARE,
            )

        assert call() == "pending"
        marks.record("svc.late:o-1")["started_at"] = 0
        assert call() == "pending"
        later_claim = marks.record("svc.late:o-1")["claim_id"]

        # When — A's work ends and succeeds.
        marks.arm()
        first.release.set()
        assert marks.wait()

        # Then — B's claim still holds the key; B's own end then settles it.
        record = marks.record("svc.late:o-1")
        assert record["status"] == "executing"
        assert record["claim_id"] == later_claim
        marks.arm()
        second.release.set()
        assert marks.wait()
        assert marks.record("svc.late:o-1")["status"] == "completed"

    def test_reused_context_later_key_keeps_its_own_outcome(self, marks, held):
        """Call B on call A's PolicyContext; A's late failure marks only A's key."""
        # Given — A deferred on key k-1; B reused the context with key k-2.
        work_a = held("raised")
        context = PolicyContext(extra={"ref": "k-1"})
        assert (
            protect(
                "svc.reuse",
                work_a,
                fallback=lambda: "pending",
                timeout=_SYNC_TIMEOUT_FIRES_S,
                idempotency_key="ref",
                context=context,
                **_BARE,
            )
            == "pending"
        )
        context.extra["ref"] = "k-2"
        assert (
            protect(
                "svc.reuse",
                lambda: "b-done",
                idempotency_key="ref",
                context=context,
                **_BARE,
            )
            == "b-done"
        )

        # When — A's work ends and fails.
        marks.arm()
        work_a.release.set()
        assert marks.wait()

        # Then
        assert marks.record("svc.reuse:k-1")["status"] == "failed"
        assert marks.record("svc.reuse:k-2")["status"] == "completed"


# =============================================================================
# An async keyed call over nested sync timed work
# =============================================================================


async def _await_status(gate: Any, key: str, status: str) -> bool:
    deadline = time.monotonic() + _HOLD_S
    while time.monotonic() < deadline:
        record = await gate._cache.aget(key)
        if record is not None and record["status"] == status:
            return True
        await asyncio.sleep(_POLL_S)
    return False


class TestAprotectNestedSyncWorkKeyBehavior:
    """SC5: an async keyed call is held by sync timed work it started, where
    the context reaches that work."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("hop", ["direct", "to_thread"])
    async def test_async_key_held_while_nested_sync_work_runs_then_released(
        self, held, hop
    ):
        # Given — the coroutine's nested sync timed call times out, its work
        # still running.
        gate = _ensure_async_policy_gate()
        work = held("returned")
        calls = {"n": 0}

        async def charge() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                timed = functools.partial(
                    protect,
                    "svc.async-inner",
                    work,
                    timeout=_SYNC_TIMEOUT_FIRES_S,
                    **_BARE,
                )
                if hop == "direct":
                    timed()
                else:
                    await asyncio.to_thread(timed)
            return "charged"

        async def call() -> Any:
            return await aprotect(
                "svc.async-outer",
                charge,
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                **_BARE,
            )

        with pytest.raises(TimeoutPolicyError):
            await call()

        # When — a repeat while the work runs; then the work ends.
        with pytest.raises(IdempotencyDuplicateError) as during:
            await call()
        work.release.set()

        # Then — released once it ended (the work was not the call's own).
        assert during.value.decision == "ABORT"
        assert await _await_status(gate, "svc.async-outer:o-1", "failed")
        assert await call() == "charged"
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_run_in_executor_work_is_not_tracked_and_key_released_at_once(
        self, held
    ):
        """Disclosed bound: loop.run_in_executor starts with an empty context."""
        work = held("returned")
        calls = {"n": 0}

        async def charge() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    functools.partial(
                        protect,
                        "svc.executor-inner",
                        work,
                        timeout=_SYNC_TIMEOUT_FIRES_S,
                        **_BARE,
                    ),
                )
            return "charged"

        async def call() -> Any:
            return await aprotect(
                "svc.executor-outer",
                charge,
                idempotency_key="order_id",
                context=PolicyContext(order_id="o-1"),
                **_BARE,
            )

        with pytest.raises(TimeoutPolicyError):
            await call()

        assert work.entered.is_set()
        assert not work.exited.is_set()
        assert await call() == "charged"
        assert calls["n"] == 2
