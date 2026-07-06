"""
Unit tests for SQLFailedOperationRepository (DLQ).

Coverage:
- create / get_by_id round-trip preserves JSON-dominant DTO fields.
- get_pending_* indexed lookups return domain-scoped results.
- try_acquire_for_replay state transition + rowcount-based race guard.
- complete_replay success/failure branches.
- archive_old_resolved + purge_archived lifecycle with time advancement.
- release_stale_replaying resets orphaned REPLAYING entries.
- Size-limit overflow helpers (count_all excludes terminal states).
- Statistics aggregation.
- PR2 review fixes:
    #2 find_sla_breached per-domain SQL,
    #4 try_acquire_for_replay atomic UPDATE+SELECT,
    #6 bulk_update_status single IN-list UPDATE,
    #9 purge_archived([]) early return,
    #11 expires_at dead index removed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from baldur.adapters.sql.failed_operation import (
    SQLFailedOperationRepository,
    _ddl,
)
from baldur.interfaces.repositories import (
    DLQCompressedEntry,
    FailedOperationStatus,
)
from baldur.settings.sql import SQLDialect
from baldur.utils.time import utc_now
from tests.factories.time_helpers import freeze_time


@pytest.fixture
def dlq(get_sqlite_conn) -> SQLFailedOperationRepository:
    return SQLFailedOperationRepository(get_sqlite_conn)


class TestSQLFailedOperationCrudBehavior:
    """create / get_by_id round-trip + indexed lookups."""

    def test_create_returns_pending_entry_with_generated_id(self, dlq):
        entry = dlq.create(domain="payment", failure_type="timeout")
        # 538 D1: opaque-string id; the SQL adapter keeps a dense int PK.
        assert int(entry.id) > 0
        assert entry.status == FailedOperationStatus.PENDING.value

    def test_get_by_id_round_trips_dto_fields(self, dlq):
        created = dlq.create(
            domain="payment",
            failure_type="timeout",
            error_message="gateway timed out",
            error_code="E_TIMEOUT",
            entity_type="order",
            entity_id="42",
            entity_refs={"order": 42, "user": 7},
            snapshot_data={"amount": 100},
            request_data={"method": "POST"},
            response_data={"status": 504},
            metadata={"trace_id": "abc"},
            max_retries=5,
            recommended_action="retry_later",
        )
        fetched = dlq.get_by_id(created.id)
        assert fetched is not None
        assert fetched.domain == "payment"
        assert fetched.failure_type == "timeout"
        assert fetched.error_message == "gateway timed out"
        assert fetched.error_code == "E_TIMEOUT"
        assert fetched.entity_type == "order"
        assert fetched.entity_id == "42"
        assert fetched.entity_refs == {"order": 42, "user": 7}
        assert fetched.snapshot_data == {"amount": 100}
        assert fetched.request_data == {"method": "POST"}
        assert fetched.response_data == {"status": 504}
        assert fetched.metadata == {"trace_id": "abc"}
        assert fetched.max_retries == 5
        assert fetched.recommended_action == "retry_later"

    def test_get_by_id_returns_none_for_missing_id(self, dlq):
        assert dlq.get_by_id(9999) is None

    def test_pending_by_domain_returns_only_matching_pending(self, dlq):
        dlq.create(domain="payment", failure_type="timeout")
        dlq.create(domain="payment", failure_type="http_5xx")
        dlq.create(domain="notification", failure_type="smtp")

        rows = dlq.get_pending_by_domain("payment")
        assert len(rows) == 2
        assert all(r.domain == "payment" for r in rows)

    def test_pending_count_by_domain(self, dlq):
        dlq.create(domain="payment", failure_type="timeout")
        dlq.create(domain="payment", failure_type="http_5xx")
        assert dlq.get_pending_count_by_domain("payment") == 2
        assert dlq.get_pending_count_by_domain("absent") == 0


class TestSQLFailedOperationAtomicAcquireBehavior:
    """try_acquire_for_replay state transition + concurrency guard."""

    def test_acquire_transitions_pending_to_replaying(self, dlq):
        entry = dlq.create(domain="payment", failure_type="timeout")
        acquired = dlq.try_acquire_for_replay(entry.id, max_retries=3)
        assert acquired is not None
        assert acquired.status == FailedOperationStatus.REPLAYING.value
        assert acquired.retry_count == 1

    def test_second_acquire_returns_none_after_first_wins(self, dlq):
        """Rowcount-based guard rejects the second claimant."""
        entry = dlq.create(domain="payment", failure_type="timeout")
        first = dlq.try_acquire_for_replay(entry.id, max_retries=3)
        second = dlq.try_acquire_for_replay(entry.id, max_retries=3)
        assert first is not None
        assert second is None

    def test_acquire_rejects_when_retry_budget_exhausted(self, dlq):
        entry = dlq.create(domain="payment", failure_type="timeout", max_retries=1)
        # Bump retry_count to the cap via the public mutator.
        dlq.increment_retry_count(entry.id)
        assert dlq.try_acquire_for_replay(entry.id, max_retries=1) is None

    def test_acquire_missing_entry_returns_none(self, dlq):
        assert dlq.try_acquire_for_replay(9999, max_retries=3) is None


class TestSQLFailedOperationCompleteReplayBehavior:
    """complete_replay success → RESOLVED; failure → PENDING."""

    def test_success_marks_resolved(self, dlq):
        entry = dlq.create(domain="payment", failure_type="timeout")
        dlq.try_acquire_for_replay(entry.id, max_retries=3)
        ok = dlq.complete_replay(entry.id, success=True, resolution_type="manual_fix")
        assert ok is True
        fetched = dlq.get_by_id(entry.id)
        assert fetched.status == FailedOperationStatus.RESOLVED.value
        assert fetched.resolution_type == "manual_fix"
        assert fetched.resolved_at is not None

    def test_failure_reverts_to_pending(self, dlq):
        entry = dlq.create(domain="payment", failure_type="timeout")
        dlq.try_acquire_for_replay(entry.id, max_retries=3)
        ok = dlq.complete_replay(
            entry.id,
            success=False,
            note="still failing",
            error_details={"last_error": "boom"},
        )
        assert ok is True
        fetched = dlq.get_by_id(entry.id)
        assert fetched.status == FailedOperationStatus.PENDING.value
        assert fetched.error_message == "still failing"
        assert fetched.metadata.get("last_error") == "boom"

    def test_failure_at_cap_marks_requires_review(self, dlq):
        """At cap (retry_count >= max_retries) a failed replay converges to
        REQUIRES_REVIEW instead of reverting to PENDING (606 D7)."""
        entry = dlq.create(domain="payment", failure_type="timeout", max_retries=1)
        # Acquire bumps retry_count 0 -> 1, reaching the cap of 1.
        dlq.try_acquire_for_replay(entry.id, max_retries=1)
        ok = dlq.complete_replay(entry.id, success=False, note="poison pill")
        assert ok is True
        fetched = dlq.get_by_id(entry.id)
        assert fetched.status == FailedOperationStatus.REQUIRES_REVIEW.value
        assert fetched.retry_count == 1

    def test_complete_replay_missing_entry_returns_false(self, dlq):
        assert dlq.complete_replay(9999, success=True) is False


class TestSQLFailedOperationLifecycleBehavior:
    """archive_old_resolved + purge_archived lifecycle."""

    def test_archive_moves_old_resolved_entries(self, dlq):
        """Entries resolved before the cutoff transition to ARCHIVED."""
        frozen_resolved = "2026-02-10 10:00:00"
        now = "2026-04-14 10:00:00"  # 63 days later

        with freeze_time(frozen_resolved):
            e = dlq.create(domain="payment", failure_type="timeout")
            dlq.mark_as_resolved(e.id, resolution_type="manual_fix")

        with freeze_time(now):
            archived = dlq.archive_old_resolved(older_than_days=30)

        assert archived == 1
        fetched = dlq.get_by_id(e.id)
        assert fetched.status == FailedOperationStatus.ARCHIVED.value

    def test_archive_skips_recent_resolved_entries(self, dlq):
        e = dlq.create(domain="payment", failure_type="timeout")
        dlq.mark_as_resolved(e.id, resolution_type="manual_fix")
        # Cutoff is very far in the past → nothing archived.
        assert dlq.archive_old_resolved(older_than_days=9999) == 0

    def test_purge_archived_by_ids_rejects_non_archived(self, dlq):
        """ValueError contract parity with memory adapter."""
        e = dlq.create(domain="payment", failure_type="timeout")
        with pytest.raises(ValueError):
            dlq.purge_archived(ids=[e.id])

    def test_purge_archived_deletes_archived_rows(self, dlq):
        e = dlq.create(domain="payment", failure_type="timeout")
        dlq.mark_as_resolved(e.id, resolution_type="x")
        dlq.archive_old_resolved(
            older_than_days=-1
        )  # cutoff in the future → archive now
        assert dlq.get_by_id(e.id).status == FailedOperationStatus.ARCHIVED.value

        purged = dlq.purge_archived(ids=[e.id])
        assert purged == 1
        assert dlq.get_by_id(e.id) is None


class TestSQLFailedOperationStaleReplayingBehavior:
    """release_stale_replaying reverts abandoned REPLAYING rows."""

    def test_stale_replaying_entries_are_reset_to_pending(self, dlq):
        with freeze_time("2026-04-14 10:00:00"):
            e = dlq.create(domain="payment", failure_type="timeout")
            dlq.try_acquire_for_replay(e.id, max_retries=3)

        # 45 minutes later — above the 30-minute default threshold.
        with freeze_time("2026-04-14 10:45:00"):
            released = dlq.release_stale_replaying(older_than_minutes=30)

        assert released == 1
        fetched = dlq.get_by_id(e.id)
        assert fetched.status == FailedOperationStatus.PENDING.value

    def test_fresh_replaying_entries_are_left_alone(self, dlq):
        e = dlq.create(domain="payment", failure_type="timeout")
        dlq.try_acquire_for_replay(e.id, max_retries=3)
        # Under the threshold.
        released = dlq.release_stale_replaying(older_than_minutes=60)
        assert released == 0
        assert dlq.get_by_id(e.id).status == FailedOperationStatus.REPLAYING.value


class TestSQLFailedOperationSizeLimitBehavior:
    """count_all / get_oldest_ids / evict_oldest parity with memory adapter."""

    def test_count_all_excludes_resolved_and_archived(self, dlq):
        active = dlq.create(domain="payment", failure_type="timeout")
        resolved = dlq.create(domain="payment", failure_type="http_5xx")
        dlq.mark_as_resolved(resolved.id, resolution_type="x")
        assert dlq.count_all() == 1
        assert dlq.get_by_id(active.id).status == FailedOperationStatus.PENDING.value

    def test_get_oldest_ids_orders_by_created_at(self, dlq):
        with freeze_time("2026-04-14 10:00:00"):
            first = dlq.create(domain="payment", failure_type="t1")
        with freeze_time("2026-04-14 10:05:00"):
            second = dlq.create(domain="payment", failure_type="t2")
        with freeze_time("2026-04-14 10:10:00"):
            third = dlq.create(domain="payment", failure_type="t3")

        oldest_two = dlq.get_oldest_ids(count=2)
        assert oldest_two == [first.id, second.id]
        _ = third  # noqa: F841 — keep the binding obvious

    def test_evict_oldest_removes_n_entries(self, dlq):
        with freeze_time("2026-04-14 10:00:00"):
            first = dlq.create(domain="payment", failure_type="t1")
        with freeze_time("2026-04-14 10:05:00"):
            second = dlq.create(domain="payment", failure_type="t2")

        assert dlq.evict_oldest(count=1) == 1
        assert dlq.get_by_id(first.id) is None
        assert dlq.get_by_id(second.id) is not None


class TestSQLFailedOperationStatisticsBehavior:
    """get_statistics aggregates by status/domain."""

    def test_statistics_includes_breakdowns(self, dlq):
        dlq.create(domain="payment", failure_type="timeout")
        dlq.create(domain="payment", failure_type="timeout")
        dlq.create(domain="notification", failure_type="smtp")

        stats = dlq.get_statistics()
        assert stats["total"] == 3
        pending = FailedOperationStatus.PENDING.value
        assert stats["by_status"][pending] == 3
        assert stats["by_domain"]["payment"] == 2
        assert stats["by_domain"]["notification"] == 1
        assert stats["pending_by_domain"]["payment"] == 2
        assert stats["pending_by_domain_and_failure_type"]["payment"]["timeout"] == 2


class TestSQLFailedOperationCompressionBehavior:
    """DLQCompressedEntry store/query round-trip."""

    def test_store_and_query_compressed_entry(self, dlq):
        now = utc_now()
        entry = DLQCompressedEntry(
            id="compressed:payment:timeout:E_X:123",
            domain="payment",
            failure_type="timeout",
            error_code="E_X",
            count=42,
            first_seen=now - timedelta(hours=1),
            last_seen=now,
            sample_error_message="gateway timed out",
        )
        assert dlq.store_compressed_entry(entry) is True

        rows = dlq.get_compressed_entries(domain="payment")
        assert len(rows) == 1
        assert rows[0].count == 42
        assert rows[0].sample_error_message == "gateway timed out"

    def test_update_compressed_status_transitions_state(self, dlq):
        entry = DLQCompressedEntry(
            id="compressed:payment:timeout:E_X:124",
            domain="payment",
            failure_type="timeout",
            error_code="E_X",
            count=1,
            first_seen=utc_now(),
            last_seen=utc_now(),
            sample_error_message="x",
        )
        dlq.store_compressed_entry(entry)
        dlq.update_compressed_status(entry.id, "stale")
        rows = dlq.get_compressed_entries(status="stale")
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# PR2 review fix #2 — find_sla_breached per-domain SQL
# ---------------------------------------------------------------------------


class TestFindSlaBreachedPerDomainBehavior:
    """Per-domain index seek instead of full PENDING scan."""

    def test_returns_breached_in_explicit_domain(self, dlq):
        """Domain present in thresholds — uses its specific cutoff."""
        # Old entry — created 2h ago.
        with freeze_time("2026-04-14 08:00:00"):
            old = dlq.create(domain="payment", failure_type="timeout")
        # Fresh entry — created just now (within threshold).
        with freeze_time("2026-04-14 09:55:00"):
            dlq.create(domain="payment", failure_type="timeout")

        with freeze_time("2026-04-14 10:00:00"):
            breached = dlq.find_sla_breached(
                current_time=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
                sla_thresholds={"payment": timedelta(hours=1)},
            )

        assert [e.id for e in breached] == [old.id]

    def test_uses_default_threshold_for_unlisted_domains(self, dlq):
        """Domain absent from thresholds — falls back to 24h default."""
        # 25h-old entry in an unlisted domain — beyond default.
        with freeze_time("2026-04-13 09:00:00"):
            ancient = dlq.create(domain="notification", failure_type="smtp")
        # 23h-old entry — within default.
        with freeze_time("2026-04-13 11:00:00"):
            dlq.create(domain="notification", failure_type="smtp")

        with freeze_time("2026-04-14 10:00:00"):
            breached = dlq.find_sla_breached(
                current_time=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
                sla_thresholds={"payment": timedelta(hours=1)},
            )

        assert [e.id for e in breached] == [ancient.id]

    def test_empty_thresholds_apply_default_to_all_pending(self, dlq):
        """Empty dict — every PENDING domain gauged against the default."""
        # Old enough to breach the default.
        with freeze_time("2026-04-13 09:00:00"):
            ancient_a = dlq.create(domain="payment", failure_type="t1")
            ancient_b = dlq.create(domain="notification", failure_type="t2")
        # Fresh — under default.
        with freeze_time("2026-04-14 09:00:00"):
            dlq.create(domain="payment", failure_type="t3")

        with freeze_time("2026-04-14 10:00:00"):
            breached = dlq.find_sla_breached(
                current_time=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
                sla_thresholds={},
            )

        assert sorted(e.id for e in breached) == sorted([ancient_a.id, ancient_b.id])

    def test_does_not_load_full_pending_set(self, dlq, monkeypatch):
        """Per-domain queries — never a single ``WHERE status = 'pending'`` scan."""
        for _ in range(3):
            dlq.create(domain="payment", failure_type="t")

        captured_sql: list[str] = []
        original_fetch_all = dlq._fetch_all

        def _spy(sql, params=()):
            captured_sql.append(sql)
            return original_fetch_all(sql, params)

        monkeypatch.setattr(dlq, "_fetch_all", _spy)

        dlq.find_sla_breached(
            current_time=datetime(2026, 4, 14, 10, 0, 0, tzinfo=UTC),
            sla_thresholds={"payment": timedelta(hours=1)},
        )

        # Every captured query must constrain by ``created_at`` — proving
        # the old "load everything PENDING" path is gone.
        for sql in captured_sql:
            assert "created_at" in sql


# ---------------------------------------------------------------------------
# PR2 review fix #4 — try_acquire_for_replay atomic UPDATE+SELECT
# ---------------------------------------------------------------------------


class TestTryAcquireAtomicScopeBehavior:
    """UPDATE + SELECT live inside one ``sql_transaction`` scope."""

    def test_acquire_returns_dto_on_success(self, dlq):
        """Happy path still returns the REPLAYING DTO."""
        e = dlq.create(domain="payment", failure_type="timeout")
        acquired = dlq.try_acquire_for_replay(e.id, max_retries=3)
        assert acquired is not None
        assert acquired.status == FailedOperationStatus.REPLAYING.value
        assert acquired.retry_count == 1

    def test_acquire_returns_none_when_row_disappears_intra_scope(
        self, dlq, monkeypatch
    ):
        """Concurrent delete simulated: SELECT inside scope finds nothing → None."""
        # Patch the SELECT-after-UPDATE path so cursor.fetchone returns None,
        # mimicking a row deleted between our UPDATE commit and same-txn read.
        original_borrow = dlq._borrow_connection

        class _ScrubbedConn:
            def __init__(self, real_conn):
                self._real = real_conn

            def cursor(self):
                real_cursor = self._real.cursor()
                wrapper = MagicMock(wraps=real_cursor)
                # Force fetchone to None (rows vanished between UPDATE+SELECT)
                wrapper.fetchone.return_value = None
                # Mark the UPDATE rowcount as 1 so we go past the early return.
                # The actual rowcount property is read after execute(), so we
                # need execute() to leave a fake rowcount on the wrapper.
                # MagicMock keeps the wrapped cursor.execute working; rowcount
                # is read from wrapper directly.
                wrapper.rowcount = 1
                return wrapper

            def __getattr__(self, name):
                return getattr(self._real, name)

        e = dlq.create(domain="payment", failure_type="timeout")

        def _patched_borrow():
            return _ScrubbedConn(original_borrow())

        monkeypatch.setattr(dlq, "_borrow_connection", _patched_borrow)

        result = dlq.try_acquire_for_replay(e.id, max_retries=3)
        assert result is None

    def test_acquire_uses_sql_transaction_scope(self, dlq, monkeypatch):
        """``sql_transaction`` is entered once during acquire."""
        e = dlq.create(domain="payment", failure_type="timeout")

        from baldur.adapters.sql import failed_operation as fo_mod

        spy_calls: list[int] = []
        original_txn = fo_mod.sql_transaction

        def _spy_txn(conn):
            spy_calls.append(id(conn))
            return original_txn(conn)

        monkeypatch.setattr(fo_mod, "sql_transaction", _spy_txn)

        dlq.try_acquire_for_replay(e.id, max_retries=3)
        assert len(spy_calls) == 1


# ---------------------------------------------------------------------------
# PR2 review fix #6 — bulk_update_status single IN-list UPDATE
# ---------------------------------------------------------------------------


class TestBulkUpdateStatusSingleQueryBehavior:
    """One UPDATE ... WHERE id IN (...) regardless of id count."""

    def test_resolved_status_sets_resolved_at(self, dlq):
        """RESOLVED → resolved_at populated for every row."""
        ids = [dlq.create(domain="payment", failure_type="t").id for _ in range(3)]

        updated = dlq.bulk_update_status(ids, FailedOperationStatus.RESOLVED.value)

        assert updated == 3
        for entry_id in ids:
            row = dlq.get_by_id(entry_id)
            assert row.status == FailedOperationStatus.RESOLVED.value
            assert row.resolved_at is not None

    def test_non_resolved_status_does_not_touch_resolved_at(self, dlq):
        """Non-RESOLVED → resolved_at left untouched (stays None)."""
        ids = [dlq.create(domain="payment", failure_type="t").id for _ in range(2)]

        dlq.bulk_update_status(ids, FailedOperationStatus.REJECTED.value)

        for entry_id in ids:
            row = dlq.get_by_id(entry_id)
            assert row.status == FailedOperationStatus.REJECTED.value
            assert row.resolved_at is None

    def test_empty_id_list_returns_zero(self, dlq):
        """Empty input — zero rows updated, no SQL round-trip."""
        assert dlq.bulk_update_status([], FailedOperationStatus.RESOLVED.value) == 0

    def test_single_sql_round_trip_for_n_ids(self, dlq, monkeypatch):
        """N ids → one cursor.execute call (proves N+1 elimination)."""
        ids = [dlq.create(domain="payment", failure_type="t").id for _ in range(5)]

        # Monkey-patch the connection borrow path so we count cursor.execute
        # against a single recording cursor across all 5 entries.
        original_borrow = dlq._borrow_connection
        execute_log: list[str] = []

        class _RecordingCursor:
            def __init__(self, real_cursor):
                self._real = real_cursor
                self.rowcount = 0

            def execute(self, sql, params=()):
                execute_log.append(sql)
                self._real.execute(sql, params)
                self.rowcount = self._real.rowcount

            def __getattr__(self, name):
                return getattr(self._real, name)

        class _RecordingConn:
            def __init__(self, real_conn):
                self._real = real_conn

            def cursor(self):
                return _RecordingCursor(self._real.cursor())

            def __getattr__(self, name):
                return getattr(self._real, name)

        monkeypatch.setattr(
            dlq, "_borrow_connection", lambda: _RecordingConn(original_borrow())
        )

        dlq.bulk_update_status(ids, FailedOperationStatus.RESOLVED.value)

        # Exactly one UPDATE — not 5 (no N+1).
        update_sqls = [
            s for s in execute_log if s.lstrip().upper().startswith("UPDATE")
        ]
        assert len(update_sqls) == 1


# ---------------------------------------------------------------------------
# PR2 review fix #9 — purge_archived([]) early return
# ---------------------------------------------------------------------------


class TestPurgeArchivedEmptyIdsBehavior:
    """``purge_archived(ids=[])`` short-circuits before any DB I/O."""

    def test_returns_zero_for_empty_ids(self, dlq):
        assert dlq.purge_archived(ids=[]) == 0

    def test_no_connection_borrowed_for_empty_ids(self, dlq, monkeypatch):
        """Empty list never reaches ``_borrow_connection``."""
        borrowed: list[int] = []
        monkeypatch.setattr(
            dlq,
            "_borrow_connection",
            lambda: borrowed.append(1) or pytest.fail("should not borrow"),
        )
        assert dlq.purge_archived(ids=[]) == 0
        assert borrowed == []

    def test_both_filters_still_rejected(self, dlq):
        """Mutually exclusive contract preserved."""
        with pytest.raises(ValueError):
            dlq.purge_archived(ids=[1], older_than_days=30)


# ---------------------------------------------------------------------------
# PR2 review fix #11 — expires_at dead index removed
# ---------------------------------------------------------------------------


class TestDdlExpiresAtIndexContract:
    """DDL no longer creates the unused expires_at index."""

    @pytest.mark.parametrize(
        "dialect",
        [SQLDialect.POSTGRESQL, SQLDialect.MYSQL, SQLDialect.SQLITE],
    )
    def test_no_expires_at_index_in_ddl(self, dialect):
        """Across all dialects the expires_at index DDL is absent."""
        for stmt in _ddl(dialect):
            assert "idx_baldur_dlq_expires_at" not in stmt

    @pytest.mark.parametrize(
        "dialect",
        [SQLDialect.POSTGRESQL, SQLDialect.MYSQL, SQLDialect.SQLITE],
    )
    def test_expires_at_column_still_present(self, dialect):
        """The column itself remains for DTO parity with other adapters."""
        ddl_text = "\n".join(_ddl(dialect))
        assert "expires_at" in ddl_text


class TestSQLCreateExpiresAtBehavior:
    """Behavior: create() accepts expires_at and persists it."""

    def test_create_with_expires_at_round_trips(self, dlq):
        """expires_at value survives create → get_by_id round-trip."""
        expires = datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)
        entry = dlq.create(
            domain="payment",
            failure_type="timeout",
            expires_at=expires,
        )

        fetched = dlq.get_by_id(entry.id)
        assert fetched.expires_at is not None
        assert abs((fetched.expires_at - expires).total_seconds()) < 2

    def test_create_without_expires_at_stores_none(self, dlq):
        """Omitting expires_at stores NULL."""
        entry = dlq.create(domain="payment", failure_type="timeout")

        fetched = dlq.get_by_id(entry.id)
        assert fetched.expires_at is None


# ---------------------------------------------------------------------------
# 541 D3 — paginated find() / count() + _build_filter_clauses
# ---------------------------------------------------------------------------


class TestSQLBuildFilterClausesBehavior:
    """_build_filter_clauses assembles WHERE text + %s params per dimension."""

    def test_no_filters_yields_empty_where_and_params(self, dlq):
        where, params = dlq._build_filter_clauses()
        assert where == ""
        assert params == []

    def test_single_status_filter(self, dlq):
        where, params = dlq._build_filter_clauses(status="pending")
        assert where == " WHERE status = %s"
        assert params == ["pending"]

    def test_all_three_filters_joined_with_and(self, dlq):
        where, params = dlq._build_filter_clauses(
            status="pending", domain="payment", failure_type="timeout"
        )
        assert where == " WHERE status = %s AND domain = %s AND failure_type = %s"
        assert params == ["pending", "payment", "timeout"]

    def test_explicit_none_dimension_is_omitted(self, dlq):
        """A None dimension contributes no clause and no param."""
        where, params = dlq._build_filter_clauses(status=None, domain="payment")
        assert where == " WHERE domain = %s"
        assert params == ["payment"]


class TestSQLFindBehavior:
    """find() — created_at DESC, LIMIT/OFFSET pagination, filter routing."""

    def test_find_no_filter_spans_all_statuses_newest_first(self, dlq):
        """Empty WHERE returns every status, ordered created_at DESC (541 D5)."""
        with freeze_time("2026-01-01 10:00:00"):
            oldest = dlq.create(domain="payment", failure_type="timeout")
        with freeze_time("2026-01-02 10:00:00"):
            middle = dlq.create(domain="auth", failure_type="http_5xx")
        with freeze_time("2026-01-03 10:00:00"):
            newest = dlq.create(domain="inventory", failure_type="timeout")

        # Move the escalated/terminal statuses off PENDING — still visible.
        dlq.bulk_update_status(
            [middle.id], FailedOperationStatus.PERMANENTLY_FAILED.value
        )
        dlq.bulk_update_status([newest.id], FailedOperationStatus.REQUIRES_REVIEW.value)

        results = dlq.find()

        assert [e.id for e in results] == [newest.id, middle.id, oldest.id]

    def test_find_status_filter_returns_only_that_status(self, dlq):
        pending = dlq.create(domain="payment", failure_type="timeout")
        resolved = dlq.create(domain="payment", failure_type="timeout")
        dlq.mark_as_resolved(resolved.id, resolution_type="x")

        results = dlq.find(status=FailedOperationStatus.PENDING.value)

        assert [e.id for e in results] == [pending.id]

    def test_find_explicit_none_status_with_domain_returns_all_statuses_in_domain(
        self, dlq
    ):
        """find(status=None, domain=...) emits only the domain clause."""
        p1 = dlq.create(domain="payment", failure_type="timeout")
        p2 = dlq.create(domain="payment", failure_type="timeout")
        dlq.mark_as_resolved(p2.id, resolution_type="x")
        dlq.create(domain="auth", failure_type="timeout")

        results = dlq.find(status=None, domain="payment")

        assert {e.id for e in results} == {p1.id, p2.id}

    def test_find_combo_status_and_domain(self, dlq):
        match = dlq.create(domain="payment", failure_type="timeout")
        dlq.create(domain="payment", failure_type="timeout")  # stays PENDING
        other = dlq.create(domain="auth", failure_type="timeout")
        dlq.mark_as_resolved(match.id, resolution_type="x")
        dlq.mark_as_resolved(other.id, resolution_type="x")

        results = dlq.find(
            status=FailedOperationStatus.RESOLVED.value, domain="payment"
        )

        assert [e.id for e in results] == [match.id]

    def test_find_failure_type_filter(self, dlq):
        timeout = dlq.create(domain="payment", failure_type="timeout")
        dlq.create(domain="payment", failure_type="http_5xx")

        results = dlq.find(domain="payment", failure_type="timeout")

        assert [e.id for e in results] == [timeout.id]

    def test_find_orders_by_created_at_desc(self, dlq):
        with freeze_time("2026-01-01 10:00:00"):
            first = dlq.create(domain="payment", failure_type="t1")
        with freeze_time("2026-01-02 10:00:00"):
            second = dlq.create(domain="payment", failure_type="t2")
        with freeze_time("2026-01-03 10:00:00"):
            third = dlq.create(domain="payment", failure_type="t3")

        results = dlq.find()

        assert [e.id for e in results] == [third.id, second.id, first.id]

    def test_find_limit_offset_paginate(self, dlq):
        ids = []
        for i in range(5):
            with freeze_time(f"2026-01-0{i + 1} 10:00:00"):
                ids.append(dlq.create(domain="payment", failure_type="t").id)

        # Newest-first ids are reversed creation order.
        newest_first = list(reversed(ids))

        page1 = dlq.find(offset=0, limit=2)
        page2 = dlq.find(offset=2, limit=2)
        page3 = dlq.find(offset=4, limit=2)

        assert [e.id for e in page1] == newest_first[0:2]
        assert [e.id for e in page2] == newest_first[2:4]
        assert [e.id for e in page3] == newest_first[4:5]  # partial last page

    def test_find_offset_beyond_count_returns_empty(self, dlq):
        dlq.create(domain="payment", failure_type="timeout")
        assert dlq.find(offset=10, limit=10) == []


class TestSQLCountBehavior:
    """count() — filter routing parity with find(), no LIMIT/OFFSET."""

    def test_count_no_filter_counts_all_statuses(self, dlq):
        a = dlq.create(domain="payment", failure_type="timeout")
        dlq.create(domain="auth", failure_type="http_5xx")
        dlq.bulk_update_status([a.id], FailedOperationStatus.PERMANENTLY_FAILED.value)

        assert dlq.count() == 2

    def test_count_status_filter(self, dlq):
        dlq.create(domain="payment", failure_type="timeout")
        resolved = dlq.create(domain="payment", failure_type="timeout")
        dlq.mark_as_resolved(resolved.id, resolution_type="x")

        assert dlq.count(status=FailedOperationStatus.PENDING.value) == 1

    def test_count_combo_filters(self, dlq):
        dlq.create(domain="payment", failure_type="timeout")
        dlq.create(domain="payment", failure_type="http_5xx")
        dlq.create(domain="auth", failure_type="timeout")

        assert dlq.count(domain="payment", failure_type="timeout") == 1

    def test_count_is_independent_of_pagination(self, dlq):
        for _ in range(5):
            dlq.create(domain="payment", failure_type="t")

        assert dlq.count(status=FailedOperationStatus.PENDING.value) == 5
        assert len(dlq.find(status=FailedOperationStatus.PENDING.value, limit=2)) == 2

    def test_count_empty_table_returns_zero(self, dlq):
        assert dlq.count() == 0


class TestSQLCountArchivedOlderThanBehavior:
    """Behavior: count_archived_older_than uses SQL COUNT for efficiency."""

    def test_count_zero_when_no_archived_entries(self, dlq):
        """Empty table returns 0."""
        assert dlq.count_archived_older_than(30) == 0

    def test_count_includes_old_archived_entries(self, dlq):
        """Archived entries resolved long ago are counted."""
        with freeze_time("2026-01-01 00:00:00"):
            entry = dlq.create(domain="payment", failure_type="timeout")
            dlq.mark_as_resolved(entry.id, "auto_resolved")

        with freeze_time("2026-01-02 00:00:00"):
            dlq.archive_old_resolved(older_than_days=0)

        with freeze_time("2026-09-01 00:00:00"):
            assert dlq.count_archived_older_than(30) == 1

    def test_count_excludes_recent_archived_entries(self, dlq):
        """Recently archived entries are not counted."""
        entry = dlq.create(domain="payment", failure_type="timeout")
        dlq.mark_as_resolved(entry.id, "auto_resolved")
        dlq.archive_old_resolved(older_than_days=0)

        assert dlq.count_archived_older_than(30) == 0

    def test_count_ignores_non_archived_status(self, dlq):
        """RESOLVED entries are not counted."""
        entry = dlq.create(domain="payment", failure_type="timeout")
        dlq.mark_as_resolved(entry.id, "auto_resolved")

        assert dlq.count_archived_older_than(0) == 0


# Window under test: [start, end] inclusive (parity with the memory adapter).
_WIN_START = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
_WIN_END = datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC)


class TestCountCreatedInWindowSql:
    """count_created_in_window() inclusive BETWEEN range + status-independence (622 D3)."""

    def _seed_at(self, dlq, when: str) -> None:
        with freeze_time(when):
            dlq.create(domain="payment", failure_type="timeout")

    def test_counts_entries_inside_window(self, dlq):
        """Entries created within the window are counted."""
        self._seed_at(dlq, "2026-06-03 12:00:00")
        self._seed_at(dlq, "2026-06-05 09:30:00")

        assert dlq.count_created_in_window(_WIN_START, _WIN_END) == 2

    def test_includes_both_inclusive_boundaries(self, dlq):
        """Entries exactly at the start and end boundaries are both counted."""
        self._seed_at(dlq, "2026-06-01 00:00:00")  # at start
        self._seed_at(dlq, "2026-06-08 00:00:00")  # at end

        assert dlq.count_created_in_window(_WIN_START, _WIN_END) == 2

    def test_excludes_entries_outside_window(self, dlq):
        """Entries just before start / just after end are excluded."""
        self._seed_at(dlq, "2026-05-31 23:59:59")  # before
        self._seed_at(dlq, "2026-06-08 00:00:01")  # after

        assert dlq.count_created_in_window(_WIN_START, _WIN_END) == 0

    def test_empty_table_returns_zero(self, dlq):
        """No rows → zero."""
        assert dlq.count_created_in_window(_WIN_START, _WIN_END) == 0

    def test_counts_across_all_statuses(self, dlq):
        """A resolved entry created in-window still counts (status-independent)."""
        with freeze_time("2026-06-04 10:00:00"):
            entry = dlq.create(domain="payment", failure_type="timeout")
        dlq.mark_as_resolved(entry.id, resolution_type="manual_fix")

        assert dlq.count_created_in_window(_WIN_START, _WIN_END) == 1
