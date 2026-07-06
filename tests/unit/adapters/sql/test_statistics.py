"""
Unit tests for SQLStatisticsRepository.

Coverage:
- DLQ aggregation: status counts, domain/failure distribution, recent
  activity, resolution rate, average retry count.
- DLQ list/detail: paginated list, entry detail with JSON data.
- SLA breach detection.
- Cleanup: cleanup stats, archive old, purge archived.
- CB statistics: summary and list (graceful degradation).
- Persistence: persist_entry upsert, sync_from_runtime batch.
- Audit trail: get/link audit trail entries.
- Async config: should_persist_async / get_async_persist_task_name.
- Graceful degradation: CB methods return defaults when table missing.

Requires both ``baldur_dlq`` and ``baldur_cb_state`` tables to be
bootstrapped via their respective repository constructors sharing the
same sqlite in-memory connection.
"""

from __future__ import annotations

import pytest

from baldur.adapters.sql.circuit_breaker import SQLCircuitBreakerStateRepository
from baldur.adapters.sql.failed_operation import SQLFailedOperationRepository
from baldur.adapters.sql.statistics import SQLStatisticsRepository
from baldur.interfaces.repositories import FailedOperationStatus
from baldur.interfaces.statistics import (
    CircuitBreakerSummary,
    CleanupStats,
    PaginatedResult,
)
from tests.factories.time_helpers import freeze_time


@pytest.fixture
def _bootstrap_tables(get_sqlite_conn):
    """Bootstrap DLQ + CB tables by triggering a read on each repo."""
    dlq_repo = SQLFailedOperationRepository(get_sqlite_conn)
    cb_repo = SQLCircuitBreakerStateRepository(get_sqlite_conn)
    dlq_repo.get_by_id(0)
    cb_repo.get_by_service_name("__bootstrap__")


@pytest.fixture
def dlq(get_sqlite_conn, _bootstrap_tables) -> SQLFailedOperationRepository:
    return SQLFailedOperationRepository(get_sqlite_conn)


@pytest.fixture
def cb(get_sqlite_conn, _bootstrap_tables) -> SQLCircuitBreakerStateRepository:
    return SQLCircuitBreakerStateRepository(get_sqlite_conn)


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

    def test_empty_cb_table_returns_zero_summary(self, stats):
        summary = stats.get_circuit_breaker_summary()
        assert isinstance(summary, CircuitBreakerSummary)
        assert summary.total == 0

    def test_cb_summary_counts_by_state(self, stats, cb):
        cb.get_or_create("api-gateway")
        cb.get_or_create("payment-svc")
        cb.update_state("payment-svc", state="open", failure_count=5)

        summary = stats.get_circuit_breaker_summary()
        assert summary.total == 2
        assert summary.closed == 1
        assert summary.open == 1

    def test_list_circuit_breakers_returns_all(self, stats, cb):
        cb.get_or_create("api-gateway")
        cb.get_or_create("payment-svc")
        cb.update_state("payment-svc", state="open", failure_count=5)

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
