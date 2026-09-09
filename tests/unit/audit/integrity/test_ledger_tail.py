"""Unit tests for ``baldur.audit.integrity.ledger_tail``.

The ledger tail is what lets a sequence source notice that it has fallen
behind the entries already on disk. Three properties a re-implementation gets
wrong, and each one is a silent duplicate-sequence ledger when it is wrong:

- the tail is the **highest** positive sequence in the window, not the last
  line — the manager lock is released before the adapter appends, so siblings
  append out of mint order;
- file selection is **exact**, derived from the writing adapter's own filename
  pattern and rotation mode — a glob over ``audit_*.jsonl`` also matches every
  partitioned sibling, and ``worker`` also matches ``celery_worker``;
- an unreadable ledger **raises** rather than reading as "no ledger", because
  "no ledger" is the one answer that puts sequence 1 inside a live one.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.1 Contract (the five window/budget constants, the compiled filename shape).
- §8.2 Exception/edge cases (unreadable directory, unreadable file, a row
  larger than the window cap, an exhausted walk budget).
- Boundary analysis (initial window, the 32-line floor, the head probe).
- Side effects (the ``ledger_tail.window_capped`` WARNING and its byte count).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur.audit.integrity import ledger_tail as ledger_tail_module
from baldur.audit.integrity.ledger_tail import (
    LEDGER_TAIL_HEAD_PROBE_BYTES,
    LEDGER_TAIL_INITIAL_WINDOW_BYTES,
    LEDGER_TAIL_MAX_TOTAL_BYTES,
    LEDGER_TAIL_MAX_WINDOW_BYTES,
    LEDGER_TAIL_MIN_LINES,
    LedgerTailReader,
    ledger_filename_regex,
    list_ledger_files,
)
from tests.factories.writable_dir import log_events

# A real audited row is ~550 B: two 64-hex hashes plus the envelope. Rows are
# padded to exactly this so a window's line count is arithmetic, not a guess.
_REAL_ROW_BYTES = 550

_HASH_A = "a" * 64
_HASH_B = "b" * 64


def _row(
    sequence: int | None,
    current_hash: str = _HASH_A,
    *,
    with_integrity: bool = True,
    pad_to: int = 0,
) -> str:
    """Build one ledger line, optionally padded to an exact byte width."""
    entry: dict = {"event_type": "test.event", "actor": "tester"}
    if with_integrity:
        entry["integrity"] = {
            "sequence": sequence,
            "previous_hash": _HASH_B,
            "current_hash": current_hash,
        }
    line = json.dumps(entry)
    if pad_to:
        # ``pad_to`` counts the line without its newline terminator.
        filler = pad_to - len(json.dumps({**entry, "pad": ""})) - 1
        entry["pad"] = "p" * max(filler, 0)
        line = json.dumps(entry)
    return line


def _write_ledger(
    path: Path, lines: list[str], *, trailing_newline: bool = True
) -> None:
    """Write ``lines`` as JSONL, optionally leaving the last line unterminated."""
    body = "\n".join(lines)
    if trailing_newline:
        body += "\n"
    path.write_text(body, encoding="utf-8")


# =============================================================================
# Contract — the shipped window and budget constants
# =============================================================================


class TestLedgerTailConstantsContract:
    """The five bounds the reader is sold on, hardcoded.

    Each one is a boundary the write path pays on every audited entry, so a
    silent widening is a latency regression inside the chain's exclusive
    section and a silent narrowing is a tail the reader can no longer see.
    """

    def test_initial_window_holds_well_over_the_line_floor_of_real_rows(self):
        """32 KiB over ~550 B rows is ~59 lines — the whole point of not
        reusing the boot sync's old 10 KiB, which held ~18."""
        assert LEDGER_TAIL_INITIAL_WINDOW_BYTES == 32768
        assert (
            LEDGER_TAIL_INITIAL_WINDOW_BYTES // _REAL_ROW_BYTES > LEDGER_TAIL_MIN_LINES
        )

    def test_min_lines_floor_exceeds_the_single_volume_writer_count(self):
        """Gunicorn workers plus a Celery worker plus cron — the writers that
        can append out of mint order onto one volume."""
        assert LEDGER_TAIL_MIN_LINES == 32

    def test_max_window_caps_one_file_at_eight_mebibytes(self):
        assert LEDGER_TAIL_MAX_WINDOW_BYTES == 8 * 1024 * 1024

    def test_head_probe_matches_the_initial_window(self):
        """A chain-less file costs head + tail, so 64 KiB total — the bound
        that keeps the walk affordable over years of daily files."""
        assert LEDGER_TAIL_HEAD_PROBE_BYTES == 32768
        assert LEDGER_TAIL_HEAD_PROBE_BYTES == LEDGER_TAIL_INITIAL_WINDOW_BYTES

    def test_walk_budget_admits_about_a_thousand_chain_less_files(self):
        assert LEDGER_TAIL_MAX_TOTAL_BYTES == 64 * 1024 * 1024
        chain_less_file_cost = (
            LEDGER_TAIL_INITIAL_WINDOW_BYTES + LEDGER_TAIL_HEAD_PROBE_BYTES
        )
        assert LEDGER_TAIL_MAX_TOTAL_BYTES // chain_less_file_cost == 1024


# =============================================================================
# Contract — the compiled filename shape
# =============================================================================


class TestLedgerFilenameRegexContract:
    """The exact shape an adapter's pattern produces, anchored end to end."""

    @pytest.mark.parametrize(
        ("pattern", "rotate_daily", "name", "matches"),
        [
            ("audit_{date}.jsonl", True, "audit_2026-09-07.jsonl", True),
            ("audit_{date}.jsonl", True, "audit_2026-09-07_worker.jsonl", False),
            ("audit_{date}.jsonl", True, "audit_all.jsonl", False),
            ("audit_{date}_worker.jsonl", True, "audit_2026-09-07_worker.jsonl", True),
            (
                "audit_{date}_worker.jsonl",
                True,
                "audit_2026-09-07_celery_worker.jsonl",
                False,
            ),
            ("audit_{date}.jsonl", False, "audit_all.jsonl", True),
            ("audit_{date}.jsonl", False, "audit_2026-09-07.jsonl", False),
            ("ledger_{date}.ndjson", True, "ledger_2026-09-07.ndjson", True),
            ("ledger_{date}.ndjson", True, "audit_2026-09-07.jsonl", False),
        ],
        ids=[
            "default_matches_its_own",
            "default_rejects_partitioned_sibling",
            "default_rejects_unrotated",
            "partition_matches_its_own",
            "partition_worker_rejects_celery_worker",
            "unrotated_matches_the_all_token",
            "unrotated_rejects_a_date",
            "operator_override_matches",
            "operator_override_rejects_the_default",
        ],
    )
    def test_pattern_selects_exactly_the_names_its_adapter_writes(
        self, pattern, rotate_daily, name, matches
    ):
        regex = ledger_filename_regex(pattern, rotate_daily)

        assert bool(regex.fullmatch(name)) is matches

    @pytest.mark.parametrize(
        "name",
        ["xaudit_2026-09-07.jsonl", "audit_2026-09-07.jsonl.bak"],
        ids=["prefix_noise", "suffix_noise"],
    )
    def test_the_shape_is_anchored_at_both_ends(self, name):
        """``fullmatch`` and nothing else: a rotated-away ``.bak`` copy read as
        the ledger would hand the source a tail no live writer maintains."""
        regex = ledger_filename_regex("audit_{date}.jsonl")

        assert regex.fullmatch(name) is None
        assert regex.search(name) is not None

    def test_rotate_daily_defaults_to_the_adapter_default(self):
        regex = ledger_filename_regex("audit_{date}.jsonl")

        assert regex.fullmatch("audit_2026-09-07.jsonl") is not None


# =============================================================================
# Behavior — enumeration
# =============================================================================


class TestListLedgerFilesBehavior:
    """Enumeration order, and the directory error that must not be swallowed."""

    def test_matching_files_come_back_newest_first(self, tmp_path):
        regex = ledger_filename_regex("audit_{date}.jsonl")
        for day in ("2026-09-05", "2026-09-07", "2026-09-06"):
            (tmp_path / f"audit_{day}.jsonl").write_text("", encoding="utf-8")

        found = list_ledger_files(tmp_path, regex)

        assert [path.name for path in found] == [
            "audit_2026-09-07.jsonl",
            "audit_2026-09-06.jsonl",
            "audit_2026-09-05.jsonl",
        ]

    def test_a_partitioned_sibling_is_not_listed_for_the_default_pattern(
        self, tmp_path
    ):
        regex = ledger_filename_regex("audit_{date}.jsonl")
        (tmp_path / "audit_2026-09-07.jsonl").write_text("", encoding="utf-8")
        (tmp_path / "audit_2026-09-07_worker.jsonl").write_text("", encoding="utf-8")

        found = list_ledger_files(tmp_path, regex)

        assert [path.name for path in found] == ["audit_2026-09-07.jsonl"]

    def test_a_missing_directory_is_an_empty_list(self, tmp_path):
        regex = ledger_filename_regex("audit_{date}.jsonl")

        assert list_ledger_files(tmp_path / "absent", regex) == []

    def test_an_unreadable_directory_raises_instead_of_reading_as_empty(self, tmp_path):
        """The whole reason enumeration is not ``Path.glob``: the glob selector
        swallows this ``OSError``, which turns "I cannot read this directory"
        into "this directory holds no ledger"."""
        regex = ledger_filename_regex("audit_{date}.jsonl")

        with patch.object(
            Path, "iterdir", autospec=True, side_effect=PermissionError("denied")
        ):
            with pytest.raises(PermissionError):
                list_ledger_files(tmp_path, regex)

    def test_enumeration_never_goes_through_glob(self, tmp_path):
        """The negative half. ``glob`` would pass every other case in this
        class and fail only the unreadable directory, in production."""
        regex = ledger_filename_regex("audit_{date}.jsonl")
        (tmp_path / "audit_2026-09-07.jsonl").write_text("", encoding="utf-8")

        with (
            patch.object(Path, "glob", autospec=True) as glob_spy,
            patch.object(Path, "rglob", autospec=True) as rglob_spy,
        ):
            list_ledger_files(tmp_path, regex)

        glob_spy.assert_not_called()
        rglob_spy.assert_not_called()


# =============================================================================
# Behavior — the read window
# =============================================================================


class TestLedgerTailWindowBehavior:
    """How much of a file one read costs, and when the window doubles."""

    def test_a_twenty_kibibyte_last_line_is_found_in_the_first_window(self, tmp_path):
        """The boot sync's old 10 KiB window returned ``(0, "")`` here —
        silently, which is the answer that re-mints from 1."""
        path = tmp_path / "audit_2026-09-07.jsonl"
        _write_ledger(
            path,
            [
                _row(41, pad_to=_REAL_ROW_BYTES),
                _row(42, current_hash=_HASH_B, pad_to=20 * 1024),
            ],
        )
        reader = LedgerTailReader(tmp_path)

        tail = reader.read()

        assert tail is not None
        assert tail.sequence == 42
        assert tail.current_hash == _HASH_B

    def test_a_ten_thousand_row_ledger_is_served_by_one_initial_window(self, tmp_path):
        """~5.4 MiB on disk; the reader pays 32 KiB. The 32-line floor is met
        inside the first window at real row sizes, so it never doubles."""
        path = tmp_path / "audit_2026-09-07.jsonl"
        _write_ledger(
            path,
            [_row(seq, pad_to=_REAL_ROW_BYTES) for seq in range(1, 10_001)],
        )
        reader = LedgerTailReader(tmp_path)

        tail, bytes_read = reader._read_file_tail(path)

        assert tail is not None
        assert tail.sequence == 10_000
        assert bytes_read == LEDGER_TAIL_INITIAL_WINDOW_BYTES

    def test_the_window_doubles_until_the_line_floor_is_met(self, tmp_path):
        """Fat rows put fewer than 32 lines in the first window, so the reader
        probes the head once and then reads a doubled window."""
        path = tmp_path / "audit_2026-09-07.jsonl"
        fat_row = 2048
        rows_needed = (8 * LEDGER_TAIL_INITIAL_WINDOW_BYTES) // fat_row
        _write_ledger(
            path,
            [_row(seq, pad_to=fat_row) for seq in range(1, rows_needed + 1)],
        )
        reader = LedgerTailReader(tmp_path)

        tail, bytes_read = reader._read_file_tail(path)

        # Two doublings: 32 KiB holds 15 whole lines and 64 KiB holds 31 — the
        # window's first segment is always dropped as a partial. The head
        # probe is paid once, on the first growth.
        assert tail is not None
        assert tail.sequence == rows_needed
        assert bytes_read == (
            LEDGER_TAIL_INITIAL_WINDOW_BYTES
            + LEDGER_TAIL_HEAD_PROBE_BYTES
            + 2 * LEDGER_TAIL_INITIAL_WINDOW_BYTES
            + 4 * LEDGER_TAIL_INITIAL_WINDOW_BYTES
        )
        assert path.stat().st_size > 4 * LEDGER_TAIL_INITIAL_WINDOW_BYTES

    def test_a_small_whole_file_is_accepted_below_the_line_floor(self, tmp_path):
        """Reaching the file's first byte is proof the window holds every
        line there is, so the 32-line floor does not apply."""
        path = tmp_path / "audit_2026-09-07.jsonl"
        _write_ledger(path, [_row(1), _row(2, current_hash=_HASH_B)])
        reader = LedgerTailReader(tmp_path)

        tail = reader.read()

        assert tail is not None
        assert tail.sequence == 2


# =============================================================================
# Behavior — which lines count
# =============================================================================


class TestLedgerTailLineFilteringBehavior:
    """A window holds rows no chain wrote; none of them may become the tail."""

    def test_the_highest_not_last_sequence_in_the_window_wins(self, tmp_path):
        """Siblings append after releasing the mint lock, so the last line can
        carry a lower number than one appended just before it. Taking the last
        line here hands the source 149 and mints a second 150."""
        path = tmp_path / "audit_2026-09-07.jsonl"
        _write_ledger(
            path,
            [_row(148), _row(150, current_hash=_HASH_B), _row(149)],
        )
        reader = LedgerTailReader(tmp_path)

        tail = reader.read()

        assert tail is not None
        assert tail.sequence == 150
        assert tail.current_hash == _HASH_B

    @pytest.mark.parametrize(
        "noise_line",
        [
            _row(-1),
            _row(None, with_integrity=False),
            "{not json at all",
            "",
            "   ",
            json.dumps({"integrity": "not-a-dict"}),
            json.dumps(["a", "list", "row"]),
            json.dumps({"integrity": {"sequence": "seven"}}),
        ],
        ids=[
            "degraded_sentinel",
            "no_integrity_block",
            "unparseable",
            "blank",
            "whitespace",
            "integrity_not_a_dict",
            "row_not_a_dict",
            "sequence_not_an_int",
        ],
    )
    def test_a_row_no_chain_wrote_never_becomes_the_tail(self, tmp_path, noise_line):
        path = tmp_path / "audit_2026-09-07.jsonl"
        _write_ledger(path, [_row(7, current_hash=_HASH_B), noise_line])
        reader = LedgerTailReader(tmp_path)

        tail = reader.read()

        assert tail is not None
        assert tail.sequence == 7
        assert tail.current_hash == _HASH_B

    def test_an_unterminated_trailing_line_is_dropped(self, tmp_path):
        """A crash mid-append leaves a partial row. Parsing it as the tail
        would either raise or adopt a truncated hash."""
        path = tmp_path / "audit_2026-09-07.jsonl"
        _write_ledger(
            path,
            [_row(7, current_hash=_HASH_B), _row(8)[: len(_row(8)) // 2]],
            trailing_newline=False,
        )
        reader = LedgerTailReader(tmp_path)

        tail = reader.read()

        assert tail is not None
        assert tail.sequence == 7

    def test_a_window_boundary_inside_a_multibyte_character_does_not_raise(
        self, tmp_path
    ):
        """The window is split on ``b"\\n"`` and parsed as bytes, so the cut
        can land anywhere. Decoding the window first would raise here."""
        path = tmp_path / "audit_2026-09-07.jsonl"
        multibyte_rows = [
            json.dumps(
                {
                    "note": "한글" * 200,
                    "integrity": {
                        "sequence": seq,
                        "previous_hash": _HASH_B,
                        "current_hash": _HASH_A,
                    },
                },
                ensure_ascii=False,
            )
            for seq in range(1, 61)
        ]
        _write_ledger(path, multibyte_rows)
        assert path.stat().st_size > LEDGER_TAIL_INITIAL_WINDOW_BYTES
        reader = LedgerTailReader(tmp_path)

        tail = reader.read()

        assert tail is not None
        assert tail.sequence == 60

    def test_an_empty_file_holds_no_tail(self, tmp_path):
        path = tmp_path / "audit_2026-09-07.jsonl"
        path.write_text("", encoding="utf-8")
        reader = LedgerTailReader(tmp_path)

        assert reader.read() is None


# =============================================================================
# Behavior — what a window that stopped growing says
# =============================================================================


class TestLedgerTailTerminalBehavior:
    """The three cap terminals. Only one of them may return ``None``."""

    def test_a_capped_window_holding_a_positive_sequence_returns_it_and_warns(
        self, tmp_path, monkeypatch
    ):
        """Case 1: something chained is visible, so the tail is real — but the
        operator is told the window was capped, because a higher sequence may
        sit below it."""
        monkeypatch.setattr(ledger_tail_module, "LEDGER_TAIL_INITIAL_WINDOW_BYTES", 512)
        monkeypatch.setattr(ledger_tail_module, "LEDGER_TAIL_MAX_WINDOW_BYTES", 512)
        path = tmp_path / "audit_2026-09-07.jsonl"
        _write_ledger(path, [_row(seq, pad_to=200) for seq in range(1, 40)])
        reader = LedgerTailReader(tmp_path)

        with capture_logs() as logs:
            tail = reader.read()

        assert tail is not None
        assert tail.sequence == 39
        capped = log_events(logs, "ledger_tail.window_capped")
        assert len(capped) == 1
        assert capped[0]["path"] == str(path)
        assert capped[0]["log_level"] == "warning"

    def test_a_capped_window_with_no_complete_line_refuses_the_read(
        self, tmp_path, monkeypatch
    ):
        """Case 3: the file's last row is itself larger than the cap. Walking
        on to the older file would hand the source a tail every number above
        which it would then re-mint."""
        monkeypatch.setattr(ledger_tail_module, "LEDGER_TAIL_INITIAL_WINDOW_BYTES", 256)
        monkeypatch.setattr(ledger_tail_module, "LEDGER_TAIL_MAX_WINDOW_BYTES", 256)
        older = tmp_path / "audit_2026-09-06.jsonl"
        _write_ledger(older, [_row(5)])
        newest = tmp_path / "audit_2026-09-07.jsonl"
        _write_ledger(newest, [_row(9, pad_to=4096)])
        reader = LedgerTailReader(tmp_path)

        with pytest.raises(OSError, match="no complete line"):
            reader.read()

    def test_a_chain_less_newest_file_defers_to_the_older_one(self, tmp_path):
        """Case 2: complete lines, none of them chained — a day the chain was
        switched off. The walk moves on rather than reporting a fresh ledger."""
        older = tmp_path / "audit_2026-09-06.jsonl"
        _write_ledger(older, [_row(5, current_hash=_HASH_B)])
        newest = tmp_path / "audit_2026-09-07.jsonl"
        _write_ledger(newest, [_row(None, with_integrity=False) for _ in range(4)])
        reader = LedgerTailReader(tmp_path)

        tail = reader.read()

        assert tail is not None
        assert tail.sequence == 5
        assert tail.path == older

    def test_a_large_chain_less_file_is_passed_over_after_the_head_probe(
        self, tmp_path
    ):
        """Chain-less at both ends: the middle is never read. Without the
        probe this file costs its whole size inside the exclusive section, and
        nothing prunes the ledger."""
        older = tmp_path / "audit_2026-09-06.jsonl"
        _write_ledger(older, [_row(5, current_hash=_HASH_B)])
        newest = tmp_path / "audit_2026-09-07.jsonl"
        # Derive the row count from the bound the assertion below checks, and
        # round up: a count sized against a literal that merely happens to
        # equal that bound lands one row short of it, and only a CRLF platform
        # pads the file back over the line.
        oversize_bytes = 16 * (
            LEDGER_TAIL_INITIAL_WINDOW_BYTES + LEDGER_TAIL_HEAD_PROBE_BYTES
        )
        chain_less_rows = oversize_bytes // _REAL_ROW_BYTES + 1
        _write_ledger(
            newest,
            [
                _row(None, with_integrity=False, pad_to=_REAL_ROW_BYTES)
                for _ in range(chain_less_rows)
            ],
        )
        assert newest.stat().st_size > oversize_bytes
        reader = LedgerTailReader(tmp_path)

        with capture_logs() as logs:
            tail = reader.read()

        assert tail is not None
        assert tail.path == older
        capped = log_events(logs, "ledger_tail.window_capped")
        assert len(capped) == 1
        assert capped[0]["bytes_scanned"] == (
            LEDGER_TAIL_INITIAL_WINDOW_BYTES + LEDGER_TAIL_HEAD_PROBE_BYTES
        )

    def test_a_whole_file_read_to_its_start_is_not_reported_as_capped(self, tmp_path):
        """A file smaller than the window was read completely — there is
        nothing below it, so the operator has nothing to act on."""
        path = tmp_path / "audit_2026-09-07.jsonl"
        _write_ledger(path, [_row(None, with_integrity=False)])
        reader = LedgerTailReader(tmp_path)

        with capture_logs() as logs:
            tail = reader.read()

        assert tail is None
        assert log_events(logs, "ledger_tail.window_capped") == []


# =============================================================================
# Behavior — the walk across files
# =============================================================================


class TestLedgerTailWalkBehavior:
    """The walk terminates, and only one terminal may say "fresh ledger"."""

    def test_no_matching_file_is_a_genuinely_fresh_ledger(self, tmp_path):
        reader = LedgerTailReader(tmp_path)

        assert reader.read() is None

    def test_a_directory_holding_only_other_adapters_files_is_fresh(self, tmp_path):
        (tmp_path / "audit_2026-09-07_worker.jsonl").write_text(
            _row(9) + "\n", encoding="utf-8"
        )
        reader = LedgerTailReader(tmp_path)

        assert reader.read() is None

    def test_an_exhausted_walk_budget_raises_instead_of_reporting_fresh(
        self, tmp_path, monkeypatch
    ):
        """Past the budget the reader no longer knows whether an unscanned
        file holds a chain, and ``None`` there re-mints from 1 into a live
        ledger. The message names the byte count and the last file scanned."""
        monkeypatch.setattr(ledger_tail_module, "LEDGER_TAIL_MAX_TOTAL_BYTES", 64)
        for day in ("2026-09-06", "2026-09-07"):
            _write_ledger(
                tmp_path / f"audit_{day}.jsonl",
                [_row(None, with_integrity=False) for _ in range(3)],
            )
        reader = LedgerTailReader(tmp_path)

        with pytest.raises(OSError, match="walk budget"):
            reader.read()

    def test_a_file_read_error_propagates_rather_than_skipping_the_file(self, tmp_path):
        """A name that matches but cannot be opened is not "no ledger"."""
        older = tmp_path / "audit_2026-09-06.jsonl"
        _write_ledger(older, [_row(5)])
        # A directory wearing the ledger's own name: ``open`` on it raises an
        # OSError on every platform this ships to.
        (tmp_path / "audit_2026-09-07.jsonl").mkdir()
        reader = LedgerTailReader(tmp_path)

        with pytest.raises(OSError):
            reader.read()

    def test_an_unreadable_directory_propagates_out_of_read(self, tmp_path):
        reader = LedgerTailReader(tmp_path)

        with patch.object(
            Path, "iterdir", autospec=True, side_effect=PermissionError("denied")
        ):
            with pytest.raises(PermissionError):
                reader.read()

    def test_the_walk_stops_at_the_first_file_that_holds_a_chain(self, tmp_path):
        for day, seq in (("2026-09-05", 1), ("2026-09-06", 2), ("2026-09-07", 3)):
            _write_ledger(tmp_path / f"audit_{day}.jsonl", [_row(seq)])
        reader = LedgerTailReader(tmp_path)

        tail = reader.read()

        assert tail is not None
        assert tail.sequence == 3
        assert tail.path.name == "audit_2026-09-07.jsonl"


# =============================================================================
# Contract — the construction facts the managers and the boot sync read back
# =============================================================================


class TestLedgerTailReaderConstructionContract:
    """``from_manager`` and both managers report the reader's own facts."""

    def test_the_reader_reports_the_directory_and_pattern_it_was_built_from(
        self, tmp_path
    ):
        reader = LedgerTailReader(tmp_path, "audit_{date}_worker.jsonl", False)

        assert reader.log_dir == tmp_path
        assert reader.filename_pattern == "audit_{date}_worker.jsonl"
        assert reader.rotate_daily is False

    def test_the_compiled_shape_matches_the_standalone_helper(self, tmp_path):
        reader = LedgerTailReader(tmp_path, "audit_{date}_worker.jsonl", False)

        assert (
            reader.filename_regex.pattern
            == ledger_filename_regex("audit_{date}_worker.jsonl", False).pattern
        )

    def test_a_string_log_dir_is_normalised_to_a_path(self, tmp_path):
        """``from_manager`` hands the adapter's own ``Path``, but the direct
        constructor is reachable from settings-derived strings."""
        reader = LedgerTailReader(str(tmp_path))

        assert isinstance(reader.log_dir, Path)
