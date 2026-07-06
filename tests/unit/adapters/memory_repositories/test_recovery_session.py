"""InMemoryRecoverySessionArchiveRepository unit tests.

Tests for the in-memory RecoverySessionArchiveRepository implementation:
- Contract: implements RecoverySessionArchiveRepository ABC
- Behavior: CRUD (save/get/find/count/update/delete_older_than/clear)
- Behavior: filter combinations (namespace, status, date range)
- Behavior: update returns False for non-existent session
- Behavior: thread safety (concurrent save/update)
"""

from __future__ import annotations

import threading
from datetime import timedelta

from baldur.adapters.memory.recovery_session import (
    InMemoryRecoverySessionArchiveRepository,
)
from baldur.interfaces.repositories import RecoverySessionArchiveRepository
from baldur.models.recovery_session import RecoverySessionData
from baldur.utils.time import utc_now

# =============================================================================
# Contract Tests
# =============================================================================


class TestInMemoryRecoverySessionRepoContract:
    """InMemoryRecoverySessionArchiveRepository interface contract verification."""

    def test_implements_recovery_session_archive_repository(self):
        """Implements RecoverySessionArchiveRepository ABC."""
        assert issubclass(
            InMemoryRecoverySessionArchiveRepository,
            RecoverySessionArchiveRepository,
        )
        repo = InMemoryRecoverySessionArchiveRepository()
        assert isinstance(repo, RecoverySessionArchiveRepository)


# =============================================================================
# Behavior Tests — CRUD
# =============================================================================


class TestInMemoryRecoverySessionCrudBehavior:
    """InMemoryRecoverySessionArchiveRepository CRUD operations."""

    def setup_method(self):
        """Create fresh repository for each test."""
        self.repo = InMemoryRecoverySessionArchiveRepository()
        self.now = utc_now()

    def _make_session(self, session_id: str = "s-1", **kwargs) -> RecoverySessionData:
        defaults = {
            "session_id": session_id,
            "namespace": "global",
            "trigger_level": "LEVEL_1",
            "status": "not_started",
            "started_at": self.now,
        }
        defaults.update(kwargs)
        return RecoverySessionData(**defaults)

    def test_save_returns_true(self):
        """save() returns True on success."""
        assert self.repo.save(self._make_session()) is True

    def test_get_by_session_id_returns_saved_data(self):
        """get_by_session_id() returns the saved data."""
        data = self._make_session()
        self.repo.save(data)
        result = self.repo.get_by_session_id("s-1")
        assert result is not None
        assert result.session_id == "s-1"

    def test_get_by_session_id_returns_none_for_missing(self):
        """get_by_session_id() returns None for non-existent ID."""
        assert self.repo.get_by_session_id("nonexistent") is None

    def test_save_overwrites_existing(self):
        """Duplicate save overwrites the existing entry."""
        self.repo.save(self._make_session(namespace="ns1"))
        self.repo.save(self._make_session(namespace="ns2"))
        result = self.repo.get_by_session_id("s-1")
        assert result.namespace == "ns2"

    def test_count_returns_total(self):
        """count() returns total number of entries."""
        self.repo.save(self._make_session("s-1"))
        self.repo.save(self._make_session("s-2"))
        assert self.repo.count() == 2

    def test_count_with_namespace_filter(self):
        """count() filters by namespace."""
        self.repo.save(self._make_session("s-1", namespace="global"))
        self.repo.save(self._make_session("s-2", namespace="seoul"))
        assert self.repo.count(namespace="global") == 1

    def test_count_with_status_filter(self):
        """count() filters by status."""
        self.repo.save(self._make_session("s-1", status="completed"))
        self.repo.save(self._make_session("s-2", status="failed"))
        assert self.repo.count(status="completed") == 1

    def test_update_returns_true_for_existing(self):
        """update() returns True for existing session."""
        self.repo.save(self._make_session("s-1", status="not_started"))
        updated = self._make_session("s-1", status="completed")
        assert self.repo.update(updated) is True
        result = self.repo.get_by_session_id("s-1")
        assert result.status == "completed"

    def test_update_returns_false_for_nonexistent(self):
        """update() returns False for non-existent session."""
        data = self._make_session("nonexistent")
        assert self.repo.update(data) is False

    def test_delete_older_than_removes_old_sessions(self):
        """delete_older_than() removes sessions older than cutoff."""
        old_time = self.now - timedelta(days=400)
        self.repo.save(self._make_session("s-old", started_at=old_time))
        self.repo.save(self._make_session("s-new", started_at=self.now))

        deleted = self.repo.delete_older_than(self.now - timedelta(days=1))

        assert deleted == 1
        assert self.repo.get_by_session_id("s-old") is None
        assert self.repo.get_by_session_id("s-new") is not None

    def test_clear_removes_all_entries(self):
        """clear() removes all entries."""
        self.repo.save(self._make_session("s-1"))
        self.repo.save(self._make_session("s-2"))
        self.repo.clear()
        assert self.repo.count() == 0


# =============================================================================
# Behavior Tests — Find & Filter
# =============================================================================


class TestInMemoryRecoverySessionFindBehavior:
    """InMemoryRecoverySessionArchiveRepository find and filter operations."""

    def setup_method(self):
        """Create repository with sample data."""
        self.repo = InMemoryRecoverySessionArchiveRepository()
        self.now = utc_now()
        self.repo.save(
            RecoverySessionData(
                session_id="s-1",
                namespace="global",
                trigger_level="LEVEL_1",
                status="completed",
                started_at=self.now,
            )
        )
        self.repo.save(
            RecoverySessionData(
                session_id="s-2",
                namespace="seoul",
                trigger_level="LEVEL_2",
                status="failed",
                started_at=self.now - timedelta(hours=1),
            )
        )
        self.repo.save(
            RecoverySessionData(
                session_id="s-3",
                namespace="global",
                trigger_level="LEVEL_3",
                status="completed",
                started_at=self.now - timedelta(hours=2),
            )
        )

    def test_find_all_returns_desc_by_started_at(self):
        """find() returns results ordered by started_at DESC."""
        results = self.repo.find()
        assert len(results) == 3
        assert results[0].session_id == "s-1"  # most recent

    def test_find_by_namespace(self):
        """find() filters by namespace."""
        results = self.repo.find(namespace="global")
        assert len(results) == 2
        assert all(r.namespace == "global" for r in results)

    def test_find_by_status(self):
        """find() filters by status."""
        results = self.repo.find(status="failed")
        assert len(results) == 1
        assert results[0].session_id == "s-2"

    def test_find_with_date_range(self):
        """find() filters by start_date and end_date."""
        results = self.repo.find(
            start_date=self.now - timedelta(minutes=30),
            end_date=self.now + timedelta(minutes=1),
        )
        assert len(results) == 1
        assert results[0].session_id == "s-1"

    def test_find_with_offset_and_limit(self):
        """find() respects offset and limit."""
        results = self.repo.find(offset=1, limit=1)
        assert len(results) == 1
        assert results[0].session_id == "s-2"


# =============================================================================
# Behavior Tests — Thread Safety
# =============================================================================


class TestInMemoryRecoverySessionThreadSafetyBehavior:
    """InMemoryRecoverySessionArchiveRepository thread safety verification."""

    def test_concurrent_save_no_data_corruption(self):
        """10 threads saving concurrently do not corrupt data."""
        repo = InMemoryRecoverySessionArchiveRepository()
        now = utc_now()
        errors: list[Exception] = []

        def worker(thread_id: int) -> None:
            try:
                for i in range(10):
                    data = RecoverySessionData(
                        session_id=f"s-{thread_id}-{i}",
                        namespace="global",
                        trigger_level="LEVEL_1",
                        started_at=now,
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

    def test_concurrent_update_no_data_corruption(self):
        """Concurrent updates do not corrupt data."""
        repo = InMemoryRecoverySessionArchiveRepository()
        now = utc_now()
        repo.save(
            RecoverySessionData(
                session_id="s-shared",
                namespace="global",
                trigger_level="LEVEL_1",
                status="not_started",
                started_at=now,
            )
        )
        errors: list[Exception] = []

        def worker(thread_id: int) -> None:
            try:
                for _ in range(20):
                    data = RecoverySessionData(
                        session_id="s-shared",
                        namespace="global",
                        trigger_level="LEVEL_1",
                        status=f"status-{thread_id}",
                        started_at=now,
                    )
                    repo.update(data)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # The final state should be one of the thread values
        result = repo.get_by_session_id("s-shared")
        assert result is not None
