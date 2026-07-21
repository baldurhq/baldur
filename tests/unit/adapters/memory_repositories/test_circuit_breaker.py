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


class TestRecordCounterBehavior:
    """719 D3 — record_failure / record_success keep plain cumulative counters.

    The ring-buffer window that used to derive these counts moved to the
    circuit breaker service. What is left here must behave like the Redis and
    SQL repositories: increments accumulate, and a reset written through
    update_state sticks.
    """

    def test_record_failure_increments_cumulative_failure_count(self):
        repo = InMemoryCircuitBreakerStateRepository()

        for expected in range(1, 6):
            assert repo.record_failure("svc").failure_count == expected

    def test_record_success_increments_cumulative_success_count(self):
        repo = InMemoryCircuitBreakerStateRepository()

        for expected in range(1, 6):
            assert repo.record_success("svc").success_count == expected

    def test_record_failure_leaves_success_count_untouched(self):
        repo = InMemoryCircuitBreakerStateRepository()
        repo.record_success("svc")
        repo.record_success("svc")

        state = repo.record_failure("svc")

        assert state.failure_count == 1
        assert state.success_count == 2

    def test_update_state_reset_sticks_across_next_failure(self):
        """Backend reset parity: the reset is not undone by the next failure.

        Pre-719 the window recomputed the count from its ring, so a
        service-issued update_state(failure_count=0) was silently resurrected.
        """
        repo = InMemoryCircuitBreakerStateRepository()
        repo.record_failure("svc")
        repo.record_failure("svc")
        repo.record_failure("svc")

        repo.update_state(service_name="svc", state="closed", failure_count=0)

        assert repo.record_failure("svc").failure_count == 1

    def test_counts_are_isolated_per_service_name(self):
        repo = InMemoryCircuitBreakerStateRepository()
        repo.record_failure("a")
        repo.record_failure("a")
        repo.record_failure("b")

        assert repo.get_by_service_name("a").failure_count == 2
        assert repo.get_by_service_name("b").failure_count == 1


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


class TestRecordCounterThreadSafety:
    """719 D3 — concurrent record calls lose no counter updates.

    The counters are a read-modify-write under the repository RLock, so total
    recorded calls must equal the number of calls made.
    """

    @pytest.mark.parametrize("n_threads", [10, 50])
    def test_concurrent_record_calls_lose_no_updates(self, n_threads):
        repo = InMemoryCircuitBreakerStateRepository()
        ops_per_thread = 100

        def worker(seed: int) -> None:
            rng = random.Random(seed)
            for _ in range(ops_per_thread):
                if rng.random() < 0.5:
                    repo.record_success("svc")
                else:
                    repo.record_failure("svc")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        state = repo.get_by_service_name("svc")
        assert state.failure_count + state.success_count == n_threads * ops_per_thread


class TestResetSiteCounterBehavior:
    """719 D3 — every reset-site path zeroes the DTO counters it advertises."""

    def _seed(self, repo: InMemoryCircuitBreakerStateRepository, name: str) -> None:
        for _ in range(3):
            repo.record_failure(name)
        for _ in range(2):
            repo.record_success(name)

    def test_reset_counts_zeroes_both_counters(self):
        repo = InMemoryCircuitBreakerStateRepository()
        self._seed(repo, "svc")

        assert repo.reset_counts("svc") is True

        state = repo.get_by_service_name("svc")
        assert state.failure_count == 0
        assert state.success_count == 0

    def test_reset_zeroes_both_counters_and_closes(self):
        repo = InMemoryCircuitBreakerStateRepository()
        self._seed(repo, "svc")

        assert repo.reset("svc") is True

        state = repo.get_by_service_name("svc")
        assert state.failure_count == 0
        assert state.success_count == 0
        assert state.state == CircuitBreakerStateEnum.CLOSED.value

    def test_atomic_force_close_zeroes_both_counters(self):
        repo = InMemoryCircuitBreakerStateRepository()
        self._seed(repo, "svc")

        repo.atomic_force_close("svc", reason="recovered")

        state = repo.get_by_service_name("svc")
        assert state.failure_count == 0
        assert state.success_count == 0

    def test_atomic_reset_zeroes_both_counters(self):
        repo = InMemoryCircuitBreakerStateRepository()
        self._seed(repo, "svc")

        repo.atomic_reset("svc", reason="operator reset")

        state = repo.get_by_service_name("svc")
        assert state.failure_count == 0
        assert state.success_count == 0

    def test_clear_manual_control_preserves_counters(self):
        """Only the manual-control flag is cleared; counters are the service's."""
        repo = InMemoryCircuitBreakerStateRepository()
        self._seed(repo, "svc")
        repo.set_manual_control("svc", CircuitBreakerStateEnum.OPEN.value)

        assert repo.clear_manual_control("svc") is True

        state = repo.get_by_service_name("svc")
        assert state.manually_controlled is False
        assert state.failure_count == 3
        assert state.success_count == 2

    def test_open_to_half_open_transition_zeroes_success_count(self):
        repo = InMemoryCircuitBreakerStateRepository()
        self._seed(repo, "svc")
        repo.update_state(service_name="svc", state=CircuitBreakerStateEnum.OPEN.value)

        acquired, previous, new = repo.try_acquire_half_open_slot(
            "svc", limit=3, stuck_timeout_seconds=60
        )

        assert acquired is True
        assert previous == CircuitBreakerStateEnum.OPEN.value
        assert new == CircuitBreakerStateEnum.HALF_OPEN.value
        assert repo.get_by_service_name("svc").success_count == 0

    def test_delete_removes_the_entry(self):
        repo = InMemoryCircuitBreakerStateRepository()
        self._seed(repo, "svc")

        assert repo.delete("svc") is True
        assert repo.get_by_service_name("svc") is None

    def test_clear_removes_every_entry(self):
        repo = InMemoryCircuitBreakerStateRepository()
        self._seed(repo, "a")
        self._seed(repo, "b")

        repo.clear()

        assert repo.get_all_states() == []
