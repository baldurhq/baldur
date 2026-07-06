"""
Unit tests for SQLRecoverySessionArchiveRepository.

Coverage:
- save / get_by_session_id round-trip preserves JSON DTO fields.
- find() with namespace, status, date range filters.
- find() ordering (DESC by started_at).
- count() aggregation with filters.
- update() full-record replacement.
- delete_older_than() lifecycle.
- Duplicate session_id save returns False.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from baldur.adapters.sql.recovery_session import SQLRecoverySessionArchiveRepository
from baldur.models.recovery_session import RecoverySessionData


@pytest.fixture
def repo(get_sqlite_conn) -> SQLRecoverySessionArchiveRepository:
    return SQLRecoverySessionArchiveRepository(get_sqlite_conn)


def _make_session(
    session_id: str = "sess-001",
    *,
    namespace: str = "payment",
    status: str = "completed",
    trigger_level: str = "L1",
    started_at: datetime | None = None,
) -> RecoverySessionData:
    return RecoverySessionData(
        session_id=session_id,
        namespace=namespace,
        trigger_level=trigger_level,
        status=status,
        initiated_by="system",
        started_at=started_at or datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
        completed_at=datetime(2026, 4, 14, 10, 5, 0, tzinfo=UTC),
        duration_seconds=300.0,
        abort_reason="",
        cascade_event_id="cascade-001",
        requires_approval=False,
        approved_by="",
        approved_at=None,
        steps_data=[
            {"step": "diagnose", "result": "ok"},
            {"step": "fix", "result": "ok"},
        ],
        metadata={"trace_id": "t-001", "source": "auto"},
    )


class TestSQLRecoverySessionCrudBehavior:
    """save / get_by_session_id round-trip."""

    def test_save_and_get_round_trips_all_fields(self, repo):
        session = _make_session("sess-001")
        assert repo.save(session) is True

        fetched = repo.get_by_session_id("sess-001")
        assert fetched is not None
        assert fetched.session_id == "sess-001"
        assert fetched.namespace == "payment"
        assert fetched.trigger_level == "L1"
        assert fetched.status == "completed"
        assert fetched.initiated_by == "system"
        assert fetched.duration_seconds == 300.0
        assert fetched.cascade_event_id == "cascade-001"
        assert fetched.requires_approval is False
        assert fetched.steps_data == [
            {"step": "diagnose", "result": "ok"},
            {"step": "fix", "result": "ok"},
        ]
        assert fetched.metadata == {"trace_id": "t-001", "source": "auto"}

    def test_get_by_session_id_returns_none_for_missing(self, repo):
        assert repo.get_by_session_id("nonexistent") is None

    def test_duplicate_session_id_save_returns_false(self, repo):
        s1 = _make_session("sess-dup")
        s2 = _make_session("sess-dup")
        assert repo.save(s1) is True
        assert repo.save(s2) is False

    def test_save_with_approval_fields(self, repo):
        session = _make_session("sess-approved")
        session.requires_approval = True
        session.approved_by = "admin@example.com"
        session.approved_at = datetime(2026, 4, 14, 10, 3, 0, tzinfo=UTC)
        repo.save(session)

        fetched = repo.get_by_session_id("sess-approved")
        assert fetched.requires_approval is True
        assert fetched.approved_by == "admin@example.com"
        assert fetched.approved_at is not None

    def test_save_with_empty_json_fields(self, repo):
        session = RecoverySessionData(
            session_id="sess-empty",
            namespace="payment",
            trigger_level="L1",
            status="started",
            started_at=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
        )
        assert repo.save(session) is True
        fetched = repo.get_by_session_id("sess-empty")
        assert fetched.steps_data == []
        assert fetched.metadata == {}


class TestSQLRecoverySessionFindBehavior:
    """find() with filter clauses."""

    def test_find_by_namespace(self, repo):
        repo.save(_make_session("s-1", namespace="payment"))
        repo.save(_make_session("s-2", namespace="notification"))

        results = repo.find(namespace="payment")
        assert len(results) == 1
        assert results[0].session_id == "s-1"

    def test_find_by_status(self, repo):
        repo.save(_make_session("s-1", status="completed"))
        repo.save(_make_session("s-2", status="aborted"))

        results = repo.find(status="aborted")
        assert len(results) == 1
        assert results[0].session_id == "s-2"

    def test_find_by_date_range(self, repo):
        repo.save(
            _make_session(
                "s-old",
                started_at=datetime(2026, 4, 10, 10, 0, 0, tzinfo=UTC),
            )
        )
        repo.save(
            _make_session(
                "s-mid",
                started_at=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
            )
        )
        repo.save(
            _make_session(
                "s-new",
                started_at=datetime(2026, 4, 18, 10, 0, 0, tzinfo=UTC),
            )
        )

        results = repo.find(
            start_date=datetime(2026, 4, 12, 0, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 4, 16, 0, 0, 0, tzinfo=UTC),
        )
        assert len(results) == 1
        assert results[0].session_id == "s-mid"

    def test_find_ordered_desc_by_started_at(self, repo):
        repo.save(
            _make_session(
                "s-old",
                started_at=datetime(2026, 4, 10, 10, 0, 0, tzinfo=UTC),
            )
        )
        repo.save(
            _make_session(
                "s-new",
                started_at=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
            )
        )

        results = repo.find()
        assert results[0].session_id == "s-new"
        assert results[1].session_id == "s-old"

    def test_find_respects_limit_and_offset(self, repo):
        for i in range(5):
            repo.save(
                _make_session(
                    f"s-{i:02d}",
                    started_at=datetime(2026, 4, 14, 10, i, 0, tzinfo=UTC),
                )
            )

        results = repo.find(limit=2, offset=1)
        assert len(results) == 2

    def test_find_combined_filters(self, repo):
        repo.save(_make_session("s-match", namespace="payment", status="completed"))
        repo.save(
            _make_session("s-wrong-ns", namespace="notification", status="completed")
        )
        repo.save(_make_session("s-wrong-st", namespace="payment", status="aborted"))

        results = repo.find(namespace="payment", status="completed")
        assert len(results) == 1
        assert results[0].session_id == "s-match"


class TestSQLRecoverySessionCountBehavior:
    """count() aggregation."""

    def test_count_all(self, repo):
        repo.save(_make_session("s-1"))
        repo.save(_make_session("s-2"))
        assert repo.count() == 2

    def test_count_with_namespace_filter(self, repo):
        repo.save(_make_session("s-1", namespace="payment"))
        repo.save(_make_session("s-2", namespace="notification"))
        assert repo.count(namespace="payment") == 1

    def test_count_with_status_filter(self, repo):
        repo.save(_make_session("s-1", status="completed"))
        repo.save(_make_session("s-2", status="aborted"))
        repo.save(_make_session("s-3", status="completed"))
        assert repo.count(status="completed") == 2


class TestSQLRecoverySessionUpdateBehavior:
    """update() full-record replacement."""

    def test_update_modifies_all_columns(self, repo):
        repo.save(_make_session("sess-001", status="started"))

        fetched = repo.get_by_session_id("sess-001")
        fetched.status = "completed"
        fetched.duration_seconds = 600.0
        fetched.abort_reason = ""
        fetched.steps_data.append({"step": "verify", "result": "ok"})

        ok = repo.update(fetched)
        assert ok is True

        updated = repo.get_by_session_id("sess-001")
        assert updated.status == "completed"
        assert updated.duration_seconds == 600.0
        assert len(updated.steps_data) == 3
        assert updated.updated_at >= fetched.updated_at

    def test_update_returns_false_for_missing_session(self, repo):
        session = _make_session("nonexistent")
        assert repo.update(session) is False

    def test_update_preserves_json_fields_on_status_change(self, repo):
        repo.save(_make_session("sess-001"))
        fetched = repo.get_by_session_id("sess-001")
        fetched.status = "aborted"
        fetched.abort_reason = "timeout"
        repo.update(fetched)

        updated = repo.get_by_session_id("sess-001")
        assert updated.status == "aborted"
        assert updated.abort_reason == "timeout"
        assert updated.steps_data == [
            {"step": "diagnose", "result": "ok"},
            {"step": "fix", "result": "ok"},
        ]
        assert updated.metadata == {"trace_id": "t-001", "source": "auto"}


class TestSQLRecoverySessionDeleteBehavior:
    """delete_older_than() lifecycle."""

    def test_delete_older_than_removes_old_sessions(self, repo):
        repo.save(
            _make_session(
                "s-old",
                started_at=datetime(2026, 4, 10, 10, 0, 0, tzinfo=UTC),
            )
        )
        repo.save(
            _make_session(
                "s-new",
                started_at=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
            )
        )

        cutoff = datetime(2026, 4, 12, 0, 0, 0, tzinfo=UTC)
        deleted = repo.delete_older_than(cutoff)
        assert deleted == 1
        assert repo.get_by_session_id("s-old") is None
        assert repo.get_by_session_id("s-new") is not None

    def test_delete_older_than_returns_zero_when_nothing_matches(self, repo):
        repo.save(
            _make_session(
                "s-new",
                started_at=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
            )
        )
        cutoff = datetime(2026, 4, 10, 0, 0, 0, tzinfo=UTC)
        assert repo.delete_older_than(cutoff) == 0
