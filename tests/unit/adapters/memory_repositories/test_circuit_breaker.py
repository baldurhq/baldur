"""
InMemoryCircuitBreakerStateRepository 테스트.
"""

import random
import threading
from datetime import UTC, datetime, timedelta

import pytest

from baldur.adapters.memory import InMemoryCircuitBreakerStateRepository
from baldur.interfaces.repositories import CircuitBreakerStateEnum


class _CountingRLock:
    """Acquire-counting RLock wrapper for the unlocked-helper contract test.

    _thread.RLock is C-implemented and exposes read-only method attributes,
    so unittest.mock.patch.object cannot wrap acquire(). We swap the whole
    lock with this delegator instead.
    """

    def __init__(self) -> None:
        self._inner = threading.RLock()
        self.acquire_calls = 0

    def acquire(self, *args, **kwargs):
        self.acquire_calls += 1
        return self._inner.acquire(*args, **kwargs)

    def release(self):
        return self._inner.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


def _baseline_sum_failures(
    repo: InMemoryCircuitBreakerStateRepository, name: str
) -> int:
    """Reference O(W) sum-based failure count (pre-490 implementation)."""
    window = repo._call_windows.get(name)
    if window is None:
        return 0
    return sum(1 for v in window if not v)


def _baseline_sum_successes(
    repo: InMemoryCircuitBreakerStateRepository, name: str
) -> int:
    """Reference O(W) sum-based success count (pre-490 implementation)."""
    window = repo._call_windows.get(name)
    if window is None:
        return 0
    return sum(1 for v in window if v)


class TestInMemoryCircuitBreakerStateRepository:
    """Tests for InMemoryCircuitBreakerStateRepository."""

    @pytest.fixture
    def repo(self):
        """Create a fresh repository for each test."""

        return InMemoryCircuitBreakerStateRepository()

    def test_get_or_create_new(self, repo):
        """Test creating a new circuit breaker state."""

        state = repo.get_or_create("toss_payment")

        assert state.id == 1
        assert state.service_name == "toss_payment"
        assert state.state == CircuitBreakerStateEnum.CLOSED.value
        assert state.failure_count == 0
        assert state.success_count == 0
        assert state.created_at is not None

    def test_get_or_create_existing(self, repo):
        """Test retrieving an existing circuit breaker state."""
        first = repo.get_or_create("toss_payment")
        second = repo.get_or_create("toss_payment")

        assert first.id == second.id
        assert first.service_name == second.service_name

    def test_get_by_service_name(self, repo):
        """Test getting state by service name."""
        repo.get_or_create("test_service")

        result = repo.get_by_service_name("test_service")
        assert result is not None
        assert result.service_name == "test_service"

        result = repo.get_by_service_name("non_existent")
        assert result is None

    def test_update_state(self, repo):
        """Test updating circuit breaker state."""

        repo.get_or_create("test_service")

        now = datetime.now(UTC)
        result = repo.update_state(
            service_name="test_service",
            state=CircuitBreakerStateEnum.OPEN.value,
            failure_count=5,
            opened_at=now,
        )

        assert result is True

        state = repo.get_by_service_name("test_service")
        assert state.state == CircuitBreakerStateEnum.OPEN.value
        assert state.failure_count == 5
        assert state.opened_at == now

    def test_increment_failure_count(self, repo):
        """Test incrementing failure count."""
        repo.get_or_create("test_service")

        new_count = repo.increment_failure_count("test_service")
        assert new_count == 1

        new_count = repo.increment_failure_count("test_service")
        assert new_count == 2

        state = repo.get_by_service_name("test_service")
        assert state.failure_count == 2
        assert state.last_failure_at is not None

    def test_reset_counts(self, repo):
        """Test resetting failure and success counts."""
        repo.get_or_create("test_service")
        repo.increment_failure_count("test_service")
        repo.increment_failure_count("test_service")

        result = repo.reset_counts("test_service")
        assert result is True

        state = repo.get_by_service_name("test_service")
        assert state.failure_count == 0
        assert state.success_count == 0

    def test_set_manual_control(self, repo):
        """Test setting manual control override."""

        repo.get_or_create("test_service")

        expires = datetime.now(UTC) + timedelta(hours=1)
        result = repo.set_manual_control(
            service_name="test_service",
            state=CircuitBreakerStateEnum.OPEN.value,
            controlled_by_id=42,
            reason="Manual intervention during maintenance",
            expires_at=expires,
        )

        assert result is True

        state = repo.get_by_service_name("test_service")
        assert state.state == CircuitBreakerStateEnum.OPEN.value
        assert state.manually_controlled is True
        assert state.controlled_by_id == 42
        assert state.control_reason == "Manual intervention during maintenance"
        assert state.manual_override_expires_at == expires

    def test_clear_manual_control(self, repo):
        """clear_manual_control은 수동 제어 플래그만 해제하고 상태/카운터는 유지한다."""

        repo.get_or_create("test_service")
        repo.set_manual_control(
            service_name="test_service",
            state=CircuitBreakerStateEnum.OPEN.value,
            controlled_by_id=42,
            reason="Test",
        )

        result = repo.clear_manual_control("test_service")
        assert result is True

        state = repo.get_by_service_name("test_service")
        # 상태는 set_manual_control에서 설정한 OPEN이 유지된다
        assert state.state == CircuitBreakerStateEnum.OPEN.value
        assert state.manually_controlled is False
        assert state.controlled_by_id is None

    def test_thread_safety(self, repo):
        """Test thread safety with concurrent increments."""
        repo.get_or_create("test_service")

        def increment():
            for _ in range(100):
                repo.increment_failure_count("test_service")

        threads = []
        for _ in range(5):
            t = threading.Thread(target=increment)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        state = repo.get_by_service_name("test_service")
        assert state.failure_count == 500


# =============================================================================
# 490 D1+D2+D3+D6 — Incremental counter / unlocked helper / reset symmetry
# =============================================================================


class TestRecordIncrementalCounterBehavior:
    """490 D1/D3 — record_success / record_failure produce identical counts vs.
    the pre-fix O(W) sum() reference, including across deque eviction.
    """

    @pytest.mark.parametrize("window_size", [10, 100, 500])
    def test_record_failure_count_matches_sum_reference_below_maxlen(self, window_size):
        # Given: a fresh repo with the parametrized window size.
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=window_size)

        # When: fewer record_failure() calls than the window can hold.
        n = window_size // 2
        for _ in range(n):
            repo.record_failure("svc")

        # Then: incremental counter matches the O(W) sum() reference.
        assert repo._failure_cnt["svc"] == _baseline_sum_failures(repo, "svc")
        assert repo._success_cnt["svc"] == _baseline_sum_successes(repo, "svc")
        assert repo._failure_cnt["svc"] == n
        assert len(repo._call_windows["svc"]) == n

    @pytest.mark.parametrize("window_size", [10, 100, 500])
    def test_record_failure_count_matches_sum_reference_at_maxlen(self, window_size):
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=window_size)

        # When: exactly window-size record_failure() calls (window now full).
        for _ in range(window_size):
            repo.record_failure("svc")

        # Then: window is full and counter still matches sum reference.
        assert len(repo._call_windows["svc"]) == window_size
        assert repo._failure_cnt["svc"] == _baseline_sum_failures(repo, "svc")
        assert repo._failure_cnt["svc"] == window_size

    @pytest.mark.parametrize("window_size", [10, 100, 500])
    def test_record_failure_count_matches_sum_reference_past_maxlen(self, window_size):
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=window_size)

        # When: 2x window-size mixed failure→success calls (forces eviction
        # of False slots by True appends — counter MUST decrement on evict).
        for _ in range(window_size):
            repo.record_failure("svc")
        for _ in range(window_size):
            repo.record_success("svc")

        # Then: window saturated at maxlen, only successes remain in window.
        assert len(repo._call_windows["svc"]) == window_size
        assert repo._failure_cnt["svc"] == _baseline_sum_failures(repo, "svc")
        assert repo._success_cnt["svc"] == _baseline_sum_successes(repo, "svc")
        assert repo._failure_cnt["svc"] == 0
        assert repo._success_cnt["svc"] == window_size

    @pytest.mark.parametrize("window_size", [10, 100, 500])
    def test_record_mixed_randomized_trace_matches_sum_reference(self, window_size):
        import random as _random

        rng = _random.Random(42)  # deterministic
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=window_size)

        # When: 4× window-size random success/failure calls — eviction fires
        # heavily; both counter directions must stay in lockstep with sum().
        for _ in range(window_size * 4):
            if rng.random() < 0.5:
                repo.record_success("svc")
            else:
                repo.record_failure("svc")

        # Then: invariant holds — counters == window-derived sums, sum equals len.
        assert repo._failure_cnt["svc"] == _baseline_sum_failures(repo, "svc")
        assert repo._success_cnt["svc"] == _baseline_sum_successes(repo, "svc")
        assert repo._success_cnt["svc"] + repo._failure_cnt["svc"] == len(
            repo._call_windows["svc"]
        )

    def test_record_success_returns_state_with_window_derived_counts(self):
        # Given: a repo where one failure has already landed.
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=10)
        repo.record_failure("svc")

        # When: we record a success.
        state = repo.record_success("svc")

        # Then: returned DTO carries the window-derived counts (1 of each).
        assert state.failure_count == 1
        assert state.success_count == 1
        assert state.failure_count == repo._failure_cnt["svc"]
        assert state.success_count == repo._success_cnt["svc"]


class TestGetOrCreateUnlockedContract:
    """490 D2 — _get_or_create_unlocked is the lock-free body extracted from
    get_or_create. Public get_or_create must remain a thin lock-acquire
    wrapper around it.
    """

    def test_get_or_create_unlocked_returns_same_object_as_public_method(self):
        # Given: a fresh repo.
        repo = InMemoryCircuitBreakerStateRepository()

        # When: we create via the unlocked helper, then re-fetch via public API.
        with repo._lock:
            unlocked_state = repo._get_or_create_unlocked("svc")
        public_state = repo.get_or_create("svc")

        # Then: same identity (cached in _storage), same shape contract.
        assert unlocked_state is public_state
        assert unlocked_state.service_name == "svc"
        assert unlocked_state.state == CircuitBreakerStateEnum.CLOSED.value
        assert unlocked_state.failure_count == 0
        assert unlocked_state.success_count == 0

    def test_get_or_create_unlocked_does_not_acquire_lock_internally(self):
        # Given: a repo whose _lock is swapped for an acquire-counting wrapper.
        # _thread.RLock is C-implemented (read-only attrs), so we can't patch
        # its acquire() in place — replacement is the only way to spy.
        repo = InMemoryCircuitBreakerStateRepository()
        counting = _CountingRLock()
        repo._lock = counting

        # When: caller already holds the lock and invokes the unlocked helper.
        with counting:
            baseline = counting.acquire_calls
            repo._get_or_create_unlocked("svc")

        # Then: the unlocked helper added zero acquires on top of the caller.
        assert counting.acquire_calls == baseline

    def test_public_get_or_create_acquires_lock(self):
        repo = InMemoryCircuitBreakerStateRepository()
        counting = _CountingRLock()
        repo._lock = counting

        # When: the public wrapper is called from outside any lock context.
        repo.get_or_create("svc")

        # Then: it acquires the lock exactly once (no reentry from inside).
        assert counting.acquire_calls == 1


class TestRecordIncrementalCounterThreadSafety:
    """490 D3 — counter consistency under multi-thread contention.

    Test Assessment: parametrize n_threads ∈ {10, 50, 100} × ops_per_thread = 100.
    """

    @pytest.mark.parametrize("n_threads", [10, 50, 100])
    @pytest.mark.parametrize("window_size", [10, 100, 500])
    def test_concurrent_record_calls_preserve_counter_invariant(
        self, n_threads, window_size
    ):
        # Given: a repo with the parametrized sliding window size.
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=window_size)
        ops_per_thread = 100

        def worker(seed: int) -> None:
            rng = random.Random(seed)
            for _ in range(ops_per_thread):
                if rng.random() < 0.5:
                    repo.record_success("svc")
                else:
                    repo.record_failure("svc")

        # When: N concurrent workers contend for the same name.
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Then: incremental counters match the O(W) sum reference exactly,
        # AND together they equal len(window) (window-derived invariant).
        assert repo._failure_cnt["svc"] == _baseline_sum_failures(repo, "svc")
        assert repo._success_cnt["svc"] == _baseline_sum_successes(repo, "svc")
        assert repo._success_cnt["svc"] + repo._failure_cnt["svc"] == len(
            repo._call_windows["svc"]
        )
        # Also: window can never exceed maxlen — sanity boundary.
        assert len(repo._call_windows["svc"]) <= window_size


class TestD6ResetSymmetryBehavior:
    """490 D6 — every reset-site path zeroes _failure_cnt/_success_cnt AND
    clears the sliding window, preserving the invariant
    _success_cnt[n] + _failure_cnt[n] == len(_call_windows[n]).
    """

    def _seed_with_calls(
        self, repo: InMemoryCircuitBreakerStateRepository, name: str
    ) -> None:
        """Pre-load the window with mixed entries so the reset is observable."""
        for _ in range(3):
            repo.record_failure(name)
        for _ in range(2):
            repo.record_success(name)

    def _assert_reset_invariant(
        self, repo: InMemoryCircuitBreakerStateRepository, name: str
    ) -> None:
        """Counters zeroed, window emptied, dict keys retained."""
        assert repo._failure_cnt.get(name, 0) == 0
        assert repo._success_cnt.get(name, 0) == 0
        # Reset sites must keep the dict key (they preserve the CB entry).
        assert name in repo._call_windows
        assert len(repo._call_windows[name]) == 0
        # Window-derived invariant.
        assert repo._success_cnt[name] + repo._failure_cnt[name] == len(
            repo._call_windows[name]
        )

    def test_reset_counts_zeroes_counters_and_clears_window(self):
        repo = InMemoryCircuitBreakerStateRepository()
        self._seed_with_calls(repo, "svc")

        repo.reset_counts("svc")

        self._assert_reset_invariant(repo, "svc")

    def test_reset_counts_clears_opened_at(self):
        # 498 D9: reset_counts also clears opened_at so the rebuilt DTO does
        # not carry a stale OPEN-era timestamp into the CLOSED-branch
        # writeback driven by Layered.record_success_with_close_check.
        repo = InMemoryCircuitBreakerStateRepository()
        opened_at = datetime.now(UTC) - timedelta(minutes=15)
        repo.get_or_create("svc")
        repo.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
            opened_at=opened_at,
        )
        self._seed_with_calls(repo, "svc")
        # Pre-condition: opened_at is non-None.
        assert repo.get_by_service_name("svc").opened_at == opened_at

        repo.reset_counts("svc")

        state = repo.get_by_service_name("svc")
        assert state is not None
        assert state.opened_at is None

    def test_clear_manual_control_zeroes_counters_and_clears_window(self):
        repo = InMemoryCircuitBreakerStateRepository()
        repo.set_manual_control(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
            controlled_by_id=1,
            reason="test",
        )
        self._seed_with_calls(repo, "svc")

        repo.clear_manual_control("svc")

        self._assert_reset_invariant(repo, "svc")

    def test_reset_zeroes_counters_and_clears_window(self):
        repo = InMemoryCircuitBreakerStateRepository()
        self._seed_with_calls(repo, "svc")

        repo.reset("svc")

        self._assert_reset_invariant(repo, "svc")

    def test_atomic_force_close_zeroes_counters_and_clears_window(self):
        repo = InMemoryCircuitBreakerStateRepository()
        self._seed_with_calls(repo, "svc")

        ok, _, _ = repo.atomic_force_close("svc", reason="test", controlled_by_id=1)

        assert ok is True
        self._assert_reset_invariant(repo, "svc")

    def test_atomic_reset_zeroes_counters_and_clears_window(self):
        repo = InMemoryCircuitBreakerStateRepository()
        self._seed_with_calls(repo, "svc")

        ok, _, _ = repo.atomic_reset("svc", reason="test", controlled_by_id=1)

        assert ok is True
        self._assert_reset_invariant(repo, "svc")

    def test_try_acquire_half_open_open_to_half_open_clears_window(self):
        # Given: a service in OPEN state with prior failure window data.
        repo = InMemoryCircuitBreakerStateRepository()
        repo.get_or_create("svc")
        self._seed_with_calls(repo, "svc")
        repo.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
        )

        # When: OPEN→HALF_OPEN transition fires.
        ok, prev, new = repo.try_acquire_half_open_slot(
            "svc", limit=3, stuck_timeout_seconds=60
        )

        # Then: transition succeeded AND window/counters reset (DTO success_count=0).
        assert ok is True
        assert prev == CircuitBreakerStateEnum.OPEN.value
        assert new == CircuitBreakerStateEnum.HALF_OPEN.value
        self._assert_reset_invariant(repo, "svc")

    def test_try_acquire_half_open_stuck_recovery_clears_window(self):
        # Given: HALF_OPEN at the limit with a stuck (very old) window —
        # success_count must be reset to 0 by the stuck-recovery path.
        repo = InMemoryCircuitBreakerStateRepository()
        repo.get_or_create("svc")
        self._seed_with_calls(repo, "svc")
        old_ts = datetime.now(UTC) - timedelta(seconds=3600)
        repo.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
            half_open_request_count=3,
        )
        # Hand-set the half-open window start to an ancient time so the
        # stuck_timeout check fires. update_state preserves the existing
        # value, so we force-write through the storage dict.
        entry = repo._storage["svc"]
        from dataclasses import replace

        repo._storage["svc"] = replace(entry, half_open_window_started_at=old_ts)

        # When: a new acquire fires while limit is exceeded — stuck recovery.
        ok, prev, new = repo.try_acquire_half_open_slot(
            "svc", limit=3, stuck_timeout_seconds=10
        )

        # Then: recovery acquired the slot AND reset window/counters.
        assert ok is True
        assert repo._last_acquire_marker == "stuck_recovery"
        self._assert_reset_invariant(repo, "svc")


class TestD6TerminalSiteContract:
    """490 D6 (terminal) — delete() and clear() must remove dict keys entirely
    from _call_windows / _failure_cnt / _success_cnt — closes both the new
    counter-leak risk AND the pre-existing _call_windows empty-deque leak.
    """

    def test_delete_removes_name_from_all_parallel_dicts(self):
        # Given: a repo with one CB entry that has window/counter state.
        repo = InMemoryCircuitBreakerStateRepository()
        repo.record_failure("svc")
        repo.record_success("svc")
        assert "svc" in repo._call_windows
        assert "svc" in repo._failure_cnt
        assert "svc" in repo._success_cnt

        # When: delete().
        ok = repo.delete("svc")

        # Then: storage AND all parallel dicts have the key removed entirely.
        assert ok is True
        assert "svc" not in repo._storage
        assert "svc" not in repo._call_windows
        assert "svc" not in repo._failure_cnt
        assert "svc" not in repo._success_cnt

    def test_clear_empties_all_parallel_dicts(self):
        # Given: multiple CB entries with state.
        repo = InMemoryCircuitBreakerStateRepository()
        for n in ("a", "b", "c"):
            repo.record_failure(n)
            repo.record_success(n)

        # When: clear().
        repo.clear()

        # Then: all four dicts are empty.
        assert repo._storage == {}
        assert repo._call_windows == {}
        assert repo._failure_cnt == {}
        assert repo._success_cnt == {}
        # And the next ID counter resets to 1 per the existing test contract.
        assert repo._next_id == 1
