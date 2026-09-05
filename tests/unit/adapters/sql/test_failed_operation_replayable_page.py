"""Keyset-paged replayable selection on the SQL DLQ store.

This adapter is the one where the cursor is a *bound value*, not a Python
comparison, so it is the only place a cursor can be encoded in a form the
store sorts differently from the rows it names. sqlite keeps ``created_at`` as
TEXT in the driver's own rendering, and a seek that bound a differently
rendered timestamp matches nothing — which a caller cannot tell apart from a
drained queue, because an empty page is exactly how the queue reports being
drained. Every cursor test here therefore asserts the *rows* a follow-up call
selects, never just that a cursor came back.

The scan bound is real here: the capture-source predicate lives inside the JSON
payload and cannot be pushed into the index, so a window can legitimately hold
no match at all and the walk has to be able to stop and say so.
"""

from __future__ import annotations

import json

import pytest

from baldur.adapters.sql.failed_operation import (
    SQLFailedOperationRepository,
    _coerce_row_id,
)
from baldur.interfaces.repositories import (
    REPLAY_SELECTION_MAX_SCAN,
    FailedOperationStatus,
    decode_replay_cursor,
)
from baldur.models.dlq import POLICY_CHAIN_CAPTURE_SOURCE
from tests.factories.time_helpers import freeze_time

OPEN_CIRCUIT = "CIRCUIT_BREAKER_OPEN"
DOMAIN = "payment_api"


@pytest.fixture
def dlq(get_sqlite_conn) -> SQLFailedOperationRepository:
    return SQLFailedOperationRepository(get_sqlite_conn)


def _seed(
    dlq,
    *,
    at,
    domain=DOMAIN,
    failure_type=OPEN_CIRCUIT,
    source=POLICY_CHAIN_CAPTURE_SOURCE,
    retry_count=0,
    status=FailedOperationStatus.PENDING.value,
):
    """One row at a frozen ``created_at``, optionally carrying a source."""
    metadata = {} if source is None else {"source": source}
    with freeze_time(at):
        entry = dlq.create(
            domain=domain,
            failure_type=failure_type,
            metadata=metadata,
            retry_count=retry_count,
            max_retries=5,
        )
    if status != FailedOperationStatus.PENDING.value:
        dlq.update_status(entry.id, status)
    return entry


def _bulk_seed(dlq, conn, *, count, at, source, failure_type=OPEN_CIRCUIT):
    """Insert ``count`` rows straight through the driver.

    Going through ``create()`` for a scan-bound-sized population would spend
    the test's whole budget on inserts; the columns bound here are exactly the
    ones ``_row_to_data`` reads back.
    """
    dlq._ensure_schema_ready()
    payload = json.dumps({"metadata": {} if source is None else {"source": source}})
    conn.executemany(
        "INSERT INTO baldur_dlq "
        "(domain, failure_type, status, retry_count, max_retries, error_code, "
        "created_at, updated_at, data) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                DOMAIN,
                failure_type,
                FailedOperationStatus.PENDING.value,
                0,
                5,
                "",
                at,
                at,
                payload,
            )
        ]
        * count,
    )
    conn.commit()


class TestCoerceRowIdContract:
    """The DTO id is opaque; the PK column is a dense integer."""

    def test_numeric_id_binds_to_the_integer_column(self):
        assert _coerce_row_id("42") == 42

    @pytest.mark.parametrize("entry_id", ["dlq:host-1:7", "", None])
    def test_non_integer_id_degrades_to_none_instead_of_raising(self, entry_id):
        """A cursor minted by another adapter must degrade to a
        timestamp-only resume rather than blow up on the driver."""
        assert _coerce_row_id(entry_id) is None


class TestSQLReplayablePageBehavior:
    """Eligibility, the source residual, and the keyset seek."""

    def test_page_returns_pending_entries_oldest_first(self, dlq):
        newest = _seed(dlq, at="2026-09-05 10:00:03")
        oldest = _seed(dlq, at="2026-09-05 10:00:01")
        middle = _seed(dlq, at="2026-09-05 10:00:02")

        page = dlq.find_replayable_page(max_retries=5)

        assert [e.id for e in page.entries] == [oldest.id, middle.id, newest.id]

    def test_entry_at_or_past_max_retries_is_not_selectable(self, dlq):
        eligible = _seed(dlq, at="2026-09-05 10:00:01", retry_count=2)
        _seed(dlq, at="2026-09-05 10:00:02", retry_count=3)

        page = dlq.find_replayable_page(max_retries=3)

        assert [e.id for e in page.entries] == [eligible.id]

    def test_non_pending_entry_is_not_selectable(self, dlq):
        pending = _seed(dlq, at="2026-09-05 10:00:01")
        _seed(
            dlq,
            at="2026-09-05 10:00:02",
            status=FailedOperationStatus.REQUIRES_REVIEW.value,
        )

        page = dlq.find_replayable_page(max_retries=5)

        assert [e.id for e in page.entries] == [pending.id]

    def test_domain_and_failure_type_narrow_the_selection(self, dlq):
        wanted = _seed(dlq, at="2026-09-05 10:00:01", failure_type="TIMEOUT")
        _seed(dlq, at="2026-09-05 10:00:02", failure_type=OPEN_CIRCUIT)
        _seed(dlq, at="2026-09-05 10:00:03", domain="point_api", failure_type="TIMEOUT")

        page = dlq.find_replayable_page(
            max_retries=5, domain=DOMAIN, failure_type="TIMEOUT"
        )

        assert [e.id for e in page.entries] == [wanted.id]

    def test_source_match_selects_only_that_capture_layer(self, dlq):
        policy_chain = _seed(dlq, at="2026-09-05 10:00:01")
        _seed(dlq, at="2026-09-05 10:00:02", source="middleware")

        page = dlq.find_replayable_page(
            max_retries=5, source=POLICY_CHAIN_CAPTURE_SOURCE
        )

        assert [e.id for e in page.entries] == [policy_chain.id]

    def test_entry_without_a_source_key_is_excluded_by_a_source_filter(self, dlq):
        _seed(dlq, at="2026-09-05 10:00:01", source=None)

        page = dlq.find_replayable_page(
            max_retries=5, source=POLICY_CHAIN_CAPTURE_SOURCE
        )

        assert page.entries == []

    def test_no_source_filter_selects_every_capture_layer(self, dlq):
        policy_chain = _seed(dlq, at="2026-09-05 10:00:01")
        middleware = _seed(dlq, at="2026-09-05 10:00:02", source="middleware")

        page = dlq.find_replayable_page(max_retries=5)

        assert [e.id for e in page.entries] == [policy_chain.id, middleware.id]

    def test_limit_truncates_to_the_oldest_matches(self, dlq):
        first = _seed(dlq, at="2026-09-05 10:00:01")
        second = _seed(dlq, at="2026-09-05 10:00:02")
        _seed(dlq, at="2026-09-05 10:00:03")

        page = dlq.find_replayable_page(max_retries=5, limit=2)

        assert [e.id for e in page.entries] == [first.id, second.id]

    # -- the seek ------------------------------------------------------------

    def test_cursor_from_page_one_selects_page_two(self, dlq):
        """The rendering defect returns zero rows here and nowhere else."""
        seeded = [
            _seed(dlq, at=f"2026-09-05 10:00:{second:02d}") for second in range(1, 6)
        ]

        first = dlq.find_replayable_page(max_retries=5, limit=2)
        second = dlq.find_replayable_page(
            max_retries=5, limit=2, cursor=first.next_cursor
        )

        assert [e.id for e in first.entries] == [seeded[0].id, seeded[1].id]
        assert [e.id for e in second.entries] == [seeded[2].id, seeded[3].id]

    def test_cursor_with_a_non_zero_microsecond_resumes_correctly(self, dlq):
        """The encoding renders six decimals; the seek rebuilds a datetime
        from them. A truncating round trip re-selects the entry it names."""
        first = _seed(dlq, at="2026-09-05 10:00:01.123456")
        second = _seed(dlq, at="2026-09-05 10:00:01.654321")

        page_one = dlq.find_replayable_page(max_retries=5, limit=1)
        page_two = dlq.find_replayable_page(
            max_retries=5, limit=10, cursor=page_one.next_cursor
        )

        assert [e.id for e in page_one.entries] == [first.id]
        assert [e.id for e in page_two.entries] == [second.id]

    def test_same_timestamp_entries_are_separated_by_the_id_half(self, dlq):
        """A seek on the timestamp alone would either re-select the whole
        second or skip the rest of it."""
        first = _seed(dlq, at="2026-09-05 10:00:01")
        second = _seed(dlq, at="2026-09-05 10:00:01")
        third = _seed(dlq, at="2026-09-05 10:00:01")

        page_one = dlq.find_replayable_page(max_retries=5, limit=1)
        page_two = dlq.find_replayable_page(
            max_retries=5, limit=10, cursor=page_one.next_cursor
        )

        assert [e.id for e in page_one.entries] == [first.id]
        assert [e.id for e in page_two.entries] == [second.id, third.id]

    def test_cursor_whose_id_half_is_foreign_falls_back_to_a_timestamp_seek(self, dlq):
        """It must still select — a raise here strands the queue."""
        first = _seed(dlq, at="2026-09-05 10:00:01")
        second = _seed(dlq, at="2026-09-05 10:00:02")

        page = dlq.find_replayable_page(
            max_retries=5,
            cursor=f"{first.created_at.timestamp():.6f}|dlq:host-1:9",
        )

        # No id half to compare, so the entry at the cursor's own second is
        # re-selected rather than skipped: over-selection, never loss.
        assert [e.id for e in page.entries] == [first.id, second.id]

    def test_cursor_past_the_end_returns_nothing_and_advances_no_further(self, dlq):
        _seed(dlq, at="2026-09-05 10:00:01")
        _seed(dlq, at="2026-09-05 10:00:02")

        exhausted = dlq.find_replayable_page(max_retries=5, limit=10)
        past_end = dlq.find_replayable_page(
            max_retries=5, limit=10, cursor=exhausted.next_cursor
        )

        assert past_end.entries == []
        assert past_end.next_cursor is None
        assert past_end.scan_exhausted is False

    def test_unusable_cursor_selects_from_the_beginning(self, dlq):
        first = _seed(dlq, at="2026-09-05 10:00:01")

        page = dlq.find_replayable_page(max_retries=5, cursor="not-a-cursor")

        assert [e.id for e in page.entries] == [first.id]

    def test_walking_a_backlog_page_by_page_reaches_every_entry_once(self, dlq):
        seeded = [
            _seed(dlq, at=f"2026-09-05 10:00:{second:02d}") for second in range(1, 8)
        ]

        collected: list[str] = []
        cursor: str | None = None
        for _ in range(len(seeded) + 1):
            page = dlq.find_replayable_page(max_retries=5, limit=2, cursor=cursor)
            if not page.entries:
                break
            collected.extend(e.id for e in page.entries)
            cursor = page.next_cursor

        assert collected == [e.id for e in seeded]


class TestSQLReplayablePageScanBoundBehavior:
    """A window can hold no match at all, so the walk needs a way to stop."""

    @pytest.fixture
    def starved_domain(self, dlq, sqlite_conn):
        """One domain whose whole scan budget is spent on non-matching rows,
        with the entries the lane wants sitting immediately behind them."""
        _bulk_seed(
            dlq,
            sqlite_conn,
            count=REPLAY_SELECTION_MAX_SCAN,
            at="2026-09-05 10:00:01",
            source="middleware",
        )
        _bulk_seed(
            dlq,
            sqlite_conn,
            count=100,
            at="2026-09-05 11:00:00",
            source=POLICY_CHAIN_CAPTURE_SOURCE,
        )
        return dlq

    def test_first_page_reports_the_bound_rather_than_an_empty_pool(
        self, starved_domain
    ):
        page = starved_domain.find_replayable_page(
            max_retries=5,
            domain=DOMAIN,
            source=POLICY_CHAIN_CAPTURE_SOURCE,
            limit=50,
        )

        assert page.entries == []
        # The distinction the caller needs: "nothing left" vs "more behind a
        # prefix of another capture layer".
        assert page.scan_exhausted is True

    def test_bound_stop_still_advances_the_cursor(self, starved_domain):
        """A page that examined rows but returned none must still hand back a
        position, or the chain re-walks the same prefix forever."""
        page = starved_domain.find_replayable_page(
            max_retries=5,
            domain=DOMAIN,
            source=POLICY_CHAIN_CAPTURE_SOURCE,
            limit=50,
        )

        position = decode_replay_cursor(page.next_cursor)
        assert position is not None
        assert int(position[1]) == REPLAY_SELECTION_MAX_SCAN

    def test_chained_pages_reach_the_starved_entries(self, starved_domain):
        """The starvation this selector removes: the wanted entries sit behind
        a whole scan bound of another capture layer's rows."""
        collected: list[str] = []
        cursor: str | None = None
        calls = 0

        while calls < 5:
            page = starved_domain.find_replayable_page(
                max_retries=5,
                domain=DOMAIN,
                source=POLICY_CHAIN_CAPTURE_SOURCE,
                limit=100,
                cursor=cursor,
            )
            calls += 1
            collected.extend(e.id for e in page.entries)
            if not page.entries and not page.scan_exhausted:
                break
            cursor = page.next_cursor

        assert len(collected) == 100
        assert len(set(collected)) == 100
