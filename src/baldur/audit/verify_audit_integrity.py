#!/usr/bin/env python3
"""
Hash Chain Verifier CLI Tool.

CLI tool for verifying audit trail integrity.

Every ledger file of one chain is verified together as one trail, in sequence
order, however many daily files or hosts it spans. Files are grouped by
partition: ``audit_<date|all>.jsonl`` is the default chain and
``audit_<date|all>_<partition>.jsonl`` a partition's own chain. The chain
manager's state, lock and temp files are never read.

Usage:
    # Verify a ledger directory
    python -m baldur.audit.verify_audit_integrity /var/log/audit/

    # Verify a distributed chain: pass every host's directory
    python -m baldur.audit.verify_audit_integrity /mnt/host-a/audit/ /mnt/host-b/audit/

    # A ledger pruned on purpose: begin the check at the oldest kept entry
    python -m baldur.audit.verify_audit_integrity /var/log/audit/ --starts-at 18001

    # JSON output
    python -m baldur.audit.verify_audit_integrity /var/log/audit/ --format json

    # Verify WAL files
    python -m baldur.audit.verify_audit_integrity /var/log/audit/wal/ --wal

The verifier needs the signing key the trail was written with
(``BALDUR_SECRETS_AUDIT_SIGNING_KEY``); without it, a keyed trail is reported
as such rather than as tampered.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from baldur.audit.integrity.verifier import (
    TrailReport,
    TrailSet,
    chain_start_at,
    iter_ledger_files,
)
from baldur.utils.time import utc_now

try:
    from baldur.audit.wal import WALConfig, WriteAheadLog
except ImportError:
    WriteAheadLog = None  # type: ignore[assignment,misc]
    WALConfig = None  # type: ignore[assignment,misc]

# How many issues the text report lists per trail before summarizing the rest.
_TEXT_ISSUES_SHOWN = 5

_STATUS_OK = "[OK]"
_STATUS_FAIL = "[FAIL]"
_STATUS_ERROR = "[ERROR]"

_LEDGER_SHAPE_HINT = "audit_<date>[_<partition>].jsonl"


class OutputFormat(str, Enum):
    """Output format."""

    TEXT = "text"
    JSON = "json"
    SUMMARY = "summary"


@dataclass
class VerificationResult:
    """The verdict on one trail, or on one WAL directory in ``--wal`` mode."""

    name: str
    is_valid: bool
    total_entries: int
    issues: list[dict[str, Any]] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    first_sequence: int | None = None
    last_sequence: int | None = None
    files: list[str] = field(default_factory=list)
    error: str | None = None
    verified_at: str = field(default_factory=lambda: utc_now().isoformat())


@dataclass
class VerificationSummary:
    """Every trail's verdict, and why nothing was verified when nothing was."""

    paths: list[str] = field(default_factory=list)
    results: list[VerificationResult] = field(default_factory=list)
    error: str | None = None

    @property
    def total_trails(self) -> int:
        return len(self.results)

    @property
    def valid_trails(self) -> int:
        return sum(1 for r in self.results if r.is_valid and not r.error)

    @property
    def invalid_trails(self) -> int:
        return sum(1 for r in self.results if not r.is_valid and not r.error)

    @property
    def error_trails(self) -> int:
        return sum(1 for r in self.results if r.error)

    @property
    def total_entries(self) -> int:
        return sum(r.total_entries for r in self.results)

    @property
    def total_issues(self) -> int:
        return sum(len(r.issues) for r in self.results)

    @property
    def is_valid(self) -> bool:
        """Every trail intact, and at least one entry verified."""
        return (
            self.error is None
            and self.invalid_trails == 0
            and self.error_trails == 0
            and self.total_entries > 0
        )


def _result_from_report(report: TrailReport) -> VerificationResult:
    return VerificationResult(
        name=report.partition or "default",
        is_valid=report.intact,
        total_entries=report.entries,
        issues=report.issues,
        notes=report.notes,
        first_sequence=report.first_sequence,
        last_sequence=report.last_sequence,
        files=report.files,
    )


class AuditIntegrityVerifier:
    """
    Audit trail integrity verifier.

    Features:
    - Whole-trail verification across every root supplied
    - Partition grouping
    - WAL file verification
    - Multiple output formats
    """

    WAL_FILE_EXTENSION = ".wal"

    def __init__(self, verbose: bool = False):
        """
        Initialize verifier.

        Args:
            verbose: Whether to print verbose output
        """
        self._verbose = verbose

    def verify_paths(
        self,
        paths: list[Path],
        *,
        recursive: bool = False,
        pattern: str | None = None,
        starts_at: int = 1,
    ) -> VerificationSummary:
        """
        Verify every ledger file under ``paths`` as whole trails.

        Args:
            paths: Ledger directories and files. Every root's files of one
                partition merge into one trail.
            recursive: Descend into sub-directories.
            pattern: Select files by this name glob instead of the ledger's
                file-name shape (an adapter built with a custom pattern).
            starts_at: The first sequence each trail must hold.

        Returns:
            VerificationSummary

        Raises:
            OSError: A directory or file cannot be read.
        """
        summary = VerificationSummary(paths=[str(path) for path in paths])
        files = iter_ledger_files(paths, recursive=recursive, pattern=pattern)
        if not files:
            roots = ", ".join(summary.paths)
            summary.error = (
                f"no audit ledger files found under {roots} "
                f"(expected {_LEDGER_SHAPE_HINT})"
            )
            return summary

        trails = TrailSet()
        for path in files:
            for _row in trails.read_file(path):
                pass

        reports = trails.reports(
            start=chain_start_at(starts_at),
            rerun_hint=lambda sequence: f"re-run with --starts-at {sequence}",
        )
        summary.results = [_result_from_report(report) for report in reports]
        if summary.total_entries == 0:
            summary.error = (
                f"no chained entry found in {len(files)} ledger file(s): nothing "
                "was verified"
            )
        return summary

    def verify_wal_directory(self, wal_dir: Path) -> VerificationResult:
        """
        Verify a WAL directory.

        Args:
            wal_dir: WAL directory path

        Returns:
            VerificationResult
        """
        if WriteAheadLog is None:
            return VerificationResult(
                name=str(wal_dir),
                is_valid=False,
                total_entries=0,
                error="WAL module not available",
            )

        if not wal_dir.exists():
            return VerificationResult(
                name=str(wal_dir),
                is_valid=False,
                total_entries=0,
                error=f"Directory not found: {wal_dir}",
            )

        try:
            wal_files = sorted(wal_dir.glob(f"*{self.WAL_FILE_EXTENSION}"))
            if not wal_files:
                return VerificationResult(
                    name=str(wal_dir),
                    is_valid=True,
                    total_entries=0,
                    issues=[{"type": "info", "message": "No WAL files found"}],
                )

            total_entries = 0
            all_issues = []

            for wal_file in wal_files:
                result = self._verify_wal_file(wal_file)
                total_entries += result.total_entries
                if result.issues:
                    all_issues.extend(result.issues)
                if result.error:
                    all_issues.append(
                        {
                            "type": "wal_error",
                            "file": str(wal_file),
                            "message": result.error,
                        }
                    )

            return VerificationResult(
                name=str(wal_dir),
                is_valid=len(all_issues) == 0,
                total_entries=total_entries,
                issues=all_issues,
            )
        except Exception as e:
            return VerificationResult(
                name=str(wal_dir),
                is_valid=False,
                total_entries=0,
                error=str(e),
            )

    def _verify_wal_file(self, wal_file: Path) -> VerificationResult:
        """Verify an individual WAL file."""
        issues: list[dict[str, Any]] = []
        entries = 0

        try:
            with open(wal_file, "rb") as f:
                while True:
                    # Read length prefix (4 bytes, big-endian)
                    length_bytes = f.read(4)
                    if not length_bytes:
                        break
                    if len(length_bytes) < 4:
                        issues.append(
                            {
                                "type": "truncated_record",
                                "file": str(wal_file),
                                "message": "Truncated length prefix",
                            }
                        )
                        break

                    length = struct.unpack(">I", length_bytes)[0]

                    # Read checksum (8 bytes ASCII)
                    checksum_bytes = f.read(8)
                    if len(checksum_bytes) < 8:
                        issues.append(
                            {
                                "type": "truncated_checksum",
                                "file": str(wal_file),
                                "entry": entries + 1,
                            }
                        )
                        break

                    stored_checksum = checksum_bytes.decode("ascii")

                    # Read entry data
                    entry_bytes = f.read(length)
                    if len(entry_bytes) < length:
                        issues.append(
                            {
                                "type": "truncated_entry",
                                "file": str(wal_file),
                                "entry": entries + 1,
                            }
                        )
                        break

                    # Verify checksum
                    computed_crc = zlib.crc32(entry_bytes) & 0xFFFFFFFF
                    computed_checksum = f"{computed_crc:08x}"

                    if stored_checksum != computed_checksum:
                        issues.append(
                            {
                                "type": "checksum_mismatch",
                                "file": str(wal_file),
                                "entry": entries + 1,
                                "stored": stored_checksum,
                                "computed": computed_checksum,
                            }
                        )

                    entries += 1

            return VerificationResult(
                name=str(wal_file),
                is_valid=len(issues) == 0,
                total_entries=entries,
                issues=issues,
            )
        except Exception as e:
            return VerificationResult(
                name=str(wal_file),
                is_valid=False,
                total_entries=entries,
                error=str(e),
            )


def _status(result: VerificationResult) -> str:
    if result.error:
        return _STATUS_ERROR
    return _STATUS_OK if result.is_valid else _STATUS_FAIL


def _issue_line(issue: dict[str, Any]) -> str:
    issue_type = issue.get("type", "unknown")
    message = issue.get("message", str(issue))
    location = ""
    if issue.get("file"):
        line = issue.get("line")
        location = f" ({issue['file']}{f':{line}' if line is not None else ''})"
    return f"[{issue_type}] {message}{location}"


def _finding_lines(
    label: str, findings: list[dict[str, Any]], verbose: bool
) -> list[str]:
    """The lines listing a trail's issues or notes."""
    if not findings:
        return []
    shown = findings if verbose else findings[:_TEXT_ISSUES_SHOWN]
    lines = [f"  {label} ({len(findings)}):"]
    lines.extend(f"    - {_issue_line(finding)}" for finding in shown)
    if len(findings) > len(shown):
        lines.append(f"    ... and {len(findings) - len(shown)} more")
    return lines


def _trail_lines(result: VerificationResult, verbose: bool) -> list[str]:
    """One trail's block of the text report."""
    lines = [f"Trail: {result.name}  {_status(result)}"]
    if result.files:
        lines.append(f"  Files:   {len(result.files)}")
        if verbose:
            lines.extend(f"    - {file}" for file in result.files)
    span = ""
    if result.first_sequence is not None:
        span = f" (sequences {result.first_sequence}-{result.last_sequence})"
    lines.append(f"  Entries: {result.total_entries}{span}")
    if result.error:
        lines.append(f"  Error:   {result.error}")
    lines.extend(_finding_lines("Issues", result.issues, verbose))
    lines.extend(_finding_lines("Notes", result.notes, verbose))
    lines.append("")
    return lines


def _result_line(summary: VerificationSummary) -> str:
    if summary.error:
        return f"Result: {_STATUS_FAIL} {summary.error}"
    if summary.is_valid:
        return (
            f"Result: {_STATUS_OK} {summary.valid_trails} trail(s) intact, "
            f"{summary.total_entries} entries"
        )
    return (
        f"Result: {_STATUS_FAIL} {summary.invalid_trails} trail(s) with issues, "
        f"{summary.error_trails} error(s), {summary.total_issues} issue(s)"
    )


def format_text_output(summary: VerificationSummary, verbose: bool = False) -> str:
    """Text format output (ASCII only, so any console encoding can print it)."""
    lines = [
        "=" * 60,
        "Audit Log Integrity Verification Report",
        "=" * 60,
        f"Verification Time: {utc_now().isoformat()}",
    ]
    if summary.paths:
        lines.append(f"Paths: {', '.join(summary.paths)}")
    lines.append("")
    for result in summary.results:
        lines.extend(_trail_lines(result, verbose))
    lines.extend(["-" * 60, _result_line(summary), "=" * 60])
    return "\n".join(lines)


def format_json_output(summary: VerificationSummary) -> str:
    """JSON format output."""
    output = {
        "verified_at": utc_now().isoformat(),
        "paths": summary.paths,
        "summary": {
            "trails": summary.total_trails,
            "valid_trails": summary.valid_trails,
            "invalid_trails": summary.invalid_trails,
            "error_trails": summary.error_trails,
            "total_entries": summary.total_entries,
            "total_issues": summary.total_issues,
            "is_valid": summary.is_valid,
            "error": summary.error,
        },
        "trails": [
            {
                "partition": r.name,
                "files": r.files,
                "entries": r.total_entries,
                "first_sequence": r.first_sequence,
                "last_sequence": r.last_sequence,
                "issues": r.issues,
                "notes": r.notes,
                "is_valid": r.is_valid,
                "error": r.error,
                "verified_at": r.verified_at,
            }
            for r in summary.results
        ],
    }
    return json.dumps(output, indent=2)


def format_summary_output(summary: VerificationSummary) -> str:
    """Short summary output: one line per trail, then the total."""
    lines = [
        f"{'PASS' if r.is_valid and not r.error else 'FAIL'} {r.name}: "
        f"{r.total_entries} entries, {len(r.issues)} issues"
        for r in summary.results
    ]
    status = "PASS" if summary.is_valid else "FAIL"
    total = (
        f"{status}: {summary.valid_trails}/{summary.total_trails} trails valid, "
        f"{summary.total_entries} entries, {summary.total_issues} issues"
    )
    if summary.error:
        total += f" ({summary.error})"
    lines.append(total)
    return "\n".join(lines)


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or higher")
    return number


def _create_argument_parser() -> argparse.ArgumentParser:
    """Build the ArgumentParser."""
    parser = argparse.ArgumentParser(
        description="Audit Log Integrity Verifier - Hash Chain Verification Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Every ledger file of one chain is verified together, in sequence order, across
every path given. Files named {_LEDGER_SHAPE_HINT} are selected and
grouped by partition; state, lock and temp files are never read.

Examples:
  # Verify a ledger directory
  python -m baldur.audit.verify_audit_integrity /var/log/audit/

  # Verify a distributed chain: pass every host's directory
  python -m baldur.audit.verify_audit_integrity /mnt/host-a/audit/ /mnt/host-b/audit/

  # A ledger whose oldest files were pruned on purpose
  python -m baldur.audit.verify_audit_integrity /var/log/audit/ --starts-at 18001

  # Verify WAL files
  python -m baldur.audit.verify_audit_integrity /var/log/audit/wal/ --wal

  # Output as JSON for automation
  python -m baldur.audit.verify_audit_integrity /var/log/audit/ -f json

Set BALDUR_SECRETS_AUDIT_SIGNING_KEY to the key the trail was written with.

Exit Codes:
  0 - Every trail is intact and at least one entry was verified
  1 - An issue was found, or nothing was verified (no ledger file, no entry)
  2 - A path does not exist, or the check could not run
        """,
    )

    parser.add_argument(
        "paths",
        type=Path,
        nargs="+",
        metavar="PATH",
        help="Ledger directories or files to verify together",
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="Also select ledger files in sub-directories",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=["text", "json", "summary"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show every file, issue and note",
    )
    parser.add_argument(
        "--wal",
        action="store_true",
        help="Verify WAL (Write-Ahead Log) files",
    )
    parser.add_argument(
        "-p",
        "--pattern",
        type=str,
        help=(
            "Select files by this name glob instead of the ledger file-name "
            "shape (e.g. 'ledger_*.ndjson' for a custom filename pattern)"
        ),
    )
    parser.add_argument(
        "--starts-at",
        type=_positive_int,
        default=1,
        metavar="K",
        help=(
            "The first sequence each trail must hold, for a ledger whose oldest "
            "files were pruned on purpose (default: 1)"
        ),
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress output, only set exit code",
    )

    return parser


def _verify_path(
    verifier: AuditIntegrityVerifier,
    paths: list[Path],
    wal_mode: bool,
    recursive: bool,
    pattern: str | None,
    starts_at: int = 1,
) -> VerificationSummary | None:
    """
    Run the verification the paths call for.

    Returns:
        VerificationSummary, or None when a path does not exist
    """
    if any(not path.exists() for path in paths):
        return None

    if wal_mode:
        return VerificationSummary(
            paths=[str(path) for path in paths],
            results=[verifier.verify_wal_directory(path) for path in paths],
        )

    return verifier.verify_paths(
        paths, recursive=recursive, pattern=pattern, starts_at=starts_at
    )


def _format_output(
    summary: VerificationSummary, format_type: str, verbose: bool
) -> str:
    """Format according to the output format."""
    if format_type == "json":
        return format_json_output(summary)
    if format_type == "summary":
        return format_summary_output(summary)
    return format_text_output(summary, verbose=verbose)


def _get_exit_code(summary: VerificationSummary, *, wal_mode: bool = False) -> int:
    """Return the exit code for the verification result."""
    if wal_mode:
        failed = any(not r.is_valid or r.error for r in summary.results)
        return 1 if failed else 0
    return 0 if summary.is_valid else 1


def _protect_console_encoding() -> None:
    """Keep a path or message the console cannot encode from ending the run."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (ValueError, OSError):
                pass


def main() -> None:
    """CLI entry point."""
    _protect_console_encoding()
    parser = _create_argument_parser()
    args = parser.parse_args()

    verifier = AuditIntegrityVerifier(verbose=args.verbose)

    try:
        summary = _verify_path(
            verifier=verifier,
            paths=args.paths,
            wal_mode=args.wal,
            recursive=args.recursive,
            pattern=args.pattern,
            starts_at=args.starts_at,
        )

        if summary is None:
            missing = [str(path) for path in args.paths if not path.exists()]
            print(f"Error: Path not found: {', '.join(missing)}", file=sys.stderr)
            sys.exit(2)

        if not args.quiet:
            print(_format_output(summary, args.format, args.verbose))

        sys.exit(_get_exit_code(summary, wal_mode=args.wal))

    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
