"""
InMemoryFailedOperationRepository 테스트.
"""

import threading
from datetime import UTC, datetime, timedelta

import pytest

FIXED_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


class TestInMemoryFailedOperationRepository:
    """Tests for InMemoryFailedOperationRepository."""

    @pytest.fixture
    def repo(self):
        """Create a fresh repository for each test."""
        from baldur.adapters.memory import InMemoryFailedOperationRepository

        return InMemoryFailedOperationRepository()

    def test_create_failed_operation(self, repo):
        """Test creating a new failed operation."""
        from baldur.interfaces.repositories import FailedOperationStatus

        entry = repo.create(
            domain="payment",
            failure_type="gateway_timeout",
            error_message="Connection timeout to payment gateway",
            error_code="TIMEOUT_001",
            entity_type="order",
            entity_id="12345",
            entity_refs={"order_id": 12345, "payment_id": 67890},
            user_id=100,
        )

        assert entry.id == "1"
        assert entry.domain == "payment"
        assert entry.failure_type == "gateway_timeout"
        assert entry.error_message == "Connection timeout to payment gateway"
        assert entry.error_code == "TIMEOUT_001"
        assert entry.entity_type == "order"
        assert entry.entity_id == "12345"
        assert entry.entity_refs.get("order_id") == 12345
        assert entry.entity_refs.get("payment_id") == 67890
        assert entry.user_id == 100
        assert entry.status == FailedOperationStatus.PENDING.value
        assert entry.created_at is not None
        assert entry.retry_count == 0

    def test_get_by_id(self, repo):
        """Test retrieving a failed operation by ID."""
        created = repo.create(
            domain="payment",
            failure_type="validation_error",
            error_message="Invalid card number",
        )

        retrieved = repo.get_by_id(created.id)
        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.domain == "payment"
        assert retrieved.failure_type == "validation_error"

    def test_get_by_id_not_found(self, repo):
        """Test retrieving a non-existent failed operation."""
        result = repo.get_by_id(99999)
        assert result is None

    def test_get_pending_by_domain(self, repo):
        """Test filtering pending operations by domain."""
        repo.create(domain="payment", failure_type="error1", error_message="err")
        repo.create(domain="payment", failure_type="error2", error_message="err")
        repo.create(domain="webhook", failure_type="error3", error_message="err")

        payment_entries = repo.get_pending_by_domain("payment")
        assert len(payment_entries) == 2

        webhook_entries = repo.get_pending_by_domain("webhook")
        assert len(webhook_entries) == 1

    def test_update_status(self, repo):
        """Test updating the status of a failed operation."""
        from baldur.interfaces.repositories import FailedOperationStatus

        entry = repo.create(
            domain="payment",
            failure_type="timeout",
            error_message="Request timeout",
        )

        result = repo.update_status(
            entry.id,
            FailedOperationStatus.RESOLVED.value,
            resolution_type="manual",
            resolution_note="Fixed by admin",
        )

        assert result is True

        updated = repo.get_by_id(entry.id)
        assert updated.status == FailedOperationStatus.RESOLVED.value
        assert updated.resolution_type == "manual"
        assert updated.resolution_note == "Fixed by admin"
        assert updated.resolved_at is not None

    def test_update_status_with_recommended_action(self, repo):
        """update_status() persists recommended_action (G3 escalation)."""
        from baldur.interfaces.repositories import FailedOperationStatus

        entry = repo.create(
            domain="payment",
            failure_type="timeout",
            error_message="Request timeout",
        )

        result = repo.update_status(
            entry.id,
            FailedOperationStatus.REQUIRES_REVIEW.value,
            resolution_note="Replay failed",
            recommended_action="escalate",
        )

        assert result is True
        updated = repo.get_by_id(entry.id)
        assert updated.status == FailedOperationStatus.REQUIRES_REVIEW.value
        assert updated.recommended_action == "escalate"

    def test_update_status_empty_recommended_action_preserves_existing(self, repo):
        """Empty recommended_action does not overwrite existing value."""
        from baldur.interfaces.repositories import FailedOperationStatus

        entry = repo.create(
            domain="payment",
            failure_type="timeout",
            error_message="Request timeout",
            recommended_action="manual_check",
        )

        repo.update_status(
            entry.id,
            FailedOperationStatus.PENDING.value,
            resolution_note="retry queued",
        )

        updated = repo.get_by_id(entry.id)
        assert updated.recommended_action == "manual_check"

    def test_increment_retry_count(self, repo):
        """Test incrementing the retry count."""
        entry = repo.create(
            domain="payment",
            failure_type="network_error",
            error_message="Connection refused",
        )

        assert entry.retry_count == 0

        result = repo.increment_retry_count(entry.id)
        assert result is True

        updated = repo.get_by_id(entry.id)
        assert updated.retry_count == 1
        assert updated.last_retry_at is not None

        repo.increment_retry_count(entry.id)
        updated = repo.get_by_id(entry.id)
        assert updated.retry_count == 2

    def test_thread_safety(self, repo):
        """Test thread safety with concurrent operations."""
        results = []
        errors = []

        def create_operation(n):
            try:
                entry = repo.create(
                    domain="payment",
                    failure_type=f"error_{n}",
                    error_message=f"Error message {n}",
                )
                results.append(entry.id)
            except Exception as e:
                errors.append(str(e))

        threads = []
        for i in range(50):
            t = threading.Thread(target=create_operation, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(set(results)) == 50
        # 538 D1: ids are opaque strings ("1".."50"); compare as a set.
        assert set(results) == {str(i) for i in range(1, 51)}


class TestInMemoryCreateExpiresAtBehavior:
    """Behavior: create() accepts and stores expires_at field."""

    @pytest.fixture
    def repo(self):
        from baldur.adapters.memory import InMemoryFailedOperationRepository

        return InMemoryFailedOperationRepository()

    def test_create_with_expires_at_sets_field(self, repo):
        """expires_at value is stored on the created entry."""
        from datetime import datetime

        expires = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
        entry = repo.create(
            domain="payment",
            failure_type="timeout",
            expires_at=expires,
        )

        assert entry.expires_at == expires

    def test_create_without_expires_at_defaults_to_none(self, repo):
        """Omitting expires_at leaves it as None."""
        entry = repo.create(
            domain="payment",
            failure_type="timeout",
        )

        assert entry.expires_at is None

    def test_create_with_expires_at_persisted_on_get(self, repo):
        """expires_at is retrievable via get_by_id."""
        from datetime import datetime

        expires = datetime(2026, 6, 15, 0, 0, 0, tzinfo=UTC)
        created = repo.create(
            domain="payment",
            failure_type="timeout",
            expires_at=expires,
        )

        retrieved = repo.get_by_id(created.id)
        assert retrieved.expires_at == expires


class TestInMemoryCountArchivedOlderThanBehavior:
    """Behavior: count_archived_older_than filters by status and resolved_at."""

    @pytest.fixture(autouse=True)
    def _freeze_now(self):
        from unittest.mock import patch as _patch

        with (
            _patch("baldur.adapters.memory.base._now", return_value=FIXED_NOW),
            _patch(
                "baldur.adapters.memory.failed_operation._now", return_value=FIXED_NOW
            ),
        ):
            yield

    @pytest.fixture
    def repo(self):
        from baldur.adapters.memory import InMemoryFailedOperationRepository

        return InMemoryFailedOperationRepository()

    def _create_archived_entry(self, repo, resolved_days_ago: int) -> None:
        from dataclasses import replace

        from baldur.interfaces.repositories import FailedOperationStatus

        entry = repo.create(domain="payment", failure_type="timeout")
        repo.update_status(entry.id, FailedOperationStatus.RESOLVED.value)
        repo.update_status(entry.id, FailedOperationStatus.ARCHIVED.value)

        stored = repo._storage[entry.id]
        repo._storage[entry.id] = replace(
            stored, resolved_at=FIXED_NOW - timedelta(days=resolved_days_ago)
        )

    def test_count_zero_when_no_archived_entries(self, repo):
        """Empty repository returns 0."""
        assert repo.count_archived_older_than(30) == 0

    def test_count_excludes_recent_archived_entries(self, repo):
        """Archived entries resolved recently are not counted."""
        self._create_archived_entry(repo, resolved_days_ago=10)

        assert repo.count_archived_older_than(30) == 0

    def test_count_includes_old_archived_entries(self, repo):
        """Archived entries resolved long ago are counted."""
        self._create_archived_entry(repo, resolved_days_ago=60)

        assert repo.count_archived_older_than(30) == 1

    def test_count_boundary_exact_day_not_counted(self, repo):
        """Entry at exactly the boundary is not older than N days."""
        self._create_archived_entry(repo, resolved_days_ago=30)

        assert repo.count_archived_older_than(30) == 0

    def test_count_boundary_one_day_beyond_is_counted(self, repo):
        """Entry one day beyond the boundary is counted."""
        self._create_archived_entry(repo, resolved_days_ago=31)

        assert repo.count_archived_older_than(30) == 1

    def test_count_ignores_non_archived_status(self, repo):
        """Non-ARCHIVED entries are never counted."""
        from baldur.interfaces.repositories import FailedOperationStatus

        entry = repo.create(domain="payment", failure_type="timeout")
        repo.update_status(entry.id, FailedOperationStatus.RESOLVED.value)

        assert repo.count_archived_older_than(0) == 0
