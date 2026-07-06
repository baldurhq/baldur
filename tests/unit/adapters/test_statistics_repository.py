"""
Unit Tests for Statistics Repository Implementations.

Hybrid Storage Architecture - Unit Tests

Tests for:
- NullStatisticsRepository (Null Object Pattern)
- StatisticsRepositoryInterface DTOs
- ProviderRegistry statistics methods

These tests do not require database or Redis connections.
"""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from baldur.adapters.statistics.null import NullStatisticsRepository
from baldur.factory import ProviderRegistry
from baldur.interfaces.statistics import (
    AuditTrailEntry,
    CircuitBreakerInfo,
    CircuitBreakerSummary,
    CleanupStats,
    DomainDistribution,
    EntityAuditTrail,
    PaginatedResult,
    RecentActivity,
    StatisticsRepositoryInterface,
    StatusCounts,
)


class TestStatusCounts:
    """Tests for StatusCounts dataclass."""

    def test_default_values(self):
        """Test default values are all zero."""
        counts = StatusCounts()

        assert counts.total == 0
        assert counts.pending == 0
        assert counts.resolved == 0
        assert counts.failed == 0
        assert counts.archived == 0

    def test_custom_values(self):
        """Test custom values are stored correctly."""
        counts = StatusCounts(
            total=100,
            pending=30,
            resolved=50,
            failed=15,
            archived=5,
        )

        assert counts.total == 100
        assert counts.pending == 30
        assert counts.resolved == 50
        assert counts.failed == 15
        assert counts.archived == 5


class TestDomainDistribution:
    """Tests for DomainDistribution dataclass."""

    def test_creation(self):
        """Test creation with required fields."""
        dist = DomainDistribution(
            domain="payment",
            count=50,
            percentage=33.3,
        )

        assert dist.domain == "payment"
        assert dist.count == 50
        assert dist.percentage == 33.3


class TestPaginatedResult:
    """Tests for PaginatedResult dataclass."""

    def test_default_values(self):
        """Test default values."""
        result = PaginatedResult()

        assert result.items == []
        assert result.total == 0
        assert result.page == 1
        assert result.page_size == 20
        assert result.has_next is False
        assert result.has_prev is False

    def test_total_pages_calculation(self):
        """Test total_pages property calculation."""
        result = PaginatedResult(total=95, page_size=20)
        assert result.total_pages == 5

        result = PaginatedResult(total=100, page_size=20)
        assert result.total_pages == 5

        result = PaginatedResult(total=0, page_size=20)
        assert result.total_pages == 0

    def test_total_pages_with_zero_page_size(self):
        """Test total_pages returns 0 for invalid page_size."""
        result = PaginatedResult(total=100, page_size=0)
        assert result.total_pages == 0


class TestEntityAuditTrail:
    """Tests for EntityAuditTrail and hash chain validation."""

    def test_empty_entries_is_valid(self):
        """Test empty entries list is considered valid."""
        trail = EntityAuditTrail(
            entity_id="dlq-123",
            entity_type="dlq_entry",
            domain="payment",
            entries=[],
        )

        assert trail.is_chain_valid is True
        assert trail.total_entries == 0

    def test_single_entry_valid(self):
        """Test single entry with no previous hash is valid."""
        entry = AuditTrailEntry(
            timestamp=datetime.now(),
            action="store",
            hash_chain="abc123",
            previous_hash=None,  # First entry has no previous
        )
        trail = EntityAuditTrail(
            entity_id="dlq-123",
            entity_type="dlq_entry",
            domain="payment",
            entries=[entry],
        )

        assert trail.is_chain_valid is True
        assert trail.total_entries == 1

    def test_valid_chain(self):
        """Test valid hash chain returns True."""
        entries = [
            AuditTrailEntry(
                timestamp=datetime.now(),
                action="store",
                hash_chain="hash1",
                previous_hash=None,
            ),
            AuditTrailEntry(
                timestamp=datetime.now(),
                action="replay",
                hash_chain="hash2",
                previous_hash="hash1",
            ),
            AuditTrailEntry(
                timestamp=datetime.now(),
                action="resolve",
                hash_chain="hash3",
                previous_hash="hash2",
            ),
        ]
        trail = EntityAuditTrail(
            entity_id="dlq-123",
            entity_type="dlq_entry",
            domain="payment",
            entries=entries,
        )

        assert trail.is_chain_valid is True

    def test_invalid_chain_first_entry_has_previous(self):
        """Test first entry with previous_hash is invalid."""
        entry = AuditTrailEntry(
            timestamp=datetime.now(),
            action="store",
            hash_chain="hash1",
            previous_hash="should_not_exist",  # First entry should not have previous
        )
        trail = EntityAuditTrail(
            entity_id="dlq-123",
            entity_type="dlq_entry",
            domain="payment",
            entries=[entry],
        )

        assert trail.is_chain_valid is False

    def test_invalid_chain_broken_link(self):
        """Test broken chain link returns False."""
        entries = [
            AuditTrailEntry(
                timestamp=datetime.now(),
                action="store",
                hash_chain="hash1",
                previous_hash=None,
            ),
            AuditTrailEntry(
                timestamp=datetime.now(),
                action="replay",
                hash_chain="hash2",
                previous_hash="wrong_hash",  # Should be "hash1"
            ),
        ]
        trail = EntityAuditTrail(
            entity_id="dlq-123",
            entity_type="dlq_entry",
            domain="payment",
            entries=entries,
        )

        assert trail.is_chain_valid is False


class TestNullStatisticsRepository:
    """Tests for NullStatisticsRepository (Null Object Pattern)."""

    @pytest.fixture
    def repo(self):
        """Create NullStatisticsRepository instance."""
        # Reset the warned flag for clean testing
        NullStatisticsRepository._warned = False
        return NullStatisticsRepository()

    def test_get_status_counts_returns_empty(self, repo):
        """Test get_status_counts returns empty StatusCounts."""
        counts = repo.get_status_counts()

        assert isinstance(counts, StatusCounts)
        assert counts.total == 0
        assert counts.pending == 0

    def test_get_domain_distribution_returns_empty(self, repo):
        """Test get_domain_distribution returns empty list."""
        dist = repo.get_domain_distribution(limit=10)

        assert isinstance(dist, list)
        assert len(dist) == 0

    def test_get_failure_type_distribution_returns_empty(self, repo):
        """Test get_failure_type_distribution returns empty list."""
        dist = repo.get_failure_type_distribution(limit=10)

        assert isinstance(dist, list)
        assert len(dist) == 0

    def test_get_recent_activity_returns_empty(self, repo):
        """Test get_recent_activity returns empty RecentActivity."""
        activity = repo.get_recent_activity(hours=24, days=7)

        assert isinstance(activity, RecentActivity)
        assert activity.new_in_24h == 0

    def test_get_resolution_rate_returns_zero(self, repo):
        """Test get_resolution_rate returns 0.0."""
        rate = repo.get_resolution_rate(days=30)

        assert rate == 0.0

    def test_get_avg_retry_count_returns_zero(self, repo):
        """Test get_avg_retry_count returns 0.0."""
        avg = repo.get_avg_retry_count()

        assert avg == 0.0

    def test_list_entries_returns_empty_paginated(self, repo):
        """Test list_entries returns empty PaginatedResult."""
        result = repo.list_entries(page=2, page_size=10)

        assert isinstance(result, PaginatedResult)
        assert result.items == []
        assert result.total == 0
        assert result.page == 2
        assert result.page_size == 10
        assert result.has_next is False
        assert result.has_prev is False

    def test_get_entry_detail_returns_none(self, repo):
        """Test get_entry_detail returns None."""
        detail = repo.get_entry_detail("dlq-123")

        assert detail is None

    def test_get_sla_breaches_returns_empty(self, repo):
        """Test get_sla_breaches returns empty dict."""
        breaches = repo.get_sla_breaches(sla_threshold_hours=4)

        assert isinstance(breaches, dict)
        assert len(breaches) == 0

    def test_get_cleanup_stats_returns_empty(self, repo):
        """Test get_cleanup_stats returns empty CleanupStats."""
        stats = repo.get_cleanup_stats()

        assert isinstance(stats, CleanupStats)
        assert stats.total == 0

    def test_archive_old_entries_returns_zero(self, repo):
        """Test archive_old_entries returns 0 (no-op)."""
        count = repo.archive_old_entries(older_than_days=30)

        assert count == 0

    def test_purge_archived_returns_zero(self, repo):
        """Test purge_archived returns 0 (no-op)."""
        count = repo.purge_archived(older_than_days=90)

        assert count == 0

    def test_get_circuit_breaker_summary_returns_empty(self, repo):
        """Test get_circuit_breaker_summary returns empty summary."""
        summary = repo.get_circuit_breaker_summary()

        assert isinstance(summary, CircuitBreakerSummary)
        assert summary.total == 0

    def test_list_circuit_breakers_returns_empty(self, repo):
        """Test list_circuit_breakers returns empty list."""
        breakers = repo.list_circuit_breakers()

        assert isinstance(breakers, list)
        assert len(breakers) == 0

    def test_persist_entry_returns_none(self, repo):
        """Test persist_entry returns None (no-op)."""
        result = repo.persist_entry({"id": "test"})

        assert result is None

    def test_sync_from_runtime_returns_zero(self, repo):
        """Test sync_from_runtime returns 0 (no-op)."""
        count = repo.sync_from_runtime([{"id": "test1"}, {"id": "test2"}])

        assert count == 0

    def test_get_audit_trail_by_entity_returns_empty(self, repo):
        """Test get_audit_trail_by_entity returns empty trail."""
        trail = repo.get_audit_trail_by_entity("dlq-123")

        assert isinstance(trail, EntityAuditTrail)
        assert trail.entity_id == "dlq-123"
        assert trail.entity_type == "dlq_entry"
        assert trail.entries == []

    def test_link_audit_entry_returns_false(self, repo):
        """Test link_audit_entry returns False (no-op)."""
        result = repo.link_audit_entry(
            entity_id="dlq-123",
            entity_type="dlq_entry",
            action="store",
        )

        assert result is False


class TestProviderRegistryStatistics:
    """Tests for ProviderRegistry statistics methods."""

    def setup_method(self):
        """Reset ProviderRegistry before each test."""
        ProviderRegistry.reset()

    def test_get_statistics_repo_returns_null_when_not_registered(self):
        """Test get_statistics_repo returns NullStatisticsRepository when not registered."""
        repo = ProviderRegistry.get_statistics_repo()

        assert isinstance(repo, NullStatisticsRepository)

    def test_has_statistics_adapter_false_initially(self):
        """Test has_statistics_adapter returns False initially."""
        assert ProviderRegistry.has_statistics_adapter() is False

    def test_register_statistics_adapter(self):
        """Test registering a statistics adapter."""
        mock_adapter = MagicMock(spec=StatisticsRepositoryInterface)

        ProviderRegistry.register_statistics_adapter(mock_adapter)

        assert ProviderRegistry.has_statistics_adapter() is True
        assert ProviderRegistry.get_statistics_repo() is mock_adapter

    def test_register_replaces_previous_adapter(self):
        """Test registering a new adapter replaces the previous one."""
        adapter1 = MagicMock(spec=StatisticsRepositoryInterface)
        adapter2 = MagicMock(spec=StatisticsRepositoryInterface)

        ProviderRegistry.register_statistics_adapter(adapter1)
        ProviderRegistry.register_statistics_adapter(adapter2)

        assert ProviderRegistry.get_statistics_repo() is adapter2

    def test_reset_clears_statistics_adapter(self):
        """Test reset clears the statistics adapter."""
        mock_adapter = MagicMock(spec=StatisticsRepositoryInterface)
        ProviderRegistry.register_statistics_adapter(mock_adapter)

        ProviderRegistry.reset()

        assert ProviderRegistry.has_statistics_adapter() is False


class TestCircuitBreakerInfo:
    """Tests for CircuitBreakerInfo dataclass."""

    def test_creation_with_required_fields(self):
        """Test creation with required fields only."""
        info = CircuitBreakerInfo(
            service_name="payment_service",
            state="closed",
        )

        assert info.service_name == "payment_service"
        assert info.state == "closed"
        assert info.failure_count == 0
        assert info.success_count == 0
        assert info.last_failure_time is None

    def test_creation_with_all_fields(self):
        """Test creation with all fields."""
        now = datetime.now()
        info = CircuitBreakerInfo(
            service_name="payment_service",
            state="open",
            failure_count=5,
            success_count=100,
            last_failure_time=now,
            last_state_change=now,
        )

        assert info.failure_count == 5
        assert info.success_count == 100
        assert info.last_failure_time == now


class TestCleanupStats:
    """Tests for CleanupStats dataclass."""

    def test_default_values(self):
        """Test default values."""
        stats = CleanupStats()

        assert stats.total == 0
        assert stats.by_status == {}
        assert stats.resolved_older_than_30_days == 0
        assert stats.archived_older_than_90_days == 0

    def test_with_by_status(self):
        """Test with by_status dict."""
        stats = CleanupStats(
            total=100,
            by_status={"pending": 30, "resolved": 50, "archived": 20},
            resolved_older_than_30_days=25,
            archived_older_than_90_days=10,
        )

        assert stats.by_status["pending"] == 30
        assert stats.resolved_older_than_30_days == 25


class TestRecentActivity:
    """Tests for RecentActivity dataclass."""

    def test_default_values(self):
        """Test default values."""
        activity = RecentActivity()

        assert activity.new_in_24h == 0
        assert activity.resolved_in_24h == 0
        assert activity.new_in_7d == 0
        assert activity.resolved_in_7d == 0
        assert activity.trend == "stable"

    def test_with_custom_values(self):
        """Test with custom values."""
        activity = RecentActivity(
            new_in_24h=10,
            resolved_in_24h=8,
            new_in_7d=50,
            resolved_in_7d=45,
            trend="up",
        )

        assert activity.new_in_24h == 10
        assert activity.trend == "up"
