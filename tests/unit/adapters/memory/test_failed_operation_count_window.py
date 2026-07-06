"""Unit tests for InMemoryFailedOperationRepository.count_created_in_window (622 D3).

The windowed inflow count powers the Error Budget DLQ stats source. It counts
every entry whose ``created_at`` falls in the INCLUSIVE [start, end] range,
across all statuses (an entry created in-window that was later resolved/archived
still consumed budget when it failed).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from baldur.adapters.memory.failed_operation import InMemoryFailedOperationRepository
from tests.factories.time_helpers import freeze_time

# Window under test: [start, end] inclusive.
_START = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
_END = datetime(2026, 6, 8, 0, 0, 0, tzinfo=UTC)


@pytest.fixture
def repo() -> InMemoryFailedOperationRepository:
    return InMemoryFailedOperationRepository()


def _seed_at(repo, when: str) -> None:
    """Create one DLQ entry whose created_at is the frozen ``when``."""
    with freeze_time(when):
        repo.create(domain="payment", failure_type="timeout")


class TestCountCreatedInWindowMemory:
    """count_created_in_window() inclusive-boundary + status-independence."""

    def test_counts_entries_strictly_inside_window(self, repo):
        """Entries created within the window are counted."""
        _seed_at(repo, "2026-06-03 12:00:00")
        _seed_at(repo, "2026-06-05 09:30:00")

        assert repo.count_created_in_window(_START, _END) == 2

    def test_includes_entry_exactly_at_start_boundary(self, repo):
        """The start boundary is inclusive."""
        _seed_at(repo, "2026-06-01 00:00:00")

        assert repo.count_created_in_window(_START, _END) == 1

    def test_includes_entry_exactly_at_end_boundary(self, repo):
        """The end boundary is inclusive."""
        _seed_at(repo, "2026-06-08 00:00:00")

        assert repo.count_created_in_window(_START, _END) == 1

    def test_excludes_entry_just_before_start(self, repo):
        """An entry one second before the start is outside the window."""
        _seed_at(repo, "2026-05-31 23:59:59")

        assert repo.count_created_in_window(_START, _END) == 0

    def test_excludes_entry_just_after_end(self, repo):
        """An entry one second after the end is outside the window."""
        _seed_at(repo, "2026-06-08 00:00:01")

        assert repo.count_created_in_window(_START, _END) == 0

    def test_empty_repository_returns_zero(self, repo):
        """A repository with no entries counts zero."""
        assert repo.count_created_in_window(_START, _END) == 0

    def test_counts_across_all_statuses(self, repo):
        """A resolved entry created in-window still counts (status-independent)."""
        with freeze_time("2026-06-04 10:00:00"):
            entry = repo.create(domain="payment", failure_type="timeout")
        repo.mark_as_resolved(entry.id, resolution_type="manual_fix")

        assert repo.count_created_in_window(_START, _END) == 1

    def test_counts_only_the_in_window_subset(self, repo):
        """Mixed in/out entries: only the in-window subset is counted."""
        _seed_at(repo, "2026-05-30 10:00:00")  # before
        _seed_at(repo, "2026-06-02 10:00:00")  # inside
        _seed_at(repo, "2026-06-07 10:00:00")  # inside
        _seed_at(repo, "2026-06-10 10:00:00")  # after

        assert repo.count_created_in_window(_START, _END) == 2
