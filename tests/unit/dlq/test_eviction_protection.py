"""
Unit tests for DLQ eviction protection — REPLAYING/REVIEWING entries must survive eviction.

Covers the R2 bug fix across all three adapter implementations:
- InMemory: adapters/memory/failed_operation.py
- Redis: adapters/redis/dlq_maintenance.py
- Overflow: baldur_pro/services/dlq/overflow.py (drop_oldest delegation)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.memory.failed_operation import InMemoryFailedOperationRepository
from baldur.interfaces.repositories import FailedOperationStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_entry(repo: InMemoryFailedOperationRepository, domain: str = "test"):
    """Create a PENDING entry and return its ID."""
    entry = repo.create(domain=domain, failure_type="error", error_message="fail")
    return entry.id


def _set_status(repo: InMemoryFailedOperationRepository, entry_id: int, status: str):
    """Directly mutate entry status for test setup (bypasses lifecycle)."""
    with repo._lock:
        entry = repo._storage.get(entry_id)
        if entry is None:
            return
        old_status = entry.status
        updated = entry.__class__(**{**entry.__dict__, "status": status})
        repo._storage[entry_id] = updated
        repo._remove_from_index(entry_id, old_status, entry.domain)
        repo._add_to_index(entry_id, status, entry.domain)


# =============================================================================
# A. Contract Tests — protected status set
# =============================================================================


class TestEvictionProtectedStatusContract:
    """Contract: exactly REPLAYING and REVIEWING are protected from eviction."""

    def test_inmemory_protected_statuses(self):
        """InMemory adapter protects REPLAYING and REVIEWING."""
        protected = InMemoryFailedOperationRepository._EVICTION_PROTECTED
        assert FailedOperationStatus.REPLAYING.value in protected
        assert FailedOperationStatus.REVIEWING.value in protected

    def test_inmemory_protected_count_is_two(self):
        """Exactly 2 statuses are protected."""
        assert len(InMemoryFailedOperationRepository._EVICTION_PROTECTED) == 2

    def test_redis_protected_statuses(self):
        """Redis maintenance adapter protects REPLAYING and REVIEWING."""
        from baldur.adapters.redis.dlq_maintenance import RedisDLQMaintenance

        protected = RedisDLQMaintenance._EVICTION_PROTECTED
        assert FailedOperationStatus.REPLAYING.value in protected
        assert FailedOperationStatus.REVIEWING.value in protected
        assert len(protected) == 2

    def test_sql_protected_statuses(self):
        """SQL adapter protects REPLAYING and REVIEWING."""
        from baldur.adapters.sql.failed_operation import SQLFailedOperationRepository

        protected = SQLFailedOperationRepository._EVICTION_PROTECTED
        assert FailedOperationStatus.REPLAYING.value in protected
        assert FailedOperationStatus.REVIEWING.value in protected
        assert len(protected) == 2

    def test_all_adapters_protect_same_statuses(self):
        """All three adapters agree on the protected status set."""
        from baldur.adapters.redis.dlq_maintenance import RedisDLQMaintenance
        from baldur.adapters.sql.failed_operation import SQLFailedOperationRepository

        inmemory = set(InMemoryFailedOperationRepository._EVICTION_PROTECTED)
        redis = set(RedisDLQMaintenance._EVICTION_PROTECTED)
        sql = set(SQLFailedOperationRepository._EVICTION_PROTECTED)
        assert inmemory == redis == sql


# =============================================================================
# B. Behavior Tests — InMemory evict_oldest protection
# =============================================================================


class TestInMemoryEvictionProtectionBehavior:
    """Behavior: InMemory evict_oldest skips protected entries."""

    @pytest.fixture
    def repo(self):
        return InMemoryFailedOperationRepository()

    def test_pending_entry_is_evicted(self, repo):
        """PENDING entries can be evicted normally."""
        eid = _create_entry(repo)
        evicted = repo.evict_oldest(10)
        assert evicted == 1
        assert repo.get_by_id(eid) is None

    def test_replaying_entry_is_protected(self, repo):
        """REPLAYING entries are skipped during eviction."""
        eid = _create_entry(repo)
        _set_status(repo, eid, FailedOperationStatus.REPLAYING.value)
        evicted = repo.evict_oldest(10)
        assert evicted == 0
        assert repo.get_by_id(eid) is not None

    def test_reviewing_entry_is_protected(self, repo):
        """REVIEWING entries are skipped during eviction."""
        eid = _create_entry(repo)
        _set_status(repo, eid, FailedOperationStatus.REVIEWING.value)
        evicted = repo.evict_oldest(10)
        assert evicted == 0
        assert repo.get_by_id(eid) is not None

    def test_resolved_entry_is_evicted(self, repo):
        """RESOLVED entries are NOT protected."""
        eid = _create_entry(repo)
        _set_status(repo, eid, FailedOperationStatus.RESOLVED.value)
        evicted = repo.evict_oldest(10)
        assert evicted == 1
        assert repo.get_by_id(eid) is None

    def test_archived_entry_is_evicted(self, repo):
        """ARCHIVED entries are NOT protected."""
        eid = _create_entry(repo)
        _set_status(repo, eid, FailedOperationStatus.ARCHIVED.value)
        evicted = repo.evict_oldest(10)
        assert evicted == 1

    def test_mixed_statuses_only_protected_survive(self, repo):
        """In a mixed set, only REPLAYING/REVIEWING survive eviction."""
        pending_id = _create_entry(repo)
        replaying_id = _create_entry(repo)
        reviewing_id = _create_entry(repo)
        resolved_id = _create_entry(repo)

        _set_status(repo, replaying_id, FailedOperationStatus.REPLAYING.value)
        _set_status(repo, reviewing_id, FailedOperationStatus.REVIEWING.value)
        _set_status(repo, resolved_id, FailedOperationStatus.RESOLVED.value)

        evicted = repo.evict_oldest(10)

        # PENDING + RESOLVED evicted, REPLAYING + REVIEWING survive
        assert evicted == 2
        assert repo.get_by_id(pending_id) is None
        assert repo.get_by_id(replaying_id) is not None
        assert repo.get_by_id(reviewing_id) is not None
        assert repo.get_by_id(resolved_id) is None

    def test_eviction_count_excludes_protected(self, repo):
        """Return count reflects only actually evicted entries."""
        for _ in range(3):
            _create_entry(repo)
        eid = _create_entry(repo)
        _set_status(repo, eid, FailedOperationStatus.REPLAYING.value)

        evicted = repo.evict_oldest(10)
        assert evicted == 3

    def test_eviction_with_count_limit_respects_protection(self, repo):
        """When count < total, protected entries don't consume the count quota."""
        # Create 5 entries: first 2 are REPLAYING, next 3 are PENDING
        ids = [_create_entry(repo) for _ in range(5)]
        _set_status(repo, ids[0], FailedOperationStatus.REPLAYING.value)
        _set_status(repo, ids[1], FailedOperationStatus.REPLAYING.value)

        # Request evict 5 (all), but 2 are protected → evict 3
        evicted = repo.evict_oldest(5)
        assert evicted == 3


# =============================================================================
# C. Behavior Tests — Redis evict_oldest protection (mocked backend)
# =============================================================================


class TestRedisEvictionProtectionBehavior:
    """Behavior: Redis evict_oldest skips protected entries via status check."""

    @pytest.fixture
    def maintenance(self):
        from baldur.adapters.redis.dlq_maintenance import RedisDLQMaintenance

        mock_repo = MagicMock()
        mock_repo._backend = MagicMock()
        mock_repo.PENDING_KEY = "dlq:pending"
        mock_repo.BY_DOMAIN_PREFIX = "dlq:domain:"
        mock_repo._make_key = lambda eid: f"dlq:entry:{eid}"
        mock_repo.delete.return_value = True

        maint = RedisDLQMaintenance(mock_repo)
        return maint, mock_repo

    def test_pending_entry_is_evicted(self, maintenance):
        """PENDING entry is deleted."""
        maint, mock_repo = maintenance
        mock_repo._backend.zrange.return_value = [b"1"]
        mock_repo._decode_entry.return_value = {
            "status": FailedOperationStatus.PENDING.value,
        }
        evicted = maint.evict_oldest(10)
        assert evicted == 1
        mock_repo.delete.assert_called_once_with("1")

    def test_replaying_entry_is_skipped(self, maintenance):
        """REPLAYING entry is not deleted."""
        maint, mock_repo = maintenance
        mock_repo._backend.zrange.return_value = [b"1"]
        mock_repo._decode_entry.return_value = {
            "status": FailedOperationStatus.REPLAYING.value,
        }
        evicted = maint.evict_oldest(10)
        assert evicted == 0
        mock_repo.delete.assert_not_called()

    def test_reviewing_entry_is_skipped(self, maintenance):
        """REVIEWING entry is not deleted."""
        maint, mock_repo = maintenance
        mock_repo._backend.zrange.return_value = [b"1"]
        mock_repo._decode_entry.return_value = {
            "status": FailedOperationStatus.REVIEWING.value,
        }
        evicted = maint.evict_oldest(10)
        assert evicted == 0
        mock_repo.delete.assert_not_called()

    def test_mixed_entries_only_unprotected_deleted(self, maintenance):
        """Mixed statuses: only PENDING deleted, REPLAYING skipped."""
        maint, mock_repo = maintenance
        mock_repo._backend.zrange.return_value = [b"1", b"2", b"3"]
        mock_repo._decode_entry.side_effect = [
            {"status": FailedOperationStatus.PENDING.value},
            {"status": FailedOperationStatus.REPLAYING.value},
            {"status": FailedOperationStatus.PENDING.value},
        ]
        evicted = maint.evict_oldest(10)
        assert evicted == 2
        assert mock_repo.delete.call_count == 2

    def test_missing_entry_data_still_evicted(self, maintenance):
        """Entry with no decoded data (already deleted) is still deleted."""
        maint, mock_repo = maintenance
        mock_repo._backend.zrange.return_value = [b"1"]
        mock_repo._decode_entry.return_value = {}
        evicted = maint.evict_oldest(10)
        assert evicted == 1


# =============================================================================
# D. Behavior Tests — Overflow drop_oldest delegates to evict_oldest
# =============================================================================


class TestOverflowDropOldestDelegationBehavior:
    """Behavior: overflow drop_oldest path uses repository.evict_oldest()."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        # Overflow eviction lives in baldur_pro.services.dlq.overflow (PRO-tier).
        pytest.importorskip("baldur_pro")

    def test_drop_oldest_calls_evict_oldest_not_delete(self):
        """drop_oldest strategy delegates to evict_oldest (with protection)."""
        mock_repo = MagicMock()
        mock_repo.count_all.return_value = 80_000
        mock_repo.evict_oldest.side_effect = [1000, 0]

        mock_settings = MagicMock()
        mock_settings.max_size = 100_000
        mock_settings.overflow_strategy = "drop_oldest"
        mock_settings.emergency_purge_threshold = 0.8
        mock_settings.overflow_evict_batch_size = 1000

        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur_pro.services.dlq.overflow._get_repository",
                return_value=mock_repo,
            ),
            patch(
                "baldur_pro.services.dlq.overflow._evict_overflow_domains",
                return_value=0,
            ),
        ):
            from baldur_pro.services.dlq.overflow import run_background_eviction

            run_background_eviction()

        mock_repo.evict_oldest.assert_called()
        mock_repo.delete.assert_not_called()

    def test_compress_strategy_does_not_use_evict_oldest(self):
        """compress_oldest uses compress_and_evict_oldest, not evict_oldest."""
        mock_repo = MagicMock()
        mock_repo.count_all.return_value = 80_000
        mock_repo.compress_and_evict_oldest.side_effect = [1000, 0]

        mock_settings = MagicMock()
        mock_settings.max_size = 100_000
        mock_settings.overflow_strategy = "compress_oldest"
        mock_settings.emergency_purge_threshold = 0.8
        mock_settings.overflow_evict_batch_size = 1000

        with (
            patch(
                "baldur.settings.dlq.get_dlq_settings",
                return_value=mock_settings,
            ),
            patch(
                "baldur_pro.services.dlq.overflow._get_repository",
                return_value=mock_repo,
            ),
            patch(
                "baldur_pro.services.dlq.overflow._evict_overflow_domains",
                return_value=0,
            ),
        ):
            from baldur_pro.services.dlq.overflow import run_background_eviction

            run_background_eviction()

        mock_repo.compress_and_evict_oldest.assert_called()
        mock_repo.evict_oldest.assert_not_called()
