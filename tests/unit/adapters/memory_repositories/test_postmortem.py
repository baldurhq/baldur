"""InMemoryPostmortemRepository unit tests.

Tests for the in-memory PostmortemRepository implementation:
- Behavior: CRUD (save/get/find/count/update_fields/clear)
- Behavior: filter combinations (date, service, min_duration)
- Behavior: thread safety (concurrent save/find)
- Behavior: idempotency (duplicate save)
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from baldur.adapters.memory.postmortem import InMemoryPostmortemRepository
from baldur.interfaces.repositories import PostmortemData, PostmortemRepository

# =============================================================================
# Contract Tests
# =============================================================================


class TestInMemoryPostmortemRepositoryContract:
    """InMemoryPostmortemRepository interface contract verification."""

    def test_implements_postmortem_repository(self):
        """InMemoryPostmortemRepository implements PostmortemRepository ABC."""
        assert issubclass(InMemoryPostmortemRepository, PostmortemRepository)
        repo = InMemoryPostmortemRepository()
        assert isinstance(repo, PostmortemRepository)


# =============================================================================
# Behavior Tests — CRUD
# =============================================================================


class TestInMemoryPostmortemCrudBehavior:
    """InMemoryPostmortemRepository CRUD operations."""

    def setup_method(self):
        """Create fresh repository for each test."""
        self.repo = InMemoryPostmortemRepository()

    def test_save_returns_true(self):
        """save() returns True on success."""
        data = PostmortemData(incident_id="inc-001")
        assert self.repo.save(data) is True

    def test_get_by_incident_id_returns_saved_data(self):
        """get_by_incident_id() returns previously saved data."""
        data = PostmortemData(incident_id="inc-001", source="manual")
        self.repo.save(data)

        result = self.repo.get_by_incident_id("inc-001")
        assert result is not None
        assert result.incident_id == "inc-001"
        assert result.source == "manual"

    def test_get_by_incident_id_returns_none_for_missing(self):
        """get_by_incident_id() returns None for non-existent incident."""
        assert self.repo.get_by_incident_id("missing") is None

    def test_find_returns_all_when_no_filters(self):
        """find() without filters returns all records."""
        for i in range(3):
            self.repo.save(
                PostmortemData(
                    incident_id=f"inc-{i:03d}",
                    started_at=datetime(2026, 1, i + 1, tzinfo=UTC),
                )
            )
        results = self.repo.find()
        assert len(results) == 3

    def test_find_ordered_by_started_at_desc(self):
        """find() returns results ordered by started_at descending."""
        self.repo.save(
            PostmortemData(
                incident_id="old",
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        self.repo.save(
            PostmortemData(
                incident_id="new",
                started_at=datetime(2026, 3, 1, tzinfo=UTC),
            )
        )

        results = self.repo.find()
        assert results[0].incident_id == "new"
        assert results[1].incident_id == "old"

    def test_find_respects_offset_and_limit(self):
        """find() pagination with offset and limit."""
        for i in range(5):
            self.repo.save(
                PostmortemData(
                    incident_id=f"inc-{i:03d}",
                    started_at=datetime(2026, 1, i + 1, tzinfo=UTC),
                )
            )
        results = self.repo.find(offset=1, limit=2)
        assert len(results) == 2

    def test_count_returns_total_when_no_filters(self):
        """count() without filters returns total record count."""
        for i in range(4):
            self.repo.save(PostmortemData(incident_id=f"inc-{i:03d}"))
        assert self.repo.count() == 4

    def test_count_empty_repo_returns_zero(self):
        """count() on empty repo returns 0."""
        assert self.repo.count() == 0

    def test_update_fields_sets_simple_field(self):
        """update_fields() sets a simple field value."""
        self.repo.save(PostmortemData(incident_id="inc-001", source="auto"))
        result = self.repo.update_fields("inc-001", {"source": "manual"})
        assert result is True
        assert self.repo.get_by_incident_id("inc-001").source == "manual"

    def test_update_fields_deep_merges_dict(self):
        """update_fields() deep-merges dict fields."""
        self.repo.save(
            PostmortemData(
                incident_id="inc-001",
                system_snapshot={"cpu": 80, "memory": 60},
            )
        )
        self.repo.update_fields("inc-001", {"system_snapshot": {"disk": 90}})
        data = self.repo.get_by_incident_id("inc-001")
        assert data.system_snapshot == {"cpu": 80, "memory": 60, "disk": 90}

    def test_update_fields_returns_false_for_missing(self):
        """update_fields() returns False for non-existent incident."""
        assert self.repo.update_fields("missing", {"source": "manual"}) is False

    def test_clear_removes_all_entries(self):
        """clear() empties the storage."""
        self.repo.save(PostmortemData(incident_id="inc-001"))
        self.repo.save(PostmortemData(incident_id="inc-002"))
        self.repo.clear()
        assert self.repo.count() == 0

    def test_save_duplicate_overwrites(self):
        """Saving with same incident_id overwrites the previous record."""
        self.repo.save(PostmortemData(incident_id="inc-001", source="auto"))
        self.repo.save(PostmortemData(incident_id="inc-001", source="manual"))
        data = self.repo.get_by_incident_id("inc-001")
        assert data.source == "manual"
        assert self.repo.count() == 1


# =============================================================================
# Behavior Tests — Filter Combinations
# =============================================================================


class TestInMemoryPostmortemFilterBehavior:
    """InMemoryPostmortemRepository filter logic verification."""

    def setup_method(self):
        """Set up repository with diverse test data."""
        self.repo = InMemoryPostmortemRepository()
        self.repo.save(
            PostmortemData(
                incident_id="short-svc-a",
                started_at=datetime(2026, 1, 15, tzinfo=UTC),
                duration_seconds=60.0,
                affected_services=["svc-a"],
            )
        )
        self.repo.save(
            PostmortemData(
                incident_id="long-svc-b",
                started_at=datetime(2026, 2, 15, tzinfo=UTC),
                duration_seconds=600.0,
                affected_services=["svc-b"],
            )
        )
        self.repo.save(
            PostmortemData(
                incident_id="medium-svc-a",
                started_at=datetime(2026, 3, 15, tzinfo=UTC),
                duration_seconds=300.0,
                affected_services=["svc-a", "svc-c"],
            )
        )

    def test_filter_by_start_date(self):
        """find() with start_date filters out earlier incidents."""
        results = self.repo.find(start_date=datetime(2026, 2, 1, tzinfo=UTC))
        assert len(results) == 2
        assert all(r.started_at >= datetime(2026, 2, 1, tzinfo=UTC) for r in results)

    def test_filter_by_end_date(self):
        """find() with end_date filters out later incidents."""
        results = self.repo.find(end_date=datetime(2026, 2, 1, tzinfo=UTC))
        assert len(results) == 1
        assert results[0].incident_id == "short-svc-a"

    def test_filter_by_service(self):
        """find() with service filters to incidents affecting that service."""
        results = self.repo.find(service="svc-a")
        assert len(results) == 2
        assert all("svc-a" in r.affected_services for r in results)

    def test_filter_by_min_duration(self):
        """find() with min_duration filters out shorter incidents."""
        results = self.repo.find(min_duration=300.0)
        assert len(results) == 2
        assert all(r.duration_seconds >= 300.0 for r in results)

    def test_combined_filters(self):
        """find() with multiple filters applies all of them."""
        results = self.repo.find(
            service="svc-a",
            min_duration=200.0,
        )
        assert len(results) == 1
        assert results[0].incident_id == "medium-svc-a"

    def test_count_with_filters_matches_find(self):
        """count() with same filters returns matching length as find()."""
        find_results = self.repo.find(service="svc-a")
        count_result = self.repo.count(service="svc-a")
        assert count_result == len(find_results)


# =============================================================================
# Behavior Tests — Thread Safety (§8.7)
# =============================================================================


class TestInMemoryPostmortemThreadSafetyBehavior:
    """InMemoryPostmortemRepository multi-thread access safety."""

    def test_concurrent_save_no_data_loss(self):
        """20 threads saving concurrently produce no data loss."""
        repo = InMemoryPostmortemRepository()
        errors = []

        def worker(thread_id):
            try:
                repo.save(
                    PostmortemData(
                        incident_id=f"thread-{thread_id}",
                        started_at=datetime(2026, 1, 1, tzinfo=UTC),
                    )
                )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert repo.count() == 20

    def test_concurrent_save_and_find_no_crash(self):
        """Concurrent save and find operations do not crash."""
        repo = InMemoryPostmortemRepository()
        errors = []

        def saver(idx):
            try:
                repo.save(
                    PostmortemData(
                        incident_id=f"s-{idx}",
                        started_at=datetime(2026, 1, 1, tzinfo=UTC),
                    )
                )
            except Exception as e:
                errors.append(e)

        def finder():
            try:
                repo.find()
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(10):
            threads.append(threading.Thread(target=saver, args=(i,)))
            threads.append(threading.Thread(target=finder))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
