"""
Ledger tail reader for the audit hash chain.

The chain keeps its **ordering** in one place — a JSON state file locally, a
Redis counter in distributed mode — and the **ledger it orders** in another:
the adapter's ``audit_*.jsonl`` files. Reading the ledger's own tail is what
lets a manager notice that its sequence source has fallen behind the entries
already on disk, and it is what the boot reconciliation compares Redis
against. Both of those readers used to be hand-rolled; this module is the one
implementation, so the write path and the boot sync cannot drift apart.

Selection is exact, derived from the adapter's own filename pattern and
rotation mode rather than from a glob: a glob over ``audit_*.jsonl`` also
matches every partitioned sibling, a partition ``worker`` also matches
``celery_worker``, and an operator override of the pattern is ignored
entirely. Enumeration goes through :func:`list_ledger_files`, never
``Path.glob`` — CPython's glob selector swallows the directory ``OSError``, so
an unreadable directory would read as "no ledger" and a lost source would then
mint ``1`` into a live ledger.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from baldur.utils.serialization import fast_loads

logger = structlog.get_logger()

__all__ = [
    "CHAIN_STATE_FILE_PREFIX",
    "DEFAULT_LEDGER_FILENAME_PATTERN",
    "LEDGER_TAIL_HEAD_PROBE_BYTES",
    "LEDGER_TAIL_INITIAL_WINDOW_BYTES",
    "LEDGER_TAIL_MAX_TOTAL_BYTES",
    "LEDGER_TAIL_MAX_WINDOW_BYTES",
    "LEDGER_TAIL_MIN_LINES",
    "PARTITIONED_LEDGER_FILENAME_PATTERN",
    "LedgerTail",
    "LedgerTailReader",
    "LedgerWindow",
    "LedgerWindowFile",
    "ledger_file_partition",
    "ledger_filename_regex",
    "list_ledger_files",
    "read_ledger_window",
]


# First read from the end of a ledger file. A real audited row is ~550 B (two
# 64-hex hashes plus the envelope), so this holds well over the line floor
# below in a single read — the whole point of not reusing the 10 KiB the boot
# sync used, which held ~18 lines and would force a second read on every
# write.
LEDGER_TAIL_INITIAL_WINDOW_BYTES = 32768

# How many complete lines the window must hold before "the highest sequence in
# the window" is trusted as "the highest sequence on disk". The manager lock is
# released before the adapter appends, so siblings append out of mint order and
# the last line can be a lower number than one appended just before it. The
# floor exceeds any single-volume writer count at tier (Gunicorn workers plus a
# Celery worker plus cron).
LEDGER_TAIL_MIN_LINES = 32

# Ceiling on one file's tail window. The read happens inside the manager's
# exclusive section, so growth must terminate; past this the window stops
# doubling and the terminal rules below decide.
LEDGER_TAIL_MAX_WINDOW_BYTES = 8 * 1024 * 1024

# How much of a file's *head* is probed before its tail window is allowed to
# grow. A file whose head and tail both hold complete lines with no positive
# sequence is chain-less — a day the chain was switched off — and the walk
# moves on without reading the middle. Without it, every such file costs its
# whole size (up to the cap) inside the exclusive section, and nothing prunes
# the ledger, so the file count is the install's age in days.
LEDGER_TAIL_HEAD_PROBE_BYTES = 32768

# Budget for one whole walk, summed across every file it reads. Past this the
# reader no longer knows whether an unscanned file holds a chain, and returning
# "no ledger" there is the one answer that re-mints from 1 into a live ledger —
# so exhausting the budget raises instead. With the head probe a chain-less
# file costs 64 KiB, so this admits ~1000 of them.
LEDGER_TAIL_MAX_TOTAL_BYTES = 64 * 1024 * 1024

# The token the adapter substitutes into its filename pattern, and what it
# substitutes: an ISO date when rotating daily, the literal ``all`` when not.
_DATE_TOKEN = "{date}"
_DATE_REGEX = r"\d{4}-\d{2}-\d{2}"
_UNROTATED_DATE = "all"

# The ledger's default file names: the un-partitioned chain, and a partition's
# own chain. The adapter builds its names from these, and a check that has no
# adapter to ask reads a file's partition back out of the same shape.
DEFAULT_LEDGER_FILENAME_PATTERN = "audit_{date}.jsonl"
PARTITIONED_LEDGER_FILENAME_PATTERN = "audit_{date}_{partition}.jsonl"
_PARTITION_TOKEN = "{partition}"
_PARTITION_CAPTURE = "(?P<partition>.+)"

# Every file the chain manager keeps beside the ledger — its state, the lock
# and the per-writer temp of an atomic save — starts with this name. None of
# them is a ledger, whatever a selection glob says.
CHAIN_STATE_FILE_PREFIX = ".hash_chain_state"


@dataclass(frozen=True)
class LedgerTail:
    """The highest-sequenced entry the ledger holds, and where it was found."""

    sequence: int
    current_hash: str
    path: Path


def ledger_filename_regex(
    filename_pattern: str,
    rotate_daily: bool | None = True,
) -> re.Pattern[str]:
    """Compile the exact filename shape an adapter's pattern produces.

    ``rotate_daily`` is a parameter rather than an inference because the
    pattern string alone cannot say which token the adapter writes: the same
    ``audit_{date}.jsonl`` produces ``audit_2026-09-07.jsonl`` when rotating
    and ``audit_all.jsonl`` when not.

    Args:
        filename_pattern: The adapter's pattern, e.g. ``audit_{date}.jsonl``
            or ``audit_{date}_worker.jsonl``. A ``{partition}`` token matches
            any partition name and captures it as the ``partition`` group.
        rotate_daily: Whether the adapter substitutes a date or ``all``;
            ``None`` accepts either.

    Returns:
        A pattern to ``fullmatch`` against a file **name**.
    """
    if rotate_daily is None:
        date_part = f"(?:{_DATE_REGEX}|{re.escape(_UNROTATED_DATE)})"
    elif rotate_daily:
        date_part = _DATE_REGEX
    else:
        date_part = re.escape(_UNROTATED_DATE)
    return re.compile(
        date_part.join(
            _PARTITION_CAPTURE.join(
                re.escape(piece) for piece in part.split(_PARTITION_TOKEN)
            )
            for part in filename_pattern.split(_DATE_TOKEN)
        )
    )


# Partitioned shape first: the default shape cannot match a partitioned name,
# so the order only decides which of two matches is tried first.
_LEDGER_FILE_SHAPES = (
    ledger_filename_regex(PARTITIONED_LEDGER_FILENAME_PATTERN, rotate_daily=None),
    ledger_filename_regex(DEFAULT_LEDGER_FILENAME_PATTERN, rotate_daily=None),
)


def ledger_file_partition(name: str) -> str | None:
    """Read the partition a ledger file belongs to out of its name.

    Args:
        name: A file **name**, e.g. ``audit_2026-09-07_worker.jsonl``.

    Returns:
        The partition (``""`` for the default chain), or ``None`` when the
        name does not have the ledger's default shape.
    """
    for shape in _LEDGER_FILE_SHAPES:
        match = shape.fullmatch(name)
        if match is not None:
            return match.groupdict().get("partition") or ""
    return None


def list_ledger_files(log_dir: Path, regex: re.Pattern[str]) -> list[Path]:
    """List the ledger files matching ``regex``, newest first.

    Enumerates with ``iterdir`` rather than ``glob`` on purpose: the glob
    selector swallows the directory's ``OSError``, which turns "I cannot read
    this directory" into "this directory holds no ledger" — the one answer a
    sequence source must never be given.

    Args:
        log_dir: The directory the adapter writes its ledger into.
        regex: The compiled shape from :func:`ledger_filename_regex`.

    Returns:
        Matching paths, newest first. A directory that does not exist is an
        empty list; one that exists but cannot be enumerated raises.
    """
    if not log_dir.is_dir():
        return []
    matches = [path for path in log_dir.iterdir() if regex.fullmatch(path.name)]
    matches.sort(key=lambda path: path.name, reverse=True)
    return matches


def _scan_window(
    window: bytes,
    *,
    starts_at_file_start: bool,
) -> tuple[tuple[int, str] | None, int]:
    """Find the highest positive sequence among the window's complete lines.

    The window is split on ``b"\\n"`` **before** decoding and each line is
    parsed as bytes, so a window boundary landing inside a multi-byte character
    can never raise. The last element is always dropped — it is either the
    newline terminator's empty tail or an unterminated line left by a crash
    mid-append — and the first is dropped whenever the window does not start at
    the file's first byte.

    Returns:
        ``((sequence, current_hash) | None, complete_line_count)``.
    """
    segments = window.split(b"\n")[:-1]
    if not starts_at_file_start:
        segments = segments[1:]

    best_sequence = 0
    best_hash = ""
    complete_lines = 0

    for segment in segments:
        if not segment.strip():
            continue
        complete_lines += 1
        try:
            row: Any = fast_loads(segment)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        integrity = row.get("integrity")
        if not isinstance(integrity, dict):
            continue
        sequence = integrity.get("sequence", 0)
        if isinstance(sequence, int) and sequence > best_sequence:
            best_sequence = sequence
            best_hash = integrity.get("current_hash") or ""

    if best_sequence > 0:
        return (best_sequence, best_hash), complete_lines
    return None, complete_lines


class LedgerTailReader:
    """Reads the tail of the ledger an adapter writes.

    The tail is the entry carrying the **highest positive sequence** in the
    reader's window, not the last line: the chain's exclusive section is
    released before the adapter appends, so sibling processes append out of
    mint order.
    """

    def __init__(
        self,
        log_dir: Path,
        filename_pattern: str = "audit_{date}.jsonl",
        rotate_daily: bool = True,
    ):
        """Initialize the reader.

        Args:
            log_dir: The directory the adapter writes its ledger into.
            filename_pattern: The adapter's resolved filename pattern.
            rotate_daily: The adapter's rotation mode.
        """
        self._log_dir = Path(log_dir)
        self._filename_pattern = filename_pattern
        self._rotate_daily = rotate_daily
        self._filename_regex = ledger_filename_regex(filename_pattern, rotate_daily)

    @property
    def log_dir(self) -> Path:
        """The directory this reader enumerates."""
        return self._log_dir

    @property
    def filename_pattern(self) -> str:
        """The adapter filename pattern this reader was built from."""
        return self._filename_pattern

    @property
    def rotate_daily(self) -> bool:
        """The adapter rotation mode this reader was built from."""
        return self._rotate_daily

    @property
    def filename_regex(self) -> re.Pattern[str]:
        """The exact filename shape this reader selects."""
        return self._filename_regex

    def read(self) -> LedgerTail | None:
        """Read the ledger's tail.

        Returns:
            The highest-sequenced entry on disk, or ``None`` when every
            matching file was read to its start and none holds a chain — a
            genuinely fresh ledger.

        Raises:
            OSError: The ledger exists but its tail cannot be read —
                enumeration failed, a file read failed, a single row is larger
                than the window cap, or the walk budget ran out. A caller must
                refuse the write rather than treat this as a fresh start.
        """
        total_read = 0
        last_path: Path | None = None

        for path in list_ledger_files(self._log_dir, self._filename_regex):
            last_path = path
            tail, file_bytes = self._read_file_tail(path)
            total_read += file_bytes
            if tail is not None:
                return tail
            if total_read >= LEDGER_TAIL_MAX_TOTAL_BYTES:
                raise OSError(
                    f"audit ledger tail unreadable: walk budget of "
                    f"{LEDGER_TAIL_MAX_TOTAL_BYTES} bytes exhausted after "
                    f"{total_read} bytes, last file scanned {last_path}"
                )

        return None

    def _read_file_tail(self, path: Path) -> tuple[LedgerTail | None, int]:
        """Read one file's tail.

        Returns:
            ``(tail | None, bytes_read)``. ``None`` means this file holds no
            chained entry — the walk moves to the next-older one.

        Raises:
            OSError: The file cannot be read, or its window cap holds no
                complete line at all (a single row larger than the cap).
                Walking on there would hand an older file's tail to a source
                that then re-mints every number above it.
        """
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            file_size = handle.tell()
            if file_size == 0:
                return None, 0

            bytes_read = 0
            window_size = LEDGER_TAIL_INITIAL_WINDOW_BYTES
            head_probed = False

            while True:
                read_size = min(file_size, window_size)
                handle.seek(file_size - read_size)
                window = handle.read(read_size)
                bytes_read += len(window)

                reached_file_start = read_size >= file_size
                best, complete_lines = _scan_window(
                    window, starts_at_file_start=reached_file_start
                )

                if best is not None and complete_lines >= LEDGER_TAIL_MIN_LINES:
                    return LedgerTail(best[0], best[1], path), bytes_read

                capped = window_size >= LEDGER_TAIL_MAX_WINDOW_BYTES
                if reached_file_start or capped:
                    return self._terminal(
                        path=path,
                        best=best,
                        complete_lines=complete_lines,
                        bytes_read=bytes_read,
                        reached_file_start=reached_file_start,
                    )

                if not head_probed:
                    head_probed = True
                    handle.seek(0)
                    head = handle.read(min(file_size, LEDGER_TAIL_HEAD_PROBE_BYTES))
                    bytes_read += len(head)
                    head_best, head_lines = _scan_window(
                        head, starts_at_file_start=True
                    )
                    if (
                        best is None
                        and complete_lines >= 1
                        and head_best is None
                        and head_lines >= 1
                    ):
                        # Chain-less at both ends: a day written with the chain
                        # off. Skip the middle rather than paying its size.
                        logger.warning(
                            "ledger_tail.window_capped",
                            path=str(path),
                            bytes_scanned=bytes_read,
                            lines=complete_lines,
                        )
                        return None, bytes_read

                window_size *= 2

    def _terminal(
        self,
        *,
        path: Path,
        best: tuple[int, str] | None,
        complete_lines: int,
        bytes_read: int,
        reached_file_start: bool,
    ) -> tuple[LedgerTail | None, int]:
        """Decide what a window that stopped growing says about this file."""
        if not reached_file_start:
            if best is None and complete_lines == 0:
                # The file's last row is itself larger than the cap, so this
                # ledger's tail cannot be read at all.
                raise OSError(
                    f"audit ledger tail unreadable: no complete line within "
                    f"{LEDGER_TAIL_MAX_WINDOW_BYTES} bytes of the end of {path}"
                )
            logger.warning(
                "ledger_tail.window_capped",
                path=str(path),
                bytes_scanned=bytes_read,
                lines=complete_lines,
            )

        if best is not None:
            return LedgerTail(best[0], best[1], path), bytes_read
        return None, bytes_read


@dataclass(frozen=True)
class LedgerWindowFile:
    """One file's share of a ledger window.

    Attributes:
        path: The ledger file.
        lines: Its complete, non-blank lines in the window, oldest first, each
            with its line number when the read covered the whole file
            (``None`` otherwise — counting would mean reading the rest).
        unterminated: The file's final line when it has no line terminator —
            a write in flight or interrupted — with its line number when
            known; ``None`` when the file ends with a newline.
        reached_file_start: Whether ``lines`` begins at the file's first line.
    """

    path: Path
    lines: tuple[tuple[int | None, bytes], ...]
    unterminated: tuple[int | None, bytes] | None
    reached_file_start: bool


@dataclass(frozen=True)
class LedgerWindow:
    """The most recent complete lines of a ledger, across its files.

    Attributes:
        files: Each file read, oldest first.
        reached_ledger_start: Whether the read covered the ledger's oldest
            file from its first line — the whole ledger was read.
    """

    files: tuple[LedgerWindowFile, ...]
    reached_ledger_start: bool

    @property
    def line_count(self) -> int:
        """How many complete lines the window holds."""
        return sum(len(part.lines) for part in self.files)


def read_ledger_window(
    log_dir: Path,
    regex: re.Pattern[str],
    line_count: int,
) -> LedgerWindow:
    """Read the ledger's last ``line_count`` complete lines, newest files first.

    Each file is read from its end through a window that doubles up to
    :data:`LEDGER_TAIL_MAX_WINDOW_BYTES`. A file whose read reaches that cap
    before its first line is the last file read: reading on into an older file
    would leave the capped file's unread middle as a stretch of absent
    entries between present ones, which a chain check reads as removed.

    Args:
        log_dir: The directory the adapter writes its ledger into.
        regex: The compiled shape from :func:`ledger_filename_regex`.
        line_count: How many complete lines the window should hold.

    Returns:
        The window, which holds fewer lines when the ledger is shorter or a
        file's read reached the cap.

    Raises:
        OSError: The directory or a file cannot be read, or a file's last
            complete line lies beyond the cap (a single row larger than it).
        ValueError: ``line_count`` is below 1.
    """
    if line_count < 1:
        raise ValueError(f"line_count must be at least 1, got {line_count}")

    newest_first = list_ledger_files(log_dir, regex)
    collected: list[LedgerWindowFile] = []
    remaining = line_count
    for path in newest_first:
        part = _read_file_lines_from_end(path, remaining)
        collected.append(part)
        remaining -= len(part.lines)
        if not part.reached_file_start or remaining <= 0:
            break

    reached_ledger_start = not collected or (
        collected[-1].reached_file_start and collected[-1].path == newest_first[-1]
    )
    return LedgerWindow(tuple(reversed(collected)), reached_ledger_start)


def _read_file_lines_from_end(path: Path, wanted: int) -> LedgerWindowFile:
    """Read up to ``wanted`` complete lines from the end of one ledger file.

    Split on ``b"\n"`` before any decoding, as :func:`_scan_window` does, so a
    window boundary inside a multi-byte character can never raise.
    """
    with open(path, "rb") as handle:
        handle.seek(0, 2)
        file_size = handle.tell()
        if file_size == 0:
            return LedgerWindowFile(path, (), None, True)

        window_size = LEDGER_TAIL_INITIAL_WINDOW_BYTES
        while True:
            read_size = min(file_size, window_size)
            handle.seek(file_size - read_size)
            window = handle.read(read_size)
            reached_file_start = read_size >= file_size

            segments = window.split(b"\n")
            last_segment = segments.pop()
            if not reached_file_start:
                # The first segment may begin mid-line.
                segments = segments[1:]

            lines = [
                (index + 1 if reached_file_start else None, segment)
                for index, segment in enumerate(segments)
                if segment.strip()
            ]
            unterminated = None
            if last_segment.strip():
                unterminated = (
                    len(segments) + 1 if reached_file_start else None,
                    last_segment,
                )

            if len(lines) >= wanted:
                return LedgerWindowFile(
                    path,
                    tuple(lines[-wanted:]),
                    unterminated,
                    reached_file_start and len(lines) == wanted,
                )
            if reached_file_start:
                return LedgerWindowFile(path, tuple(lines), unterminated, True)
            if window_size >= LEDGER_TAIL_MAX_WINDOW_BYTES:
                if not lines:
                    raise OSError(
                        f"audit ledger window unreadable: no complete line within "
                        f"{LEDGER_TAIL_MAX_WINDOW_BYTES} bytes of the end of {path}"
                    )
                logger.debug(
                    "ledger_tail.window_read_capped",
                    path=str(path),
                    bytes_scanned=read_size,
                    lines=len(lines),
                )
                return LedgerWindowFile(path, tuple(lines), unterminated, False)
            window_size *= 2
