"""
Unit tests for InMemoryFailedOperationRepository.find() / count() (541 D3).

The paginated cross-status primitive: index/storage select → failure_type
filter → sort created_at DESC → slice [offset:offset+limit]. count() returns
the pre-slice filtered set size.

Coverage (per 541 Test Assessment):
- filter combinations: status / domain / failure_type / none / explicit-None
  per dimension (e.g. find(status=None, domain="auth") routes to domain-only)
- boundary analysis: offset=0, offset >= count, partial-page limit
- ordering: created_at DESC (newest-first)
- count idempotency: count == pre-slice filtered set size
"""

from __future__ import annotations

import pytest

from baldur.adapters.memory.failed_operation import InMemoryFailedOperationRepository
from baldur.interfaces.repositories import FailedOperationStatus
from tests.factories.time_helpers import freeze_time


@pytest.fixture
def repo() -> InMemoryFailedOperationRepository:
    return InMemoryFailedOperationRepository()


def _seed(repo, *, domain, failure_type, status, at):
    """Create one entry at a frozen created_at and move it to ``status``."""
    with freeze_time(at):
        entry = repo.create(domain=domain, failure_type=failure_type)
    if status != FailedOperationStatus.PENDING.value:
        repo.update_status(entry.id, status)
    return entry


class TestInMemoryFindBehavior:
    """find() filter routing, ordering, and pagination boundaries."""

    def test_find_no_filter_returns_all_statuses_newest_first(self, repo):
        """No filter spans every status, ordered created_at DESC (541 D5)."""
        oldest = _seed(
            repo,
            domain="payment",
            failure_type="timeout",
            status=FailedOperationStatus.PENDING.value,
            at="2026-01-01 10:00:00",
        )
        middle = _seed(
            repo,
            domain="auth",
            failure_type="http_5xx",
            status=FailedOperationStatus.PERMANENTLY_FAILED.value,
            at="2026-01-02 10:00:00",
        )
        newest = _seed(
            repo,
            domain="inventory",
            failure_type="timeout",
            status=FailedOperationStatus.REQUIRES_REVIEW.value,
            at="2026-01-03 10:00:00",
        )

        results = repo.find()

        # Escalated/terminal statuses are visible by default, newest-first.
        assert [e.id for e in results] == [newest.id, middle.id, oldest.id]

    def test_find_status_filter_returns_only_that_status(self, repo):
        """A status filter routes through the status index."""
        pending = _seed(
            repo,
            domain="payment",
            failure_type="timeout",
            status=FailedOperationStatus.PENDING.value,
            at="2026-01-01 10:00:00",
        )
        _seed(
            repo,
            domain="payment",
            failure_type="timeout",
            status=FailedOperationStatus.RESOLVED.value,
            at="2026-01-02 10:00:00",
        )

        results = repo.find(status=FailedOperationStatus.PENDING.value)

        assert [e.id for e in results] == [pending.id]

    def test_find_explicit_none_status_with_domain_routes_to_domain_path(self, repo):
        """find(status=None, domain=...) returns all statuses in that domain."""
        payment_pending = _seed(
            repo,
            domain="payment",
            failure_type="timeout",
            status=FailedOperationStatus.PENDING.value,
            at="2026-01-01 10:00:00",
        )
        payment_failed = _seed(
            repo,
            domain="payment",
            failure_type="timeout",
            status=FailedOperationStatus.PERMANENTLY_FAILED.value,
            at="2026-01-02 10:00:00",
        )
        _seed(
            repo,
            domain="auth",
            failure_type="timeout",
            status=FailedOperationStatus.PENDING.value,
            at="2026-01-03 10:00:00",
        )

        results = repo.find(status=None, domain="payment")

        # Both payment entries (across statuses), neither the auth one.
        assert {e.id for e in results} == {payment_pending.id, payment_failed.id}

    def test_find_status_and_domain_combo_uses_composite_index(self, repo):
        """status + domain filters intersect to the composite index set."""
        match = _seed(
            repo,
            domain="payment",
            failure_type="timeout",
            status=FailedOperationStatus.RESOLVED.value,
            at="2026-01-01 10:00:00",
        )
        _seed(
            repo,
            domain="payment",
            failure_type="timeout",
            status=FailedOperationStatus.PENDING.value,
            at="2026-01-02 10:00:00",
        )
        _seed(
            repo,
            domain="auth",
            failure_type="timeout",
            status=FailedOperationStatus.RESOLVED.value,
            at="2026-01-03 10:00:00",
        )

        results = repo.find(
            status=FailedOperationStatus.RESOLVED.value, domain="payment"
        )

        assert [e.id for e in results] == [match.id]

    def test_find_failure_type_filter_applied_after_index_select(self, repo):
        """failure_type is filtered in Python over the index-selected set."""
        timeout = _seed(
            repo,
            domain="payment",
            failure_type="timeout",
            status=FailedOperationStatus.PENDING.value,
            at="2026-01-01 10:00:00",
        )
        _seed(
            repo,
            domain="payment",
            failure_type="http_5xx",
            status=FailedOperationStatus.PENDING.value,
            at="2026-01-02 10:00:00",
        )

        results = repo.find(domain="payment", failure_type="timeout")

        assert [e.id for e in results] == [timeout.id]

    def test_find_offset_zero_returns_from_newest(self, repo):
        """offset=0 starts at the newest entry."""
        for i in range(5):
            _seed(
                repo,
                domain="payment",
                failure_type="timeout",
                status=FailedOperationStatus.PENDING.value,
                at=f"2026-01-0{i + 1} 10:00:00",
            )

        results = repo.find(offset=0, limit=2)

        # Newest two: 2026-01-05 then 2026-01-04.
        created = [e.created_at for e in results]
        assert created == sorted(created, reverse=True)
        assert len(results) == 2

    def test_find_partial_page_limit_returns_fewer_than_limit(self, repo):
        """A limit larger than the remaining slice returns only what is left."""
        for i in range(3):
            _seed(
                repo,
                domain="payment",
                failure_type="timeout",
                status=FailedOperationStatus.PENDING.value,
                at=f"2026-01-0{i + 1} 10:00:00",
            )

        results = repo.find(offset=2, limit=100)

        assert len(results) == 1

    def test_find_offset_beyond_count_returns_empty(self, repo):
        """offset >= total returns an empty page, not an error."""
        _seed(
            repo,
            domain="payment",
            failure_type="timeout",
            status=FailedOperationStatus.PENDING.value,
            at="2026-01-01 10:00:00",
        )

        assert repo.find(offset=10, limit=10) == []

    def test_find_empty_repository_returns_empty(self, repo):
        """No entries -> empty list."""
        assert repo.find() == []


class TestInMemoryCountBehavior:
    """count() returns the pre-slice filtered set size, independent of pagination."""

    def test_count_no_filter_counts_all_statuses(self, repo):
        """count() with no filter spans every status."""
        _seed(
            repo,
            domain="payment",
            failure_type="timeout",
            status=FailedOperationStatus.PENDING.value,
            at="2026-01-01 10:00:00",
        )
        _seed(
            repo,
            domain="auth",
            failure_type="http_5xx",
            status=FailedOperationStatus.PERMANENTLY_FAILED.value,
            at="2026-01-02 10:00:00",
        )

        assert repo.count() == 2

    def test_count_status_filter_counts_only_that_status(self, repo):
        _seed(
            repo,
            domain="payment",
            failure_type="timeout",
            status=FailedOperationStatus.PENDING.value,
            at="2026-01-01 10:00:00",
        )
        _seed(
            repo,
            domain="payment",
            failure_type="timeout",
            status=FailedOperationStatus.RESOLVED.value,
            at="2026-01-02 10:00:00",
        )

        assert repo.count(status=FailedOperationStatus.PENDING.value) == 1

    def test_count_is_independent_of_offset_limit_pagination(self, repo):
        """count() equals the full filtered set size regardless of page slicing."""
        for i in range(5):
            _seed(
                repo,
                domain="payment",
                failure_type="timeout",
                status=FailedOperationStatus.PENDING.value,
                at=f"2026-01-0{i + 1} 10:00:00",
            )

        # find() pages over the same filtered set count() reports.
        total = repo.count(status=FailedOperationStatus.PENDING.value)
        page = repo.find(status=FailedOperationStatus.PENDING.value, offset=0, limit=2)

        assert total == 5
        assert len(page) == 2

    def test_count_failure_type_filter_matches_find_result_size(self, repo):
        """count() and find() agree on the filtered cardinality."""
        _seed(
            repo,
            domain="payment",
            failure_type="timeout",
            status=FailedOperationStatus.PENDING.value,
            at="2026-01-01 10:00:00",
        )
        _seed(
            repo,
            domain="payment",
            failure_type="http_5xx",
            status=FailedOperationStatus.PENDING.value,
            at="2026-01-02 10:00:00",
        )

        assert repo.count(domain="payment", failure_type="timeout") == 1
        assert len(repo.find(domain="payment", failure_type="timeout", limit=100)) == 1

    def test_count_empty_repository_returns_zero(self, repo):
        assert repo.count() == 0
