"""
Unit tests for domain state store InMemory implementations (367).

Tests InMemoryConfigHistoryStore, InMemoryCanaryRolloutStore,
InMemoryChaosExperimentStore, InMemoryCrossClusterStore.

Verification techniques:
- Contract: ABC conformance, CRUD roundtrip, boundary values
- Thread safety: concurrent operations
- Time dependency: TTL expiration via mock_time
- State transition: config lock lifecycle
"""

from __future__ import annotations

import threading
from datetime import timedelta
from unittest.mock import patch

import pytest

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def config_store():
    from baldur.adapters.memory.config_history import InMemoryConfigHistoryStore

    return InMemoryConfigHistoryStore()


@pytest.fixture
def rollout_store():
    from baldur.adapters.memory.canary_rollout import InMemoryCanaryRolloutStore

    return InMemoryCanaryRolloutStore()


@pytest.fixture
def chaos_store():
    from baldur.adapters.memory.chaos_experiment import (
        InMemoryChaosExperimentStore,
    )

    return InMemoryChaosExperimentStore()


@pytest.fixture
def cluster_store():
    from baldur.adapters.memory.cross_cluster import InMemoryCrossClusterStore

    return InMemoryCrossClusterStore()


# =============================================================================
# ConfigHistoryStore
# =============================================================================


class TestInMemoryConfigHistoryStoreContract:
    """Contract tests for InMemoryConfigHistoryStore."""

    def test_implements_abc(self):
        """InMemoryConfigHistoryStore implements ConfigHistoryStore ABC."""
        from baldur.adapters.memory.config_history import (
            InMemoryConfigHistoryStore,
        )
        from baldur.interfaces.config_history_store import ConfigHistoryStore

        assert issubclass(InMemoryConfigHistoryStore, ConfigHistoryStore)

    def test_next_version_starts_from_one(self, config_store):
        """First call to next_version returns 1."""
        assert config_store.next_version("circuit_breaker") == 1

    def test_next_version_increments_monotonically(self, config_store):
        """Successive calls to next_version increment by 1."""
        v1 = config_store.next_version("dlq")
        v2 = config_store.next_version("dlq")
        v3 = config_store.next_version("dlq")

        assert v1 == 1
        assert v2 == 2
        assert v3 == 3

    def test_next_version_independent_per_config_type(self, config_store):
        """Different config types have independent version counters."""
        config_store.next_version("circuit_breaker")
        config_store.next_version("circuit_breaker")

        assert config_store.next_version("dlq") == 1
        assert config_store.next_version("circuit_breaker") == 3

    def test_save_version_and_get_current_roundtrip(self, config_store):
        """save_version updates current pointer, retrievable via get_current."""
        data = {"version": 1, "values": {"threshold": 5}}
        config_store.save_version("circuit_breaker", data, max_entries=10)

        current = config_store.get_current("circuit_breaker")
        assert current == data

    def test_save_version_and_get_history_roundtrip(self, config_store):
        """save_version prepends to history, retrievable via get_history."""
        data_v1 = {"version": 1}
        data_v2 = {"version": 2}

        config_store.save_version("cb", data_v1, max_entries=10)
        config_store.save_version("cb", data_v2, max_entries=10)

        history = config_store.get_history("cb", limit=10)
        assert len(history) == 2
        assert history[0] == data_v2  # newest first
        assert history[1] == data_v1

    def test_save_version_trims_to_max_entries(self, config_store):
        """History is trimmed to max_entries (boundary analysis)."""
        for i in range(5):
            config_store.save_version("cb", {"v": i}, max_entries=3)

        assert config_store.get_version_count("cb") == 3
        history = config_store.get_history("cb", limit=10)
        # Should contain versions 4, 3, 2 (newest first, oldest trimmed)
        assert [h["v"] for h in history] == [4, 3, 2]

    def test_get_history_limit_respected(self, config_store):
        """get_history returns at most 'limit' entries."""
        for i in range(5):
            config_store.save_version("cb", {"v": i}, max_entries=10)

        history = config_store.get_history("cb", limit=2)
        assert len(history) == 2

    def test_get_current_returns_none_for_empty(self, config_store):
        """get_current returns None when no versions exist."""
        assert config_store.get_current("nonexistent") is None

    def test_get_history_returns_empty_for_missing_type(self, config_store):
        """get_history returns empty list for unknown config_type."""
        assert config_store.get_history("nonexistent", limit=10) == []

    def test_get_version_count_returns_zero_for_empty(self, config_store):
        """get_version_count returns 0 for unknown config_type."""
        assert config_store.get_version_count("nonexistent") == 0

    def test_clear_removes_all_state(self, config_store):
        """clear removes versions, history, and current for a config type."""
        config_store.next_version("cb")
        config_store.save_version("cb", {"v": 1}, max_entries=10)

        config_store.clear("cb")

        assert config_store.get_current("cb") is None
        assert config_store.get_history("cb", limit=10) == []
        assert config_store.get_version_count("cb") == 0
        # Version counter also reset
        assert config_store.next_version("cb") == 1

    def test_clear_does_not_affect_other_config_types(self, config_store):
        """clear is scoped to a single config type."""
        config_store.save_version("cb", {"v": 1}, max_entries=10)
        config_store.save_version("dlq", {"v": 1}, max_entries=10)

        config_store.clear("cb")

        assert config_store.get_current("cb") is None
        assert config_store.get_current("dlq") == {"v": 1}


class TestInMemoryConfigHistoryStoreBehavior:
    """Behavior tests for thread safety and concurrency."""

    def test_concurrent_next_version_no_duplicates(self, config_store):
        """Concurrent next_version calls produce unique monotonic values."""
        results = []
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            results.append(config_store.next_version("cb"))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 10
        assert len(set(results)) == 10  # all unique
        assert sorted(results) == list(range(1, 11))


# =============================================================================
# CanaryRolloutStore
# =============================================================================


class TestInMemoryCanaryRolloutStoreContract:
    """Contract tests for InMemoryCanaryRolloutStore."""

    def test_implements_abc(self):
        from baldur.adapters.memory.canary_rollout import (
            InMemoryCanaryRolloutStore,
        )
        from baldur.interfaces.canary_rollout_store import CanaryRolloutStore

        assert issubclass(InMemoryCanaryRolloutStore, CanaryRolloutStore)

    # -- Rollout CRUD ---------------------------------------------------------

    def test_save_and_get_rollout_roundtrip(self, rollout_store):
        """save_rollout + get_rollout returns saved data."""
        data = {"id": "r1", "state": "created"}
        rollout_store.save_rollout("r1", data, ttl_seconds=3600)

        assert rollout_store.get_rollout("r1") == data

    def test_get_rollout_returns_none_for_missing(self, rollout_store):
        assert rollout_store.get_rollout("nonexistent") is None

    def test_rollout_ttl_expiration(self, rollout_store):
        """Expired rollout returns None."""
        data = {"id": "r1"}
        rollout_store.save_rollout("r1", data, ttl_seconds=100)

        # Simulate time passing beyond TTL
        future = rollout_store._rollouts["r1"][1] + 1
        with patch("baldur.adapters.memory.canary_rollout.time") as mock_time:
            mock_time.time.return_value = future
            assert rollout_store.get_rollout("r1") is None

    def test_save_rollout_overwrites_existing(self, rollout_store):
        """Saving with same ID overwrites data."""
        rollout_store.save_rollout("r1", {"v": 1}, ttl_seconds=3600)
        rollout_store.save_rollout("r1", {"v": 2}, ttl_seconds=3600)

        assert rollout_store.get_rollout("r1") == {"v": 2}

    # -- Active set -----------------------------------------------------------

    def test_active_set_add_and_get(self, rollout_store):
        """add_active adds to active set, get_active_ids returns them."""
        rollout_store.add_active("r1")
        rollout_store.add_active("r2")

        assert rollout_store.get_active_ids() == {"r1", "r2"}

    def test_active_set_remove(self, rollout_store):
        """remove_active removes from active set."""
        rollout_store.add_active("r1")
        rollout_store.add_active("r2")
        rollout_store.remove_active("r1")

        assert rollout_store.get_active_ids() == {"r2"}

    def test_active_set_remove_nonexistent_is_noop(self, rollout_store):
        """Removing nonexistent ID from active set does not raise."""
        rollout_store.remove_active("nonexistent")  # should not raise
        assert rollout_store.get_active_ids() == set()

    def test_active_set_returns_copy(self, rollout_store):
        """get_active_ids returns a copy, not the internal set."""
        rollout_store.add_active("r1")
        ids = rollout_store.get_active_ids()
        ids.add("hacker")

        assert "hacker" not in rollout_store.get_active_ids()

    # -- Config lock ----------------------------------------------------------

    def test_acquire_lock_succeeds_when_free(self, rollout_store):
        assert rollout_store.acquire_config_lock("cb", "r1") is True

    def test_acquire_lock_fails_when_held(self, rollout_store):
        """Second acquire for same config_type fails."""
        rollout_store.acquire_config_lock("cb", "r1")
        assert rollout_store.acquire_config_lock("cb", "r2") is False

    def test_release_lock_by_owner_succeeds(self, rollout_store):
        rollout_store.acquire_config_lock("cb", "r1")
        assert rollout_store.release_config_lock("cb", "r1") is True

    def test_release_lock_by_non_owner_fails(self, rollout_store):
        """Non-owner cannot release lock."""
        rollout_store.acquire_config_lock("cb", "r1")
        assert rollout_store.release_config_lock("cb", "r2") is False

    def test_release_lock_not_held_fails(self, rollout_store):
        assert rollout_store.release_config_lock("cb", "r1") is False

    def test_get_config_lock_owner(self, rollout_store):
        rollout_store.acquire_config_lock("cb", "r1")
        assert rollout_store.get_config_lock_owner("cb") == "r1"

    def test_get_config_lock_owner_none_when_free(self, rollout_store):
        assert rollout_store.get_config_lock_owner("cb") is None

    def test_is_config_locked(self, rollout_store):
        assert rollout_store.is_config_locked("cb") is False
        rollout_store.acquire_config_lock("cb", "r1")
        assert rollout_store.is_config_locked("cb") is True

    def test_force_release_config_lock(self, rollout_store):
        """force_release removes lock without owner check."""
        rollout_store.acquire_config_lock("cb", "r1")
        assert rollout_store.force_release_config_lock("cb") is True
        assert rollout_store.is_config_locked("cb") is False

    def test_force_release_returns_false_when_not_locked(self, rollout_store):
        assert rollout_store.force_release_config_lock("cb") is False

    def test_extend_config_lock_by_owner(self, rollout_store):
        rollout_store.acquire_config_lock("cb", "r1")
        assert rollout_store.extend_config_lock("cb", "r1") is True

    def test_extend_config_lock_by_non_owner_fails(self, rollout_store):
        rollout_store.acquire_config_lock("cb", "r1")
        assert rollout_store.extend_config_lock("cb", "r2") is False

    def test_extend_config_lock_not_held_fails(self, rollout_store):
        assert rollout_store.extend_config_lock("cb", "r1") is False

    def test_lock_acquire_after_release_succeeds(self, rollout_store):
        """Lock can be re-acquired after release (state transition)."""
        rollout_store.acquire_config_lock("cb", "r1")
        rollout_store.release_config_lock("cb", "r1")
        assert rollout_store.acquire_config_lock("cb", "r2") is True
        assert rollout_store.get_config_lock_owner("cb") == "r2"

    def test_lock_expired_allows_reacquire(self, rollout_store):
        """Expired lock allows new acquisition."""
        rollout_store.acquire_config_lock("cb", "r1", timeout=timedelta(seconds=1))

        # Simulate time past expiration
        _, expires_at = rollout_store._config_locks["cb"]
        future = expires_at + 1
        with patch("baldur.adapters.memory.canary_rollout.time") as mock_time:
            mock_time.time.return_value = future
            assert rollout_store.acquire_config_lock("cb", "r2") is True

    def test_acquire_lock_with_custom_timeout(self, rollout_store):
        """Custom timeout is respected."""
        rollout_store.acquire_config_lock("cb", "r1", timeout=timedelta(seconds=60))

        _, expires_at = rollout_store._config_locks["cb"]
        # Verify TTL is approximately 60 seconds (not default 30 minutes)
        import time as _time

        remaining = expires_at - _time.time()
        assert 50 < remaining < 70


class TestExtendResetFromNow:
    """extend_config_lock resets the deadline to now + additional_time (Redis
    PEXPIRE parity, 623 D7), not an increment to the existing deadline — so
    repeated renewals do not accumulate and the crash-freeze valve is kept."""

    def test_extend_config_lock_resets_deadline_from_now(self, rollout_store):
        import time as _time

        # Given: a lock with a long (100s) remaining TTL.
        rollout_store.acquire_config_lock("cb", "r1", timeout=timedelta(seconds=100))
        _, before = rollout_store._config_locks["cb"]

        # When: extended by a SHORTER additional_time (30s).
        assert rollout_store.extend_config_lock("cb", "r1", timedelta(seconds=30))

        # Then: the deadline is reset to ~now + 30s (NOT 100 + 30 = 130s) — so
        # it moves EARLIER, proving non-accumulating reset-from-now semantics.
        _, after = rollout_store._config_locks["cb"]
        remaining = after - _time.time()
        assert 20 < remaining < 40
        assert after < before

    def test_repeated_extend_does_not_drift_deadline_forward(self, rollout_store):
        import time as _time

        # Given: a lock extended several times with the same short TTL.
        rollout_store.acquire_config_lock("cb", "r1", timeout=timedelta(seconds=30))
        for _ in range(5):
            assert rollout_store.extend_config_lock("cb", "r1", timedelta(seconds=30))

        # Then: the deadline stays anchored to ~now + 30s, not 30 * 6 = 180s.
        _, after = rollout_store._config_locks["cb"]
        remaining = after - _time.time()
        assert 20 < remaining < 40


# =============================================================================
# ChaosExperimentStore
# =============================================================================


class TestInMemoryChaosExperimentStoreContract:
    """Contract tests for InMemoryChaosExperimentStore."""

    def test_implements_abc(self):
        from baldur.adapters.memory.chaos_experiment import (
            InMemoryChaosExperimentStore,
        )
        from baldur.interfaces.chaos_experiment_store import ChaosExperimentStore

        assert issubclass(InMemoryChaosExperimentStore, ChaosExperimentStore)

    def test_save_and_get_roundtrip(self, chaos_store):
        data = {"id": "exp-1", "status": "active"}
        chaos_store.save("exp-1", data, ttl_seconds=3600)

        assert chaos_store.get("exp-1") == data

    def test_get_returns_none_for_missing(self, chaos_store):
        assert chaos_store.get("nonexistent") is None

    def test_delete_removes_experiment(self, chaos_store):
        chaos_store.save("exp-1", {"id": "exp-1"}, ttl_seconds=3600)
        chaos_store.delete("exp-1")

        assert chaos_store.get("exp-1") is None

    def test_delete_nonexistent_is_noop(self, chaos_store):
        """Deleting nonexistent experiment does not raise."""
        chaos_store.delete("nonexistent")  # should not raise

    def test_find_active_filters_by_status(self, chaos_store):
        """find_active returns only experiments with status=='active'."""
        chaos_store.save("exp-1", {"id": "1", "status": "active"}, ttl_seconds=3600)
        chaos_store.save("exp-2", {"id": "2", "status": "completed"}, ttl_seconds=3600)
        chaos_store.save("exp-3", {"id": "3", "status": "active"}, ttl_seconds=3600)

        active = chaos_store.find_active()
        assert len(active) == 2
        ids = {e["id"] for e in active}
        assert ids == {"1", "3"}

    def test_find_active_excludes_expired(self, chaos_store):
        """find_active skips expired experiments even if status is active."""
        chaos_store.save("exp-1", {"id": "1", "status": "active"}, ttl_seconds=100)

        future = chaos_store._experiments["exp-1"][1] + 1
        with patch("baldur.adapters.memory.chaos_experiment.time") as mock_time:
            mock_time.time.return_value = future
            assert chaos_store.find_active() == []

    def test_find_active_lazy_cleanup(self, chaos_store):
        """find_active removes expired entries (lazy cleanup)."""
        chaos_store.save("exp-1", {"id": "1", "status": "active"}, ttl_seconds=100)

        future = chaos_store._experiments["exp-1"][1] + 1
        with patch("baldur.adapters.memory.chaos_experiment.time") as mock_time:
            mock_time.time.return_value = future
            chaos_store.find_active()

        # After lazy cleanup, internal storage should be empty
        assert "exp-1" not in chaos_store._experiments

    def test_get_expired_returns_none_and_cleans_up(self, chaos_store):
        """get() on expired experiment returns None and removes entry."""
        chaos_store.save("exp-1", {"id": "1"}, ttl_seconds=100)

        future = chaos_store._experiments["exp-1"][1] + 1
        with patch("baldur.adapters.memory.chaos_experiment.time") as mock_time:
            mock_time.time.return_value = future
            assert chaos_store.get("exp-1") is None

        assert "exp-1" not in chaos_store._experiments

    def test_find_active_returns_empty_when_no_experiments(self, chaos_store):
        assert chaos_store.find_active() == []


# =============================================================================
# CrossClusterStore
# =============================================================================


class TestInMemoryCrossClusterStoreContract:
    """Contract tests for InMemoryCrossClusterStore."""

    def test_implements_abc(self):
        from baldur.adapters.memory.cross_cluster import InMemoryCrossClusterStore
        from baldur.interfaces.cross_cluster_store import CrossClusterStore

        assert issubclass(InMemoryCrossClusterStore, CrossClusterStore)

    # -- Propagation requests -------------------------------------------------

    def test_save_and_get_request_roundtrip(self, cluster_store):
        data = {"request_id": "req-1", "source": "cluster-a"}
        cluster_store.save_request("req-1", data, ttl_seconds=3600)

        assert cluster_store.get_request("req-1") == data

    def test_get_request_returns_none_for_missing(self, cluster_store):
        assert cluster_store.get_request("nonexistent") is None

    def test_request_ttl_expiration(self, cluster_store):
        """Expired request returns None."""
        cluster_store.save_request("req-1", {"id": "req-1"}, ttl_seconds=100)

        future = cluster_store._requests["req-1"][1] + 1
        with patch("baldur.adapters.memory.cross_cluster.time") as mock_time:
            mock_time.time.return_value = future
            assert cluster_store.get_request("req-1") is None

    # -- Pending set ----------------------------------------------------------

    def test_add_and_remove_pending(self, cluster_store):
        cluster_store.add_pending("cluster-a", "req-1")
        cluster_store.add_pending("cluster-a", "req-2")

        assert cluster_store._pending["cluster-a"] == {"req-1", "req-2"}

        cluster_store.remove_pending("cluster-a", "req-1")
        assert cluster_store._pending["cluster-a"] == {"req-2"}

    def test_remove_pending_nonexistent_is_noop(self, cluster_store):
        """Removing from nonexistent cluster does not raise."""
        cluster_store.remove_pending("cluster-x", "req-1")  # should not raise

    def test_pending_sets_independent_per_cluster(self, cluster_store):
        """Each cluster has its own pending set."""
        cluster_store.add_pending("cluster-a", "req-1")
        cluster_store.add_pending("cluster-b", "req-2")

        assert cluster_store._pending["cluster-a"] == {"req-1"}
        assert cluster_store._pending["cluster-b"] == {"req-2"}

    # -- Governance policies --------------------------------------------------

    def test_save_and_get_policy_roundtrip(self, cluster_store):
        data = {"policy_id": "cb-limits", "rules": [{"field": "threshold", "max": 10}]}
        cluster_store.save_policy("circuit_breaker", data)

        assert cluster_store.get_policy("circuit_breaker") == data

    def test_get_policy_returns_none_for_missing(self, cluster_store):
        assert cluster_store.get_policy("nonexistent") is None

    def test_save_policy_overwrites_existing(self, cluster_store):
        cluster_store.save_policy("cb", {"v": 1})
        cluster_store.save_policy("cb", {"v": 2})

        assert cluster_store.get_policy("cb") == {"v": 2}
