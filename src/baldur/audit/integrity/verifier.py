"""
Audit trail verification — the one walk of the hash chain.

Every integrity check of the audit trail runs here: the CLI, the file adapter's
own check, the admin route's recent window, the export tool's pass, the daily
anchor's check and the list API below. A second copy of the walk is how two
checks end up applying different rules to the same trail.

The walk reads in two passes. Each entry's fingerprint is checked as it is
read, which needs no order; one compact record per entry is kept; the records
are then sorted by sequence and linked. File and line order are storage, not
chain order: writers append out of mint order, an entry minted just before
midnight can land in the next day's file, and a fleet's files interleave.

Contains:
- ChainStart: where a check expects the trail to begin
- TrailReport: the verdict on one trail
- TrailWalk / TrailSet: the walk over one trail / over several, by partition
- verify_ledger_window: the walk over a recent window of one ledger
- HashChainVerifier: list views of the walk (first issue / all issues)
- verify_audit_log_integrity: the walk over a file or a directory
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from baldur.audit.integrity.ledger_tail import (
    CHAIN_STATE_FILE_PREFIX,
    LEDGER_TAIL_MIN_LINES,
    LedgerWindow,
    ledger_file_partition,
)
from baldur.audit.integrity.models import (
    KEY_FROM_SETTINGS,
    _audit_signing_key,
    compute_hash,
)

logger = structlog.get_logger()

GENESIS_HASH = "GENESIS"

# Fingerprint verdicts of one entry.
_FINGERPRINT_OK = 0
_FINGERPRINT_UNKEYED = 1
_FINGERPRINT_BAD = 2

# Failing issue types.
ISSUE_ENTRY_MODIFIED = "entry_modified"
ISSUE_CHAIN_BROKEN = "chain_broken"
ISSUE_MISSING_ENTRY = "missing_entry"
ISSUE_DUPLICATE_ENTRY = "duplicate_entry"
ISSUE_UNREADABLE_ROW = "unreadable_row"
ISSUE_UNKEYED_ENTRY = "unkeyed_entry"
ISSUE_SIGNING_KEY_MISSING = "signing_key_missing"
ISSUE_SIGNING_KEY_MISMATCH = "signing_key_mismatch"

# Non-failing note types.
NOTE_CHAIN_FORK = "chain_fork"
NOTE_NOT_HELD_HERE = "not_held_here"
NOTE_HEAD_ABSENT = "head_absent"
NOTE_ROWS_WITHOUT_CHAIN = "rows_without_chain"
NOTE_UNCHAINED = "unchained"
NOTE_INCOMPLETE_LAST_LINE = "incomplete_last_line"

_SIGNING_KEY_ENV = "BALDUR_SECRETS_AUDIT_SIGNING_KEY"


@dataclass(frozen=True)
class ChainStart:
    """Where a check expects the trail to begin.

    The trail's first entry is the one at ``sequence + 1``, and it must link
    to ``hash``. The chain's own beginning is ``ChainStart(0, "GENESIS")``.
    A ``hash`` of ``None`` trusts that first entry's link — the shape of a
    check told to begin at a later entry, whose predecessor it never reads.

    Attributes:
        sequence: The sequence just before the trail's first entry.
        hash: The stored hash the first entry links to, or ``None``.
    """

    sequence: int
    hash: str | None


CHAIN_BEGINNING = ChainStart(0, GENESIS_HASH)


def chain_start_at(sequence: int) -> ChainStart:
    """Build the start of a check that expects the trail to begin at ``sequence``.

    Args:
        sequence: The first sequence the trail must hold, 1 or higher.

    Returns:
        The chain's own beginning for ``1``; otherwise a start that trusts the
        link of the entry at ``sequence``.

    Raises:
        ValueError: ``sequence`` is below 1.
    """
    if sequence < 1:
        raise ValueError(f"a trail starts at sequence 1 or later, got {sequence}")
    if sequence == 1:
        return CHAIN_BEGINNING
    return ChainStart(sequence - 1, None)


@dataclass
class TrailReport:
    """The verdict on one trail.

    Attributes:
        partition: The trail's partition (``""`` for the default chain).
        intact: No failing issue was found.
        entries: Entries carrying an integrity block with a sequence.
        rows: Every non-blank row read, chained or not.
        first_sequence: The lowest positive sequence present, if any.
        last_sequence: The highest positive sequence present, if any.
        issues: Failing findings, each with ``type``, ``sequence``, ``file``,
            ``line`` and ``message`` (plus ``last_sequence`` for a range).
        notes: Non-failing findings, in the same shape.
        files: Every file a row of the trail was read from.
    """

    partition: str = ""
    intact: bool = True
    entries: int = 0
    rows: int = 0
    first_sequence: int | None = None
    last_sequence: int | None = None
    issues: list[dict[str, Any]] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    files: list[str] = field(default_factory=list)


class _Record:
    """The compact form of one chained entry the walk keeps between passes."""

    __slots__ = (
        "file",
        "line",
        "linked",
        "previous_hash",
        "redis_minted",
        "sequence",
        "stamped",
        "stored_hash",
        "verdict",
    )

    def __init__(
        self,
        *,
        sequence: int,
        previous_hash: str,
        stored_hash: str,
        file: int | None,
        line: int | None,
        redis_minted: bool,
        stamped: bool,
        verdict: int,
    ) -> None:
        self.sequence = sequence
        self.previous_hash = previous_hash
        self.stored_hash = stored_hash
        self.file = file
        self.line = line
        self.redis_minted = redis_minted
        self.stamped = stamped
        self.verdict = verdict
        self.linked = True


def _equal(left: str, right: str) -> bool:
    """Compare two hash strings in constant time."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _default_rerun_hint(sequence: int) -> str:
    return f"verify with start=ChainStart({sequence - 1}, None) to begin at {sequence}"


def _range_text(first: int, last: int, *, noun: str = "entry") -> str:
    if first == last:
        return f"{noun} {first}"
    plural = "entries" if noun == "entry" else f"{noun}s"
    return f"{plural} {first}-{last}"


class TrailWalk:
    """The walk over one trail.

    Feed it every row of the trail, in any order, then ask for the report.
    The walk holds one compact record per chained entry — about 275 bytes —
    so a trail of a million entries costs about 275 MB.
    """

    def __init__(self, partition: str = "", *, key: Any = KEY_FROM_SETTINGS) -> None:
        """Initialize the walk.

        Args:
            partition: The trail's partition, reported back unchanged.
            key: The signing key to verify with, ``None`` for keyless. Left
                out, the configured key is read once, here.
        """
        self._partition = partition
        self._key: bytes | None = (
            _audit_signing_key() if key is KEY_FROM_SETTINGS else key
        )
        self._files: list[str] = []
        self._file_index: dict[str, int] = {}
        self._records: list[_Record] = []
        self._unchained: list[_Record] = []
        self._unreadable: list[dict[str, Any]] = []
        self._incomplete: list[dict[str, Any]] = []
        self._rows = 0
        self._rows_without_chain = 0

    # ------------------------------------------------------------------
    # First pass: one row at a time
    # ------------------------------------------------------------------

    def add_row(
        self, row: Any, *, file: str | None = None, line: int | None = None
    ) -> None:
        """Check one row's fingerprint and keep its compact record.

        Args:
            row: The parsed row.
            file: Where it was read from, if from a file.
            line: Its line number (or its position in a list).
        """
        self._rows += 1
        file_index = self._intern_file(file)
        integrity = row.get("integrity") if isinstance(row, dict) else None
        if not isinstance(integrity, dict):
            self._rows_without_chain += 1
            return

        sequence = integrity.get("sequence")
        if type(sequence) is not int:
            self.add_unreadable(
                file=file,
                line=line,
                reason="its integrity block carries no integer sequence",
                counted=True,
            )
            return

        stored_hash = integrity.get("current_hash")
        stored_hash = stored_hash if isinstance(stored_hash, str) else ""
        previous_hash = integrity.get("previous_hash")
        previous_hash = previous_hash if isinstance(previous_hash, str) else ""

        record = _Record(
            sequence=sequence,
            previous_hash=previous_hash,
            stored_hash=stored_hash,
            file=file_index,
            line=line,
            redis_minted="pod_id" in integrity,
            stamped="source_reset" in integrity or bool(integrity.get("degraded")),
            verdict=self._fingerprint(row, integrity, stored_hash),
        )
        if sequence > 0:
            self._records.append(record)
        else:
            self._unchained.append(record)

    def add_unreadable(
        self,
        *,
        file: str | None,
        line: int | None,
        reason: str,
        counted: bool = False,
    ) -> None:
        """Record a row that cannot be read as an audit entry.

        Args:
            file: Where it was read from.
            line: Its line number.
            reason: Why it cannot be read.
            counted: Whether ``add_row`` already counted the row.
        """
        if not counted:
            self._rows += 1
            self._intern_file(file)
        self._unreadable.append(
            {
                "type": ISSUE_UNREADABLE_ROW,
                "sequence": None,
                "file": file,
                "line": line,
                "message": f"Row is not a readable audit entry: {reason}",
            }
        )

    def add_incomplete_last_line(self, *, file: str | None, line: int | None) -> None:
        """Record a file whose final line has no line terminator."""
        self._intern_file(file)
        self._incomplete.append(
            {
                "type": NOTE_INCOMPLETE_LAST_LINE,
                "sequence": None,
                "file": file,
                "line": line,
                "message": (
                    "The file's final line has no line terminator: a write in "
                    "flight or interrupted (the entry's loss, if any, shows as a gap)"
                ),
            }
        )

    def _intern_file(self, file: str | None) -> int | None:
        if file is None:
            return None
        index = self._file_index.get(file)
        if index is None:
            index = len(self._files)
            self._files.append(file)
            self._file_index[file] = index
        return index

    def _fingerprint(
        self, row: dict[str, Any], integrity: dict[str, Any], stored_hash: str
    ) -> int:
        body = dict(row)
        hashed_integrity = dict(integrity)
        hashed_integrity.pop("current_hash", None)
        body["integrity"] = hashed_integrity

        if _equal(stored_hash, compute_hash(body, key=self._key)):
            return _FINGERPRINT_OK
        if self._key is not None and _equal(stored_hash, compute_hash(body, key=None)):
            return _FINGERPRINT_UNKEYED
        return _FINGERPRINT_BAD

    # ------------------------------------------------------------------
    # Second pass: sort, link, report
    # ------------------------------------------------------------------

    def report(
        self,
        *,
        start: ChainStart | None = CHAIN_BEGINNING,
        host_local: bool = False,
        head_absent_failing: bool = True,
        rerun_hint: Callable[[int], str] = _default_rerun_hint,
    ) -> TrailReport:
        """Sort the records by sequence, link them, and report.

        Args:
            start: Where the trail is expected to begin; ``None`` trusts the
                lowest entry present.
            host_local: The files are one host's own: a gap whose successor
                was sequenced through Redis is another host's entries (a
                note), not a removal.
            head_absent_failing: An absent head (the trail begins above the
                start) fails; otherwise it is a note.
            rerun_hint: The re-run that begins the check at a given sequence,
                named in a failing head's message.

        Returns:
            The trail's report.
        """
        return _Linker(self, start, host_local, head_absent_failing, rerun_hint).run()


class _Linker:
    """The second pass of one :class:`TrailWalk`."""

    def __init__(
        self,
        walk: TrailWalk,
        start: ChainStart | None,
        host_local: bool,
        head_absent_failing: bool,
        rerun_hint: Callable[[int], str],
    ) -> None:
        self._walk = walk
        self._host_local = host_local
        self._head_absent_failing = head_absent_failing
        self._rerun_hint = rerun_hint
        self._records = sorted(walk._records, key=lambda record: record.sequence)
        if start is None and self._records:
            start = ChainStart(self._records[0].sequence - 1, None)
        self._start = start or CHAIN_BEGINNING
        self._issues: list[tuple[int, dict[str, Any]]] = []
        self._notes: list[dict[str, Any]] = []
        self._hash_lookup: dict[str, int] | None = None
        self._key_issue = self._classify_key()

    # -- helpers ---------------------------------------------------------

    def _location(self, record: _Record) -> dict[str, Any]:
        file = None if record.file is None else self._walk._files[record.file]
        return {"file": file, "line": record.line}

    def _fingerprint_valid(self, record: _Record) -> bool:
        return self._key_issue is not None or record.verdict != _FINGERPRINT_BAD

    def _issue(self, sequence: int, issue: dict[str, Any]) -> None:
        self._issues.append((sequence, issue))

    def _lowest_sequence_holding(self, stored_hash: str) -> int | None:
        if self._hash_lookup is None:
            lookup: dict[str, int] = {}
            for record in self._records:
                if record.stored_hash and record.stored_hash not in lookup:
                    lookup[record.stored_hash] = record.sequence
            self._hash_lookup = lookup
        return self._hash_lookup.get(stored_hash)

    # -- the trail-level key verdict ------------------------------------

    def _classify_key(self) -> dict[str, Any] | None:
        fingerprinted = self._walk._records + self._walk._unchained
        if not fingerprinted:
            return None
        if any(record.verdict == _FINGERPRINT_OK for record in fingerprinted):
            return None
        if self._walk._key is None:
            return {
                "type": ISSUE_SIGNING_KEY_MISSING,
                "sequence": None,
                "file": None,
                "line": None,
                "message": (
                    "No entry verifies without a signing key: set "
                    f"{_SIGNING_KEY_ENV} to the key the trail was written with"
                ),
            }
        if any(record.verdict == _FINGERPRINT_UNKEYED for record in fingerprinted):
            return None
        return {
            "type": ISSUE_SIGNING_KEY_MISMATCH,
            "sequence": None,
            "file": None,
            "line": None,
            "message": (
                "No entry verifies under the configured signing key: a "
                "different key, or every entry rewritten"
            ),
        }

    # -- the walk ---------------------------------------------------------

    def run(self) -> TrailReport:
        self._report_fingerprints()

        start = self._start
        previous_present = start.sequence
        seen_above_start = False
        by_sequence: dict[int, list[_Record]] = {}
        for record in self._records:
            by_sequence.setdefault(record.sequence, []).append(record)

        for sequence, group in by_sequence.items():
            if sequence <= start.sequence:
                self._check_duplicates(group)
                continue

            gap = sequence > previous_present + 1
            if gap:
                self._report_gap(
                    previous_present + 1, sequence - 1, group, head=not seen_above_start
                )
            else:
                self._link_group(group, by_sequence.get(sequence - 1, ()))
            self._check_duplicates(group)
            self._check_fork(sequence, group)
            previous_present = sequence
            seen_above_start = True

        return self._build_report()

    def _report_fingerprints(self) -> None:
        if self._key_issue is not None:
            return
        unkeyed_run: list[_Record] = []
        for record in sorted(
            self._walk._unchained + self._records, key=lambda item: item.sequence
        ):
            if record.verdict == _FINGERPRINT_BAD:
                self._issue(
                    record.sequence,
                    {
                        "type": ISSUE_ENTRY_MODIFIED,
                        "sequence": record.sequence,
                        **self._location(record),
                        "message": f"Entry {record.sequence} has been modified: hash mismatch",
                    },
                )
            if record.verdict == _FINGERPRINT_UNKEYED:
                if unkeyed_run and record.sequence > unkeyed_run[-1].sequence + 1:
                    self._report_unkeyed(unkeyed_run)
                    unkeyed_run = []
                unkeyed_run.append(record)
        if unkeyed_run:
            self._report_unkeyed(unkeyed_run)

    def _report_unkeyed(self, run: list[_Record]) -> None:
        first, last = run[0].sequence, run[-1].sequence
        self._issue(
            first,
            {
                "type": ISSUE_UNKEYED_ENTRY,
                "sequence": first,
                "last_sequence": last,
                **self._location(run[0]),
                "message": (
                    f"{_range_text(first, last).capitalize()} "
                    f"{'matches' if first == last else 'match'} only the "
                    "keyless hash (hash mismatch under the configured signing "
                    "key): anyone without the key can produce such an entry"
                ),
            },
        )

    def _report_gap(
        self, first: int, last: int, successors: list[_Record], *, head: bool
    ) -> None:
        successor = successors[0]
        redis_minted = any(record.redis_minted for record in successors)
        absent = _range_text(first, last)
        are = "is" if first == last else "are"

        if self._host_local and redis_minted:
            self._notes.append(
                {
                    "type": NOTE_NOT_HELD_HERE,
                    "sequence": first,
                    "last_sequence": last,
                    **self._location(successor),
                    "message": (
                        f"{absent.capitalize()} {are} not in this host's files: held "
                        "by another host, or missing - verify the fleet with every "
                        "host's directory on the CLI"
                    ),
                }
            )
            return

        distributed = (
            "; this is a distributed chain: pass every host's directory"
            if redis_minted
            else ""
        )
        if head:
            if not self._head_absent_failing:
                self._notes.append(
                    {
                        "type": NOTE_HEAD_ABSENT,
                        "sequence": first,
                        "last_sequence": last,
                        **self._location(successor),
                        "message": (
                            f"The trail read begins at entry {successor.sequence}: "
                            f"{absent} {are} not in these files"
                        ),
                    }
                )
                return
            message = (
                f"Missing {absent}: the trail begins at {successor.sequence} - "
                "earlier files were removed, or the trail was pruned on purpose; "
                f"if pruned, {self._rerun_hint(successor.sequence)}{distributed}"
            )
        else:
            message = (
                f"Missing {absent}: not present between entries {first - 1} and "
                f"{last + 1} - removed, or never written{distributed}"
            )
        self._issue(
            first,
            {
                "type": ISSUE_MISSING_ENTRY,
                "sequence": first,
                "last_sequence": last,
                **self._location(successor),
                "message": message,
            },
        )

    def _link_group(
        self, group: list[_Record], predecessors: Iterable[_Record]
    ) -> None:
        start = self._start
        candidates = [record.stored_hash for record in predecessors]
        against_start_only = not candidates
        first_after_start = group[0].sequence == start.sequence + 1
        trusted = first_after_start and start.hash is None
        if first_after_start and start.hash is not None:
            candidates.append(start.hash)

        for record in group:
            if trusted or any(_equal(record.previous_hash, c) for c in candidates):
                continue
            linked_to = None
            if self._fingerprint_valid(record):
                found = self._lowest_sequence_holding(record.previous_hash)
                if found is not None and found < record.sequence:
                    linked_to = found
            if linked_to is not None:
                self._notes.append(
                    {
                        "type": NOTE_CHAIN_FORK,
                        "sequence": record.sequence,
                        "explained": record.stamped,
                        **self._location(record),
                        "message": (
                            f"Entry {record.sequence} links to entry {linked_to}, "
                            f"not {record.sequence - 1}: a writer-made fork"
                        ),
                    }
                )
                continue
            record.linked = False
            if first_after_start and against_start_only:
                reason = (
                    f"it does not link to the start of the check (entry "
                    f"{start.sequence}, hash {(start.hash or '')[:16]}...)"
                )
            else:
                reason = "its predecessor is not present - removed, or never written"
            self._issue(
                record.sequence,
                {
                    "type": ISSUE_CHAIN_BROKEN,
                    "sequence": record.sequence,
                    **self._location(record),
                    "message": f"Chain broken at entry {record.sequence}: {reason}",
                },
            )

    def _check_duplicates(self, group: list[_Record]) -> None:
        if len(group) < 2:
            return
        seen: set[str] = set()
        for record in group:
            if record.stored_hash in seen:
                self._issue(
                    record.sequence,
                    {
                        "type": ISSUE_DUPLICATE_ENTRY,
                        "sequence": record.sequence,
                        **self._location(record),
                        "message": (
                            f"Entry {record.sequence} appears more than once: a "
                            "copy was inserted"
                        ),
                    },
                )
            seen.add(record.stored_hash)

    def _check_fork(self, sequence: int, group: list[_Record]) -> None:
        if len(group) < 2:
            return
        members: dict[str, _Record] = {}
        for record in group:
            if record.linked and self._fingerprint_valid(record):
                members.setdefault(record.stored_hash, record)
        if len(members) < 2:
            return
        explained = any(record.stamped for record in members.values())
        first = next(iter(members.values()))
        self._notes.append(
            {
                "type": NOTE_CHAIN_FORK,
                "sequence": sequence,
                "explained": explained,
                **self._location(first),
                "message": (
                    f"Entry {sequence} has {len(members)} distinct valid versions: "
                    "a writer-made fork"
                    + (
                        " (a stamped source reset or degraded write)"
                        if explained
                        else ""
                    )
                ),
            }
        )

    def _build_report(self) -> TrailReport:
        walk = self._walk
        notes = list(self._notes)
        if walk._rows_without_chain:
            notes.append(
                {
                    "type": NOTE_ROWS_WITHOUT_CHAIN,
                    "count": walk._rows_without_chain,
                    "message": (
                        f"{walk._rows_without_chain} row(s) carry no integrity "
                        "block: written while the chain was off"
                    ),
                }
            )
        if walk._unchained:
            notes.append(
                {
                    "type": NOTE_UNCHAINED,
                    "count": len(walk._unchained),
                    "message": (
                        f"{len(walk._unchained)} entr{'y' if len(walk._unchained) == 1 else 'ies'} "
                        "carry a sequence of 0 or below: written while no chain "
                        "source answered"
                    ),
                }
            )
        notes.extend(walk._incomplete)

        issues = list(walk._unreadable)
        issues.extend(
            issue for _, issue in sorted(self._issues, key=lambda pair: pair[0])
        )
        if self._key_issue is not None:
            issues.append(self._key_issue)

        records = self._records
        return TrailReport(
            partition=walk._partition,
            intact=not issues,
            entries=len(records) + len(walk._unchained),
            rows=walk._rows,
            first_sequence=records[0].sequence if records else None,
            last_sequence=records[-1].sequence if records else None,
            issues=issues,
            notes=notes,
            files=list(walk._files),
        )


def _row_partition(row: Any) -> str:
    partition = row.get("partition") if isinstance(row, dict) else None
    return partition if isinstance(partition, str) else ""


class TrailSet:
    """The walk over several trails, one per partition.

    Two partitions are never one trail: each has its own sequence source, so
    merged, every shared sequence would read as a fork. A file whose name has
    the ledger's shape belongs to the partition its name carries; any other
    file contributes each row to the partition the row names.
    """

    def __init__(self, *, key: Any = KEY_FROM_SETTINGS) -> None:
        """Initialize the set.

        Args:
            key: The signing key to verify with, ``None`` for keyless. Left
                out, the configured key is read once, here.
        """
        self._key = _audit_signing_key() if key is KEY_FROM_SETTINGS else key
        self._walks: dict[str, TrailWalk] = {}

    def walk(self, partition: str) -> TrailWalk:
        """The walk of one partition, created on first use."""
        walk = self._walks.get(partition)
        if walk is None:
            walk = TrailWalk(partition, key=self._key)
            self._walks[partition] = walk
        return walk

    def read_file(self, path: Path, *, partition: str | None = None) -> Iterator[Any]:
        """Read one file into the set, yielding each row it parses.

        Rows are parsed with the standard library's ``json`` — the serializer
        the hash is computed with — so a value the fast parser would read
        differently (``NaN``, an integer beyond 64 bits) verifies as written.

        Args:
            path: The file to read.
            partition: Put every row in this partition, whatever the file's
                name or the rows say.

        Yields:
            Each row that parses, in file order.

        Raises:
            OSError: The file cannot be read.
        """
        file = str(path)
        fixed = partition if partition is not None else ledger_file_partition(path.name)
        current = fixed if fixed is not None else ""
        with open(path, "rb") as handle:
            for line, raw in enumerate(handle, start=1):
                terminated = raw.endswith(b"\n")
                if not raw.strip():
                    continue
                if not terminated:
                    self.walk(current).add_incomplete_last_line(file=file, line=line)
                try:
                    row = json.loads(raw)
                except ValueError as e:
                    if terminated:
                        self.walk(current).add_unreadable(
                            file=file, line=line, reason=str(e)
                        )
                    continue
                if fixed is None:
                    current = _row_partition(row)
                self.walk(current).add_row(row, file=file, line=line)
                yield row

    def read_window(self, window: LedgerWindow, *, partition: str) -> None:
        """Read a ledger window's lines into one partition's walk."""
        walk = self.walk(partition)
        for part in window.files:
            file = str(part.path)
            for line, raw in part.lines:
                try:
                    row = json.loads(raw)
                except ValueError as e:
                    walk.add_unreadable(file=file, line=line, reason=str(e))
                    continue
                walk.add_row(row, file=file, line=line)
            if part.unterminated is not None:
                line, raw = part.unterminated
                walk.add_incomplete_last_line(file=file, line=line)
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                walk.add_row(row, file=file, line=line)

    def reports(self, **report_options: Any) -> list[TrailReport]:
        """Report every trail, ordered by partition (default first).

        Args:
            **report_options: Passed to :meth:`TrailWalk.report`.
        """
        return [
            self._walks[partition].report(**report_options)
            for partition in sorted(self._walks)
        ]


def iter_ledger_files(
    roots: Iterable[Path],
    *,
    recursive: bool = False,
    pattern: str | None = None,
) -> list[Path]:
    """Select the ledger files under some roots, each file once.

    A directory contributes the files whose names have the ledger's shape
    (``audit_<date|all>[_<partition>].jsonl``), or match ``pattern`` when one
    is given; a file named as a root is read whatever its name. The chain
    manager's state, lock and temp files are never selected, and only regular
    files are. A file reached twice — overlapping roots, a root and its own
    sub-directory — is listed once, by resolved path.

    Args:
        roots: Files and directories to select from.
        recursive: Descend into sub-directories.
        pattern: A glob on the file name, replacing the ledger shape.

    Returns:
        The selected files, sorted by path.

    Raises:
        OSError: A directory cannot be enumerated.
    """
    selected: dict[Path, Path] = {}
    visited_dirs: set[Path] = set()
    for root in roots:
        if root.is_dir():
            _select_in_directory(root, recursive, pattern, selected, visited_dirs)
        elif root.is_file():
            selected.setdefault(root.resolve(), root)
    return sorted(selected.values())


def _selects_ledger_name(name: str, pattern: str | None) -> bool:
    from fnmatch import fnmatch

    if name.startswith(CHAIN_STATE_FILE_PREFIX):
        return False
    if pattern is not None:
        return fnmatch(name, pattern)
    return ledger_file_partition(name) is not None


def _select_in_directory(
    directory: Path,
    recursive: bool,
    pattern: str | None,
    selected: dict[Path, Path],
    visited_dirs: set[Path],
) -> None:
    resolved_dir = directory.resolve()
    if resolved_dir in visited_dirs:
        return
    visited_dirs.add(resolved_dir)
    for entry in directory.iterdir():
        if entry.is_dir():
            if recursive:
                _select_in_directory(entry, recursive, pattern, selected, visited_dirs)
        elif entry.is_file() and _selects_ledger_name(entry.name, pattern):
            selected.setdefault(entry.resolve(), entry)


def verify_ledger_window(
    window: LedgerWindow,
    *,
    partition: str = "",
    key: Any = KEY_FROM_SETTINGS,
) -> TrailReport:
    """Verify a recent window of one host's ledger.

    The oldest :data:`LEDGER_TAIL_MIN_LINES` lines read are a margin: their
    highest entry is where the check begins, so entries appended out of order
    across the window's lower edge are not read as absent. Only when the read
    covered the whole ledger does the check begin at the chain's own start,
    and even then an absent head is a note: the window vouches for what it
    read, not for files that are not there.

    Args:
        window: The lines read by
            :func:`~baldur.audit.integrity.ledger_tail.read_ledger_window`.
        partition: The ledger's partition.
        key: The signing key, as for :class:`TrailWalk`.

    Returns:
        The window's report.
    """
    trails = TrailSet(key=key)
    trails.read_window(window, partition=partition)
    walk = trails.walk(partition)

    if window.reached_ledger_start:
        start: ChainStart | None = CHAIN_BEGINNING
    else:
        start = _margin_start(window)
    return walk.report(start=start, host_local=True, head_absent_failing=False)


def _margin_start(window: LedgerWindow) -> ChainStart | None:
    """The highest entry among the window's oldest lines, or ``None``."""
    remaining = LEDGER_TAIL_MIN_LINES
    best: tuple[int, str] | None = None
    for part in window.files:
        for _, raw in part.lines:
            if remaining <= 0:
                break
            remaining -= 1
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            integrity = row.get("integrity") if isinstance(row, dict) else None
            if not isinstance(integrity, dict):
                continue
            sequence = integrity.get("sequence")
            stored_hash = integrity.get("current_hash")
            if (
                type(sequence) is int
                and sequence > 0
                and isinstance(stored_hash, str)
                and (best is None or sequence > best[0])
            ):
                best = (sequence, stored_hash)
        if remaining <= 0:
            break
    if best is None:
        return None
    return ChainStart(best[0], best[1])


class HashChainVerifier:  # verified-by: test_forge_without_key_fails
    """
    List views of the audit trail walk.

    Detects:
    - Modified entries (fingerprint mismatch)
    - Removed entries (a sequence range absent between present ones)
    - Inserted entries (a link to no present predecessor, or a copy)

    Entries are linked in sequence order, not list order. A list position is
    reported as an issue's ``line``.
    """

    GENESIS_HASH = GENESIS_HASH

    def verify_chain(
        self,
        entries: list[dict[str, Any]],
        *,
        start: ChainStart | None = CHAIN_BEGINNING,
    ) -> tuple[bool, str | None]:
        """
        Verify the integrity of an audit log chain.

        Args:
            entries: List of log entries with integrity fields
            start: Where the chain is expected to begin; ``None`` trusts the
                lowest entry present.

        Returns:
            Tuple of (is_valid, the first failing issue's message)
        """
        report = self._walk(entries, start)
        if report.intact:
            return True, None
        return False, report.issues[0]["message"]

    def find_tampering(
        self,
        entries: list[dict[str, Any]],
        *,
        start: ChainStart | None = CHAIN_BEGINNING,
    ) -> list[dict[str, Any]]:
        """
        Find every failing issue in a chain.

        Args:
            entries: List of log entries with integrity fields
            start: Where the chain is expected to begin; ``None`` trusts the
                lowest entry present.

        Returns:
            List of issues found
        """
        return self._walk(entries, start).issues

    @staticmethod
    def _walk(entries: list[dict[str, Any]], start: ChainStart | None) -> TrailReport:
        walk = TrailWalk()
        for position, entry in enumerate(entries):
            walk.add_row(entry, line=position)
        return walk.report(start=start)


def verify_audit_log_integrity(
    log_path: Path,
    *,
    expected_start: int = 1,
) -> tuple[bool, list[dict[str, Any]]]:
    """
    Verify the integrity of an audit log file, or of every ledger file in a
    directory, as whole trails.

    Args:
        log_path: A JSON Lines audit log file, or a directory of them
        expected_start: The first sequence each trail must hold

    Returns:
        Tuple of (is_valid, issues_list)
    """
    if not log_path.exists():
        return True, []

    trails = TrailSet()
    try:
        for path in iter_ledger_files([log_path]):
            for _row in trails.read_file(path):
                pass
    except OSError as e:
        return False, [{"type": "read_error", "message": str(e)}]

    reports = trails.reports(start=chain_start_at(expected_start))
    issues = [issue for report in reports for issue in report.issues]
    return not issues, issues


__all__ = [
    "CHAIN_BEGINNING",
    "GENESIS_HASH",
    "ChainStart",
    "HashChainVerifier",
    "TrailReport",
    "TrailSet",
    "TrailWalk",
    "chain_start_at",
    "iter_ledger_files",
    "verify_audit_log_integrity",
    "verify_ledger_window",
]
