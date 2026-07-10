"""Unit tests for the shared rate-limiting algorithm primitives.

Target: core/rate_limiting.py
- SlidingWindowCounter: try_acquire / record / record_and_count / count /
  snapshot / restore / retention / cleanup_interval / reset, clock injection.
- TokenBucket: consume / refill-over-time / capacity / set_rate / get_rate /
  get_token_ratio / wait_for_token, clock injection.

Both primitives take an injectable ``clock`` so the window/refill arithmetic is
steered deterministically without patching the global clock or sleeping.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from baldur.core import rate_limiting
from baldur.core.rate_limiting import SlidingWindowCounter, TokenBucket


class _FakeClock:
    """Deterministic injectable clock; ``advance`` steps time forward."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class _AutoAdvanceClock:
    """Clock that advances a fixed step on every read.

    Used only for the ``wait_for_token`` timeout path, whose loop must make
    progress across reads to terminate deterministically.
    """

    def __init__(self, start: float = 1000.0, step: float = 0.5) -> None:
        self._t = start
        self._step = step

    def __call__(self) -> float:
        value = self._t
        self._t += self._step
        return value


# =============================================================================
# Module contract
# =============================================================================


class TestRateLimitingModuleContract:
    """Module-surface contract."""

    def test_module_exports_exactly_the_two_primitives(self):
        """__all__ declares SlidingWindowCounter + TokenBucket, nothing else."""
        assert set(rate_limiting.__all__) == {"SlidingWindowCounter", "TokenBucket"}


# =============================================================================
# SlidingWindowCounter — contract (hardcoded spec semantics)
# =============================================================================


class TestSlidingWindowCounterContract:
    """Window-boundary and clock-resolution contracts."""

    def test_cutoff_is_strictly_greater_than_window_start(self):
        """An event exactly at ``now - window_seconds`` is EXCLUDED (strict >)."""
        clock = _FakeClock(start=1000.0)
        counter = SlidingWindowCounter(clock=clock)
        counter.record("k")  # timestamp == 1000.0

        # now == 1009 -> window_start == 999 -> 1000 > 999 -> included.
        clock.advance(9.0)
        assert counter.count("k", window_seconds=10.0) == 1

        # now == 1010 -> window_start == 1000 -> 1000 > 1000 is False -> excluded.
        clock.advance(1.0)
        assert counter.count("k", window_seconds=10.0) == 0

    def test_default_clock_resolves_time_time_at_call_time_and_is_patchable(self):
        """The default (clock=None) resolves time.time() per call, so patching
        the module attribute steers the window (Execution Notes refinement #1)."""
        counter = SlidingWindowCounter()  # no injected clock

        with patch("time.time", return_value=1000.0):
            counter.record("k")

        # 5s later the event is still inside a 10s window.
        with patch("time.time", return_value=1005.0):
            assert counter.count("k", window_seconds=10.0) == 1

        # 11s later it has aged out (window_start == 1001 > 1000).
        with patch("time.time", return_value=1011.0):
            assert counter.count("k", window_seconds=10.0) == 0

    def test_injected_clock_steers_the_window(self):
        """A consumer-supplied clock fully determines in-window membership."""
        clock = _FakeClock(start=1000.0)
        counter = SlidingWindowCounter(clock=clock)
        counter.record("k")

        clock.advance(9.0)
        assert counter.count("k", window_seconds=10.0) == 1
        clock.advance(2.0)  # total 11s elapsed, past the 10s window
        assert counter.count("k", window_seconds=10.0) == 0


# =============================================================================
# SlidingWindowCounter — behavior
# =============================================================================


class TestSlidingWindowCounterBehavior:
    """Enforcement, counting, persistence, retention, and lifecycle behavior."""

    @pytest.mark.parametrize(
        ("max_events", "prior_records", "expected_allowed", "expected_count"),
        [
            (1, 0, True, 1),  # first event, limit 1 -> allowed
            (1, 1, False, 1),  # at limit 1 -> denied, count unchanged
            (3, 2, True, 3),  # under limit 3 -> allowed
            (3, 3, False, 3),  # at limit 3 -> denied
        ],
    )
    def test_try_acquire_respects_limit_boundary(
        self, max_events, prior_records, expected_allowed, expected_count
    ):
        """try_acquire admits strictly below the limit and denies at/over it."""
        clock = _FakeClock()
        counter = SlidingWindowCounter(clock=clock)
        for _ in range(prior_records):
            counter.record("k")

        allowed, count = counter.try_acquire("k", max_events, window_seconds=60.0)

        assert allowed is expected_allowed
        assert count == expected_count

    def test_try_acquire_denied_does_not_record_the_event(self):
        """A denied acquire must not append (memory bounded to ~max_events)."""
        clock = _FakeClock()
        counter = SlidingWindowCounter(clock=clock)
        counter.record("k")  # at limit for max_events=1

        for _ in range(5):
            allowed, count = counter.try_acquire("k", 1, window_seconds=60.0)
            assert allowed is False
            assert count == 1  # never grows past the single stored event

        assert counter.count("k", window_seconds=60.0) == 1

    def test_try_acquire_recovers_capacity_after_window_slides(self):
        """State transition: events leaving the window free capacity again."""
        clock = _FakeClock(start=1000.0)
        counter = SlidingWindowCounter(clock=clock)

        assert counter.try_acquire("k", 2, window_seconds=10.0) == (True, 1)
        assert counter.try_acquire("k", 2, window_seconds=10.0) == (True, 2)
        assert counter.try_acquire("k", 2, window_seconds=10.0) == (False, 2)

        clock.advance(11.0)  # both prior events now older than the window
        assert counter.try_acquire("k", 2, window_seconds=10.0) == (True, 1)

    def test_record_and_count_appends_prunes_and_counts_atomically(self):
        """record_and_count reflects only the events inside the moving window."""
        clock = _FakeClock(start=1000.0)
        counter = SlidingWindowCounter(clock=clock)

        assert counter.record_and_count("k", window_seconds=10.0) == 1  # t=1000
        clock.advance(5.0)
        assert counter.record_and_count("k", window_seconds=10.0) == 2  # t=1005
        clock.advance(6.0)  # t=1011, window_start=1001 -> 1000 pruned
        assert counter.record_and_count("k", window_seconds=10.0) == 2  # 1005,1011

    def test_count_does_not_prune_the_stored_series(self):
        """count is non-destructive: a narrow-window read does not evict events
        that a wider-window read still sees."""
        clock = _FakeClock(start=1000.0)
        counter = SlidingWindowCounter(clock=clock)
        counter.record("k")

        clock.advance(20.0)
        assert counter.count("k", window_seconds=10.0) == 0  # aged out of 10s
        assert counter.count("k", window_seconds=60.0) == 1  # still stored

    def test_records_are_isolated_per_key(self):
        """Distinct keys maintain independent series."""
        clock = _FakeClock()
        counter = SlidingWindowCounter(clock=clock)
        counter.record("a")
        counter.record("a")
        counter.record("b")

        assert counter.count("a", window_seconds=60.0) == 2
        assert counter.count("b", window_seconds=60.0) == 1
        assert counter.count("c", window_seconds=60.0) == 0
        assert set(counter.keys()) == {"a", "b"}

    def test_snapshot_returns_an_isolated_copy(self):
        """Mutating the snapshot list must not affect the counter's state."""
        clock = _FakeClock()
        counter = SlidingWindowCounter(clock=clock)
        counter.record("k")

        snap = counter.snapshot("k", window_seconds=60.0)
        snap.append(99999.0)
        snap.clear()

        assert counter.count("k", window_seconds=60.0) == 1

    def test_snapshot_then_restore_preserves_the_window(self):
        """snapshot -> restore round-trips the in-window series into a peer."""
        clock = _FakeClock(start=1000.0)
        src = SlidingWindowCounter(clock=clock)
        src.record("k")
        src.record("k")
        src.record("k")

        snap = src.snapshot("k", window_seconds=60.0)
        dst = SlidingWindowCounter(clock=clock)
        dst.restore("k", snap)

        assert dst.count("k", window_seconds=60.0) == 3
        assert dst.snapshot("k", window_seconds=60.0) == snap

    def test_restore_sorts_input_ascending(self):
        """restore normalizes order so later window arithmetic stays correct."""
        clock = _FakeClock(start=1000.0)
        counter = SlidingWindowCounter(clock=clock)

        counter.restore("k", [1005.0, 1000.0, 1003.0])

        assert counter.snapshot("k", window_seconds=60.0) == [1000.0, 1003.0, 1005.0]

    def test_record_with_retention_front_trims_old_events_on_write(self):
        """retention_seconds bounds an append-only series via prune-on-write."""
        clock = _FakeClock(start=1000.0)
        counter = SlidingWindowCounter(retention_seconds=10.0, clock=clock)

        counter.record("k")  # t=1000
        clock.advance(5.0)
        counter.record("k")  # t=1005, cutoff 995 -> keep both
        clock.advance(20.0)
        counter.record("k")  # t=1025, cutoff 1015 -> drop 1000 and 1005

        assert counter.snapshot("k", window_seconds=60.0) == [1025.0]

    def test_record_without_retention_is_append_only(self):
        """Default (retention None) never trims on record."""
        clock = _FakeClock(start=1000.0)
        counter = SlidingWindowCounter(clock=clock)

        counter.record("k")
        clock.advance(1000.0)
        counter.record("k")

        assert counter.count("k", window_seconds=5000.0) == 2

    def test_cleanup_interval_evicts_emptied_keys_after_the_interval(self):
        """The periodic sweep drops keys whose events have all aged out."""
        clock = _FakeClock(start=1000.0)
        counter = SlidingWindowCounter(cleanup_interval=30.0, clock=clock)

        counter.try_acquire("stale", 5, window_seconds=10.0)  # records at t=1000
        assert "stale" in counter.keys()

        clock.advance(31.0)  # past both the cleanup interval and the 10s window
        counter.try_acquire("active", 5, window_seconds=10.0)  # triggers the sweep

        assert "stale" not in counter.keys()
        assert "active" in counter.keys()

    def test_cleanup_interval_does_not_sweep_before_the_interval_elapses(self):
        """No stale-key eviction until cleanup_interval has passed (boundary)."""
        clock = _FakeClock(start=1000.0)
        counter = SlidingWindowCounter(cleanup_interval=30.0, clock=clock)

        counter.try_acquire("stale", 5, window_seconds=10.0)  # t=1000
        clock.advance(15.0)  # within the 30s interval though past the 10s window
        counter.try_acquire("active", 5, window_seconds=10.0)

        assert "stale" in counter.keys()  # not yet swept

    def test_no_cleanup_interval_retains_emptied_keys(self):
        """Default (cleanup_interval None) never evicts — the parked stale-key gap."""
        clock = _FakeClock(start=1000.0)
        counter = SlidingWindowCounter(clock=clock)

        counter.try_acquire("k", 5, window_seconds=10.0)
        clock.advance(100.0)
        counter.try_acquire("other", 5, window_seconds=10.0)

        assert "k" in counter.keys()

    def test_reset_removes_a_single_key_and_reports_prior_existence(self):
        """reset returns True only when the key existed."""
        counter = SlidingWindowCounter(clock=_FakeClock())
        counter.record("k")

        assert counter.reset("k") is True
        assert counter.reset("k") is False
        assert counter.count("k", window_seconds=60.0) == 0

    def test_reset_all_clears_every_key(self):
        """reset_all drops all tracked keys."""
        counter = SlidingWindowCounter(clock=_FakeClock())
        counter.record("a")
        counter.record("b")

        counter.reset_all()

        assert counter.keys() == []

    def test_try_acquire_admits_exactly_the_limit_under_concurrency(self):
        """The atomic check+append admits no more than max_events under a race."""
        counter = SlidingWindowCounter(clock=_FakeClock())  # frozen -> all in window
        limit = 50
        thread_count = 200
        results: list[bool] = []
        results_lock = threading.Lock()

        def worker() -> None:
            allowed, _ = counter.try_acquire("k", limit, window_seconds=60.0)
            with results_lock:
                results.append(allowed)

        threads = [threading.Thread(target=worker) for _ in range(thread_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results) == limit  # no over-admission
        assert counter.count("k", window_seconds=60.0) == limit


# =============================================================================
# TokenBucket — behavior
# =============================================================================


class TestTokenBucketBehavior:
    """Consumption, time-based refill, capacity, rate control, and clock."""

    def test_consume_depletes_available_tokens(self):
        """consume succeeds while tokens remain and fails once empty."""
        clock = _FakeClock()
        bucket = TokenBucket(rate=10.0, capacity=5.0, clock=clock)

        assert bucket.consume(1) is True
        assert bucket.consume(4) is True  # 5 total consumed
        assert bucket.consume(1) is False  # empty, no elapsed time to refill

    def test_tokens_refill_in_proportion_to_elapsed_time(self):
        """Refill == elapsed * rate between consume calls."""
        clock = _FakeClock(start=1000.0)
        bucket = TokenBucket(rate=2.0, capacity=10.0, clock=clock)

        assert bucket.consume(10) is True  # drain
        assert bucket.consume(1) is False

        clock.advance(3.0)  # 3s * 2 tokens/s == 6 tokens
        assert bucket.consume(6) is True
        assert bucket.consume(1) is False

    def test_refill_is_capped_at_capacity(self):
        """Idle time cannot accumulate tokens beyond capacity."""
        clock = _FakeClock(start=1000.0)
        bucket = TokenBucket(rate=5.0, capacity=10.0, clock=clock)

        bucket.consume(10)  # drain
        clock.advance(100.0)  # would refill 500, capped at 10

        assert bucket.consume(10) is True
        assert bucket.consume(1) is False

    def test_capacity_defaults_to_rate_when_omitted(self):
        """Omitting capacity makes it equal to rate."""
        bucket = TokenBucket(rate=7.0, clock=_FakeClock())

        assert bucket.consume(7) is True
        assert bucket.consume(1) is False

    def test_set_rate_changes_the_refill_rate(self):
        """set_rate/get_rate update the refill applied on the next consume."""
        clock = _FakeClock(start=1000.0)
        bucket = TokenBucket(rate=1.0, capacity=10.0, clock=clock)

        assert bucket.get_rate() == 1.0
        bucket.set_rate(5.0)
        assert bucket.get_rate() == 5.0

        bucket.consume(10)  # drain
        clock.advance(1.0)  # 1s * new rate 5 == 5 tokens
        assert bucket.consume(5) is True
        assert bucket.consume(1) is False

    def test_get_token_ratio_reflects_refill_without_consuming_it(self):
        """get_token_ratio is read-only: it projects refill but does not advance
        _last_update, so repeated reads at the same time are stable."""
        clock = _FakeClock(start=1000.0)
        bucket = TokenBucket(rate=2.0, capacity=10.0, clock=clock)

        assert bucket.get_token_ratio() == pytest.approx(1.0)  # full
        bucket.consume(5)
        assert bucket.get_token_ratio() == pytest.approx(0.5)

        clock.advance(1.0)  # projects +2 tokens -> (5 + 2) / 10
        assert bucket.get_token_ratio() == pytest.approx(0.7)
        assert bucket.get_token_ratio() == pytest.approx(0.7)  # stable, not consumed

    def test_wait_for_token_returns_true_when_a_token_is_available(self):
        """A non-empty bucket grants immediately without waiting."""
        bucket = TokenBucket(rate=1.0, capacity=5.0, clock=_FakeClock(start=1000.0))

        assert bucket.wait_for_token(timeout=1.0) is True

    def test_wait_for_token_returns_false_after_the_timeout(self):
        """An always-empty bucket exhausts the timeout and returns False.

        rate 0 never refills; the auto-advancing clock makes the loop terminate
        and time.sleep is patched so no real wall-clock delay is incurred.
        """
        bucket = TokenBucket(rate=0.0, capacity=0.0, clock=_AutoAdvanceClock())

        with patch("time.sleep") as mock_sleep:
            assert bucket.wait_for_token(timeout=1.0) is False

        assert mock_sleep.called  # the wait loop actually ran

    def test_default_clock_resolves_time_time_and_is_patchable(self):
        """TokenBucket's default clock resolves time.time() per call (patchable),
        matching the historical inlined refill/measurement behavior."""
        with patch("time.time", return_value=1000.0):
            bucket = TokenBucket(rate=2.0, capacity=10.0)
            assert bucket.consume(10) is True  # drain at t=1000

        with patch("time.time", return_value=1003.0):
            assert bucket.consume(6) is True  # 3s * 2 == 6 refilled
            assert bucket.consume(1) is False
