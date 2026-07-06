"""
InMemory FailedOperationRepository Overflow Operations Unit Tests (329_DLQ_SIZE_LIMIT).

Test targets:
    - baldur.adapters.memory.failed_operation.InMemoryFailedOperationRepository
      (count_all, count_by_domain, get_oldest_ids, evict_oldest)

Test Categories:
    A. Contract: count_all / count_by_domain return correct counts
    B. Behavior: get_oldest_ids sort order and boundary
    C. Behavior: evict_oldest index consistency after eviction
    D. Behavior: Domain isolation — cross-domain eviction does not affect other domains
"""

from baldur.adapters.memory.failed_operation import (
    InMemoryFailedOperationRepository,
)


def _create_entries(repo, domain="payment", count=5):
    """Helper: create N entries in the given domain."""
    entries = []
    for i in range(count):
        entry = repo.create(
            domain=domain,
            failure_type="PG_TIMEOUT",
            error_message=f"error_{i}",
        )
        entries.append(entry)
    return entries


# =============================================================================
# A. Contract Tests — count_all / count_by_domain
# =============================================================================


class TestCountAllContract:
    """count_all returns correct total item count."""

    def test_count_all_empty_repository(self):
        """Empty repository returns 0."""
        repo = InMemoryFailedOperationRepository()
        assert repo.count_all() == 0

    def test_count_all_single_entry(self):
        """Single entry returns 1."""
        repo = InMemoryFailedOperationRepository()
        _create_entries(repo, count=1)
        assert repo.count_all() == 1

    def test_count_all_multiple_entries(self):
        """Multiple entries returns correct count."""
        repo = InMemoryFailedOperationRepository()
        _create_entries(repo, domain="payment", count=3)
        _create_entries(repo, domain="inventory", count=2)
        assert repo.count_all() == 5


class TestCountByDomainContract:
    """count_by_domain returns correct per-domain item count."""

    def test_count_by_domain_empty_repository(self):
        """Empty repository returns 0 for any domain."""
        repo = InMemoryFailedOperationRepository()
        assert repo.count_by_domain("payment") == 0

    def test_count_by_domain_with_entries(self):
        """Returns correct count for domain with entries."""
        repo = InMemoryFailedOperationRepository()
        _create_entries(repo, domain="payment", count=3)
        _create_entries(repo, domain="inventory", count=2)
        assert repo.count_by_domain("payment") == 3
        assert repo.count_by_domain("inventory") == 2

    def test_count_by_domain_nonexistent_domain(self):
        """Returns 0 for domain with no entries."""
        repo = InMemoryFailedOperationRepository()
        _create_entries(repo, domain="payment", count=3)
        assert repo.count_by_domain("notification") == 0


# =============================================================================
# B. Behavior Tests — get_oldest_ids
# =============================================================================


class TestGetOldestIdsBehavior:
    """get_oldest_ids sort order and boundary behavior."""

    def test_get_oldest_ids_empty_repository(self):
        """Empty repository returns empty list."""
        repo = InMemoryFailedOperationRepository()
        assert repo.get_oldest_ids(5) == []

    def test_get_oldest_ids_returns_oldest_first(self):
        """Returns IDs sorted by created_at ascending (oldest first)."""
        repo = InMemoryFailedOperationRepository()
        entries = _create_entries(repo, count=5)

        oldest_ids = repo.get_oldest_ids(3)

        # First 3 created entries are oldest
        expected = [entries[0].id, entries[1].id, entries[2].id]
        assert oldest_ids == expected

    def test_get_oldest_ids_count_exceeds_total(self):
        """Requesting more IDs than exist returns all available."""
        repo = InMemoryFailedOperationRepository()
        _create_entries(repo, count=3)

        oldest_ids = repo.get_oldest_ids(10)

        assert len(oldest_ids) == 3

    def test_get_oldest_ids_with_domain_filter(self):
        """Domain filter returns only IDs from specified domain."""
        repo = InMemoryFailedOperationRepository()
        payment_entries = _create_entries(repo, domain="payment", count=3)
        _create_entries(repo, domain="inventory", count=3)

        oldest_ids = repo.get_oldest_ids(2, domain="payment")

        assert len(oldest_ids) == 2
        assert all(eid in [e.id for e in payment_entries] for eid in oldest_ids)

    def test_get_oldest_ids_domain_filter_empty(self):
        """Domain filter with no matching entries returns empty."""
        repo = InMemoryFailedOperationRepository()
        _create_entries(repo, domain="payment", count=3)

        oldest_ids = repo.get_oldest_ids(5, domain="notification")

        assert oldest_ids == []


# =============================================================================
# C. Behavior Tests — evict_oldest
# =============================================================================


class TestEvictOldestBehavior:
    """evict_oldest deletion and index consistency behavior."""

    def test_evict_oldest_removes_entries(self):
        """evict_oldest removes entries and returns evicted count."""
        repo = InMemoryFailedOperationRepository()
        _create_entries(repo, count=5)

        evicted = repo.evict_oldest(3)

        assert evicted == 3
        assert repo.count_all() == 2

    def test_evict_oldest_empty_repository(self):
        """evict_oldest on empty repository returns 0."""
        repo = InMemoryFailedOperationRepository()
        evicted = repo.evict_oldest(5)
        assert evicted == 0

    def test_evict_oldest_removes_from_domain_index(self):
        """Evicted entries are removed from domain index."""
        repo = InMemoryFailedOperationRepository()
        _create_entries(repo, domain="payment", count=5)

        repo.evict_oldest(3)

        assert repo.count_by_domain("payment") == 2

    def test_evict_oldest_removes_from_status_index(self):
        """Evicted entries are removed from status index."""
        repo = InMemoryFailedOperationRepository()
        _create_entries(repo, count=5)

        repo.evict_oldest(3)

        # Remaining entries should still be findable by status
        from baldur.interfaces.repositories import FailedOperationStatus

        pending = repo.find_by_status(FailedOperationStatus.PENDING.value)
        assert len(pending) == 2

    def test_evict_oldest_with_domain_filter(self):
        """evict_oldest with domain filter only removes from that domain."""
        repo = InMemoryFailedOperationRepository()
        _create_entries(repo, domain="payment", count=5)
        _create_entries(repo, domain="inventory", count=3)

        evicted = repo.evict_oldest(3, domain="payment")

        assert evicted == 3
        assert repo.count_by_domain("payment") == 2
        assert repo.count_by_domain("inventory") == 3
        assert repo.count_all() == 5

    def test_evict_oldest_entries_not_retrievable_by_id(self):
        """Evicted entries cannot be retrieved by get_by_id."""
        repo = InMemoryFailedOperationRepository()
        entries = _create_entries(repo, count=5)
        evicted_ids = [entries[0].id, entries[1].id, entries[2].id]

        repo.evict_oldest(3)

        for eid in evicted_ids:
            assert repo.get_by_id(eid) is None

    def test_evict_oldest_count_exceeds_total(self):
        """Evicting more than total removes all entries."""
        repo = InMemoryFailedOperationRepository()
        _create_entries(repo, count=3)

        evicted = repo.evict_oldest(10)

        assert evicted == 3
        assert repo.count_all() == 0


# =============================================================================
# D. Behavior Tests — Domain isolation
# =============================================================================


class TestDomainIsolationBehavior:
    """Domain isolation — eviction does not cross domains."""

    def test_domain_a_eviction_does_not_affect_domain_b(self):
        """Evicting from domain A does not delete domain B entries."""
        repo = InMemoryFailedOperationRepository()
        _create_entries(repo, domain="payment", count=5)
        inv_entries = _create_entries(repo, domain="inventory", count=3)

        repo.evict_oldest(5, domain="payment")

        assert repo.count_by_domain("payment") == 0
        assert repo.count_by_domain("inventory") == 3
        for entry in inv_entries:
            assert repo.get_by_id(entry.id) is not None

    def test_global_eviction_respects_oldest_across_domains(self):
        """Global eviction (no domain filter) evicts oldest across all domains."""
        repo = InMemoryFailedOperationRepository()
        # Create payment entries first (oldest)
        payment_entries = _create_entries(repo, domain="payment", count=3)
        # Then inventory entries (newer)
        _create_entries(repo, domain="inventory", count=3)

        evicted_count = repo.evict_oldest(3)

        assert evicted_count == 3
        # The 3 oldest (payment) should be evicted
        for entry in payment_entries:
            assert repo.get_by_id(entry.id) is None
        assert repo.count_all() == 3
        assert repo.count_by_domain("inventory") == 3
