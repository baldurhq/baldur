"""
Mock-based integration tests for Store + Service composition (367).

Verifies end-to-end behavior of services with InMemory store implementations.
No Docker or external infra required.

Purpose: Validate that service → store → InMemory pipeline works correctly
Expected: Full lifecycle operations (save → query → rollback → clear) succeed
"""

from __future__ import annotations

import pytest

# =============================================================================
# ConfigHistoryService + InMemoryConfigHistoryStore
# =============================================================================


class TestConfigHistoryServiceIntegration:
    """Integration: ConfigHistoryService with InMemoryConfigHistoryStore."""

    @pytest.fixture
    def service(self):
        from baldur.adapters.memory.config_history import (
            InMemoryConfigHistoryStore,
        )
        from baldur.services.config_history.service import ConfigHistoryService

        store = InMemoryConfigHistoryStore()
        return ConfigHistoryService(store=store)

    def test_save_and_retrieve_version_lifecycle(self, service):
        """
        Purpose: Full save → get_current → get_history lifecycle.
        Expected: Saved version is retrievable via both current and history.
        """
        # Given
        config_type = "circuit_breaker"
        values = {"failure_threshold": 5, "reset_timeout": 30}

        # When — save version
        version = service.save_version(
            config_type=config_type,
            values=values,
            changed_by="admin@corp.com",
            reason="Lower threshold for high load",
        )

        # Then — version created
        assert version is not None
        assert version.version == 1
        assert version.config_type == config_type
        assert version.values == values
        assert version.changed_by == "admin@corp.com"
        assert version.hash is not None

        # Then — retrievable as current
        current = service.get_current_version(config_type)
        assert current is not None
        assert current.version == 1
        assert current.values == values

        # Then — in history
        history = service.get_history(config_type, limit=10)
        assert len(history) == 1
        assert history[0].version == 1

    def test_multiple_versions_history_ordering(self, service):
        """
        Purpose: Multiple saves produce correct history ordering.
        Expected: History is newest-first.
        """
        for i in range(5):
            service.save_version(
                config_type="dlq",
                values={"retry_count": i},
                changed_by="admin",
            )

        history = service.get_history("dlq", limit=10)
        assert len(history) == 5
        # Newest first
        versions = [h.version for h in history]
        assert versions == [5, 4, 3, 2, 1]

        # Current is the latest
        current = service.get_current_version("dlq")
        assert current.version == 5

    def test_rollback_creates_new_version_with_old_values(self, service):
        """
        Purpose: Rollback to older version saves as new version.
        Expected: New version with old values, version number incremented.
        """
        # Given — two versions
        service.save_version("circuit_breaker", {"threshold": 5}, "admin")
        service.save_version("circuit_breaker", {"threshold": 3}, "admin")

        # When — rollback to version 1
        rolled = service.rollback(
            "circuit_breaker", target_version=1, rolled_back_by="ops"
        )

        # Then — new version 3 with old values
        assert rolled is not None
        assert rolled.version == 3
        assert rolled.values == {"threshold": 5}
        assert "Rollback to version 1" in rolled.reason

    def test_clear_and_restart(self, service):
        """
        Purpose: Clear history and start fresh.
        Expected: All state removed, next version starts from 1.
        """
        service.save_version("circuit_breaker", {"v": 1}, "admin")
        service.save_version("circuit_breaker", {"v": 2}, "admin")

        service.clear_history("circuit_breaker")

        assert service.get_version_count("circuit_breaker") == 0
        assert service.get_current_version("circuit_breaker") is None

        # New save starts from version 1
        new_ver = service.save_version("circuit_breaker", {"v": 10}, "admin")
        assert new_ver.version == 1

    def test_version_count_matches_history(self, service):
        """
        Purpose: get_version_count matches actual history length.
        Expected: Consistent counts.
        """
        for i in range(7):
            service.save_version("circuit_breaker", {"v": i}, "admin")

        assert service.get_version_count("circuit_breaker") == 7
        assert len(service.get_history("circuit_breaker", limit=100)) == 7


# =============================================================================
# CanaryRolloutService + InMemoryCanaryRolloutStore
# =============================================================================


class TestCanaryRolloutServiceIntegration:
    """Integration: CanaryRolloutService with InMemoryCanaryRolloutStore."""

    @pytest.fixture
    def store(self):
        from baldur.adapters.memory.canary_rollout import (
            InMemoryCanaryRolloutStore,
        )

        return InMemoryCanaryRolloutStore()

    @pytest.fixture
    def service(self, store):
        pytest.importorskip("baldur_pro")

        from baldur_pro.services.canary.service import CanaryRolloutService

        return CanaryRolloutService(store=store)

    def test_store_lock_acquire_and_release(self, store):
        """
        Purpose: Config lock lifecycle via store.
        Expected: acquire → is_locked → release → not locked.
        """
        assert store.acquire_config_lock("cb", "rollout-1") is True
        assert store.is_config_locked("cb") is True
        assert store.get_config_lock_owner("cb") == "rollout-1"

        assert store.release_config_lock("cb", "rollout-1") is True
        assert store.is_config_locked("cb") is False

    def test_store_rollout_crud_lifecycle(self, store):
        """
        Purpose: Rollout save → active set → retrieve → remove.
        Expected: Full CRUD cycle works.
        """
        data = {"id": "r-abc", "state": "created", "config_type": "cb"}

        # Save + activate
        store.save_rollout("r-abc", data, ttl_seconds=86400)
        store.add_active("r-abc")

        # Verify
        assert store.get_rollout("r-abc") == data
        assert "r-abc" in store.get_active_ids()

        # Deactivate
        store.remove_active("r-abc")
        assert "r-abc" not in store.get_active_ids()

        # Rollout data still accessible
        assert store.get_rollout("r-abc") is not None

    def test_store_multiple_locks_independent(self, store):
        """
        Purpose: Different config types have independent locks.
        Expected: Locking 'cb' does not affect 'dlq'.
        """
        store.acquire_config_lock("cb", "r1")
        store.acquire_config_lock("dlq", "r2")

        assert store.get_config_lock_owner("cb") == "r1"
        assert store.get_config_lock_owner("dlq") == "r2"

        store.release_config_lock("cb", "r1")
        assert store.is_config_locked("cb") is False
        assert store.is_config_locked("dlq") is True
