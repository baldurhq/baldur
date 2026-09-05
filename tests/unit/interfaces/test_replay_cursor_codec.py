"""Selection-cursor codec for the paged replayable walk.

A cursor is the only thing that makes the on-recovery drain a *chain* rather
than one budget of it: each pass hands the next one the position it stopped at,
over a broker message, so the encoding has to be a plain JSON-safe string and
has to round-trip to a position that compares equal to the one derived from the
live entry it names.

Two failure shapes this file pins:

- a cursor that decodes one float ulp below the entry it names re-selects that
  entry on every pass, so the walk never advances;
- a malformed cursor that raised would strand the queue it was meant to resume,
  because the value arrives from a message the selector does not control.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from baldur.interfaces.repositories import (
    REPLAY_SELECTION_MAX_SCAN,
    ReplayablePage,
    decode_replay_cursor,
    encode_replay_cursor,
    replay_cursor_position,
)

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
WITH_MICROS = datetime(2026, 9, 5, 12, 34, 56, 123456, tzinfo=UTC)


class TestReplayCursorCodecContract:
    """The wire form and its documented degradation, asserted literally."""

    def test_encode_renders_six_decimal_epoch_then_pipe_then_id(self):
        assert encode_replay_cursor(EPOCH, "42") == "0.000000|42"

    def test_encode_keeps_the_microsecond_component(self):
        """Truncating to whole seconds collapses a whole second of entries
        into one indistinguishable position."""
        assert encode_replay_cursor(WITH_MICROS, "7").endswith(".123456|7")

    def test_scan_bound_is_ten_thousand_members(self):
        assert REPLAY_SELECTION_MAX_SCAN == 10_000

    def test_replayable_page_defaults_to_an_empty_unexhausted_page(self):
        page = ReplayablePage()

        assert page.entries == []
        assert page.next_cursor is None
        assert page.scan_exhausted is False

    @pytest.mark.parametrize(
        "cursor",
        [
            None,
            "",
            "1757000000.000000",  # no separator at all
            "not-a-float|42",
            "|42",
        ],
    )
    def test_unusable_cursor_decodes_to_none_instead_of_raising(self, cursor):
        """Reads as "no cursor": refusing to select would strand the queue."""
        assert decode_replay_cursor(cursor) is None

    def test_id_half_may_itself_contain_the_separator_free_form(self):
        """Redis mints composite ids; only the FIRST separator splits."""
        decoded = decode_replay_cursor("1757000000.500000|dlq:host:1|2")

        assert decoded == (1757000000.5, "dlq:host:1|2")

    def test_empty_id_half_still_decodes_so_the_walk_can_advance(self):
        assert decode_replay_cursor("0.000000|") == (0.0, "")


class TestReplayCursorRoundTripBehavior:
    """Positions derived two ways must compare equal, and stay equal."""

    @pytest.mark.parametrize(
        "moment",
        [
            EPOCH,
            WITH_MICROS,
            datetime(2026, 9, 5, 12, 34, 56, 1, tzinfo=UTC),
            datetime(2026, 9, 5, 12, 34, 56, 999999, tzinfo=UTC),
        ],
    )
    def test_decoded_cursor_equals_the_live_entry_position(self, moment):
        """The ulp guard: an entry whose own cursor sorts below it is selected
        forever, because the selector skips positions ``<= floor``."""
        cursor = encode_replay_cursor(moment, "e-1")

        assert decode_replay_cursor(cursor) == replay_cursor_position(moment, "e-1")

    def test_encode_decode_encode_is_idempotent(self):
        first = encode_replay_cursor(WITH_MICROS, "e-1")
        decoded = decode_replay_cursor(first)
        assert decoded is not None

        assert f"{decoded[0]:.6f}|{decoded[1]}" == first

    def test_positions_order_by_timestamp_then_id(self):
        """The pair is the ordering key: a walk that ordered on the timestamp
        alone could advance past same-timestamp entries it never returned."""
        same_moment = WITH_MICROS

        assert replay_cursor_position(same_moment, "a") < replay_cursor_position(
            same_moment, "b"
        )
        assert replay_cursor_position(EPOCH, "z") < replay_cursor_position(
            same_moment, "a"
        )
