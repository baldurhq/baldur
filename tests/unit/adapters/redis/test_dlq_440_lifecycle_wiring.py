"""
Redis DLQ adapter tests for 440_DLQ_LIFECYCLE_WIRING_GAPS.

Covers:
- _STATUS_INDEXED includes EXPIRED
- count_archived_older_than delegation from RedisDLQRepository → RedisDLQMaintenance
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from baldur.adapters.redis.dlq_maintenance import RedisDLQMaintenance
from baldur.interfaces.repositories import (
    FailedOperationData,
    FailedOperationStatus,
)


def _make_failed_op_data(
    *,
    id: int = 1,
    status: str = FailedOperationStatus.ARCHIVED.value,
    resolved_at: datetime | None = None,
) -> FailedOperationData:
    return FailedOperationData(
        id=id,
        domain="payment",
        failure_type="timeout",
        status=status,
        resolved_at=resolved_at,
    )


class TestStatusIndexedContract:
    """Contract: _STATUS_INDEXED indexes every status except PENDING (541 D6)."""

    def test_expired_in_status_indexed(self):
        """EXPIRED is indexed for ZRANGEBYSCORE performance."""
        from baldur.adapters.redis.dlq import RedisDLQRepository

        assert FailedOperationStatus.EXPIRED.value in RedisDLQRepository._STATUS_INDEXED

    def test_status_indexed_contains_all_required_statuses(self):
        """541 D6: every FailedOperationStatus except PENDING is indexed
        (PENDING uses its own PENDING_KEY), so no status filter falls back to
        an O(N) keyspace SCAN."""
        from baldur.adapters.redis.dlq import RedisDLQRepository

        expected = {
            s.value
            for s in FailedOperationStatus
            if s is not FailedOperationStatus.PENDING
        }
        assert expected == RedisDLQRepository._STATUS_INDEXED


class TestRedisDLQMaintenanceCountArchivedBehavior:
    """Behavior: count_archived_older_than filters by resolved_at."""

    @pytest.fixture
    def maintenance(self):
        mock_repo = MagicMock()
        return RedisDLQMaintenance(mock_repo)

    def test_count_returns_zero_when_no_archived(self, maintenance):
        """No archived entries → 0."""
        maintenance._repo.query.by_status.return_value = []

        result = maintenance.count_archived_older_than(30)

        assert result == 0

    def test_count_filters_by_resolved_at_cutoff(self, maintenance):
        """Only entries with resolved_at before cutoff are counted."""
        now = datetime.now(UTC)
        old_entry = _make_failed_op_data(id=1, resolved_at=now - timedelta(days=60))
        recent_entry = _make_failed_op_data(id=2, resolved_at=now - timedelta(days=10))
        maintenance._repo.query.by_status.return_value = [old_entry, recent_entry]

        result = maintenance.count_archived_older_than(30)

        assert result == 1

    def test_count_excludes_entries_without_resolved_at(self, maintenance):
        """Entries with resolved_at=None are not counted."""
        entry = _make_failed_op_data(id=1, resolved_at=None)
        maintenance._repo.query.by_status.return_value = [entry]

        result = maintenance.count_archived_older_than(0)

        assert result == 0

    def test_count_queries_archived_status(self, maintenance):
        """Queries the ARCHIVED status sorted set."""
        maintenance._repo.query.by_status.return_value = []

        maintenance.count_archived_older_than(30)

        maintenance._repo.query.by_status.assert_called_once_with(
            FailedOperationStatus.ARCHIVED.value, limit=10000
        )


class TestRedisDLQRepositoryCountArchivedDelegationBehavior:
    """Behavior: RedisDLQRepository.count_archived_older_than delegates to maintenance."""

    def test_delegation_calls_maintenance(self):
        """count_archived_older_than on repo delegates to maintenance sub-module."""
        from baldur.adapters.redis.dlq import RedisDLQRepository

        mock_backend = MagicMock()
        repo = RedisDLQRepository(mock_backend)
        repo.maintenance = MagicMock(spec=RedisDLQMaintenance)
        repo.maintenance.count_archived_older_than.return_value = 5

        result = repo.count_archived_older_than(90)

        assert result == 5
        repo.maintenance.count_archived_older_than.assert_called_once_with(90)
