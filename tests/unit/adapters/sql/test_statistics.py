"""
Unit tests for SQLStatisticsRepository.

Coverage:
- DLQ aggregation: status counts, domain/failure distribution, recent
  activity, resolution rate, average retry count.
- DLQ list/detail: paginated list, entry detail with JSON data.
- SLA breach detection.
- Cleanup: cleanup stats, archive old, purge archived.
- CB statistics: summary and list (graceful degradation). Baldur never
  writes ``baldur_cb_state`` — breaker state lives in memory or Redis — so
  these tests seed the table directly, the way an operator's own table or a
  leftover from an older install would present itself.
- Persistence: persist_entry upsert, sync_from_runtime batch.
- Audit trail: get/link audit trail entries.
- Async config: should_persist_async / get_async_persist_task_name.
- Graceful degradation: CB methods return defaults when table missing.

``baldur_dlq`` is bootstrapped by its repository constructor on the shared
sqlite in-memory connection.
"""

from __future__ import annotations

import pytest

from baldur.adapters.sql.failed_operation import SQLFailedOperationRepository
from baldur.adapters.sql.statistics import SQLStatisticsRepository
from baldur.interfaces.repositories import FailedOperationStatus
from baldur.interfaces.statistics import (
    CircuitBreakerSummary,
    CleanupStats,
    PaginatedResult,
)
from baldur.settings.sql import SQLDialect
from tests.factories.time_helpers import freeze_time


@pytest.fixture
def _bootstrap_tables(get_sqlite_conn):
    """Bootstrap the DLQ table by triggering a read on its repository."""
    dlq_repo = SQLFailedOperationRepository(get_sqlite_conn)
    dlq_repo.get_by_id(0)


@pytest.fixture
def dlq(get_sqlite_conn, _bootstrap_tables) -> SQLFailedOperationRepository:
    return SQLFailedOperationRepository(get_sqlite_conn)


@pytest.fixture
def cb_table(get_sqlite_conn, _bootstrap_tables):
    """Create ``baldur_cb_state`` and return a seeder for it.

    No Baldur repository writes this table — breaker state is volatile
    coordination data and stays in memory or Redis. The statistics adapter
    still reads it, because an operator may keep such a table themselves or
    have one left from an older install, so the read path is exercised
    against a table this test populates by hand.
    """
    conn = get_sqlite_conn()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS baldur_cb_state ("
        "service_name TEXT PRIMARY KEY, state TEXT, failure_count INTEGER, "
        "success_count INTEGER, last_failure_at TEXT, updated_at TEXT)"
    )

    def seed(service_name: str, state: str = "closed", failure_count: int = 0):
        conn.execute(
            "INSERT INTO baldur_cb_state "
            "(service_name, state, failure_count, success_count, "
            "last_failure_at, updated_at) VALUES (?, ?, ?, 0, NULL, NULL)",
            (service_name, state, failure_count),
        )

    return seed


@pytest.fixture
def stats(get_sqlite_conn, _bootstrap_tables) -> SQLStatisticsRepository:
    return SQLStatisticsRepository(get_sqlite_conn)


class TestSQLStatisticsStatusCountsBehavior:
    """get_status_counts aggregation over DLQ entries."""

    def test_empty_table_returns_zero_counts(self, stats):
        counts = stats.get_status_counts()
        assert counts.total == 0
        assert counts.pending == 0

    def test_counts_reflect_inserted_entries(self, stats, dlq):
        dlq.create(domain="payment", failure_type="timeout")
        dlq.create(domain="payment", failure_type="http_5xx")

        counts = stats.get_status_counts()
        assert counts.total == 2
        assert counts.pending == 2

    def test_counts_track_resolved_entries(self, stats, dlq):
        e = dlq.create(domain="payment", failure_type="timeout")
        dlq.mark_as_resolved(e.id, resolution_type="manual_fix")

        counts = stats.get_status_counts()
        assert counts.total == 1
        assert counts.resolved == 1
        assert counts.pending == 0


class TestSQLStatisticsDomainDistributionBehavior:
    """get_domain_distribution."""

    def test_empty_table_returns_empty_list(self, stats):
        assert stats.get_domain_distribution() == []

    def test_distribution_calculates_percentages(self, stats, dlq):
        dlq.create(domain="payment", failure_type="timeout")
        dlq.create(domain="payment", failure_type="http_5xx")
        dlq.create(domain="notification", failure_type="smtp")

        dist = stats.get_domain_distribution()
        assert len(dist) == 2
        payment = next(d for d in dist if d.domain == "payment")
        assert payment.count == 2
        assert payment.percentage == pytest.approx(66.67, abs=0.01)

    def test_distribution_respects_limit(self, stats, dlq):
        for i in range(5):
            dlq.create(domain=f"domain-{i}", failure_type="t")
        dist = stats.get_domain_distribution(limit=3)
        assert len(dist) == 3


class TestSQLStatisticsFailureTypeDistributionBehavior:
    """get_failure_type_distribution."""

    def test_distribution_groups_by_failure_type(self, stats, dlq):
        dlq.create(domain="payment", failure_type="timeout")
        dlq.create(domain="payment", failure_type="timeout")
        dlq.create(domain="payment", failure_type="http_5xx")

        dist = stats.get_failure_type_distribution()
        assert len(dist) == 2
        timeout = next(d for d in dist if d.failure_type == "timeout")
        assert timeout.count == 2


class TestSQLStatisticsRecentActivityBehavior:
    """get_recent_activity time-window counts + trend."""

    def test_empty_table_returns_default_activity(self, stats):
        activity = stats.get_recent_activity()
        assert activity.new_in_24h == 0
        assert activity.trend == "stable"

    def test_recent_entries_counted_correctly(self, stats, dlq):
        with freeze_time("2026-04-14 09:00:00"):
            dlq.create(domain="payment", failure_type="timeout")
            dlq.create(domain="payment", failure_type="http_5xx")

        with freeze_time("2026-04-14 10:00:00"):
            activity = stats.get_recent_activity(hours=24)

        assert activity.new_in_24h == 2


class TestSQLStatisticsResolutionRateBehavior:
    """get_resolution_rate."""

    def test_zero_total_returns_zero(self, stats):
        assert stats.get_resolution_rate() == 0.0

    def test_rate_reflects_resolved_ratio(self, stats, dlq):
        e1 = dlq.create(domain="payment", failure_type="timeout")
        dlq.create(domain="payment", failure_type="http_5xx")
        dlq.mark_as_resolved(e1.id, resolution_type="manual_fix")

        rate = stats.get_resolution_rate(days=30)
        assert rate == pytest.approx(0.5, abs=0.01)


class TestSQLStatisticsAvgRetryBehavior:
    """get_avg_retry_count."""

    def test_empty_table_returns_zero(self, stats):
        assert stats.get_avg_retry_count() == 0.0

    def test_avg_reflects_retry_counts(self, stats, dlq):
        e1 = dlq.create(domain="payment", failure_type="timeout")
        dlq.create(domain="payment", failure_type="http_5xx")
        dlq.increment_retry_count(e1.id)
        dlq.increment_retry_count(e1.id)

        avg = stats.get_avg_retry_count()
        assert avg == pytest.approx(1.0, abs=0.01)


class TestSQLStatisticsListEntriesBehavior:
    """list_entries paginated queries."""

    def test_empty_table_returns_empty_result(self, stats):
        result = stats.list_entries()
        assert isinstance(result, PaginatedResult)
        assert result.total == 0
        assert result.items == []

    def test_pagination_works(self, stats, dlq):
        for _ in range(5):
            dlq.create(domain="payment", failure_type="timeout")

        page1 = stats.list_entries(page=1, page_size=2)
        assert len(page1.items) == 2
        assert page1.total == 5
        assert page1.has_next is True
        assert page1.has_prev is False

        page2 = stats.list_entries(page=2, page_size=2)
        assert len(page2.items) == 2
        assert page2.has_prev is True

    def test_filter_by_status(self, stats, dlq):
        e1 = dlq.create(domain="payment", failure_type="timeout")
        dlq.create(domain="payment", failure_type="http_5xx")
        dlq.mark_as_resolved(e1.id, resolution_type="x")

        result = stats.list_entries(status="resolved")
        assert result.total == 1

    def test_filter_by_domain(self, stats, dlq):
        dlq.create(domain="payment", failure_type="timeout")
        dlq.create(domain="notification", failure_type="smtp")

        result = stats.list_entries(domain="payment")
        assert result.total == 1


class TestSQLStatisticsEntryDetailBehavior:
    """get_entry_detail."""

    def test_returns_none_for_missing(self, stats):
        assert stats.get_entry_detail("9999") is None

    def test_returns_full_detail_with_json_data(self, stats, dlq):
        entry = dlq.create(
            domain="payment",
            failure_type="timeout",
            error_message="gateway error",
            snapshot_data={"amount": 100},
            metadata={"trace_id": "abc"},
        )
        detail = stats.get_entry_detail(str(entry.id))
        assert detail is not None
        assert detail["domain"] == "payment"
        assert detail["failure_type"] == "timeout"
        assert detail["error_message"] == "gateway error"
        assert detail["snapshot_data"] == {"amount": 100}


class TestSQLStatisticsSlaBreachesBehavior:
    """get_sla_breaches."""

    def test_empty_table_returns_empty_dict(self, stats):
        assert stats.get_sla_breaches() == {}

    def test_detects_breaches_by_domain(self, stats, dlq):
        with freeze_time("2026-04-14 05:00:00"):
            dlq.create(domain="payment", failure_type="timeout")

        with freeze_time("2026-04-14 10:00:00"):
            breaches = stats.get_sla_breaches(sla_threshold_hours=4)

        assert "payment" in breaches
        assert breaches["payment"] == 1


class TestSQLStatisticsCleanupBehavior:
    """get_cleanup_stats / archive_old_entries / purge_archived."""

    def test_cleanup_stats_empty_table(self, stats):
        cs = stats.get_cleanup_stats()
        assert isinstance(cs, CleanupStats)
        assert cs.total == 0

    def test_cleanup_stats_counts_by_status(self, stats, dlq):
        e = dlq.create(domain="payment", failure_type="timeout")
        dlq.create(domain="payment", failure_type="http_5xx")
        dlq.mark_as_resolved(e.id, resolution_type="x")

        cs = stats.get_cleanup_stats()
        assert cs.total == 2
        assert cs.by_status.get("resolved") == 1
        assert cs.by_status.get("pending") == 1

    def test_archive_old_entries_transitions_resolved(self, stats, dlq):
        with freeze_time("2026-02-10 10:00:00"):
            e = dlq.create(domain="payment", failure_type="timeout")
            dlq.mark_as_resolved(e.id, resolution_type="x")

        with freeze_time("2026-04-14 10:00:00"):
            archived = stats.archive_old_entries(older_than_days=30)

        assert archived == 1
        fetched = dlq.get_by_id(e.id)
        assert fetched.status == FailedOperationStatus.ARCHIVED.value

    def test_purge_archived_deletes_archived_rows(self, stats, dlq):
        with freeze_time("2026-01-10 10:00:00"):
            e = dlq.create(domain="payment", failure_type="timeout")
            dlq.mark_as_resolved(e.id, resolution_type="x")

        with freeze_time("2026-02-15 10:00:00"):
            stats.archive_old_entries(older_than_days=30)

        with freeze_time("2026-04-14 10:00:00"):
            purged = stats.purge_archived(older_than_days=30)

        assert purged == 1
        assert dlq.get_by_id(e.id) is None

    def test_purge_archived_all_removes_all_archived(self, stats, dlq):
        with freeze_time("2026-02-10 10:00:00"):
            e = dlq.create(domain="payment", failure_type="timeout")
            dlq.mark_as_resolved(e.id, resolution_type="x")

        with freeze_time("2026-04-14 10:00:00"):
            stats.archive_old_entries(older_than_days=30)
            purged = stats.purge_archived()

        assert purged == 1


class TestSQLStatisticsCBSummaryBehavior:
    """Circuit breaker summary and list."""

    def test_empty_cb_table_returns_zero_summary(self, stats, cb_table):
        summary = stats.get_circuit_breaker_summary()
        assert isinstance(summary, CircuitBreakerSummary)
        assert summary.total == 0

    def test_cb_summary_counts_by_state(self, stats, cb_table):
        cb_table("api-gateway")
        cb_table("payment-svc", state="open", failure_count=5)

        summary = stats.get_circuit_breaker_summary()
        assert summary.total == 2
        assert summary.closed == 1
        assert summary.open == 1

    def test_list_circuit_breakers_returns_all(self, stats, cb_table):
        cb_table("api-gateway")
        cb_table("payment-svc", state="open", failure_count=5)

        breakers = stats.list_circuit_breakers()
        assert len(breakers) == 2
        names = {b.service_name for b in breakers}
        assert names == {"api-gateway", "payment-svc"}


class TestSQLStatisticsCBGracefulDegradationBehavior:
    """CB methods return empty defaults when table is missing."""

    def test_cb_summary_returns_empty_when_table_missing(self, get_sqlite_conn):
        stats = SQLStatisticsRepository(get_sqlite_conn)
        summary = stats.get_circuit_breaker_summary()
        assert summary.total == 0

    def test_cb_list_returns_empty_when_table_missing(self, get_sqlite_conn):
        stats = SQLStatisticsRepository(get_sqlite_conn)
        breakers = stats.list_circuit_breakers()
        assert breakers == []


class TestSQLStatisticsPersistEntryBehavior:
    """persist_entry upsert and sync_from_runtime."""

    def test_persist_entry_inserts_new_row(self, stats, dlq):
        entry_data = {
            "id": 90001,
            "domain": "payment",
            "failure_type": "timeout",
            "status": "pending",
            "entity_type": "order",
            "entity_id": "42",
            "error_message": "gateway timed out",
        }
        result = stats.persist_entry(entry_data)
        assert result == "90001"

        detail = stats.get_entry_detail("90001")
        assert detail is not None
        assert detail["domain"] == "payment"

    def test_persist_entry_without_id_returns_none(self, stats):
        assert stats.persist_entry({"domain": "payment"}) is None

    def test_sync_from_runtime_returns_synced_count(self, stats, dlq):
        entries = [
            {"id": 90100 + i, "domain": "payment", "failure_type": "timeout"}
            for i in range(3)
        ]
        synced = stats.sync_from_runtime(entries)
        assert synced == 3


class TestSQLStatisticsAuditTrailBehavior:
    """get_audit_trail_by_entity / link_audit_entry."""

    def test_get_audit_trail_for_missing_entity(self, stats):
        trail = stats.get_audit_trail_by_entity("nonexistent")
        assert trail.entity_id == "nonexistent"
        assert trail.entries == []

    def test_link_and_get_audit_trail(self, stats, dlq):
        entry = dlq.create(domain="payment", failure_type="timeout")
        entry_id = str(entry.id)

        linked = stats.link_audit_entry(
            entity_id=entry_id,
            entity_type="dlq_entry",
            action="store",
            actor_id="system",
            status="pending",
            audit_record_hash="hash-001",
        )
        assert linked is True

        trail = stats.get_audit_trail_by_entity(entry_id)
        assert trail.domain == "payment"
        assert len(trail.entries) >= 1

    def test_link_audit_entry_non_dlq_type_returns_false(self, stats):
        assert (
            stats.link_audit_entry(
                entity_id="x",
                entity_type="other",
                action="store",
            )
            is False
        )


class TestSQLStatisticsAsyncConfigBehavior:
    """Async persistence config methods."""

    def test_should_persist_async_returns_false(self, stats):
        assert stats.should_persist_async() is False

    def test_get_async_persist_task_name_returns_none(self, stats):
        assert stats.get_async_persist_task_name() is None


# =============================================================================
# 778 D11 — the mirror upsert never walks a finished entry backwards
# =============================================================================


class TestSQLStatisticsPersistEntryMonotonicBehavior:
    """778 D11 — a stale snapshot cannot reopen a finished entry.

    The mirror is written from snapshots, and snapshots arrive late: a
    replay worker resolves an entry while a task carrying the older PENDING
    picture is still in flight. Under a plain last-write-wins upsert that
    task reopened a closed entry — and a reopened entry is a replay
    candidate again, which is the double execution the dead-letter queue
    exists to prevent. Load-bearing now that the mirror table can be the
    live store.
    """

    def _snapshot(self, entry_id, status, **overrides):
        data = {
            "id": entry_id,
            "domain": "payment",
            "failure_type": "timeout",
            "status": status,
            "entity_type": "order",
            "entity_id": "42",
            "error_message": "gateway timed out",
        }
        data.update(overrides)
        return data

    def _stored_status(self, stats, entry_id):
        detail = stats.get_entry_detail(str(entry_id))
        assert detail is not None
        return detail["status"]

    def test_persist_entry_stale_pending_snapshot_leaves_a_resolved_entry_resolved(
        self, stats
    ):
        """The regression the guard exists for."""
        stats.persist_entry(self._snapshot(91001, FailedOperationStatus.PENDING.value))
        stats.persist_entry(self._snapshot(91001, FailedOperationStatus.RESOLVED.value))

        stats.persist_entry(self._snapshot(91001, FailedOperationStatus.PENDING.value))

        assert self._stored_status(stats, 91001) == (
            FailedOperationStatus.RESOLVED.value
        )

    @pytest.mark.parametrize(
        "terminal",
        [
            FailedOperationStatus.RESOLVED.value,
            FailedOperationStatus.REJECTED.value,
            FailedOperationStatus.ARCHIVED.value,
        ],
    )
    def test_persist_entry_downgrades_no_terminal_status(self, stats, terminal):
        """All three finished statuses are protected, not just ``resolved``."""
        entry_id = 91100 + len(terminal)
        stats.persist_entry(self._snapshot(entry_id, terminal))

        stats.persist_entry(
            self._snapshot(entry_id, FailedOperationStatus.REPLAYING.value)
        )

        assert self._stored_status(stats, entry_id) == terminal

    def test_persist_entry_terminal_snapshot_still_updates_a_pending_entry(self, stats):
        """The guard blocks regressions, not progress.

        The mirror has to be able to learn that an entry finished, or it
        would freeze at whatever status it first saw.
        """
        stats.persist_entry(self._snapshot(91002, FailedOperationStatus.PENDING.value))

        stats.persist_entry(self._snapshot(91002, FailedOperationStatus.RESOLVED.value))

        assert self._stored_status(stats, 91002) == (
            FailedOperationStatus.RESOLVED.value
        )

    def test_persist_entry_terminal_snapshot_still_updates_a_terminal_entry(
        self, stats
    ):
        """Terminal to terminal is a legitimate move (resolved, then archived)."""
        stats.persist_entry(self._snapshot(91003, FailedOperationStatus.RESOLVED.value))

        stats.persist_entry(self._snapshot(91003, FailedOperationStatus.ARCHIVED.value))

        assert self._stored_status(stats, 91003) == (
            FailedOperationStatus.ARCHIVED.value
        )

    def test_persist_entry_non_terminal_entry_takes_any_snapshot(self, stats):
        """An unfinished entry is not guarded — the guard reads the *stored*
        status, and an unfinished one has nothing to protect."""
        stats.persist_entry(self._snapshot(91004, FailedOperationStatus.PENDING.value))

        stats.persist_entry(
            self._snapshot(91004, FailedOperationStatus.REPLAYING.value)
        )

        assert self._stored_status(stats, 91004) == (
            FailedOperationStatus.REPLAYING.value
        )

    def test_persist_entry_first_insert_is_unguarded(self, stats):
        """The guard gates the conflict branch only.

        A brand-new entry arriving already finished — an archived row synced
        from a runtime that outlived this mirror — inserts as it is.
        """
        stats.persist_entry(self._snapshot(91005, FailedOperationStatus.ARCHIVED.value))

        assert self._stored_status(stats, 91005) == (
            FailedOperationStatus.ARCHIVED.value
        )

    def test_persist_entry_blocked_update_leaves_the_other_columns_alone(self, stats):
        """A refused status change refuses the whole update, not half of it.

        The guard rides the conflict branch as a unit: letting the payload
        land while the status is held back would leave the row describing an
        attempt that the status says never reopened.
        """
        stats.persist_entry(
            self._snapshot(
                91006, FailedOperationStatus.RESOLVED.value, error_message="settled"
            )
        )

        stats.persist_entry(
            self._snapshot(
                91006,
                FailedOperationStatus.PENDING.value,
                error_message="stale retry in flight",
            )
        )

        detail = stats.get_entry_detail("91006")
        assert detail["status"] == FailedOperationStatus.RESOLVED.value
        assert detail["error_message"] == "settled"

    def test_sync_from_runtime_batches_carry_the_same_persist_entry_guard(self, stats):
        """``sync_from_runtime`` routes through the same upsert.

        It is the other advertised mirror entry point, so a batch of stale
        snapshots must not do what a single stale snapshot cannot.
        """
        stats.persist_entry(self._snapshot(91007, FailedOperationStatus.RESOLVED.value))

        synced = stats.sync_from_runtime(
            [self._snapshot(91007, FailedOperationStatus.PENDING.value)]
        )

        assert synced == 1
        assert self._stored_status(stats, 91007) == (
            FailedOperationStatus.RESOLVED.value
        )


class TestPersistUpsertTailContract:
    """778 D11 — the upsert tail each dialect gets.

    PostgreSQL and sqlite carry the guard in a ``WHERE`` on the conflict
    branch; MySQL's upsert has no ``WHERE``, so the guard rides every
    assignment instead — and ``status`` must be assigned last there, because
    MySQL evaluates the assignment list left to right and every guard reads
    the stored status.
    """

    def _repo(self, get_sqlite_conn, dialect):
        return SQLStatisticsRepository(get_sqlite_conn, dialect=dialect)

    def test_update_columns_end_with_status(self, get_sqlite_conn):
        """The MySQL branch's correctness depends on this ordering."""
        repo = self._repo(get_sqlite_conn, SQLDialect.SQLITE)

        assert repo._PERSIST_UPDATE_COLS[-1] == "status"
        assert set(repo._PERSIST_UPDATE_COLS) == {
            "status",
            "retry_count",
            "resolved_at",
            "updated_at",
            "data",
        }

    @pytest.mark.parametrize(
        ("dialect", "incoming_ref"),
        [
            (SQLDialect.POSTGRESQL, "EXCLUDED"),
            (SQLDialect.SQLITE, "excluded"),
        ],
    )
    def test_conflict_branch_dialects_guard_with_a_where(
        self, get_sqlite_conn, dialect, incoming_ref
    ):
        tail = self._repo(get_sqlite_conn, dialect)._persist_upsert_tail()

        assert " WHERE " in tail
        assert "baldur_dlq.status NOT IN ('resolved', 'rejected', 'archived')" in tail
        assert f"{incoming_ref}.status IN ('resolved', 'rejected', 'archived')" in tail

    def test_mysql_guards_every_assignment_instead(self, get_sqlite_conn):
        tail = self._repo(get_sqlite_conn, SQLDialect.MYSQL)._persist_upsert_tail()

        assert tail.startswith("ON DUPLICATE KEY UPDATE ")
        assert " WHERE " not in tail
        for column in ("retry_count", "resolved_at", "updated_at", "data", "status"):
            assert f"{column} = IF(" in tail
        assert "VALUES(status) IN ('resolved', 'rejected', 'archived')" in tail
        # Assigned last, so the guards ahead of it still read the stored row.
        assert tail.rindex("status = IF(") > tail.rindex("data = IF(")
