"""Paged replayable selection on the in-memory DLQ store.

The zero-config backing. Its index is a plain ``set``, so it has no useful
iteration order and no scan bound to stop on: the pass walks the whole status
(or status+domain) index and keeps the ``limit`` smallest by ``(created_at,
id)``. Two things separate it from the indexed adapters, and this file pins
both:

- ordering cannot be inherited from the index, so it must be re-established
  from the position key on every call — a walk that returned members in set
  order would hand a chain a cursor that skips everything it never looked at;
- ``scan_exhausted`` is always False, which is what tells a caller "the pool
  really is empty" rather than "the walk gave up".
"""

from __future__ import annotations

import pytest

from baldur.adapters.memory.failed_operation import InMemoryFailedOperationRepository
from baldur.interfaces.repositories import (
    FailedOperationStatus,
    decode_replay_cursor,
    replay_cursor_position,
)
from baldur.models.dlq import POLICY_CHAIN_CAPTURE_SOURCE
from tests.factories.time_helpers import freeze_time

OPEN_CIRCUIT = "CIRCUIT_BREAKER_OPEN"


@pytest.fixture
def repo() -> InMemoryFailedOperationRepository:
    return InMemoryFailedOperationRepository()


def _seed(
    repo,
    *,
    at,
    domain="payment_api",
    failure_type=OPEN_CIRCUIT,
    source=POLICY_CHAIN_CAPTURE_SOURCE,
    retry_count=0,
    status=FailedOperationStatus.PENDING.value,
):
    """One entry at a frozen ``created_at``, optionally carrying a source."""
    metadata = {} if source is None else {"source": source}
    with freeze_time(at):
        entry = repo.create(
            domain=domain,
            failure_type=failure_type,
            metadata=metadata,
            retry_count=retry_count,
            max_retries=5,
        )
    if status != FailedOperationStatus.PENDING.value:
        repo.update_status(entry.id, status)
    return entry


class TestMemoryReplayablePageBehavior:
    """Eligibility, ordering, the source predicate, and where the walk stops."""

    def test_page_returns_pending_entries_oldest_first(self, repo):
        newest = _seed(repo, at="2026-09-05 10:00:03")
        oldest = _seed(repo, at="2026-09-05 10:00:01")
        middle = _seed(repo, at="2026-09-05 10:00:02")

        page = repo.find_replayable_page(max_retries=5)

        assert [e.id for e in page.entries] == [oldest.id, middle.id, newest.id]

    def test_same_timestamp_entries_come_back_in_id_order(self, repo):
        """Ten entries share one ``created_at`` so only the id half can order
        them, and there are enough of them that string order differs from
        creation order (id "10" sorts below id "2")."""
        for _ in range(10):
            _seed(repo, at="2026-09-05 10:00:00")

        page = repo.find_replayable_page(max_retries=5)

        ids = [e.id for e in page.entries]
        assert ids == sorted(ids)
        assert ids != sorted(ids, key=int), "ids must not order numerically here"

    def test_entry_at_or_past_max_retries_is_not_selectable(self, repo):
        eligible = _seed(repo, at="2026-09-05 10:00:01", retry_count=2)
        _seed(repo, at="2026-09-05 10:00:02", retry_count=3)

        page = repo.find_replayable_page(max_retries=3)

        assert [e.id for e in page.entries] == [eligible.id]

    def test_non_pending_entry_is_not_selectable(self, repo):
        pending = _seed(repo, at="2026-09-05 10:00:01")
        _seed(
            repo,
            at="2026-09-05 10:00:02",
            status=FailedOperationStatus.REQUIRES_REVIEW.value,
        )

        page = repo.find_replayable_page(max_retries=5)

        assert [e.id for e in page.entries] == [pending.id]

    def test_failure_type_filter_excludes_other_types(self, repo):
        wanted = _seed(repo, at="2026-09-05 10:00:01", failure_type="TIMEOUT")
        _seed(repo, at="2026-09-05 10:00:02", failure_type=OPEN_CIRCUIT)

        page = repo.find_replayable_page(max_retries=5, failure_type="TIMEOUT")

        assert [e.id for e in page.entries] == [wanted.id]

    def test_domain_filter_routes_through_the_status_domain_index(self, repo):
        wanted = _seed(repo, at="2026-09-05 10:00:01", domain="payment_api")
        _seed(repo, at="2026-09-05 10:00:02", domain="point_api")

        page = repo.find_replayable_page(max_retries=5, domain="payment_api")

        assert [e.id for e in page.entries] == [wanted.id]

    # -- the source predicate ------------------------------------------------

    def test_source_match_selects_only_that_capture_layer(self, repo):
        policy_chain = _seed(repo, at="2026-09-05 10:00:01")
        _seed(repo, at="2026-09-05 10:00:02", source="middleware")

        page = repo.find_replayable_page(
            max_retries=5, source=POLICY_CHAIN_CAPTURE_SOURCE
        )

        assert [e.id for e in page.entries] == [policy_chain.id]

    def test_entry_without_a_source_key_is_excluded_by_a_source_filter(self, repo):
        _seed(repo, at="2026-09-05 10:00:01", source=None)

        page = repo.find_replayable_page(
            max_retries=5, source=POLICY_CHAIN_CAPTURE_SOURCE
        )

        assert page.entries == []

    def test_entry_with_non_dict_metadata_is_excluded_by_a_source_filter(self, repo):
        """A payload whose ``metadata`` decoded to something that is not a
        mapping must not be read as a source match."""
        entry = _seed(repo, at="2026-09-05 10:00:01")
        repo._storage[entry.id].metadata = []

        page = repo.find_replayable_page(
            max_retries=5, source=POLICY_CHAIN_CAPTURE_SOURCE
        )

        assert page.entries == []

    def test_no_source_filter_selects_every_capture_layer(self, repo):
        policy_chain = _seed(repo, at="2026-09-05 10:00:01")
        middleware = _seed(repo, at="2026-09-05 10:00:02", source="middleware")
        unsourced = _seed(repo, at="2026-09-05 10:00:03", source=None)

        page = repo.find_replayable_page(max_retries=5)

        assert [e.id for e in page.entries] == [
            policy_chain.id,
            middleware.id,
            unsourced.id,
        ]

    # -- limit and cursor boundaries ----------------------------------------

    def test_limit_truncates_to_the_oldest_matches(self, repo):
        first = _seed(repo, at="2026-09-05 10:00:01")
        second = _seed(repo, at="2026-09-05 10:00:02")
        _seed(repo, at="2026-09-05 10:00:03")

        page = repo.find_replayable_page(max_retries=5, limit=2)

        assert [e.id for e in page.entries] == [first.id, second.id]

    def test_truncated_page_stops_its_cursor_at_the_last_entry_returned(self, repo):
        """Advancing to the highest position *examined* would step over the
        matches the limit left behind."""
        _seed(repo, at="2026-09-05 10:00:01")
        second = _seed(repo, at="2026-09-05 10:00:02")
        _seed(repo, at="2026-09-05 10:00:03")

        page = repo.find_replayable_page(max_retries=5, limit=2)

        assert decode_replay_cursor(page.next_cursor) == replay_cursor_position(
            second.created_at, second.id
        )

    def test_untruncated_page_advances_past_the_entries_it_rejected(self, repo):
        """The rejected tail was examined and can never match, so a follow-up
        that re-walked it would spend its scan on the same rejects."""
        match = _seed(repo, at="2026-09-05 10:00:01")
        rejected = _seed(repo, at="2026-09-05 10:00:05", source="middleware")

        page = repo.find_replayable_page(
            max_retries=5, source=POLICY_CHAIN_CAPTURE_SOURCE, limit=100
        )

        assert [e.id for e in page.entries] == [match.id]
        assert decode_replay_cursor(page.next_cursor) == replay_cursor_position(
            rejected.created_at, rejected.id
        )

    def test_cursor_resumes_strictly_after_the_position_it_names(self, repo):
        first = _seed(repo, at="2026-09-05 10:00:01")
        second = _seed(repo, at="2026-09-05 10:00:02")
        third = _seed(repo, at="2026-09-05 10:00:03")

        first_page = repo.find_replayable_page(max_retries=5, limit=1)
        second_page = repo.find_replayable_page(
            max_retries=5, limit=10, cursor=first_page.next_cursor
        )

        assert [e.id for e in first_page.entries] == [first.id]
        assert [e.id for e in second_page.entries] == [second.id, third.id]

    def test_cursor_past_the_end_returns_nothing_and_advances_no_further(self, repo):
        _seed(repo, at="2026-09-05 10:00:01")
        last = _seed(repo, at="2026-09-05 10:00:02")

        exhausted = repo.find_replayable_page(max_retries=5, limit=10)
        past_end = repo.find_replayable_page(
            max_retries=5, limit=10, cursor=exhausted.next_cursor
        )

        assert decode_replay_cursor(exhausted.next_cursor) == replay_cursor_position(
            last.created_at, last.id
        )
        assert past_end.entries == []
        # None means "keep the cursor you had" — not "start over".
        assert past_end.next_cursor is None

    def test_unusable_cursor_selects_from_the_beginning(self, repo):
        """The value arrives over a broker message; refusing to select would
        strand the queue it was meant to resume."""
        first = _seed(repo, at="2026-09-05 10:00:01")

        page = repo.find_replayable_page(max_retries=5, cursor="not-a-cursor")

        assert [e.id for e in page.entries] == [first.id]

    def test_walking_a_backlog_page_by_page_reaches_every_entry_once(self, repo):
        seeded = [
            _seed(repo, at=f"2026-09-05 10:00:{second:02d}") for second in range(1, 8)
        ]

        collected: list[str] = []
        cursor: str | None = None
        for _ in range(len(seeded) + 1):
            page = repo.find_replayable_page(max_retries=5, limit=2, cursor=cursor)
            if not page.entries:
                break
            collected.extend(e.id for e in page.entries)
            cursor = page.next_cursor

        assert collected == [e.id for e in seeded]

    def test_memory_walk_never_reports_a_scan_bound_stop(self, repo):
        """It has no bound: an empty page here really does mean an empty pool."""
        _seed(repo, at="2026-09-05 10:00:01", source="middleware")

        page = repo.find_replayable_page(
            max_retries=5, source=POLICY_CHAIN_CAPTURE_SOURCE
        )

        assert page.entries == []
        assert page.scan_exhausted is False
