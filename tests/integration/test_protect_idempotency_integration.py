"""Mock-based integration tests for ``protect(idempotency_key=…)`` (#564).

Exercises the full idempotency bracket end-to-end through the public
``protect()`` / ``aprotect()`` facade against a real in-process, cache-backed
``IdempotencyGate``:

    IdempotencyGuard.check (Phase 1: check_and_acquire)
      → PolicyComposer chain (fn)
        → IdempotencyHook.on_success/on_failure (Phase 2: mark_completed/failed)

The guard, the hook, and the gate share one ``_POLICY_FALLBACK_CACHE`` resolved
via ``_ensure_policy_gate()``, so the acquire → mark state-transition lifecycle
spans a transaction boundary across two components plus the cache — a
composition a single-function unit test cannot drive end-to-end.

A keyed call whose wait was cut short (810) adds the timeout stage, the work
scope it records the running charge into, and the outside-end settle a
``BaseException`` exit takes instead of the hook: the key is held while the
charge runs, then follows it.

Infrastructure: in-process ``InMemoryCacheAdapter`` fallback (no Docker).
``ProviderRegistry.get_cache`` is patched to raise ``AdapterNotFoundError`` so
resolution lands on the fallback. The cross-worker (Redis) variants live in
``redis/test_timeout_key_redis.py``.
"""

from __future__ import annotations

import asyncio
import functools
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import patch

import pytest

from baldur.core.exceptions import (
    AdapterNotFoundError,
    IdempotencyDuplicateError,
    TimeoutPolicyError,
)
from baldur.interfaces.resilience_policy import PolicyContext, PolicyOutcome
from baldur.protect_facade import (
    aprotect,
    aprotect_with_meta,
    protect,
    protect_with_meta,
    reset_protect_caches,
)
from tests.factories.interruptions import (
    GeventTimeout,
    SoftTimeLimitExceeded,
    interrupted_timeout_wait,
)


@pytest.fixture(autouse=True)
def _isolate_in_process_idempotency():
    """Reset protect/idempotency singletons and force the in-process fallback
    cache so each test starts from a clean dedup state."""
    from baldur.runtime import reset_runtime
    from baldur.settings.idempotency import reset_idempotency_settings
    from baldur.settings.protect import reset_protect_settings

    def _reset() -> None:
        reset_protect_settings()
        reset_idempotency_settings()
        reset_runtime()
        reset_protect_caches()

    _reset()
    with patch(
        "baldur.factory.registry.ProviderRegistry.get_cache",
        side_effect=AdapterNotFoundError(adapter_type="cache"),
    ):
        yield
    _reset()


class TestProtectIdempotencyLifecycle:
    """Acquire → mark lifecycle across guard + hook + shared gate + composer."""

    def test_success_then_duplicate_is_blocked(self):
        # Given — a side-effecting fn protected with an idempotency key.
        calls = {"n": 0}

        def charge():
            calls["n"] += 1
            return "charged"

        # When — the operation runs once and is completed (Phase 2 mark).
        first = protect_with_meta(
            "payment.charge",
            charge,
            idempotency_key="order_id",
            context=PolicyContext(order_id="ord-1"),
            circuit_breaker=False,
            retry=False,
            dlq=False,
        )
        # Then — a duplicate carrying the same key is dedup-blocked, not re-run.
        second = protect_with_meta(
            "payment.charge",
            charge,
            idempotency_key="order_id",
            context=PolicyContext(order_id="ord-1"),
            circuit_breaker=False,
            retry=False,
            dlq=False,
        )

        assert first.success is True
        assert first.value == "charged"
        assert second.outcome == PolicyOutcome.REJECTED
        assert second.success is False
        assert calls["n"] == 1

    def test_distinct_keys_both_execute(self):
        calls = {"n": 0}

        def charge():
            calls["n"] += 1
            return calls["n"]

        for order_id in ("ord-a", "ord-b"):
            protect_with_meta(
                "payment.charge",
                charge,
                idempotency_key="order_id",
                context=PolicyContext(order_id=order_id),
                circuit_breaker=False,
                retry=False,
                dlq=False,
            )

        assert calls["n"] == 2

    def test_failure_marks_failed_and_allows_subsequent_retry(self):
        # A FAILED operation must NOT dedup-block a later attempt: the hook
        # marks it failed (not completed), so the next acquire CONTINUEs.
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return "recovered"

        first = protect_with_meta(
            "payment.flaky",
            flaky,
            idempotency_key="order_id",
            context=PolicyContext(order_id="ord-retry"),
            circuit_breaker=False,
            retry=False,
            dlq=False,
        )
        second = protect_with_meta(
            "payment.flaky",
            flaky,
            idempotency_key="order_id",
            context=PolicyContext(order_id="ord-retry"),
            circuit_breaker=False,
            retry=False,
            dlq=False,
        )

        assert first.success is False  # first attempt failed
        assert second.success is True  # retry allowed (not dedup-blocked)
        assert second.value == "recovered"
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_async_success_then_duplicate_is_blocked(self):
        calls = {"n": 0}

        async def submit():
            calls["n"] += 1
            return "submitted"

        first = await aprotect_with_meta(
            "webhook.submit",
            submit,
            idempotency_key="order_id",
            context=PolicyContext(order_id="evt-1"),
            circuit_breaker=False,
            retry=False,
            dlq=False,
        )
        second = await aprotect_with_meta(
            "webhook.submit",
            submit,
            idempotency_key="order_id",
            context=PolicyContext(order_id="evt-1"),
            circuit_breaker=False,
            retry=False,
            dlq=False,
        )

        assert first.success is True
        assert second.outcome == PolicyOutcome.REJECTED
        assert calls["n"] == 1


class TestProtectIdempotencyConcurrency:
    """#567 D1/G1: N concurrent ``protect(idempotency_key=)`` calls on ONE key
    run the side effect exactly once — exactly one wins ``CONTINUE`` and the
    rest get ``ABORT`` (``IdempotencyDuplicateError``). Exercises the
    guard → ``IdempotencyGate.check_and_acquire`` (atomic setnx on one shared
    cache record) → composer reject → facade ``_finalize_value`` lifecycle under
    real thread contention — a state dependency a single mocked-gate unit cannot
    prove."""

    def test_concurrent_duplicates_run_side_effect_exactly_once(self):
        n_callers = 8
        side_effect_runs = {"n": 0}
        runs_lock = threading.Lock()
        # The winner blocks inside fn until released, so it holds the gate record
        # in ``executing`` while the losers attempt their acquire — making them
        # deterministically observe ABORT (in-flight), not a stale/completed key.
        # Deterministic synchronization over time.sleep (UNIT_GUIDELINES §6.5.6).
        release = threading.Event()

        def fn():
            with runs_lock:
                side_effect_runs["n"] += 1
            release.wait(timeout=5.0)
            return "charged"

        def task():
            try:
                value = protect(
                    "payment.concurrent",
                    fn,
                    idempotency_key="order_id",
                    context=PolicyContext(order_id="same-key"),
                    circuit_breaker=False,
                    retry=False,
                    dlq=False,
                )
                return ("ok", value)
            except IdempotencyDuplicateError as exc:
                return ("dup", exc.decision)

        outcomes: list[tuple[str, object]] = []
        try:
            with ThreadPoolExecutor(max_workers=n_callers) as executor:
                futures = [executor.submit(task) for _ in range(n_callers)]
                for future in as_completed(futures, timeout=15):
                    outcomes.append(future.result())
                    # Once every loser has rejected, release the blocked winner.
                    if sum(1 for o in outcomes if o[0] == "dup") >= n_callers - 1:
                        release.set()
        finally:
            release.set()  # never leave the winner blocked

        oks = [o for o in outcomes if o[0] == "ok"]
        dups = [o for o in outcomes if o[0] == "dup"]

        # Exactly one caller ran the side effect; the rest were dedup-blocked.
        assert len(oks) == 1
        assert oks[0][1] == "charged"
        assert len(dups) == n_callers - 1
        assert all(decision == "ABORT" for _, decision in dups)
        assert side_effect_runs["n"] == 1


# =============================================================================
# A keyed call whose wait was cut short holds its key while its work runs
# =============================================================================

_HOLD_S = 5.0
# A sync timeout that fires while the work is already running.
_SYNC_TIMEOUT_FIRES_S = 1.0
_BARE = {"circuit_breaker": False, "retry": False, "dlq": False}
_poll = threading.Event()


def _eventually(predicate, timeout: float = _HOLD_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        _poll.wait(0.002)
    return predicate()


class _HeldCharge:
    """A charge that runs until released, then returns or raises."""

    def __init__(self, outcome: str = "returned") -> None:
        self.outcome = outcome
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.calls > 1:
            return "charged-again"
        self.entered.set()
        self.release.wait(_HOLD_S)
        if self.outcome == "raised":
            raise ConnectionError("gateway reset")
        return "charged"


def _decision(call) -> str:
    """A repeat's refusal decision, or "ran"."""
    try:
        call()
    except IdempotencyDuplicateError as e:
        return e.decision
    return "ran"


class TestProtectInterruptedKeyedCallLifecycle:
    """Guard → composer → timeout stage → (hook | outside-end settle) → gate,
    with the timeout stage's wait cut short by an interruption while the
    charge runs on the shared timeout executor."""

    @pytest.mark.parametrize(
        "interruption",
        [SoftTimeLimitExceeded, GeventTimeout],
        ids=["soft_time_limit_through_hook", "gevent_timeout_from_outside"],
    )
    @pytest.mark.parametrize(
        ("outcome", "after_end"),
        [("returned", "SKIP"), ("raised", "ran")],
        ids=["work_returned", "work_raised"],
    )
    def test_interrupted_keyed_call_holds_key_then_follows_its_own_work(
        self, interruption, outcome, after_end
    ):
        """
        Purpose:
            Verify that a keyed call whose wait on its running charge was cut
            short — by an ``Exception`` the hook sees (a soft time limit) or a
            ``BaseException`` that skips it (a gevent timeout) — keeps its key
            while the charge runs, then follows how the charge ended.
        Expected:
            - the interruption reaches the caller unchanged
            - a same-key repeat reads ``ABORT`` while the charge runs
            - once it ends: ``SKIP`` when it returned (never run twice),
              the repeat runs when it raised
        """
        # Given
        charge = _HeldCharge(outcome)

        def call():
            return protect(
                "payment.interrupted",
                charge,
                timeout=_HOLD_S,
                idempotency_key="order_id",
                context=PolicyContext(order_id="ord-cut"),
                **_BARE,
            )

        # When — the wait is cut while the charge runs; a repeat arrives.
        try:
            with interrupted_timeout_wait(interruption(), entered=charge.entered):
                with pytest.raises(interruption):
                    call()
            during = _decision(call)
        finally:
            charge.release.set()
        outcomes: list[str] = []

        def _settled() -> bool:
            outcomes.append(_decision(call))
            return outcomes[-1] != "ABORT"

        # Then
        assert during == "ABORT"
        assert _eventually(_settled)
        assert outcomes[-1] == after_end
        assert charge.calls == (1 if after_end == "SKIP" else 2)

    @pytest.mark.asyncio
    async def test_cancelled_aprotect_holds_key_while_nested_work_runs_then_releases(
        self,
    ):
        """
        Purpose:
            Verify that an ``aprotect`` call cancelled from outside while sync
            work it started (a nested ``protect(timeout=...)`` cut off on a
            worker thread) still runs keeps its key until that work ends, and
            is then released.
        Expected:
            - the cancel reaches the caller
            - a same-key repeat reads ``ABORT`` while the nested work runs
            - once it ends, a repeat runs (the nested work never decides)
        """
        # Given — the outer call's nested timed work is cut off and runs on.
        inner = _HeldCharge("returned")
        cut = asyncio.Event()
        calls = {"n": 0}

        async def outer() -> str:
            calls["n"] += 1
            if calls["n"] > 1:
                return "charged"
            timed = functools.partial(
                protect,
                "payment.inner",
                inner,
                timeout=_SYNC_TIMEOUT_FIRES_S,
                **_BARE,
            )
            try:
                await asyncio.to_thread(timed)
            except TimeoutPolicyError:
                cut.set()
            await asyncio.Event().wait()  # only the cancel ends it
            return "never"

        async def call():
            return await aprotect(
                "payment.outer",
                outer,
                idempotency_key="order_id",
                context=PolicyContext(order_id="ord-outer"),
                **_BARE,
            )

        async def decision() -> str:
            try:
                await call()
            except IdempotencyDuplicateError as e:
                return e.decision
            return "ran"

        # When — cancelled from outside; a repeat; then the nested work ends.
        try:
            task = asyncio.ensure_future(call())
            await asyncio.wait_for(cut.wait(), _HOLD_S)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            during = await decision()
        finally:
            inner.release.set()
        after = "ABORT"
        deadline = time.monotonic() + _HOLD_S
        while after == "ABORT" and time.monotonic() < deadline:
            await asyncio.sleep(0.002)
            after = await decision()

        # Then
        assert during == "ABORT"
        assert after == "ran"
        assert calls["n"] == 2
