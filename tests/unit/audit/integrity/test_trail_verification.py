"""Unit tests for audit trail verification — the one chain walk (804).

Every integrity check of the audit trail runs through one walk: each entry's
fingerprint is checked as it is read, then the entries are sorted by sequence
and linked. File and line order are storage, not chain order. The false alarms
this replaced shipped because the only chains under test began at sequence 1
in one file, and the admin route never met a ledger — so the cases here are the
shapes a real trail takes:

- a trail spread over three UTC days, verified by the CLI, the adapter and the
  admin route (an untouched one is intact on every surface);
- a distributed chain whose two hosts each hold part of it (intact as a fleet,
  host-local gaps named as notes, tampering found with its sequence and file);
- a head pruned on purpose (fails naming the re-run, passes with it);
- writer-made forks (a counter reset, a writer race) read as notes, and copies
  or foreign entries read as issues;
- a verifier without the signing key, or with another one (one trail-level
  issue instead of an entry-by-entry flood).

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- Equivalence partitioning over issue kinds (altered / removed / inserted /
  stored hash rewritten / sequence edited) and key modes.
- Boundary analysis (an absent sequence vs a range, the head vs mid-trail, the
  window's margin edge, ``chain_start_at`` at 0 / 1 / 2).
- Decision tables (the link rule, the trail-level key verdict).
- Exception/edge cases (unreadable rows, unterminated final lines, rows without
  a chain, sequences at or below zero).
- Contract (the CLI's exit-path inventory and report shapes, the issue and note
  vocabulary, the verification constants).
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from baldur.adapters.audit.hashchain_adapter import HashChainFileAuditLogAdapter
from baldur.api.handlers.continuous_audit import continuous_audit_integrity_verify
from baldur.audit.config import AuditConfig
from baldur.audit.continuous_audit import (
    ADMIN_VERIFY_WINDOW_ENTRIES,
    ContinuousAuditRecorder,
)
from baldur.audit.integrity.ledger_tail import (
    CHAIN_STATE_FILE_PREFIX,
    DEFAULT_LEDGER_FILENAME_PATTERN,
    LEDGER_TAIL_MIN_LINES,
    PARTITIONED_LEDGER_FILENAME_PATTERN,
    ledger_filename_regex,
    read_ledger_window,
)
from baldur.audit.integrity.models import compute_hash
from baldur.audit.integrity.redis_manager import (
    CHAIN_SEQUENCE_KEY,
    chain_namespace_prefix,
)
from baldur.audit.integrity.verifier import (
    CHAIN_BEGINNING,
    GENESIS_HASH,
    ISSUE_CHAIN_BROKEN,
    ISSUE_DUPLICATE_ENTRY,
    ISSUE_ENTRY_MODIFIED,
    ISSUE_MISSING_ENTRY,
    ISSUE_SIGNING_KEY_MISMATCH,
    ISSUE_SIGNING_KEY_MISSING,
    ISSUE_UNCHAINED_ROW,
    ISSUE_UNKEYED_ENTRY,
    ISSUE_UNREADABLE_ROW,
    NOTE_CHAIN_FORK,
    NOTE_HEAD_ABSENT,
    NOTE_INCOMPLETE_LAST_LINE,
    NOTE_NOT_HELD_HERE,
    NOTE_ROWS_WITHOUT_CHAIN,
    NOTE_UNCHAINED,
    ChainStart,
    HashChainVerifier,
    TrailReport,
    TrailSet,
    TrailWalk,
    chain_start_at,
    iter_ledger_files,
    verify_ledger_window,
)
from baldur.audit.verify_audit_integrity import AuditIntegrityVerifier
from baldur.audit.verify_audit_integrity import main as verify_cli_main
from baldur.interfaces.audit_adapter import AuditAction, AuditEntry
from baldur.interfaces.web_framework import HttpMethod, RequestContext, ResponseContext
from baldur.settings.secrets import reset_secrets_settings
from tests.factories import MockRedisClient

_KEY_ENV = "BALDUR_SECRETS_AUDIT_SIGNING_KEY"
_WRITER_KEY = "trail-verification-writer-key"
_KEY = _WRITER_KEY.encode()
_OTHER_KEY = b"a-different-signing-key"

# Three UTC days with four entries each: the shape the second-day false alarm
# hid in. Noon, so no write sits near a day boundary.
_DAYS = (
    datetime(2026, 9, 28, 12, tzinfo=UTC),
    datetime(2026, 9, 29, 12, tzinfo=UTC),
    datetime(2026, 9, 30, 12, tzinfo=UTC),
)
_ENTRIES_PER_DAY = 4

# The order the two hosts of one distributed chain mint in. Host A takes
# sequences 1, 4 and 7 — each one isolated, so every gap in host B's files is a
# single sequence.
_FLEET_ORDER = ("a", "b", "b", "a", "b", "b", "a", "b", "b")

_ADAPTER_CLOCK = "baldur.adapters.audit.hashchain_adapter.utc_now"
_RECORDER_LOOKUP = "baldur.api.handlers.continuous_audit._recorder"


@pytest.fixture(autouse=True)
def _writer_key():
    """Pin the signing key every writer and verifier reads.

    The test app sets a key ambiently at import. A trail written under one key
    and verified under another is its own case (TestSigningKeyBehavior), so
    every other test holds the key fixed and never relies on ambient state.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(_KEY_ENV, _WRITER_KEY)
        reset_secrets_settings()
        yield
    reset_secrets_settings()


# =============================================================================
# Helpers — entries built in the writers' shape
# =============================================================================


def _signed(
    sequence: int,
    previous_hash: str,
    *,
    key: bytes | None = _KEY,
    event: str | None = None,
    **integrity: Any,
) -> dict[str, Any]:
    """One entry in the writers' shape, its hash computed by ``compute_hash``."""
    row: dict[str, Any] = {
        "event": event if event is not None else f"event-{sequence}",
        "integrity": {
            **integrity,
            "sequence": sequence,
            "previous_hash": previous_hash,
        },
    }
    row["integrity"]["current_hash"] = compute_hash(row, key=key)
    return row


def _hash(row: dict[str, Any]) -> str:
    return row["integrity"]["current_hash"]


def _sequence_of(row: dict[str, Any]) -> int:
    return row["integrity"]["sequence"]


def _chain(
    count: int,
    *,
    first: int = 1,
    previous: str = GENESIS_HASH,
    key: bytes | None = _KEY,
    event_prefix: str = "event",
) -> list[dict[str, Any]]:
    """``count`` linked entries from ``first``, the first linking to ``previous``."""
    rows: list[dict[str, Any]] = []
    for sequence in range(first, first + count):
        rows.append(
            _signed(sequence, previous, key=key, event=f"{event_prefix}-{sequence}")
        )
        previous = _hash(rows[-1])
    return rows


def _walk(
    rows: list[dict[str, Any]],
    *,
    key: bytes | None = _KEY,
    **report_options: Any,
) -> TrailReport:
    """Walk a list as one trail, each row's position as its line."""
    walk = TrailWalk(key=key)
    for position, row in enumerate(rows):
        walk.add_row(row, line=position)
    return walk.report(**report_options)


def _found(findings: list[dict[str, Any]]) -> list[tuple[str, int | None]]:
    """(type, sequence) of each finding, in report order."""
    return [(finding["type"], finding.get("sequence")) for finding in findings]


def _types(findings: list[dict[str, Any]]) -> list[str]:
    return [finding["type"] for finding in findings]


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> Path:
    """Write rows as JSON Lines, one terminated line each."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return path


def _rows_of(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _locate(paths: list[Path], sequence: int) -> tuple[str, int]:
    """The file and line holding ``sequence``."""
    for path in paths:
        for line, row in enumerate(_rows_of(path), start=1):
            if _sequence_of(row) == sequence:
                return str(path), line
    raise AssertionError(f"sequence {sequence} is in none of {paths}")


# =============================================================================
# Helpers — trails written by the real adapter, and the three surfaces
# =============================================================================


def _config_entry(target_id: str) -> AuditEntry:
    return AuditEntry(
        action=AuditAction.CONFIG_CHANGE,
        target_type="RETRY_CONFIG",
        target_id=target_id,
        actor_id="alice",
        reason="tuning",
        details={"old_value": {"v": 1}, "new_value": {"v": 2}},
    )


def _write_days(
    adapter: HashChainFileAuditLogAdapter,
    days: tuple[datetime, ...] = _DAYS,
    per_day: int = _ENTRIES_PER_DAY,
) -> None:
    """Write ``per_day`` entries on each UTC day, then close the adapter."""
    for day in days:
        with patch(_ADAPTER_CLOCK, return_value=day):
            for n in range(per_day):
                adapter.log(_config_entry(f"{day:%m%d}-{n}"))
    adapter.close()


def _ledger_files(log_dir: Path) -> list[Path]:
    return sorted(log_dir.glob("audit_*.jsonl"))


def _run_cli(*args: Any) -> int:
    """Run the verify CLI in-process; return its exit code."""
    argv = ["verify_audit_integrity", *(str(arg) for arg in args)]
    with patch.object(sys, "argv", argv), pytest.raises(SystemExit) as exited:
        verify_cli_main()
    return exited.value.code


def _run_cli_json(capsys, *args: Any) -> tuple[int, dict[str, Any]]:
    """Run the CLI with ``--format json``; return the exit code and the report."""
    capsys.readouterr()
    code = _run_cli(*args, "--format", "json")
    return code, json.loads(capsys.readouterr().out)


def _cli_issues(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [issue for trail in payload["trails"] for issue in trail["issues"]]


def _cli_notes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [note for trail in payload["trails"] for note in trail["notes"]]


def _recorder_over(adapter: Any) -> ContinuousAuditRecorder:
    return ContinuousAuditRecorder(
        audit_adapter=adapter, config=AuditConfig(hash_seed="trail-test-seed")
    )


def _route_verify(adapter: Any) -> ResponseContext:
    """``GET /audit/integrity/verify`` against a recorder over ``adapter``."""
    with patch(_RECORDER_LOOKUP, return_value=_recorder_over(adapter)):
        return continuous_audit_integrity_verify(
            RequestContext(method=HttpMethod.GET, path="/audit/integrity/verify")
        )


@pytest.fixture
def three_day_ledger(tmp_path) -> HashChainFileAuditLogAdapter:
    """An untouched trail written across three UTC days by the file adapter."""
    adapter = HashChainFileAuditLogAdapter(log_dir=str(tmp_path / "audit"))
    _write_days(adapter)
    return adapter


@pytest.fixture
def one_day_ledger(tmp_path) -> Path:
    """A one-day untouched ledger directory, the manager's state file beside it."""
    adapter = HashChainFileAuditLogAdapter(log_dir=str(tmp_path / "audit"))
    _write_days(adapter, days=_DAYS[:1], per_day=3)
    return adapter.log_dir


@pytest.fixture
def fleet(tmp_path, monkeypatch) -> dict[str, HashChainFileAuditLogAdapter]:
    """A Redis-sequenced chain written by two hosts into separate directories."""
    redis = MockRedisClient()
    hosts = {
        name: HashChainFileAuditLogAdapter(
            log_dir=str(tmp_path / f"host-{name}"),
            distributed_hash_chain=True,
            redis_client=redis,
            enable_anchor_backup=False,
            enable_pending_manager=False,
        )
        for name in sorted(set(_FLEET_ORDER))
    }
    with patch(_ADAPTER_CLOCK, return_value=_DAYS[0]):
        for index, name in enumerate(_FLEET_ORDER):
            monkeypatch.setenv("HOSTNAME", f"host-{name}")
            hosts[name].log(_config_entry(f"fleet-{index}"))
    for adapter in hosts.values():
        adapter.close()
    return hosts


def _fleet_file(fleet: dict[str, HashChainFileAuditLogAdapter], host: str) -> Path:
    (path,) = _ledger_files(fleet[host].log_dir)
    return path


# =============================================================================
# Contract — the verification constants and the finding vocabulary
# =============================================================================


class TestAuditVerificationConstantsContract:
    """Spec values the trail check is built on, hardcoded."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (ADMIN_VERIFY_WINDOW_ENTRIES, 10_000),
            (CHAIN_STATE_FILE_PREFIX, ".hash_chain_state"),
            (DEFAULT_LEDGER_FILENAME_PATTERN, "audit_{date}.jsonl"),
            (PARTITIONED_LEDGER_FILENAME_PATTERN, "audit_{date}_{partition}.jsonl"),
            (CHAIN_BEGINNING, ChainStart(0, "GENESIS")),
        ],
        ids=[
            "admin_window_entries",
            "chain_state_file_prefix",
            "default_ledger_pattern",
            "partitioned_ledger_pattern",
            "chain_beginning",
        ],
    )
    def test_verification_constant_matches_spec(self, value, expected):
        assert value == expected

    @pytest.mark.parametrize(
        ("constant", "wire_name"),
        [
            (ISSUE_ENTRY_MODIFIED, "entry_modified"),
            (ISSUE_CHAIN_BROKEN, "chain_broken"),
            (ISSUE_MISSING_ENTRY, "missing_entry"),
            (ISSUE_DUPLICATE_ENTRY, "duplicate_entry"),
            (ISSUE_UNREADABLE_ROW, "unreadable_row"),
            (ISSUE_UNCHAINED_ROW, "unchained_row"),
            (ISSUE_UNKEYED_ENTRY, "unkeyed_entry"),
            (ISSUE_SIGNING_KEY_MISSING, "signing_key_missing"),
            (ISSUE_SIGNING_KEY_MISMATCH, "signing_key_mismatch"),
            (NOTE_CHAIN_FORK, "chain_fork"),
            (NOTE_NOT_HELD_HERE, "not_held_here"),
            (NOTE_HEAD_ABSENT, "head_absent"),
            (NOTE_ROWS_WITHOUT_CHAIN, "rows_without_chain"),
            (NOTE_UNCHAINED, "unchained"),
            (NOTE_INCOMPLETE_LAST_LINE, "incomplete_last_line"),
        ],
        ids=lambda value: value if isinstance(value, str) else None,
    )
    def test_finding_type_wire_name_matches_spec(self, constant, wire_name):
        """The JSON reports and the testbed read these names."""
        assert constant == wire_name


# =============================================================================
# Behavior — the walk over one trail
# =============================================================================


def _altered(rows):
    """Entry 3's payload edited; its stored hash kept."""
    rows = copy.deepcopy(rows)
    rows[2]["event"] = "TAMPERED"
    return rows


def _hash_rewritten(rows):
    """Entry 3's stored hash replaced."""
    rows = copy.deepcopy(rows)
    rows[2]["integrity"]["current_hash"] = "f" * 64
    return rows


def _removed(rows):
    """Entry 3 deleted."""
    return rows[:2] + rows[3:]


def _inserted(rows):
    """An entry appended by someone without the writer's key."""
    return [*rows, _signed(len(rows) + 1, _hash(rows[-1]), key=_OTHER_KEY)]


def _sequence_edited(rows):
    """Entry 3 renumbered to 4 without re-signing."""
    rows = copy.deepcopy(rows)
    rows[2]["integrity"]["sequence"] = 4
    return rows


class TestTrailWalkBehavior:
    """``TrailWalk``: every issue kind and note over one list of entries."""

    def test_untouched_chain_is_intact_with_its_span(self):
        report = _walk(_chain(6))

        assert report.intact is True
        assert report.issues == []
        assert report.notes == []
        assert (report.entries, report.first_sequence, report.last_sequence) == (
            6,
            1,
            6,
        )

    @pytest.mark.parametrize(
        ("mutate", "expected"),
        [
            (_altered, [(ISSUE_ENTRY_MODIFIED, 3)]),
            (_hash_rewritten, [(ISSUE_ENTRY_MODIFIED, 3), (ISSUE_CHAIN_BROKEN, 4)]),
            (_removed, [(ISSUE_MISSING_ENTRY, 3)]),
            (_inserted, [(ISSUE_ENTRY_MODIFIED, 7)]),
            (_sequence_edited, [(ISSUE_MISSING_ENTRY, 3), (ISSUE_ENTRY_MODIFIED, 4)]),
        ],
        ids=["altered", "hash_rewritten", "removed", "inserted", "sequence_edited"],
    )
    def test_tampered_entry_is_reported_at_its_sequence(self, mutate, expected):
        report = _walk(mutate(_chain(6)))

        assert report.intact is False
        assert _found(report.issues) == expected

    @pytest.mark.parametrize(
        ("removed", "expected_range"),
        [({3}, (3, 3)), ({3, 4, 5}, (3, 5))],
        ids=["one_sequence", "a_range"],
    )
    def test_absent_sequences_are_one_issue_per_range(self, removed, expected_range):
        rows = [row for row in _chain(8) if _sequence_of(row) not in removed]

        report = _walk(rows)

        assert len(report.issues) == 1
        issue = report.issues[0]
        assert issue["type"] == ISSUE_MISSING_ENTRY
        assert (issue["sequence"], issue["last_sequence"]) == expected_range

    def test_absent_head_fails_naming_the_rerun_at_the_lowest_entry(self):
        # Given a trail whose entries 1-2 are gone
        rows = _chain(6)[2:]

        # When the walk expects the chain's own beginning
        report = _walk(rows, rerun_hint=lambda sequence: f"<rerun from {sequence}>")

        # Then the head is a failing range naming the re-run at entry 3
        assert _found(report.issues) == [(ISSUE_MISSING_ENTRY, 1)]
        assert report.issues[0]["last_sequence"] == 2
        assert "<rerun from 3>" in report.issues[0]["message"]

    def test_absent_head_is_a_note_when_the_head_may_be_elsewhere(self):
        report = _walk(_chain(6)[2:], head_absent_failing=False)

        assert report.intact is True
        assert _found(report.notes) == [(NOTE_HEAD_ABSENT, 1)]
        assert report.notes[0]["last_sequence"] == 2

    def test_issues_do_not_depend_on_row_order(self):
        rows = _hash_rewritten(_chain(6))

        in_order = _walk(rows)
        reversed_order = _walk(list(reversed(rows)))

        assert _found(reversed_order.issues) == _found(in_order.issues)

    def test_rows_without_an_integrity_block_beside_chained_entries_fail(self):
        # A row with no integrity block needs no key to write, so beside
        # chained entries it is reported where it is, as an insertion
        rows = [{"event": "chain off"}, *_chain(3), {"event": "forged"}]

        report = _walk(rows)

        assert report.intact is False
        assert (report.rows, report.entries) == (5, 3)
        assert _found(report.issues) == [(ISSUE_UNCHAINED_ROW, None)] * 2
        assert [issue["line"] for issue in report.issues] == [0, 4]
        assert NOTE_ROWS_WITHOUT_CHAIN not in _types(report.notes)

    def test_rows_without_an_integrity_block_and_no_chained_entry_are_a_note(
        self,
    ):
        report = _walk([{"event": "chain off"}, {"event": "chain off again"}])

        assert report.intact is True
        assert _types(report.notes) == [NOTE_ROWS_WITHOUT_CHAIN]
        assert report.notes[0]["count"] == 2

    @pytest.mark.parametrize(
        "sequence",
        ["3", 3.0, True, None],
        ids=["string", "float", "bool", "none"],
    )
    def test_integrity_block_without_an_integer_sequence_is_unreadable(self, sequence):
        rows = [*_chain(2), {"event": "odd", "integrity": {"sequence": sequence}}]

        report = _walk(rows)

        assert _found(report.issues) == [(ISSUE_UNREADABLE_ROW, None)]
        assert report.issues[0]["line"] == 2

    @pytest.mark.parametrize("sequence", [0, -1], ids=["zero", "negative"])
    def test_entry_at_sequence_zero_or_below_is_an_unchained_note(self, sequence):
        rows = [*_chain(3), _signed(sequence, "DEGRADED", degraded=True)]

        report = _walk(rows)

        assert report.intact is True
        assert report.entries == 4
        assert report.last_sequence == 3
        assert _types(report.notes) == [NOTE_UNCHAINED]

    def test_empty_walk_is_intact_with_no_span(self):
        report = TrailWalk(key=_KEY).report()

        assert report.intact is True
        assert (report.entries, report.first_sequence, report.last_sequence) == (
            0,
            None,
            None,
        )


class TestHashChainVerifierListViewBehavior:
    """The list API over the walk: first issue / all issues, list position as line."""

    def test_find_tampering_reports_the_list_position_as_line(self):
        issues = HashChainVerifier().find_tampering(_altered(_chain(5)))

        assert _found(issues) == [(ISSUE_ENTRY_MODIFIED, 3)]
        assert (issues[0]["file"], issues[0]["line"]) == (None, 2)

    def test_verify_chain_returns_the_first_failing_message(self):
        entries = _hash_rewritten(_chain(5))
        verifier = HashChainVerifier()

        is_valid, message = verifier.verify_chain(entries)

        assert is_valid is False
        assert message == verifier.find_tampering(entries)[0]["message"]

    @pytest.mark.parametrize(
        ("start", "expected_valid"),
        [(CHAIN_BEGINNING, False), (None, True)],
        ids=["chain_beginning", "trust_lowest_entry"],
    )
    def test_verify_chain_start_decides_whether_a_late_chain_passes(
        self, start, expected_valid
    ):
        entries = _chain(4, first=5, previous="e" * 64)

        is_valid, _ = HashChainVerifier().verify_chain(entries, start=start)

        assert is_valid is expected_valid


# =============================================================================
# Behavior — the link rule
# =============================================================================


class TestLinkRuleBehavior:
    """An entry at *s* links to some entry at *s-1*, or to the start."""

    @pytest.mark.parametrize(
        ("sequence", "expected"),
        [(1, CHAIN_BEGINNING), (2, ChainStart(1, None)), (5, ChainStart(4, None))],
        ids=["first_sequence", "second_sequence", "later_sequence"],
    )
    def test_chain_start_at_builds_the_start_before_sequence(self, sequence, expected):
        assert chain_start_at(sequence) == expected

    @pytest.mark.parametrize("sequence", [0, -1], ids=["zero", "negative"])
    def test_chain_start_at_below_one_raises(self, sequence):
        with pytest.raises(ValueError, match="sequence 1 or later"):
            chain_start_at(sequence)

    def test_first_entry_linking_to_the_start_hash_is_linked(self):
        start_hash = "a" * 64

        report = _walk(
            _chain(3, first=4, previous=start_hash), start=ChainStart(3, start_hash)
        )

        assert report.intact is True

    def test_first_entry_not_linking_to_the_start_hash_is_chain_broken(self):
        report = _walk(
            _chain(3, first=4, previous="a" * 64), start=ChainStart(3, "b" * 64)
        )

        assert _found(report.issues) == [(ISSUE_CHAIN_BROKEN, 4)]
        assert "does not link to the start of the check" in report.issues[0]["message"]

    def test_trusted_start_leaves_the_first_link_unchecked(self):
        report = _walk(
            _chain(3, first=4, previous="not-a-real-hash"), start=ChainStart(3, None)
        )

        assert report.intact is True

    def test_skip_link_to_a_lower_present_entry_is_a_fork_note(self):
        # Given a writer race: entry 5 linked to entry 3's hash, not entry 4's
        rows = _chain(4)
        rows.append(_signed(5, _hash(rows[2])))

        # When the trail is walked
        report = _walk(rows)

        # Then the skip link is a writer-made fork, not tampering
        assert report.intact is True
        assert _found(report.notes) == [(NOTE_CHAIN_FORK, 5)]
        assert report.notes[0]["explained"] is False
        assert "links to entry 3, not 4" in report.notes[0]["message"]

    def test_predecessor_absent_while_its_sequence_is_present_is_chain_broken(self):
        rows = _chain(4)
        rows.append(_signed(5, "f" * 64))

        report = _walk(rows)

        assert _found(report.issues) == [(ISSUE_CHAIN_BROKEN, 5)]
        assert "its predecessor is not present" in report.issues[0]["message"]
        assert report.notes == []

    def test_entry_copied_from_another_chain_under_the_same_key_is_not_a_fork(self):
        # Given a trail, and entry 3 of another chain signed with the same key
        rows = _chain(5)
        foreign_three = _chain(3, event_prefix="foreign")[2]

        # When the foreign entry is inserted beside this trail's own entry 3
        report = _walk([*rows, foreign_three])

        # Then its fingerprint is valid but its link names no present entry:
        # chain_broken, and the two versions of entry 3 are not read as a fork
        assert _found(report.issues) == [(ISSUE_CHAIN_BROKEN, 3)]
        assert report.issues[0]["line"] == len(rows)
        assert report.notes == []

    def test_entry_after_a_gap_is_not_link_checked(self):
        # Given entry 3 removed, and entry 4 linking to nothing present
        rows = _chain(2)
        orphan = _signed(4, "d" * 64)
        rows += [orphan, *_chain(2, first=5, previous=_hash(orphan))]

        # When the trail is walked
        report = _walk(rows)

        # Then only the absence is reported, never a break at the entry after it
        assert _found(report.issues) == [(ISSUE_MISSING_ENTRY, 3)]


# =============================================================================
# Behavior — forks and copies at one sequence
# =============================================================================


class TestForkAndDuplicateBehavior:
    """Distinct valid versions of one entry are writer-made; a copy is inserted."""

    def test_two_distinct_valid_versions_of_one_entry_are_an_unexplained_fork(self):
        rows = _chain(3)
        twin = _signed(3, _hash(rows[1]), event="the other writer's entry 3")

        report = _walk([*rows, twin, *_chain(2, first=4, previous=_hash(rows[2]))])

        assert report.intact is True
        assert _found(report.notes) == [(NOTE_CHAIN_FORK, 3)]
        assert report.notes[0]["explained"] is False

    @pytest.mark.parametrize(
        "stamp",
        [
            {
                "source_reset": {
                    "manager": "redis",
                    "reason": "source_behind_ledger",
                    "observed": 1,
                    "adopted": 2,
                }
            },
            {"degraded": True, "fallback_source": "local"},
        ],
        ids=["source_reset", "degraded"],
    )
    def test_fork_with_a_stamped_version_is_explained(self, stamp):
        rows = _chain(3)
        stamped_twin = _signed(3, _hash(rows[1]), event="re-anchored", **stamp)

        report = _walk([*rows, stamped_twin])

        assert _found(report.notes) == [(NOTE_CHAIN_FORK, 3)]
        assert report.notes[0]["explained"] is True

    def test_copy_of_an_entry_is_duplicate_entry_not_a_fork(self):
        rows = _chain(5)

        report = _walk([*rows, copy.deepcopy(rows[1])])

        assert _found(report.issues) == [(ISSUE_DUPLICATE_ENTRY, 2)]
        assert report.issues[0]["line"] == len(rows)
        assert report.notes == []

    def test_counter_reset_fork_between_two_hosts_verifies_intact_and_explained(
        self, tmp_path, monkeypatch
    ):
        # Given host A writes 1-3 and host B writes 4, then the chain's Redis
        # loses its counter, and each host writes once more
        redis = MockRedisClient()
        hosts = {
            name: HashChainFileAuditLogAdapter(
                log_dir=str(tmp_path / f"host-{name}"),
                distributed_hash_chain=True,
                redis_client=redis,
                enable_anchor_backup=False,
                enable_pending_manager=False,
            )
            for name in ("a", "b")
        }
        with patch(_ADAPTER_CLOCK, return_value=_DAYS[0]):
            for index, name in enumerate(("a", "a", "a", "b", "reset", "a", "b")):
                if name == "reset":
                    sequence_key = (
                        chain_namespace_prefix(hosts["a"].redis_key_prefix, "")
                        + CHAIN_SEQUENCE_KEY
                    )
                    assert redis.delete(sequence_key) == 1
                    continue
                monkeypatch.setenv("HOSTNAME", f"host-{name}")
                hosts[name].log(_config_entry(f"reset-{index}"))
        for adapter in hosts.values():
            adapter.close()

        # When the fleet is verified with both hosts' directories
        summary = AuditIntegrityVerifier().verify_paths(
            [hosts["a"].log_dir, hosts["b"].log_dir]
        )

        # Then the re-anchored entry is a fork explained by its stamp
        assert summary.is_valid is True
        (trail,) = summary.results
        assert trail.issues == []
        assert _found(trail.notes) == [(NOTE_CHAIN_FORK, 4)]
        assert trail.notes[0]["explained"] is True


# =============================================================================
# Behavior — the trail-level key verdict
# =============================================================================


class TestSigningKeyBehavior:
    """A verifier without the trail's key, or with another, says so once."""

    @pytest.mark.parametrize(
        ("verifier_key", "expected_type"),
        [(None, ISSUE_SIGNING_KEY_MISSING), (_OTHER_KEY, ISSUE_SIGNING_KEY_MISMATCH)],
        ids=["no_key", "wrong_key"],
    )
    def test_signing_key_problem_is_one_trail_level_issue(
        self, verifier_key, expected_type
    ):
        report = _walk(_chain(6), key=verifier_key)

        assert report.intact is False
        assert _types(report.issues) == [expected_type]
        assert ISSUE_ENTRY_MODIFIED not in _types(report.issues)

    def test_signing_key_missing_names_the_setting_to_set(self):
        report = _walk(_chain(2), key=None)

        assert _KEY_ENV in report.issues[0]["message"]

    @pytest.mark.parametrize(
        ("unkeyed", "expected_ranges"),
        [({3}, [(3, 3)]), ({3, 4, 5}, [(3, 5)]), ({2, 5, 6}, [(2, 2), (5, 6)])],
        ids=["one_entry", "consecutive_entries", "two_runs"],
    )
    def test_unkeyed_entries_in_a_keyed_trail_are_one_issue_per_run(
        self, unkeyed, expected_ranges
    ):
        # Given a keyed trail some of whose entries carry only the keyless hash
        rows: list[dict[str, Any]] = []
        previous = GENESIS_HASH
        for sequence in range(1, 8):
            key = None if sequence in unkeyed else _KEY
            rows.append(_signed(sequence, previous, key=key))
            previous = _hash(rows[-1])

        # When a keyed verifier walks it
        report = _walk(rows)

        # Then each run of keyless entries is one unkeyed_entry range
        assert _types(report.issues) == [ISSUE_UNKEYED_ENTRY] * len(expected_ranges)
        assert [
            (issue["sequence"], issue["last_sequence"]) for issue in report.issues
        ] == expected_ranges

    def test_links_are_still_checked_under_a_trail_level_key_issue(self):
        rows = _removed(_hash_rewritten(_chain(6)))

        report = _walk(rows, key=None)

        assert _found(report.issues) == [
            (ISSUE_MISSING_ENTRY, 3),
            (ISSUE_SIGNING_KEY_MISSING, None),
        ]

    def test_writer_race_is_a_fork_note_under_a_trail_level_key_issue(self):
        rows = _chain(4)
        rows.append(_signed(5, _hash(rows[2])))

        report = _walk(rows, key=_OTHER_KEY)

        assert _types(report.issues) == [ISSUE_SIGNING_KEY_MISMATCH]
        assert _found(report.notes) == [(NOTE_CHAIN_FORK, 5)]

    def test_signing_key_missing_on_the_cli_exits_one_with_the_type(
        self, one_day_ledger, capsys, monkeypatch
    ):
        monkeypatch.delenv(_KEY_ENV)
        reset_secrets_settings()

        code, payload = _run_cli_json(capsys, one_day_ledger)

        assert code == 1
        assert _types(_cli_issues(payload)) == [ISSUE_SIGNING_KEY_MISSING]


# =============================================================================
# Behavior — reading files into trails
# =============================================================================


class TestTrailSetBehavior:
    """``TrailSet.read_file``: partition grouping, unreadable rows, the last line."""

    def _read(self, *paths: Path, **read_options: Any) -> list[TrailReport]:
        trails = TrailSet(key=_KEY)
        for path in paths:
            list(trails.read_file(path, **read_options))
        return trails.reports()

    def test_ledger_shaped_name_puts_the_file_in_its_partition(self, tmp_path):
        path = _write_rows(
            tmp_path / "audit_2026-09-28_worker.jsonl", _partition_chain(3, "worker")
        )

        (report,) = self._read(path)

        assert (report.partition, report.entries, report.intact) == ("worker", 3, True)

    def test_ledger_shaped_name_wins_over_a_rows_partition_field(self, tmp_path):
        rows = _chain(2)
        stray = _signed(3, _hash(rows[-1]))
        stray["partition"] = "worker"
        path = _write_rows(tmp_path / "audit_2026-09-28.jsonl", [*rows, stray])

        reports = self._read(path)

        assert [(report.partition, report.entries) for report in reports] == [("", 3)]

    def test_other_name_puts_each_row_in_the_partition_it_names(self, tmp_path):
        # Given one export file interleaving two partitions' chains
        default_rows = _chain(3)
        worker_rows = _relinked(
            [{**row, "partition": "worker"} for row in _chain(3, event_prefix="worker")]
        )
        interleaved = [
            row for pair in zip(default_rows, worker_rows, strict=True) for row in pair
        ]
        path = _write_rows(tmp_path / "export.jsonl", interleaved)

        # When the file is read
        reports = self._read(path)

        # Then each partition is its own intact trail, with no fork between them
        assert [(r.partition, r.entries, r.intact) for r in reports] == [
            ("", 3, True),
            ("worker", 3, True),
        ]
        assert all(report.notes == [] for report in reports)

    def test_row_signed_for_another_partition_is_chain_broken_at_sequence_one(
        self, tmp_path
    ):
        # Given a default-partition ledger, and the worker partition's entry 1
        # (validly signed, linking to GENESIS) copied into it
        worker_one = _worker_entry_one()
        path = _write_rows(
            tmp_path / "audit_2026-09-28.jsonl", [*_chain(3), worker_one]
        )

        # When the ledger is read
        (report,) = self._read(path)

        # Then the copy is a break at its own sequence, never a writer-made fork
        assert _found(report.issues) == [(ISSUE_CHAIN_BROKEN, 1)]
        assert report.issues[0]["line"] == 4
        assert report.notes == []

    def test_explicit_partition_overrides_name_and_rows(self, tmp_path):
        path = _write_rows(tmp_path / "audit_2026-09-28_worker.jsonl", _chain(2))

        (report,) = self._read(path, partition="payments")

        assert report.partition == "payments"

    def test_unreadable_row_is_reported_with_its_file_and_line(self, tmp_path):
        rows = _chain(3)
        path = tmp_path / "audit_2026-09-28.jsonl"
        lines = [json.dumps(row) for row in rows]
        lines.insert(2, "{not json")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        (report,) = self._read(path)

        assert _found(report.issues) == [(ISSUE_UNREADABLE_ROW, None)]
        assert (report.issues[0]["file"], report.issues[0]["line"]) == (str(path), 3)
        assert report.entries == 3

    def test_unterminated_final_line_that_parses_is_walked_and_noted(self, tmp_path):
        rows = _chain(3)
        path = tmp_path / "audit_2026-09-28.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

        (report,) = self._read(path)

        assert report.entries == 3
        assert report.intact is True
        assert _types(report.notes) == [NOTE_INCOMPLETE_LAST_LINE]
        assert report.notes[0]["line"] == 3

    def test_unterminated_final_line_that_does_not_parse_is_only_noted(self, tmp_path):
        rows = _chain(3)
        path = tmp_path / "audit_2026-09-28.jsonl"
        torn = json.dumps(rows[2])[:40]
        path.write_text(
            "\n".join([json.dumps(rows[0]), json.dumps(rows[1]), torn]),
            encoding="utf-8",
        )

        (report,) = self._read(path)

        assert report.issues == []
        assert report.entries == 2
        assert _types(report.notes) == [NOTE_INCOMPLETE_LAST_LINE]

    def test_read_file_yields_each_parsed_row_in_file_order(self, tmp_path):
        rows = _chain(3)
        path = tmp_path / "audit_2026-09-28.jsonl"
        path.write_text(
            json.dumps(rows[0]) + "\n\n" + json.dumps(rows[1]) + "\n"
            "{not json\n" + json.dumps(rows[2]) + "\n",
            encoding="utf-8",
        )

        yielded = list(TrailSet(key=_KEY).read_file(path))

        assert yielded == rows

    def test_reports_come_back_ordered_by_partition_default_first(self, tmp_path):
        paths = [
            _write_rows(tmp_path / f"audit_2026-09-28{suffix}.jsonl", _chain(1))
            for suffix in ("_zeta", "", "_alpha")
        ]

        reports = self._read(*paths)

        assert [report.partition for report in reports] == ["", "alpha", "zeta"]


def _partition_chain(count: int, partition: str) -> list[dict[str, Any]]:
    """``count`` entries of ``partition``'s chain, each signing its partition field."""
    return _relinked(
        [
            {**row, "partition": partition}
            for row in _chain(count, event_prefix=partition)
        ]
    )


def _worker_entry_one() -> dict[str, Any]:
    """Entry 1 of the ``worker`` partition's chain, its partition field signed."""
    (row,) = _relinked([{**_chain(1, event_prefix="worker")[0], "partition": "worker"}])
    return row


def _relinked(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-sign ``rows`` as one chain after their bodies were edited."""
    previous = GENESIS_HASH
    relinked = []
    for row in rows:
        row = copy.deepcopy(row)
        row["integrity"]["previous_hash"] = previous
        row["integrity"].pop("current_hash", None)
        row["integrity"]["current_hash"] = compute_hash(row, key=_KEY)
        relinked.append(row)
        previous = _hash(row)
    return relinked


class TestStdlibWrittenRowBehavior:
    """Rows parse with the hash's own serializer, not the fast parser."""

    def test_stdlib_written_row_with_nan_and_big_integer_verifies_intact(
        self, tmp_path
    ):
        # Given an entry written without the fast-json extra: the stdlib writes
        # NaN, and keeps an integer above 2**64 exact
        orjson = pytest.importorskip("orjson")
        row = _signed(1, GENESIS_HASH, event="stdlib")
        row["ratio"] = float("nan")
        row["big"] = 2**65 + 1
        row["integrity"].pop("current_hash")
        row["integrity"]["current_hash"] = compute_hash(row, key=_KEY)
        line = json.dumps(row)
        path = tmp_path / "audit_2026-09-28.jsonl"
        path.write_text(line + "\n", encoding="utf-8")
        with pytest.raises(orjson.JSONDecodeError):
            orjson.loads(line)  # the fast parser cannot read this line at all

        # When the trail is read with orjson installed
        trails = TrailSet(key=_KEY)
        list(trails.read_file(path))
        (report,) = trails.reports()

        # Then the untouched entry verifies as written
        assert report.intact is True
        assert report.entries == 1


# =============================================================================
# Behavior — the recent window of one ledger
# =============================================================================


_UNROTATED = ledger_filename_regex(DEFAULT_LEDGER_FILENAME_PATTERN, rotate_daily=False)


def _window_report(log_dir: Path, line_count: int) -> TrailReport:
    return verify_ledger_window(
        read_ledger_window(log_dir, _UNROTATED, line_count), key=_KEY
    )


class TestLedgerWindowVerificationBehavior:
    """``verify_ledger_window``: where a window's check begins."""

    def test_window_over_a_whole_pruned_ledger_reports_the_head_as_a_note(
        self, tmp_path
    ):
        _write_rows(tmp_path / "audit_all.jsonl", _chain(10)[4:])

        report = _window_report(tmp_path, 100)

        assert report.intact is True
        assert _found(report.notes) == [(NOTE_HEAD_ABSENT, 1)]
        assert report.notes[0]["last_sequence"] == 4

    @pytest.mark.parametrize(
        ("removed_sequence", "expected"),
        [
            (70, []),
            (92, [(ISSUE_MISSING_ENTRY, 92)]),
            (95, [(ISSUE_MISSING_ENTRY, 95)]),
        ],
        ids=["inside_the_margin", "directly_above_the_margin", "above_the_margin"],
    )
    def test_window_gap_is_reported_only_above_the_margin(
        self, tmp_path, removed_sequence, expected
    ):
        # Given a 100-entry ledger with one entry removed, and a window holding
        # the margin plus eight lines
        rows = [row for row in _chain(100) if _sequence_of(row) != removed_sequence]
        _write_rows(tmp_path / "audit_all.jsonl", rows)

        # When the window is verified
        report = _window_report(tmp_path, LEDGER_TAIL_MIN_LINES + 8)

        # Then the margin is where the check begins: a gap inside it is not
        # read, a gap above it is
        assert _found(report.issues) == expected
        assert NOTE_HEAD_ABSENT not in _types(report.notes)

    def test_row_signed_for_another_partition_in_the_window_is_chain_broken(
        self, tmp_path
    ):
        _write_rows(tmp_path / "audit_all.jsonl", [*_chain(5), _worker_entry_one()])

        report = _window_report(tmp_path, 100)

        assert _found(report.issues) == [(ISSUE_CHAIN_BROKEN, 1)]
        assert report.notes == []

    def test_out_of_order_append_across_the_lower_edge_is_not_a_gap(self, tmp_path):
        # Given entry 62 appended before entry 61, and a window whose lower
        # edge falls between them
        rows = _chain(100)
        rows[60], rows[61] = rows[61], rows[60]
        _write_rows(tmp_path / "audit_all.jsonl", rows)
        line_count = len(rows) - 61
        window = read_ledger_window(tmp_path, _UNROTATED, line_count)
        windowed = [
            json.loads(raw)["integrity"]["sequence"]
            for part in window.files
            for _, raw in part.lines
        ]
        assert 62 not in windowed
        assert 61 in windowed

        # When the window is verified
        report = verify_ledger_window(window, key=_KEY)

        # Then 62, below the margin's highest entry, is not read as absent
        assert report.intact is True
        assert report.issues == []

    def test_margin_without_a_chained_entry_trusts_the_lowest_entry(self, tmp_path):
        rows = [{"event": f"chain off {n}"} for n in range(LEDGER_TAIL_MIN_LINES)]
        rows += _chain(8, first=50, previous="e" * 64)
        _write_rows(tmp_path / "audit_all.jsonl", rows)

        report = _window_report(tmp_path, len(rows) - 2)

        # The chain starts at the lowest entry; the chainless rows beside it
        # are reported as rows, never as a gap or an absent head
        assert report.first_sequence == 50
        assert set(_types(report.issues)) == {ISSUE_UNCHAINED_ROW}
        assert NOTE_HEAD_ABSENT not in _types(report.notes)


# =============================================================================
# Behavior — selecting ledger files
# =============================================================================


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


class TestLedgerFileSelectionBehavior:
    """``iter_ledger_files``: the ledger's own files, each once."""

    def test_directory_selects_only_ledger_shaped_files(self, tmp_path):
        ledgers = [
            _touch(tmp_path / name)
            for name in (
                "audit_2026-09-28.jsonl",
                "audit_all.jsonl",
                "audit_2026-09-28_worker.jsonl",
            )
        ]
        for name in (
            ".hash_chain_state.json",
            ".hash_chain_state.lock",
            ".hash_chain_state.json.4242.tmp",
            "notes.txt",
            "audit.log",
        ):
            _touch(tmp_path / name)

        selected = iter_ledger_files([tmp_path])

        assert sorted(selected) == sorted(ledgers)

    def test_pattern_selects_by_glob_but_never_the_chain_state_files(self, tmp_path):
        kept = [_touch(tmp_path / "ledger.ndjson"), _touch(tmp_path / "notes.txt")]
        for name in (".hash_chain_state.json", ".hash_chain_state.lock"):
            _touch(tmp_path / name)

        selected = iter_ledger_files([tmp_path], pattern="*")

        assert sorted(selected) == sorted(kept)

    def test_explicit_file_is_selected_whatever_its_name(self, tmp_path):
        custom = _touch(tmp_path / "custom.ndjson")

        assert iter_ledger_files([custom]) == [custom]

    @pytest.mark.parametrize(
        ("recursive", "expected_names"),
        [
            (False, ["audit_2026-09-28.jsonl"]),
            (True, ["audit_2026-09-28.jsonl", "audit_2026-09-29.jsonl"]),
        ],
        ids=["flat", "recursive"],
    )
    def test_sub_directories_are_selected_only_when_recursive(
        self, tmp_path, recursive, expected_names
    ):
        _touch(tmp_path / "audit_2026-09-28.jsonl")
        _touch(tmp_path / "older" / "audit_2026-09-29.jsonl")

        selected = iter_ledger_files([tmp_path], recursive=recursive)

        assert sorted(path.name for path in selected) == expected_names

    def test_directory_named_like_a_ledger_is_not_selected(self, tmp_path):
        (tmp_path / "audit_2026-09-28.jsonl").mkdir()

        assert iter_ledger_files([tmp_path]) == []

    def test_root_and_its_sub_directory_select_each_file_once(self, tmp_path):
        _touch(tmp_path / "sub" / "audit_2026-09-28.jsonl")

        selected = iter_ledger_files([tmp_path, tmp_path / "sub"], recursive=True)

        assert len(selected) == 1

    def test_file_reached_through_two_spellings_is_selected_once(self, tmp_path):
        # Read twice, every entry of the file would be a duplicate_entry.
        ledger = _touch(tmp_path / "audit_2026-09-28.jsonl")
        (tmp_path / "sub").mkdir()

        selected = iter_ledger_files([tmp_path / "sub" / "..", ledger])

        assert len(selected) == 1

    def test_symlink_loop_is_walked_once(self, tmp_path):
        _touch(tmp_path / "audit_2026-09-28.jsonl")
        try:
            (tmp_path / "loop").symlink_to(tmp_path, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("this host cannot create a directory symlink")

        selected = iter_ledger_files([tmp_path], recursive=True)

        assert len(selected) == 1

    def test_unreadable_directory_raises_instead_of_selecting_nothing(self, tmp_path):
        with (
            patch.object(
                Path, "iterdir", autospec=True, side_effect=PermissionError("denied")
            ),
            pytest.raises(PermissionError),
        ):
            iter_ledger_files([tmp_path])


# =============================================================================
# Contract — the CLI's exit-path inventory and report shapes
# =============================================================================


class TestVerifyAuditIntegrityCliContract:
    """``python -m baldur.audit.verify_audit_integrity``: exit codes and output."""

    def test_state_file_ignored_one_day_ledger_exits_zero(self, one_day_ledger, capsys):
        # Given a one-day ledger directory holding the manager's state files
        assert any(
            path.name.startswith(CHAIN_STATE_FILE_PREFIX)
            for path in one_day_ledger.iterdir()
        )
        capsys.readouterr()

        # When the CLI verifies the directory
        code = _run_cli(one_day_ledger)

        # Then the state file is never read as a ledger
        assert code == 0
        assert "[OK]" in capsys.readouterr().out

    def test_cp949_console_one_day_ledger_exits_zero(self, one_day_ledger):
        # A cp949 console cannot encode a check mark: the report must be ASCII.
        completed = subprocess.run(
            [
                sys.executable,
                "-P",
                "-m",
                "baldur.audit.verify_audit_integrity",
                str(one_day_ledger),
            ],
            capture_output=True,
            env={**os.environ, "PYTHONIOENCODING": "cp949"},
            timeout=120,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr.decode(
            "cp949", errors="replace"
        )
        assert "[OK]" in completed.stdout.decode("cp949")

    def test_issue_found_exits_one(self, one_day_ledger, capsys):
        (path,) = _ledger_files(one_day_ledger)
        rows = _rows_of(path)
        rows[1]["change"]["reason"] = "TAMPERED"
        _write_rows(path, rows)
        capsys.readouterr()

        code = _run_cli(one_day_ledger)

        assert code == 1
        assert "[FAIL]" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("rows", "expected_message"),
        [
            (None, "no audit ledger files found"),
            ([{"event": "chain off"}], "no chained entry found"),
        ],
        ids=["no_ledger_file", "no_chained_entry"],
    )
    def test_nothing_verified_exits_one(self, tmp_path, capsys, rows, expected_message):
        if rows is not None:
            _write_rows(tmp_path / "audit_2026-09-28.jsonl", rows)

        code, payload = _run_cli_json(capsys, tmp_path)

        assert code == 1
        assert expected_message in payload["summary"]["error"]

    def test_missing_path_exits_two(self, tmp_path, capsys):
        code = _run_cli(tmp_path / "absent")

        assert code == 2
        assert "Path not found" in capsys.readouterr().err

    @pytest.mark.parametrize("starts_at", ["0", "-3"], ids=["zero", "negative"])
    def test_starts_at_below_one_is_refused_by_the_parser(
        self, one_day_ledger, starts_at
    ):
        assert _run_cli(one_day_ledger, "--starts-at", starts_at) == 2

    def test_json_report_carries_each_trails_span_and_findings(
        self, one_day_ledger, capsys
    ):
        code, payload = _run_cli_json(capsys, one_day_ledger)

        assert code == 0
        (trail,) = payload["trails"]
        assert set(trail) >= {
            "partition",
            "files",
            "entries",
            "first_sequence",
            "last_sequence",
            "issues",
            "notes",
            "is_valid",
        }
        assert (trail["entries"], trail["first_sequence"], trail["last_sequence"]) == (
            3,
            1,
            3,
        )
        assert payload["summary"]["is_valid"] is True

    def test_summary_report_has_a_line_per_trail_and_a_total(self, tmp_path, capsys):
        _write_rows(tmp_path / "audit_2026-09-28.jsonl", _chain(2))
        _write_rows(
            tmp_path / "audit_2026-09-28_worker.jsonl", _partition_chain(3, "worker")
        )
        capsys.readouterr()

        code = _run_cli(tmp_path, "--format", "summary")

        lines = capsys.readouterr().out.strip().splitlines()
        assert code == 0
        assert lines == [
            "PASS default: 2 entries, 0 issues",
            "PASS worker: 3 entries, 0 issues",
            "PASS: 2/2 trails valid, 5 entries, 0 issues",
        ]

    @pytest.mark.parametrize(
        ("tamper", "marker"),
        [(False, "[OK]"), (True, "[FAIL]")],
        ids=["intact", "failing"],
    )
    def test_text_report_is_ascii(self, one_day_ledger, capsys, tamper, marker):
        if tamper:
            (path,) = _ledger_files(one_day_ledger)
            rows = _rows_of(path)
            rows[0]["change"]["reason"] = "TAMPERED"
            _write_rows(path, rows)
        capsys.readouterr()

        _run_cli(one_day_ledger)

        out = capsys.readouterr().out
        assert out.isascii()
        assert marker in out


# =============================================================================
# Behavior — the file adapter's own check, and the surfaces over its trail
# =============================================================================


def _verdict_by_cli(adapter, capsys) -> tuple[bool, list[str], list[str]]:
    code, payload = _run_cli_json(capsys, adapter.log_dir)
    return code == 0, _types(_cli_issues(payload)), _types(_cli_notes(payload))


def _verdict_by_adapter(adapter, capsys) -> tuple[bool, list[str], list[str]]:
    report = adapter.verify_trail()
    intact, issues = adapter.verify_integrity()
    assert (intact, issues) == (report.intact, report.issues)
    return intact, _types(issues), _types(report.notes)


def _verdict_by_route(adapter, capsys) -> tuple[bool, list[str], list[str]]:
    response = _route_verify(adapter)
    return (
        response.status_code == 200,
        _types(response.body["issues"]),
        _types(response.body["notes"]),
    )


_SURFACES: dict[str, Callable[..., tuple[bool, list[str], list[str]]]] = {
    "cli": _verdict_by_cli,
    "adapter": _verdict_by_adapter,
    "route": _verdict_by_route,
}


class TestHashChainAdapterTrailBehavior:
    """The adapter's ``verify_trail`` / ``verify_integrity``, and every surface."""

    @pytest.mark.parametrize("surface", ["cli", "adapter", "route"])
    def test_three_days_untouched_trail_verifies_intact(
        self, three_day_ledger, capsys, surface
    ):
        # Given an untouched trail continued across three daily files
        assert len(_ledger_files(three_day_ledger.log_dir)) == len(_DAYS)

        # When the surface checks it
        verified, issue_types, _ = _SURFACES[surface](three_day_ledger, capsys)

        # Then no day after the first reads as removed or broken
        assert verified is True
        assert ISSUE_MISSING_ENTRY not in issue_types
        assert ISSUE_CHAIN_BROKEN not in issue_types
        assert issue_types == []

    @pytest.mark.parametrize("surface", ["adapter", "route"])
    def test_host_local_check_reports_not_held_here_without_missing_entry(
        self, fleet, capsys, surface
    ):
        # When host B checks its own files of the two-host chain
        verified, issue_types, note_types = _SURFACES[surface](fleet["b"], capsys)

        # Then host A's entries are notes, including the head host B never held
        held_by_a = [n + 1 for n, host in enumerate(_FLEET_ORDER) if host == "a"]
        assert verified is True
        assert issue_types == []
        assert note_types == [NOTE_NOT_HELD_HERE] * len(held_by_a)

    def test_host_local_adapter_notes_name_the_sequences_another_host_holds(
        self, fleet
    ):
        report = fleet["b"].verify_trail()

        held_by_a = [n + 1 for n, host in enumerate(_FLEET_ORDER) if host == "a"]
        assert [note["sequence"] for note in report.notes] == held_by_a

    def test_host_local_cli_given_one_host_exits_one_naming_the_distributed_chain(
        self, fleet, capsys
    ):
        code, payload = _run_cli_json(capsys, fleet["b"].log_dir)

        issues = _cli_issues(payload)
        assert code == 1
        assert set(_types(issues)) == {ISSUE_MISSING_ENTRY}
        assert all("distributed chain" in issue["message"] for issue in issues)

    def test_verify_trail_with_the_oldest_day_removed_names_expected_start(
        self, three_day_ledger
    ):
        _ledger_files(three_day_ledger.log_dir)[0].unlink()
        first_kept = _ENTRIES_PER_DAY + 1

        report = three_day_ledger.verify_trail()

        assert _found(report.issues) == [(ISSUE_MISSING_ENTRY, 1)]
        assert report.issues[0]["last_sequence"] == first_kept - 1
        assert f"expected_start={first_kept}" in report.issues[0]["message"]

    def test_verify_trail_expected_start_verifies_a_pruned_ledger(
        self, three_day_ledger
    ):
        _ledger_files(three_day_ledger.log_dir)[0].unlink()

        report = three_day_ledger.verify_trail(expected_start=_ENTRIES_PER_DAY + 1)

        assert report.intact is True
        assert report.first_sequence == _ENTRIES_PER_DAY + 1

    def test_verify_integrity_returns_flat_issues_with_their_location(
        self, three_day_ledger
    ):
        # Given the second entry of the second day altered
        day_two = _ledger_files(three_day_ledger.log_dir)[1]
        rows = _rows_of(day_two)
        rows[1]["change"]["reason"] = "TAMPERED"
        _write_rows(day_two, rows)

        # When the adapter checks its trail
        intact, issues = three_day_ledger.verify_integrity()

        # Then one flat issue names the entry's sequence, file and line
        assert intact is False
        (issue,) = issues
        assert set(issue) >= {"type", "sequence", "file", "line", "message"}
        assert (issue["type"], issue["sequence"]) == (
            ISSUE_ENTRY_MODIFIED,
            _ENTRIES_PER_DAY + 2,
        )
        assert (issue["file"], issue["line"]) == (str(day_two), 2)

    def test_verify_integrity_read_failure_is_a_verify_error(self, three_day_ledger):
        with patch.object(
            three_day_ledger,
            "verify_trail",
            autospec=True,
            side_effect=OSError("ledger unreadable"),
        ):
            intact, issues = three_day_ledger.verify_integrity()

        assert intact is False
        assert issues == [{"type": "verify_error", "message": "ledger unreadable"}]


# =============================================================================
# Behavior — the CLI over real ledgers
# =============================================================================


def _tamper_altered(path: Path, files: list[Path]) -> tuple[int, str, int]:
    rows = _rows_of(path)
    rows[1]["change"]["reason"] = "TAMPERED"
    _write_rows(path, rows)
    return _sequence_of(rows[1]), str(path), 2


def _tamper_removed(path: Path, files: list[Path]) -> tuple[int, str, int]:
    rows = _rows_of(path)
    removed = rows.pop(1)
    _write_rows(path, rows)
    sequence = _sequence_of(removed)
    # A gap is reported where the check met it: at the entry after it.
    return (sequence, *_locate(files, sequence + 1))


def _tamper_inserted(path: Path, files: list[Path]) -> tuple[int, str, int]:
    rows = _rows_of(path)
    rows.append(copy.deepcopy(rows[1]))
    _write_rows(path, rows)
    return _sequence_of(rows[1]), str(path), len(rows)


_TAMPERS = {
    "altered": (_tamper_altered, ISSUE_ENTRY_MODIFIED),
    "removed": (_tamper_removed, ISSUE_MISSING_ENTRY),
    "inserted": (_tamper_inserted, ISSUE_DUPLICATE_ENTRY),
}


def _select_by_pattern(log_dir: Path) -> list[Any]:
    return [log_dir, "--pattern", "*.jsonl"]


def _select_explicitly(log_dir: Path) -> list[Any]:
    return list(_ledger_files(log_dir))


def _select_renamed(log_dir: Path) -> list[Any]:
    for index, path in enumerate(_ledger_files(log_dir)):
        path.rename(log_dir / f"export-{index}.ndjson")
    return [log_dir, "--pattern", "*.ndjson"]


class TestCliTrailBehavior:
    """The CLI merges every root's files of one partition into one trail."""

    def test_two_hosts_untouched_fleet_verifies_intact(self, fleet, capsys):
        code, payload = _run_cli_json(capsys, fleet["a"].log_dir, fleet["b"].log_dir)

        assert code == 0
        (trail,) = payload["trails"]
        assert trail["entries"] == len(_FLEET_ORDER)
        assert (trail["issues"], trail["notes"]) == ([], [])

    @pytest.mark.parametrize("host", ["a", "b"], ids=["host_a", "host_b"])
    @pytest.mark.parametrize("kind", ["altered", "removed", "inserted"])
    def test_two_hosts_tampered_entry_is_reported_with_its_sequence_and_file(
        self, fleet, capsys, kind, host
    ):
        # Given one entry of one host's file altered, removed or inserted
        files = [_fleet_file(fleet, name) for name in ("a", "b")]
        tamper, issue_type = _TAMPERS[kind]
        sequence, expected_file, expected_line = tamper(_fleet_file(fleet, host), files)

        # When the fleet is verified with both hosts' directories
        code, payload = _run_cli_json(capsys, fleet["a"].log_dir, fleet["b"].log_dir)

        # Then the one issue names the entry's sequence and where it was met
        issues = _cli_issues(payload)
        assert code == 1
        assert _found(issues) == [(issue_type, sequence)]
        assert (issues[0]["file"], issues[0]["line"]) == (expected_file, expected_line)

    def test_starts_at_oldest_day_removed_fails_naming_the_rerun(
        self, three_day_ledger, capsys
    ):
        _ledger_files(three_day_ledger.log_dir)[0].unlink()
        first_kept = _ENTRIES_PER_DAY + 1

        code, payload = _run_cli_json(capsys, three_day_ledger.log_dir)

        issues = _cli_issues(payload)
        assert code == 1
        assert _found(issues) == [(ISSUE_MISSING_ENTRY, 1)]
        assert issues[0]["last_sequence"] == first_kept - 1
        assert f"--starts-at {first_kept}" in issues[0]["message"]

    def test_starts_at_given_k_verifies_a_pruned_ledger(self, three_day_ledger):
        _ledger_files(three_day_ledger.log_dir)[0].unlink()

        code = _run_cli(three_day_ledger.log_dir, "--starts-at", _ENTRIES_PER_DAY + 1)

        assert code == 0

    def test_line_order_differing_from_sequence_order_verifies_intact(
        self, one_day_ledger
    ):
        (path,) = _ledger_files(one_day_ledger)
        rows = list(reversed(_rows_of(path)))
        _write_rows(path, rows)
        assert [_sequence_of(row) for row in rows] != sorted(
            _sequence_of(row) for row in rows
        )

        assert _run_cli(one_day_ledger) == 0

    @pytest.mark.parametrize(
        "select",
        [_select_by_pattern, _select_explicitly, _select_renamed],
        ids=["pattern_partitions", "explicit_partitions", "renamed_partitions"],
    )
    def test_two_partition_directory_reports_two_intact_trails(
        self, tmp_path, capsys, select
    ):
        # Given one directory holding two untouched partitions' ledgers
        log_dir = tmp_path / "audit"
        for partition in ("", "worker"):
            adapter = HashChainFileAuditLogAdapter(
                log_dir=str(log_dir), partition=partition
            )
            _write_days(adapter, days=_DAYS[:1], per_day=3)

        # When the CLI selects them by glob, by name, or renamed by glob
        code, payload = _run_cli_json(capsys, *select(log_dir))

        # Then they stay two trails: no shared sequence reads as a fork
        assert code == 0
        assert [(t["partition"], t["entries"]) for t in payload["trails"]] == [
            ("default", 3),
            ("worker", 3),
        ]
        assert NOTE_CHAIN_FORK not in _types(_cli_notes(payload))

    @pytest.mark.parametrize(
        "roots",
        [
            lambda log_dir: [log_dir, log_dir],
            lambda log_dir: [log_dir, _ledger_files(log_dir)[0]],
        ],
        ids=["same_dir_twice", "dir_and_its_own_file"],
    )
    def test_overlapping_roots_report_no_duplicate_entry(
        self, three_day_ledger, capsys, roots
    ):
        code, payload = _run_cli_json(capsys, *roots(three_day_ledger.log_dir))

        assert code == 0
        assert _cli_issues(payload) == []
        assert payload["summary"]["total_entries"] == len(_DAYS) * _ENTRIES_PER_DAY
