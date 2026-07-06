#!/usr/bin/env python3
"""
Audit Log Export CLI Tool.

CLI utility for shipping audit logs to external systems.

Usage scenarios:
1. Normal operation: store as local JSONL files (non-invasive)
2. Audit time: ship to S3/Parquet/external systems via this tool

Non-invasive principle:
- The main application only writes to local files
- External transmission is performed by this tool on demand
- Auditors run it directly, or it runs on cron/CI

Examples::

    # Default: emit JSONL to stdout
    python -m baldur.audit.export --input /var/log/audit/*.jsonl

    # Export to S3 (requires baldur_dormant per doc 528 D10-v2)
    python -m baldur.audit.export --input /var/log/audit/*.jsonl --target s3 --bucket my-bucket

    # Convert to Parquet (for analytics)
    python -m baldur.audit.export --input /var/log/audit/*.jsonl --format parquet --output audit.parquet

    # Filter by date range
    python -m baldur.audit.export --input /var/log/audit/*.jsonl --start 2025-01-01 --end 2025-01-31
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, TextIO, cast

import structlog

from baldur.utils.serialization import fast_dumps_str, fast_loads
from baldur.utils.time import utc_now

logger = structlog.get_logger()


class ExportFormat(str, Enum):
    """Export format."""

    JSONL = "jsonl"  # JSON Lines (default)
    JSON = "json"  # JSON Array
    CSV = "csv"  # CSV (auditor-friendly)
    PARQUET = "parquet"  # Apache Parquet (analytics)


class ExportTarget(str, Enum):
    """Export target."""

    STDOUT = "stdout"  # Standard output
    FILE = "file"  # Local file
    S3 = "s3"  # AWS S3 (routed through baldur_dormant)
    HTTP = "http"  # HTTP POST


@dataclass
class ExportOptions:
    """Export options."""

    # Input
    input_paths: list[str]

    # Filtering
    start_time: datetime | None = None
    end_time: datetime | None = None
    actions: list[str] | None = None
    actor_ids: list[str] | None = None

    # Output
    format: ExportFormat = ExportFormat.JSONL
    target: ExportTarget = ExportTarget.STDOUT
    output_path: str | None = None

    # S3 options
    s3_bucket: str | None = None
    s3_prefix: str = "audit-export/"
    s3_region: str = "ap-northeast-2"

    # HTTP options
    http_endpoint: str | None = None
    http_headers: dict[str, str] | None = None

    # Integrity verification
    verify_integrity: bool = True

    # JSON format entry limit (OOM prevention)
    max_entries_json_format: int = 50000

    # Misc
    verbose: bool = False


@dataclass
class ExportStats:
    """Export statistics."""

    total_files: int = 0
    total_entries: int = 0
    filtered_entries: int = 0
    exported_entries: int = 0
    integrity_errors: int = 0
    start_time: datetime | None = None
    end_time: datetime | None = None


class AuditExporter:
    """Audit log exporter."""

    def __init__(self, options: ExportOptions):
        self._options = options
        self._stats = ExportStats()

    def export(self) -> ExportStats:
        """Run the export."""
        self._stats.start_time = utc_now()

        # Collect input files
        input_files = self._collect_input_files()
        self._stats.total_files = len(input_files)

        if not input_files:
            logger.warning("audit_export.no_input_files")
            self._stats.end_time = utc_now()
            return self._stats

        # Read and filter entries
        entries = self._read_and_filter_entries(input_files)

        # Integrity verification (optional)
        if self._options.verify_integrity:
            entries = self._verify_integrity(entries)

        # Export
        self._export_entries(entries)

        self._stats.end_time = utc_now()
        return self._stats

    def _collect_input_files(self) -> list[Path]:
        """Collect input files."""
        files = []
        for pattern in self._options.input_paths:
            matched = glob.glob(pattern, recursive=True)
            for path_str in matched:
                path = Path(path_str)
                if path.is_file() and path.suffix in (".jsonl", ".json", ".log"):
                    files.append(path)
        return sorted(files)

    def _read_and_filter_entries(
        self, input_files: list[Path]
    ) -> Iterator[dict[str, Any]]:
        """Read and filter entries."""
        for file_path in input_files:
            try:
                with open(file_path, encoding="utf-8") as f:
                    for line in f:
                        if not line.strip():
                            continue

                        self._stats.total_entries += 1

                        try:
                            entry = fast_loads(line)
                        except ValueError as e:
                            logger.warning(
                                "invalid.json",
                                file_path=file_path,
                                error=e,
                            )
                            continue

                        if self._matches_filters(entry):
                            self._stats.filtered_entries += 1
                            yield entry

            except Exception as e:
                logger.exception(
                    "audit_export.read_failed",
                    file_path=file_path,
                    error=e,
                )

    def _matches_filters(self, entry: dict[str, Any]) -> bool:  # noqa: C901
        """Check filter conditions."""
        # Time filter
        if self._options.start_time or self._options.end_time:
            timestamp_str = entry.get("timestamp")
            if timestamp_str:
                try:
                    # ISO format parsing
                    if timestamp_str.endswith("Z"):
                        timestamp_str = timestamp_str[:-1] + "+00:00"
                    timestamp = datetime.fromisoformat(timestamp_str)

                    if (
                        self._options.start_time
                        and timestamp < self._options.start_time
                    ):
                        return False
                    if self._options.end_time and timestamp > self._options.end_time:
                        return False
                except ValueError:
                    pass

        # Action filter
        if self._options.actions:
            action = entry.get("action")
            if action not in self._options.actions:
                return False

        # Actor filter
        if self._options.actor_ids:
            actor_id = entry.get("actor_id")
            if actor_id not in self._options.actor_ids:
                return False

        return True

    def _verify_integrity(
        self, entries: Iterator[dict[str, Any]]
    ) -> Iterator[dict[str, Any]]:
        """Hash-chain integrity verification."""
        prev_hash = None

        for entry in entries:
            # Checksum verification
            checksum = entry.get("checksum")
            prev_hash_in_entry = entry.get("prev_hash")

            if (
                prev_hash is not None
                and prev_hash_in_entry
                and prev_hash != prev_hash_in_entry
            ):
                self._stats.integrity_errors += 1
                logger.warning(
                    "hash.chain_broken_expected",
                    entry=entry.get("audit_id"),
                    prev_hash=prev_hash,
                    prev_hash_in_entry=prev_hash_in_entry,
                )

            prev_hash = checksum
            yield entry

    def _export_entries(self, entries: Iterator[dict[str, Any]]) -> None:
        """Dispatch entries by target."""
        target = self._options.target

        if target == ExportTarget.STDOUT:
            self._export_to_stdout(entries)
        elif target == ExportTarget.FILE:
            self._export_to_file(entries)
        elif target == ExportTarget.S3:
            self._export_to_s3(entries)
        elif target == ExportTarget.HTTP:
            self._export_to_http(entries)

    def _export_to_stdout(self, entries: Iterator[dict[str, Any]]) -> None:
        """Export to standard output."""
        self._write_entries(entries, sys.stdout)

    def _export_to_file(self, entries: Iterator[dict[str, Any]]) -> None:
        """Export to a local file."""
        output_path = self._options.output_path
        if not output_path:
            raise ValueError("--output is required for file target")

        with open(output_path, "w", encoding="utf-8") as f:
            self._write_entries(entries, f)

    def _write_entries(self, entries: Iterator[dict[str, Any]], output: TextIO) -> None:
        """Write entries in the requested format."""
        import itertools

        from baldur.audit.constants import FIXED_AUDIT_FIELDS

        format_type = self._options.format

        if format_type == ExportFormat.JSONL:
            for entry in entries:
                output.write(fast_dumps_str(entry, default=str) + "\n")
                self._stats.exported_entries += 1

        elif format_type == ExportFormat.JSON:
            limit = self._options.max_entries_json_format
            entries_list = list(itertools.islice(entries, limit + 1))
            if len(entries_list) > limit:
                raise ValueError(
                    f"JSON format supports max {limit} entries. "
                    f"Use JSONL format for larger exports."
                )
            self._stats.exported_entries = len(entries_list)
            json.dump(entries_list, output, default=str, ensure_ascii=False, indent=2)
            output.write("\n")

        elif format_type == ExportFormat.CSV:
            import csv

            writer = csv.DictWriter(
                output,
                fieldnames=FIXED_AUDIT_FIELDS,
                extrasaction="ignore",
            )
            writer.writeheader()
            for entry in entries:
                writer.writerow(
                    {k: str(v) if v is not None else "" for k, v in entry.items()}
                )
                self._stats.exported_entries += 1

        elif format_type == ExportFormat.PARQUET:
            raise NotImplementedError(
                "Parquet format requires pyarrow. "
                "Install with: pip install pyarrow\n"
                "Then use export_to_parquet() method directly."
            )

    def _export_to_s3(self, entries: Iterator[dict[str, Any]]) -> None:
        """Export to S3 via the registered ``audit_s3_exporter`` provider.

        Per doc 528 D10-v2 the boto3-touching upload code lives in
        ``baldur_dormant.audit.s3_exporter.S3Exporter``. OSS callers reach it
        through ``ProviderRegistry.audit_s3_exporter``; when ``baldur_dormant``
        is absent, the slot resolves to ``NoOpS3Exporter`` (defined below)
        which raises ``RuntimeError`` — compliance audit is fail-closed.
        """
        if not self._options.s3_bucket:
            raise ValueError("--s3-bucket is required for S3 target")

        # Write entries to a temporary NDJSON file first; the exporter only
        # needs to upload a finished payload, not stream JSON encoding.
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        ) as f:
            temp_path = f.name
            # _TemporaryFileWrapper[str] structurally satisfies TextIO at runtime
            # but isn't a TextIO subtype in typeshed.
            self._write_entries(entries, cast(TextIO, f))

        try:
            self._upload_to_s3(temp_path)
        finally:
            Path(temp_path).unlink()

    def _upload_to_s3(self, file_path: str) -> None:
        """Delegate to the registered S3 exporter provider."""
        from baldur.factory.registry import ProviderRegistry

        exporter = ProviderRegistry.audit_s3_exporter.get()
        exporter.upload(file_path, self._options)

    def _export_to_http(self, entries: Iterator[dict[str, Any]]) -> None:
        """HTTP NDJSON chunked POST."""
        import itertools
        import urllib.error
        import urllib.request

        from baldur.utils.http import safe_urlopen

        if not self._options.http_endpoint:
            raise ValueError("--http-endpoint is required for HTTP target")

        headers = {
            "Content-Type": "application/x-ndjson",
            **(self._options.http_headers or {}),
        }

        while True:
            chunk = list(itertools.islice(entries, 500))
            if not chunk:
                break
            self._stats.exported_entries += len(chunk)
            data = "\n".join(fast_dumps_str(e, default=str) for e in chunk).encode(
                "utf-8"
            )
            req = urllib.request.Request(
                self._options.http_endpoint,
                data=data,
                headers=headers,
                method="POST",
            )
            try:
                with safe_urlopen(req, timeout=30) as response:
                    logger.debug(
                        "http.export_chunk_sent",
                        response_status=response.status,
                        chunk_size=len(chunk),
                    )
            except urllib.error.URLError as e:
                raise RuntimeError(f"HTTP export failed: {e}") from e

    def export_to_parquet(
        self,
        input_files: list[Path],
        output_path: str,
    ) -> None:
        """
        Export to Parquet.

        Parquet can be efficiently queried by analytics tools (Athena, Spark,
        Pandas) and is well-suited for large audit-log analysis.

        Usage::

            exporter = AuditExporter(options)
            exporter.export_to_parquet(
                input_files=[Path("/var/log/audit/*.jsonl")],
                output_path="audit.parquet"
            )
        """
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as err:
            raise ImportError(
                "Parquet export requires pyarrow. Install with: pip install pyarrow"
            ) from err

        # Collect all entries
        entries = []
        for file_path in self._collect_input_files():
            with open(file_path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        entries.append(fast_loads(line))

        if not entries:
            logger.warning("audit_export.no_entries")
            return

        # Build PyArrow Table
        table = pa.Table.from_pylist(entries)

        # Write Parquet
        pq.write_table(table, output_path, compression="snappy")
        logger.info(
            "exported.entries",
            entries_count=len(entries),
            output_path=output_path,
        )


# =============================================================================
# S3 Exporter NoOp default
# =============================================================================
# Doc 528 D10-v2 splits the S3 export path out of ``AuditExporter`` so the
# boto3-touching code can live in ``baldur_dormant.audit.s3_exporter``.
# ``AuditExporter`` in OSS routes through ``ProviderRegistry.audit_s3_exporter``;
# when ``baldur_dormant`` is absent, the slot resolves to ``NoOpS3Exporter``
# which raises rather than silently dropping.
#
# Fail-loud rationale (D10-v2): silently no-op'ing an S3 audit export would
# mask a configuration error (user asked for S3 backup -> got nothing). S3
# audit is compliance-driven; the cross-service standards' fail-behavior matrix
# classifies compliance as fail-closed. The non-S3 paths (file, HTTP) keep
# working independently because ``AuditExporter`` dispatches by target
# before calling into the S3 slot.


class NoOpS3Exporter:
    """Fail-loud NoOp for ``audit_s3_exporter`` registry slot.

    Registered as the OSS-side default. When the caller explicitly asks
    for the S3 export path but ``baldur_dormant`` is not installed, the
    call raises ``RuntimeError`` with the install hint — compliance
    auditing is fail-closed (see module docstring).
    """

    def upload(self, file_path: str, options: ExportOptions) -> None:
        raise RuntimeError(
            "S3 export requires baldur_dormant. "
            "Install with: pip install 'baldur-pro[aws]'"
        )


def parse_datetime(value: str) -> datetime:
    """Parse a date/time string."""
    formats = [
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ValueError(f"Invalid datetime format: {value}")


def main(args: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        prog="baldur.audit.export",
        description="Export audit logs to external systems",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Export to stdout (JSONL)
  %(prog)s --input /var/log/audit/*.jsonl

  # Export to file (JSON array)
  %(prog)s --input /var/log/audit/*.jsonl --format json --target file --output audit.json

  # Export to CSV
  %(prog)s --input /var/log/audit/*.jsonl --format csv --target file --output audit.csv

  # Export to S3 (requires baldur_dormant)
  %(prog)s --input /var/log/audit/*.jsonl --target s3 --s3-bucket my-audit-bucket

  # Filter by date range
  %(prog)s --input /var/log/audit/*.jsonl --start 2025-01-01 --end 2025-01-31

  # Filter by action
  %(prog)s --input /var/log/audit/*.jsonl --actions governance_blocked,cb_force_open
""",
    )

    # Input options
    parser.add_argument(
        "--input",
        "-i",
        dest="input_paths",
        nargs="+",
        required=True,
        help="Input file paths (glob patterns supported)",
    )

    # Filter options
    parser.add_argument(
        "--start",
        type=parse_datetime,
        help="Start time (ISO format: 2025-01-01 or 2025-01-01T00:00:00)",
    )
    parser.add_argument(
        "--end",
        type=parse_datetime,
        help="End time (ISO format)",
    )
    parser.add_argument(
        "--actions",
        help="Filter by actions (comma-separated)",
    )
    parser.add_argument(
        "--actors",
        help="Filter by actor IDs (comma-separated)",
    )

    # Output options
    parser.add_argument(
        "--format",
        "-f",
        type=ExportFormat,
        choices=list(ExportFormat),
        default=ExportFormat.JSONL,
        help="Output format (default: jsonl)",
    )
    parser.add_argument(
        "--target",
        "-t",
        type=ExportTarget,
        choices=list(ExportTarget),
        default=ExportTarget.STDOUT,
        help="Export target (default: stdout)",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Output file path (for file target)",
    )

    # S3 options
    parser.add_argument("--s3-bucket", help="S3 bucket name")
    parser.add_argument("--s3-prefix", default="audit-export/", help="S3 key prefix")
    parser.add_argument("--s3-region", default="ap-northeast-2", help="AWS region")

    # HTTP options
    parser.add_argument("--http-endpoint", help="HTTP endpoint URL")

    # Misc options
    parser.add_argument(
        "--skip-integrity",
        action="store_true",
        help="Skip hash chain integrity verification",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose output",
    )

    parsed = parser.parse_args(args)

    # Logging setup
    logging.basicConfig(
        level=logging.DEBUG if parsed.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    # Build options
    options = ExportOptions(
        input_paths=parsed.input_paths,
        start_time=parsed.start,
        end_time=parsed.end,
        actions=parsed.actions.split(",") if parsed.actions else None,
        actor_ids=parsed.actors.split(",") if parsed.actors else None,
        format=parsed.format,
        target=parsed.target,
        output_path=parsed.output,
        s3_bucket=parsed.s3_bucket,
        s3_prefix=parsed.s3_prefix,
        s3_region=parsed.s3_region,
        http_endpoint=parsed.http_endpoint,
        verify_integrity=not parsed.skip_integrity,
        verbose=parsed.verbose,
    )

    # Run export
    exporter = AuditExporter(options)

    try:
        stats = exporter.export()

        if parsed.verbose or parsed.target != ExportTarget.STDOUT:
            print("\n=== Export Statistics ===", file=sys.stderr)
            print(f"Files processed: {stats.total_files}", file=sys.stderr)
            print(f"Total entries: {stats.total_entries}", file=sys.stderr)
            print(f"Filtered entries: {stats.filtered_entries}", file=sys.stderr)
            print(f"Exported entries: {stats.exported_entries}", file=sys.stderr)
            if stats.integrity_errors > 0:
                print(f"!! Integrity errors: {stats.integrity_errors}", file=sys.stderr)

        return 0

    except Exception as e:
        logger.exception(
            "export.failed",
            error=e,
        )
        if parsed.verbose:
            import traceback

            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
