"""InMemoryCascadeEventArchiveRepository unit tests.

Tests for the in-memory CascadeEventArchiveRepository implementation:
- Contract: implements CascadeEventArchiveRepository ABC
- Behavior: CRUD (save/get/find/count/delete_older_than/get_chain/clear)
- Behavior: filter combinations (namespace, trigger_type, date range, is_test)
- Behavior: thread safety (concurrent save/find)
- Behavior: idempotency (duplicate save overwrites)
"""

from __future__ import annotations

import threading
from datetime import timedelta

from baldur.adapters.memory.cascade_event import (
    InMemoryCascadeEventArchiveRepository,
)
from baldur.interfaces.repositories import CascadeEventArchiveRepository
from baldur.models.cascade_event import CascadeEventData
from baldur.utils.time import utc_now

# =============================================================================
# Contract Tests
# =============================================================================


class TestInMemoryCascadeEventRepoContract:
    """InMemoryCascadeEventArchiveRepository interface contract verification."""

    def test_implements_cascade_event_archive_repository(self):
        """Implements CascadeEventArchiveRepository ABC."""
        assert issubclass(
            InMemoryCascadeEventArchiveRepository, CascadeEventArchiveRepository
        )
        repo = InMemoryCascadeEventArchiveRepository()
        assert isinstance(repo, CascadeEventArchiveRepository)


# =============================================================================
# Behavior Tests — CRUD
# =============================================================================


class TestInMemoryCascadeEventCrudBehavior:
    """InMemoryCascadeEventArchiveRepository CRUD operations."""

    def setup_method(self):
        """Create fresh repository for each test."""
        self.repo = InMemoryCascadeEventArchiveRepository()
        self.now = utc_now()

    def _make_event(self, cascade_id: str = "c-1", **kwargs) -> CascadeEventData:
        defaults = {
            "cascade_id": cascade_id,
            "namespace": "global",
            "trigger_type": "CANARY_ROLLBACK",
            "current_hash": "abc123",
            "timestamp": self.now,
        }
        defaults.update(kwargs)
        return CascadeEventData(**defaults)

    def test_save_returns_true(self):
        """save() returns True on success."""
        assert self.repo.save(self._make_event()) is True

    def test_get_by_cascade_id_returns_saved_data(self):
        """get_by_cascade_id() returns the saved data."""
        data = self._make_event()
        self.repo.save(data)
        result = self.repo.get_by_cascade_id("c-1")
        assert result is not None
        assert result.cascade_id == "c-1"

    def test_get_by_cascade_id_returns_none_for_missing(self):
        """get_by_cascade_id() returns None for non-existent ID."""
        assert self.repo.get_by_cascade_id("nonexistent") is None

    def test_save_overwrites_existing(self):
        """Duplicate save overwrites the existing entry."""
        self.repo.save(self._make_event(namespace="ns1"))
        self.repo.save(self._make_event(namespace="ns2"))
        result = self.repo.get_by_cascade_id("c-1")
        assert result.namespace == "ns2"

    def test_count_returns_total(self):
        """count() returns total number of entries."""
        self.repo.save(self._make_event("c-1"))
        self.repo.save(self._make_event("c-2"))
        assert self.repo.count() == 2

    def test_count_with_namespace_filter(self):
        """count() filters by namespace."""
        self.repo.save(self._make_event("c-1", namespace="global"))
        self.repo.save(self._make_event("c-2", namespace="seoul"))
        assert self.repo.count(namespace="global") == 1

    def test_delete_older_than_removes_old_events(self):
        """delete_older_than() removes events older than cutoff."""
        old_time = self.now - timedelta(days=400)
        self.repo.save(self._make_event("c-old", timestamp=old_time))
        self.repo.save(self._make_event("c-new", timestamp=self.now))

        deleted = self.repo.delete_older_than(self.now - timedelta(days=1))

        assert deleted == 1
        assert self.repo.get_by_cascade_id("c-old") is None
        assert self.repo.get_by_cascade_id("c-new") is not None

    def test_clear_removes_all_entries(self):
        """clear() removes all entries."""
        self.repo.save(self._make_event("c-1"))
        self.repo.save(self._make_event("c-2"))
        self.repo.clear()
        assert self.repo.count() == 0


# =============================================================================
# Behavior Tests — Find & Filter
# =============================================================================


class TestInMemoryCascadeEventFindBehavior:
    """InMemoryCascadeEventArchiveRepository find and filter operations."""

    def setup_method(self):
        """Create repository with sample data."""
        self.repo = InMemoryCascadeEventArchiveRepository()
        self.now = utc_now()
        self.repo.save(
            CascadeEventData(
                cascade_id="c-1",
                namespace="global",
                trigger_type="CANARY_ROLLBACK",
                current_hash="h1",
                timestamp=self.now,
                is_test=False,
            )
        )
        self.repo.save(
            CascadeEventData(
                cascade_id="c-2",
                namespace="seoul",
                trigger_type="DEESCALATION",
                current_hash="h2",
                timestamp=self.now - timedelta(hours=1),
                is_test=True,
            )
        )
        self.repo.save(
            CascadeEventData(
                cascade_id="c-3",
                namespace="global",
                trigger_type="CANARY_ROLLBACK",
                current_hash="h3",
                timestamp=self.now - timedelta(hours=2),
                is_test=False,
            )
        )

    def test_find_all_returns_desc_by_timestamp(self):
        """find() returns results ordered by timestamp DESC."""
        results = self.repo.find()
        assert len(results) == 3
        assert results[0].cascade_id == "c-1"  # most recent

    def test_find_by_namespace(self):
        """find() filters by namespace."""
        results = self.repo.find(namespace="global")
        assert len(results) == 2
        assert all(r.namespace == "global" for r in results)

    def test_find_by_trigger_type(self):
        """find() filters by trigger_type."""
        results = self.repo.find(trigger_type="DEESCALATION")
        assert len(results) == 1
        assert results[0].cascade_id == "c-2"

    def test_find_by_is_test(self):
        """find() filters by is_test flag."""
        results = self.repo.find(is_test=True)
        assert len(results) == 1
        assert results[0].cascade_id == "c-2"

    def test_find_with_date_range(self):
        """find() filters by start_date and end_date."""
        results = self.repo.find(
            start_date=self.now - timedelta(minutes=30),
            end_date=self.now + timedelta(minutes=1),
        )
        assert len(results) == 1
        assert results[0].cascade_id == "c-1"

    def test_find_with_offset_and_limit(self):
        """find() respects offset and limit."""
        results = self.repo.find(offset=1, limit=1)
        assert len(results) == 1
        assert results[0].cascade_id == "c-2"

    def test_get_chain_returns_asc_by_timestamp(self):
        """get_chain() returns results ordered by timestamp ASC."""
        results = self.repo.get_chain("global")
        assert len(results) == 2
        assert results[0].cascade_id == "c-3"  # oldest first
        assert results[1].cascade_id == "c-1"

    def test_get_chain_filters_by_namespace(self):
        """get_chain() only returns events for the given namespace."""
        results = self.repo.get_chain("seoul")
        assert len(results) == 1
        assert results[0].cascade_id == "c-2"


# =============================================================================
# Behavior Tests — Thread Safety
# =============================================================================


class TestInMemoryCascadeEventThreadSafetyBehavior:
    """InMemoryCascadeEventArchiveRepository thread safety verification."""

    def test_concurrent_save_no_data_corruption(self):
        """10 threads saving concurrently do not corrupt data."""
        repo = InMemoryCascadeEventArchiveRepository()
        now = utc_now()
        errors: list[Exception] = []

        def worker(thread_id: int) -> None:
            try:
                for i in range(10):
                    data = CascadeEventData(
                        cascade_id=f"c-{thread_id}-{i}",
                        namespace="global",
                        trigger_type="CANARY_ROLLBACK",
                        current_hash=f"hash-{thread_id}-{i}",
                        timestamp=now,
                    )
                    repo.save(data)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert repo.count() == 100
