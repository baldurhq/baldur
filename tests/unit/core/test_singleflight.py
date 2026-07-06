"""
Singleflight unit tests (doc 594 D1).

Verification techniques:
- Concurrency: winner/waiter dedup under simultaneous same-key calls (§8.7)
- Side effects: in-flight map self-cleaning after success and exception (§8.4)
- Exception/edge cases: shared exception instance, re-entrancy fast-fail (§8.2)
- Dependency interaction: compute callable call count (§8.5)

Determinism note: concurrent dedup assertions use
``make_observable_singleflight`` (tests/factories/concurrency_helpers.py)
so the winner's compute completes only after every caller has committed
to the winner or waiter role - no sleeps, no late-arrival flake.
"""

from __future__ import annotations

import threading

import pytest

from baldur.core.singleflight import Singleflight
from tests.factories.concurrency_helpers import make_observable_singleflight

N_CALLERS = 8


class TestSingleflightBehavior:
    """Winner/waiter result sharing, exception sharing, map self-cleaning."""

    def test_run_single_caller_returns_fn_result(self):
        """A lone caller gets fn's return value directly."""
        sf: Singleflight[str] = Singleflight()
        assert sf.run("key", lambda: "value") == "value"

    def test_run_sequential_calls_compute_each_time(self):
        """Singleflight dedups in-flight calls only - it is not a cache."""
        sf: Singleflight[int] = Singleflight()
        calls: list[int] = []

        def fn() -> int:
            calls.append(1)
            return len(calls)

        first = sf.run("key", fn)
        second = sf.run("key", fn)

        assert (first, second) == (1, 2)
        assert len(calls) == 2

    def test_concurrent_same_key_executes_fn_exactly_once(self):
        """N concurrent callers on one key -> 1 compute, shared value."""
        # Given - a singleflight that signals when all callers entered
        sf, all_entered = make_observable_singleflight(N_CALLERS)
        calls: list[int] = []
        results: list[str] = []
        errors: list[Exception] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(N_CALLERS)

        def gated_fn() -> str:
            calls.append(1)  # winner-only - no lock needed
            all_entered.wait(timeout=5.0)
            return "computed"

        def worker() -> None:
            try:
                barrier.wait(timeout=5.0)
                value = sf.run("hot-key", gated_fn)
                with results_lock:
                    results.append(value)
            except Exception as e:  # pragma: no cover - failure diagnostics
                with results_lock:
                    errors.append(e)

        # When - all callers race on the same key
        threads = [threading.Thread(target=worker) for _ in range(N_CALLERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        # Then - exactly one compute, every caller got the winner's value
        assert errors == []
        assert len(calls) == 1
        assert results == ["computed"] * N_CALLERS

    def test_concurrent_callers_share_winner_exception_instance(self):
        """Winner's exception propagates as the SAME instance to all waiters."""
        # Given
        sf, all_entered = make_observable_singleflight(N_CALLERS)
        calls: list[int] = []
        captured: list[ValueError] = []
        captured_lock = threading.Lock()

        def failing_fn() -> str:
            calls.append(1)
            all_entered.wait(timeout=5.0)
            raise ValueError("backend down")

        def worker() -> None:
            try:
                sf.run("hot-key", failing_fn)
            except ValueError as e:
                with captured_lock:
                    captured.append(e)

        # When
        threads = [threading.Thread(target=worker) for _ in range(N_CALLERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        # Then - one fetch-failure total, identical instance everywhere
        assert len(calls) == 1
        assert len(captured) == N_CALLERS
        assert all(e is captured[0] for e in captured)

    def test_inflight_map_empty_after_success(self):
        """The in-flight entry is removed in the winner's finally."""
        sf: Singleflight[str] = Singleflight()
        sf.run("key", lambda: "value")
        assert sf._inflight == {}

    def test_inflight_map_empty_after_exception(self):
        """A raising fn still removes its in-flight entry (no leak)."""
        sf: Singleflight[str] = Singleflight()

        def failing_fn() -> str:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            sf.run("key", failing_fn)

        assert sf._inflight == {}

    def test_failed_key_is_recomputable_immediately(self):
        """After a failure, the next caller for the key computes fresh."""
        sf: Singleflight[str] = Singleflight()

        def failing_fn() -> str:
            raise RuntimeError("transient")

        with pytest.raises(RuntimeError):
            sf.run("key", failing_fn)

        assert sf.run("key", lambda: "recovered") == "recovered"

    def test_blocked_key_does_not_block_other_keys(self):
        """An in-flight slow key does not delay unrelated keys (no false sharing)."""
        # Given - "slow-key" is mid-compute on another thread
        sf: Singleflight[str] = Singleflight()
        started = threading.Event()
        release = threading.Event()

        def slow_fn() -> str:
            started.set()
            release.wait(timeout=5.0)
            return "slow"

        slow_thread = threading.Thread(target=lambda: sf.run("slow-key", slow_fn))
        slow_thread.start()
        try:
            assert started.wait(timeout=5.0)

            # When - an unrelated key computes while slow-key is in flight
            fast_result = sf.run("fast-key", lambda: "fast")

            # Then - it returns without waiting on slow-key
            assert fast_result == "fast"
        finally:
            release.set()
            slow_thread.join(timeout=5.0)

        assert sf._inflight == {}


class TestSingleflightReentrancyBehavior:
    """Re-entrancy fast-fail: same-thread same-key recursion raises, not hangs."""

    def test_reentrant_same_key_raises_runtime_error(self):
        """fn re-entering run() for its own key fast-fails with RuntimeError."""
        sf: Singleflight[str] = Singleflight()

        def outer() -> str:
            return sf.run("key", lambda: "inner")

        with pytest.raises(RuntimeError, match="re-entrancy detected"):
            sf.run("key", outer)

    def test_reentrancy_failure_leaves_map_clean_and_key_reusable(self):
        """The fast-fail still self-cleans; the key works on the next call."""
        sf: Singleflight[str] = Singleflight()

        with pytest.raises(RuntimeError, match="re-entrancy detected"):
            sf.run("key", lambda: sf.run("key", lambda: "never"))

        assert sf._inflight == {}
        assert sf.run("key", lambda: "recovered") == "recovered"

    def test_reentrant_different_key_is_allowed(self):
        """Nested compute on a DIFFERENT key is legitimate composition."""
        sf: Singleflight[str] = Singleflight()

        def outer() -> str:
            inner = sf.run("dep-key", lambda: "dep")
            return f"outer+{inner}"

        assert sf.run("main-key", outer) == "outer+dep"
        assert sf._inflight == {}

    def test_same_key_sequential_on_same_thread_is_not_reentrancy(self):
        """Winner bookkeeping is discarded after run() - sequential reuse is fine."""
        sf: Singleflight[str] = Singleflight()
        assert sf.run("key", lambda: "first") == "first"
        assert sf.run("key", lambda: "second") == "second"
