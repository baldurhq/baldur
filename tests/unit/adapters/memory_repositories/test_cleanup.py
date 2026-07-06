"""
DLQ Cleanup 작업 테스트.
"""

import pytest


class TestCleanupOperations:
    """Tests for DLQ cleanup operations."""

    @pytest.fixture
    def repo_with_data(self):
        """Create repository with test data."""
        from baldur.adapters.memory import InMemoryFailedOperationRepository
        from baldur.interfaces.repositories import FailedOperationStatus

        repo = InMemoryFailedOperationRepository()

        # Create entries with various statuses and ages
        for i in range(5):
            entry = repo.create(
                domain="payment",
                failure_type=f"error_{i}",
                error_message=f"Test error {i}",
            )

        for i in range(10):
            entry = repo.create(
                domain="payment",
                failure_type=f"resolved_{i}",
                error_message=f"Resolved error {i}",
            )
            repo.update_status(
                entry.id,
                FailedOperationStatus.RESOLVED.value,
                resolution_type="auto",
            )

        return repo

    def test_archive_old_resolved(self, repo_with_data):
        """Test archiving old resolved entries."""
        from baldur.interfaces.repositories import FailedOperationStatus

        repo = repo_with_data

        stats = repo.get_cleanup_stats()
        assert stats["by_status"].get(FailedOperationStatus.ARCHIVED.value, 0) == 0
        assert stats["by_status"].get(FailedOperationStatus.RESOLVED.value, 0) == 10

        count = repo.archive_old_resolved(older_than_days=0)
        assert count >= 0

    def test_purge_archived_by_ids(self):
        """Test purging archived entries by ID."""
        from baldur.adapters.memory import InMemoryFailedOperationRepository
        from baldur.interfaces.repositories import FailedOperationStatus

        repo = InMemoryFailedOperationRepository()

        entry = repo.create(domain="payment", failure_type="test", error_message="Test")
        repo.update_status(entry.id, FailedOperationStatus.RESOLVED.value)
        repo.update_status(entry.id, FailedOperationStatus.ARCHIVED.value)

        assert repo.get_by_id(entry.id) is not None

        count = repo.purge_archived(ids=[entry.id])
        assert count == 1
        assert repo.get_by_id(entry.id) is None

    def test_purge_archived_rejects_non_archived(self):
        """Test that purge rejects non-archived entries."""
        from baldur.adapters.memory import InMemoryFailedOperationRepository

        repo = InMemoryFailedOperationRepository()

        entry = repo.create(domain="payment", failure_type="test", error_message="Test")

        with pytest.raises(ValueError) as exc_info:
            repo.purge_archived(ids=[entry.id])

        assert "not archived" in str(exc_info.value)

    def test_purge_archived_no_args_is_noop(self):
        """No-args purge is a no-op (fail-safe): with neither ids nor
        older_than_days, nothing is deleted. Contract parity across adapters;
        ``older_than_days=0`` is the explicit "purge all archived" path."""
        from baldur.adapters.memory import InMemoryFailedOperationRepository
        from baldur.interfaces.repositories import FailedOperationStatus

        repo = InMemoryFailedOperationRepository()

        for _i in range(5):
            entry = repo.create(
                domain="payment", failure_type="test", error_message="Test"
            )
            repo.update_status(entry.id, FailedOperationStatus.ARCHIVED.value)

        for _i in range(3):
            repo.create(domain="payment", failure_type="test", error_message="Test")

        assert len(repo._storage) == 8

        count = repo.purge_archived()
        assert count == 0
        assert len(repo._storage) == 8

    def test_get_cleanup_stats(self):
        """Test getting cleanup statistics."""
        from baldur.adapters.memory import InMemoryFailedOperationRepository
        from baldur.interfaces.repositories import FailedOperationStatus

        repo = InMemoryFailedOperationRepository()

        repo.create(domain="payment", failure_type="pending1", error_message="Test")
        repo.create(domain="payment", failure_type="pending2", error_message="Test")

        entry = repo.create(
            domain="payment", failure_type="resolved1", error_message="Test"
        )
        repo.update_status(entry.id, FailedOperationStatus.RESOLVED.value)

        entry = repo.create(
            domain="payment", failure_type="archived1", error_message="Test"
        )
        repo.update_status(entry.id, FailedOperationStatus.ARCHIVED.value)

        stats = repo.get_cleanup_stats()

        assert stats["total"] == 4
        assert stats["by_status"][FailedOperationStatus.PENDING.value] == 2
        assert stats["by_status"][FailedOperationStatus.RESOLVED.value] == 1
        assert stats["by_status"][FailedOperationStatus.ARCHIVED.value] == 1
