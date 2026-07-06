"""
Unit tests for 428 — InMemoryFailedOperationRepository.get_statistics() breakdown.

Test target:
  - baldur.adapters.memory.failed_operation.InMemoryFailedOperationRepository
    .get_statistics() — new pending_by_domain and
    pending_by_domain_and_failure_type fields (D9). These feed
    DailyReportService._collect_dlq_pending_breakdown().
"""

from __future__ import annotations

import pytest


@pytest.fixture
def repo():
    from baldur.adapters.memory import InMemoryFailedOperationRepository

    return InMemoryFailedOperationRepository()


class TestGetStatisticsPendingBreakdownBehavior:
    """pending_by_domain and pending_by_domain_and_failure_type (D9)."""

    def test_empty_repo_returns_empty_breakdown_maps(self):
        """No entries -> both new maps are empty dicts."""
        from baldur.adapters.memory import InMemoryFailedOperationRepository

        repo = InMemoryFailedOperationRepository()

        stats = repo.get_statistics()

        assert stats["pending_by_domain"] == {}
        assert stats["pending_by_domain_and_failure_type"] == {}

    def test_single_entry_reflected_in_both_maps(self, repo):
        """One pending entry -> counted under its domain + failure_type."""
        repo.create(domain="payment", failure_type="TIMEOUT", error_message="x")

        stats = repo.get_statistics()

        assert stats["pending_by_domain"] == {"payment": 1}
        assert stats["pending_by_domain_and_failure_type"] == {
            "payment": {"TIMEOUT": 1}
        }

    def test_multiple_entries_aggregate_counts(self, repo):
        """Multiple entries aggregate under the correct domain/type combo."""
        repo.create(domain="payment", failure_type="TIMEOUT", error_message="x")
        repo.create(domain="payment", failure_type="TIMEOUT", error_message="x")
        repo.create(domain="payment", failure_type="AUTH_ERROR", error_message="x")
        repo.create(domain="inventory", failure_type="TIMEOUT", error_message="x")

        stats = repo.get_statistics()

        assert stats["pending_by_domain"] == {"payment": 3, "inventory": 1}
        assert stats["pending_by_domain_and_failure_type"] == {
            "payment": {"TIMEOUT": 2, "AUTH_ERROR": 1},
            "inventory": {"TIMEOUT": 1},
        }

    def test_resolved_entries_not_counted_in_pending_breakdown(self, repo):
        """Entries marked RESOLVED are excluded from pending_by_domain."""
        from baldur.interfaces.repositories import FailedOperationStatus

        e1 = repo.create(domain="payment", failure_type="TIMEOUT", error_message="x")
        repo.create(domain="payment", failure_type="TIMEOUT", error_message="x")
        repo.update_status(e1.id, FailedOperationStatus.RESOLVED.value)

        stats = repo.get_statistics()

        # Only 1 pending remains
        assert stats["pending_by_domain"] == {"payment": 1}
        assert stats["pending_by_domain_and_failure_type"] == {
            "payment": {"TIMEOUT": 1}
        }

    def test_baseline_fields_remain_present(self, repo):
        """total / by_status / by_domain still returned alongside new fields."""
        repo.create(domain="payment", failure_type="TIMEOUT", error_message="x")

        stats = repo.get_statistics()

        assert "total" in stats
        assert "by_status" in stats
        assert "by_domain" in stats
        assert "pending_by_domain" in stats
        assert "pending_by_domain_and_failure_type" in stats
