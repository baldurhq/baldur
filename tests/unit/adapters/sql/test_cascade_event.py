"""
Unit tests for SQLCascadeEventArchiveRepository.

Coverage:
- save / get_by_cascade_id round-trip preserves JSON DTO fields.
- find() with namespace, trigger_type, date range, is_test filters.
- find() ordering (DESC by timestamp).
- get_chain() ordering (ASC by timestamp) for integrity verification.
- count() aggregation with filters.
- delete_older_than() lifecycle.
- Duplicate cascade_id save returns False.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from baldur.adapters.sql.cascade_event import SQLCascadeEventArchiveRepository
from baldur.models.cascade_event import CascadeEventData


@pytest.fixture
def repo(get_sqlite_conn) -> SQLCascadeEventArchiveRepository:
    return SQLCascadeEventArchiveRepository(get_sqlite_conn)


def _make_event(
    cascade_id: str = "cascade-001",
    *,
    namespace: str = "payment",
    trigger_type: str = "circuit_breaker",
    timestamp: datetime | None = None,
    is_test: bool = False,
    total_effects: int = 3,
    success_count: int = 2,
    failure_count: int = 1,
) -> CascadeEventData:
    return CascadeEventData(
        cascade_id=cascade_id,
        namespace=namespace,
        trigger_type=trigger_type,
        current_hash="abc123",
        previous_hash="prev000",
        total_effects=total_effects,
        success_count=success_count,
        failure_count=failure_count,
        timestamp=timestamp or datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
        archived_at=datetime(2026, 4, 14, 10, 5, 0, tzinfo=UTC),
        version="1.0",
        is_test=is_test,
        trigger_details={"reason": "threshold_exceeded"},
        effects=[{"target": "api", "result": "ok"}],
        causation_chain=["event-a", "event-b"],
        external_trace={"trace_id": "t-001"},
    )


class TestSQLCascadeEventCrudBehavior:
    """save / get_by_cascade_id round-trip."""

    def test_save_and_get_round_trips_all_fields(self, repo):
        event = _make_event("cascade-001")
        assert repo.save(event) is True

        fetched = repo.get_by_cascade_id("cascade-001")
        assert fetched is not None
        assert fetched.cascade_id == "cascade-001"
        assert fetched.namespace == "payment"
        assert fetched.trigger_type == "circuit_breaker"
        assert fetched.current_hash == "abc123"
        assert fetched.previous_hash == "prev000"
        assert fetched.total_effects == 3
        assert fetched.success_count == 2
        assert fetched.failure_count == 1
        assert fetched.version == "1.0"
        assert fetched.is_test is False
        assert fetched.trigger_details == {"reason": "threshold_exceeded"}
        assert fetched.effects == [{"target": "api", "result": "ok"}]
        assert fetched.causation_chain == ["event-a", "event-b"]
        assert fetched.external_trace == {"trace_id": "t-001"}

    def test_get_by_cascade_id_returns_none_for_missing(self, repo):
        assert repo.get_by_cascade_id("nonexistent") is None

    def test_duplicate_cascade_id_save_returns_false(self, repo):
        event1 = _make_event("cascade-dup")
        event2 = _make_event("cascade-dup")
        assert repo.save(event1) is True
        assert repo.save(event2) is False

    def test_save_with_none_external_trace(self, repo):
        event = _make_event("cascade-none-trace")
        event.external_trace = None
        repo.save(event)
        fetched = repo.get_by_cascade_id("cascade-none-trace")
        assert fetched.external_trace is None

    def test_is_test_flag_round_trips(self, repo):
        repo.save(_make_event("cascade-test", is_test=True))
        fetched = repo.get_by_cascade_id("cascade-test")
        assert fetched.is_test is True


class TestSQLCascadeEventFindBehavior:
    """find() with SQL filter clauses."""

    def test_find_by_namespace(self, repo):
        repo.save(_make_event("c-1", namespace="payment"))
        repo.save(_make_event("c-2", namespace="notification"))

        results = repo.find(namespace="payment")
        assert len(results) == 1
        assert results[0].cascade_id == "c-1"

    def test_find_by_trigger_type(self, repo):
        repo.save(_make_event("c-1", trigger_type="circuit_breaker"))
        repo.save(_make_event("c-2", trigger_type="manual"))

        results = repo.find(trigger_type="manual")
        assert len(results) == 1
        assert results[0].cascade_id == "c-2"

    def test_find_by_date_range(self, repo):
        repo.save(
            _make_event(
                "c-old",
                timestamp=datetime(2026, 4, 10, 10, 0, 0, tzinfo=UTC),
            )
        )
        repo.save(
            _make_event(
                "c-mid",
                timestamp=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
            )
        )
        repo.save(
            _make_event(
                "c-new",
                timestamp=datetime(2026, 4, 18, 10, 0, 0, tzinfo=UTC),
            )
        )

        results = repo.find(
            start_date=datetime(2026, 4, 12, 0, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 4, 16, 0, 0, 0, tzinfo=UTC),
        )
        assert len(results) == 1
        assert results[0].cascade_id == "c-mid"

    def test_find_by_is_test(self, repo):
        repo.save(_make_event("c-prod", is_test=False))
        repo.save(_make_event("c-test", is_test=True))

        results = repo.find(is_test=True)
        assert len(results) == 1
        assert results[0].cascade_id == "c-test"

    def test_find_ordered_desc_by_timestamp(self, repo):
        repo.save(
            _make_event(
                "c-old",
                timestamp=datetime(2026, 4, 10, 10, 0, 0, tzinfo=UTC),
            )
        )
        repo.save(
            _make_event(
                "c-new",
                timestamp=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
            )
        )

        results = repo.find()
        assert results[0].cascade_id == "c-new"
        assert results[1].cascade_id == "c-old"

    def test_find_respects_limit_and_offset(self, repo):
        for i in range(5):
            repo.save(
                _make_event(
                    f"c-{i:02d}",
                    timestamp=datetime(2026, 4, 14, 10, i, 0, tzinfo=UTC),
                )
            )

        results = repo.find(limit=2, offset=1)
        assert len(results) == 2

    def test_find_combined_filters(self, repo):
        repo.save(
            _make_event(
                "c-match",
                namespace="payment",
                trigger_type="circuit_breaker",
                timestamp=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
            )
        )
        repo.save(
            _make_event(
                "c-wrong-ns",
                namespace="notification",
                trigger_type="circuit_breaker",
            )
        )
        repo.save(
            _make_event(
                "c-wrong-type",
                namespace="payment",
                trigger_type="manual",
            )
        )

        results = repo.find(namespace="payment", trigger_type="circuit_breaker")
        assert len(results) == 1
        assert results[0].cascade_id == "c-match"


class TestSQLCascadeEventChainBehavior:
    """get_chain() returns ASC ordered events for hash verification."""

    def test_get_chain_returns_asc_by_timestamp(self, repo):
        repo.save(
            _make_event(
                "c-1",
                namespace="payment",
                timestamp=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
            )
        )
        repo.save(
            _make_event(
                "c-2",
                namespace="payment",
                timestamp=datetime(2026, 4, 14, 11, 0, 0, tzinfo=UTC),
            )
        )
        repo.save(
            _make_event(
                "c-3",
                namespace="payment",
                timestamp=datetime(2026, 4, 14, 12, 0, 0, tzinfo=UTC),
            )
        )

        chain = repo.get_chain("payment")
        assert len(chain) == 3
        assert chain[0].cascade_id == "c-1"
        assert chain[1].cascade_id == "c-2"
        assert chain[2].cascade_id == "c-3"

    def test_get_chain_filters_by_namespace(self, repo):
        repo.save(_make_event("c-pay", namespace="payment"))
        repo.save(_make_event("c-notif", namespace="notification"))

        chain = repo.get_chain("payment")
        assert len(chain) == 1
        assert chain[0].cascade_id == "c-pay"

    def test_get_chain_with_date_range(self, repo):
        repo.save(
            _make_event(
                "c-old",
                namespace="payment",
                timestamp=datetime(2026, 4, 10, 10, 0, 0, tzinfo=UTC),
            )
        )
        repo.save(
            _make_event(
                "c-mid",
                namespace="payment",
                timestamp=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
            )
        )
        repo.save(
            _make_event(
                "c-new",
                namespace="payment",
                timestamp=datetime(2026, 4, 18, 10, 0, 0, tzinfo=UTC),
            )
        )

        chain = repo.get_chain(
            "payment",
            start_date=datetime(2026, 4, 12, 0, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 4, 16, 0, 0, 0, tzinfo=UTC),
        )
        assert len(chain) == 1
        assert chain[0].cascade_id == "c-mid"


class TestSQLCascadeEventCountBehavior:
    """count() aggregation."""

    def test_count_all(self, repo):
        repo.save(_make_event("c-1"))
        repo.save(_make_event("c-2"))
        assert repo.count() == 2

    def test_count_with_namespace_filter(self, repo):
        repo.save(_make_event("c-1", namespace="payment"))
        repo.save(_make_event("c-2", namespace="notification"))
        assert repo.count(namespace="payment") == 1

    def test_count_with_date_range(self, repo):
        repo.save(
            _make_event(
                "c-old",
                timestamp=datetime(2026, 4, 10, 10, 0, 0, tzinfo=UTC),
            )
        )
        repo.save(
            _make_event(
                "c-new",
                timestamp=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
            )
        )
        c = repo.count(
            start_date=datetime(2026, 4, 12, 0, 0, 0, tzinfo=UTC),
        )
        assert c == 1


class TestSQLCascadeEventDeleteBehavior:
    """delete_older_than() lifecycle."""

    def test_delete_older_than_removes_old_events(self, repo):
        repo.save(
            _make_event(
                "c-old",
                timestamp=datetime(2026, 4, 10, 10, 0, 0, tzinfo=UTC),
            )
        )
        repo.save(
            _make_event(
                "c-new",
                timestamp=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
            )
        )

        cutoff = datetime(2026, 4, 12, 0, 0, 0, tzinfo=UTC)
        deleted = repo.delete_older_than(cutoff)
        assert deleted == 1
        assert repo.get_by_cascade_id("c-old") is None
        assert repo.get_by_cascade_id("c-new") is not None

    def test_delete_older_than_returns_zero_when_nothing_matches(self, repo):
        repo.save(
            _make_event(
                "c-new",
                timestamp=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
            )
        )
        cutoff = datetime(2026, 4, 10, 0, 0, 0, tzinfo=UTC)
        assert repo.delete_older_than(cutoff) == 0
