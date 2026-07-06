#!/usr/bin/env python3
"""
Hash Chain Verifier CLI Tool.

감사 로그 무결성 검증을 위한 CLI 도구.

Usage:
    # 단일 파일 검증
    python -m baldur.audit.verify_audit_integrity audit.jsonl

    # 디렉토리 내 모든 감사 로그 검증
    python -m baldur.audit.verify_audit_integrity /var/log/audit/ --recursive

    # JSON 출력
    python -m baldur.audit.verify_audit_integrity audit.jsonl --format json

    # 상세 모드
    python -m baldur.audit.verify_audit_integrity audit.jsonl --verbose

    # WAL 파일 검증
    python -m baldur.audit.verify_audit_integrity /var/log/audit/wal/ --wal

최소 의존성: 표준 라이브러리만 사용
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from baldur.utils.time import utc_now

# 상대 임포트 (패키지 내에서 실행 시)
try:
    from baldur.audit.integrity import (
        HashChainVerifier,
        verify_audit_log_integrity,  # noqa: F401
    )
    from baldur.audit.wal import WALConfig, WriteAheadLog
except ImportError:
    # 직접 실행 시
    from integrity import HashChainVerifier

    try:
        from wal import WALConfig, WriteAheadLog
    except ImportError:
        WriteAheadLog = None  # type: ignore[assignment,misc]
        WALConfig = None  # type: ignore[assignment,misc]


class OutputFormat(str, Enum):
    """출력 형식."""

    TEXT = "text"
    JSON = "json"
    SUMMARY = "summary"


@dataclass
class VerificationResult:
    """검증 결과."""

    file_path: str
    is_valid: bool
    total_entries: int
    issues: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    verified_at: str = field(default_factory=lambda: utc_now().isoformat())


@dataclass
class VerificationSummary:
    """검증 요약."""

    total_files: int = 0
    valid_files: int = 0
    invalid_files: int = 0
    error_files: int = 0
    total_entries: int = 0
    total_issues: int = 0
    results: list[VerificationResult] = field(default_factory=list)


class AuditIntegrityVerifier:
    """
    감사 로그 무결성 검증기.

    기능:
    - 단일/다중 파일 검증
    - 디렉토리 재귀 검증
    - WAL 파일 검증
    - 다양한 출력 형식
    """

    AUDIT_FILE_EXTENSIONS = {".jsonl", ".json", ".log", ".audit"}
    WAL_FILE_EXTENSION = ".wal"

    def __init__(self, verbose: bool = False):
        """
        Initialize verifier.

        Args:
            verbose: 상세 출력 여부
        """
        self._verbose = verbose
        self._verifier = HashChainVerifier()

    def verify_file(self, file_path: Path) -> VerificationResult:
        """
        단일 파일 검증.

        Args:
            file_path: 검증할 파일 경로

        Returns:
            VerificationResult
        """
        if not file_path.exists():
            return VerificationResult(
                file_path=str(file_path),
                is_valid=False,
                total_entries=0,
                error=f"File not found: {file_path}",
            )

        try:
            entries = self._load_entries(file_path)
            issues = self._verifier.find_tampering(entries)

            return VerificationResult(
                file_path=str(file_path),
                is_valid=len(issues) == 0,
                total_entries=len(entries),
                issues=issues,
            )
        except Exception as e:
            return VerificationResult(
                file_path=str(file_path),
                is_valid=False,
                total_entries=0,
                error=str(e),
            )

    def verify_wal_directory(self, wal_dir: Path) -> VerificationResult:
        """
        WAL 디렉토리 검증.

        Args:
            wal_dir: WAL 디렉토리 경로

        Returns:
            VerificationResult
        """
        if WriteAheadLog is None:
            return VerificationResult(
                file_path=str(wal_dir),
                is_valid=False,
                total_entries=0,
                error="WAL module not available",
            )

        if not wal_dir.exists():
            return VerificationResult(
                file_path=str(wal_dir),
                is_valid=False,
                total_entries=0,
                error=f"Directory not found: {wal_dir}",
            )

        try:
            wal_files = sorted(wal_dir.glob(f"*{self.WAL_FILE_EXTENSION}"))
            if not wal_files:
                return VerificationResult(
                    file_path=str(wal_dir),
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
                file_path=str(wal_dir),
                is_valid=len(all_issues) == 0,
                total_entries=total_entries,
                issues=all_issues,
            )
        except Exception as e:
            return VerificationResult(
                file_path=str(wal_dir),
                is_valid=False,
                total_entries=0,
                error=str(e),
            )

    def _verify_wal_file(self, wal_file: Path) -> VerificationResult:
        """WAL 파일 개별 검증."""
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

                    import struct
                    import zlib

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
                file_path=str(wal_file),
                is_valid=len(issues) == 0,
                total_entries=entries,
                issues=issues,
            )
        except Exception as e:
            return VerificationResult(
                file_path=str(wal_file),
                is_valid=False,
                total_entries=entries,
                error=str(e),
            )

    def verify_directory(
        self,
        directory: Path,
        recursive: bool = False,
        pattern: str | None = None,
    ) -> VerificationSummary:
        """
        디렉토리 내 파일들 검증.

        Args:
            directory: 디렉토리 경로
            recursive: 재귀 검색 여부
            pattern: 파일 패턴 (예: "*.jsonl")

        Returns:
            VerificationSummary
        """
        summary = VerificationSummary()

        if not directory.exists():
            return summary

        # 파일 검색
        if pattern:
            if recursive:
                files = list(directory.rglob(pattern))
            else:
                files = list(directory.glob(pattern))
        else:
            if recursive:
                files = [
                    f
                    for f in directory.rglob("*")
                    if f.suffix in self.AUDIT_FILE_EXTENSIONS
                ]
            else:
                files = [
                    f
                    for f in directory.glob("*")
                    if f.suffix in self.AUDIT_FILE_EXTENSIONS
                ]

        for file_path in sorted(files):
            result = self.verify_file(file_path)
            summary.results.append(result)
            summary.total_files += 1
            summary.total_entries += result.total_entries
            summary.total_issues += len(result.issues)

            if result.error:
                summary.error_files += 1
            elif result.is_valid:
                summary.valid_files += 1
            else:
                summary.invalid_files += 1

        return summary

    def _load_entries(self, file_path: Path) -> list[dict[str, Any]]:
        """파일에서 엔트리 로드."""
        entries = []

        with open(file_path, encoding="utf-8") as f:
            content = f.read().strip()

            # JSON Lines 형식
            if file_path.suffix == ".jsonl" or "\n" in content:
                for line in content.split("\n"):
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
            else:
                # 단일 JSON (배열)
                data = json.loads(content)
                entries = data if isinstance(data, list) else [data]

        return entries


def format_text_output(summary: VerificationSummary, verbose: bool = False) -> str:
    """텍스트 형식 출력."""
    lines = []
    lines.append("=" * 60)
    lines.append("Audit Log Integrity Verification Report")
    lines.append("=" * 60)
    lines.append(f"Verification Time: {utc_now().isoformat()}")
    lines.append("")

    # 요약
    lines.append("Summary:")
    lines.append(f"  Total Files:   {summary.total_files}")
    lines.append(f"  Valid:         {summary.valid_files}")
    lines.append(f"  Invalid:       {summary.invalid_files}")
    lines.append(f"  Errors:        {summary.error_files}")
    lines.append(f"  Total Entries: {summary.total_entries}")
    lines.append(f"  Total Issues:  {summary.total_issues}")
    lines.append("")

    # 상세 결과
    if verbose or summary.invalid_files > 0 or summary.error_files > 0:
        lines.append("Details:")
        lines.append("-" * 60)

        for result in summary.results:
            status = "✓" if result.is_valid else "✗"
            if result.error:
                status = "!"

            lines.append(f"  [{status}] {result.file_path}")
            lines.append(f"      Entries: {result.total_entries}")

            if result.error:
                lines.append(f"      Error: {result.error}")

            if result.issues:
                lines.append(f"      Issues ({len(result.issues)}):")
                for issue in result.issues[:5]:  # 최대 5개만 표시
                    issue_type = issue.get("type", "unknown")
                    message = issue.get("message", str(issue))
                    lines.append(f"        - [{issue_type}] {message}")
                if len(result.issues) > 5:
                    lines.append(f"        ... and {len(result.issues) - 5} more")

            lines.append("")

    # 최종 결과
    lines.append("-" * 60)
    if summary.invalid_files == 0 and summary.error_files == 0:
        lines.append("Result: ✓ All audit logs are VALID")
    else:
        lines.append(
            f"Result: ✗ Found {summary.invalid_files} invalid, {summary.error_files} errors"
        )
    lines.append("=" * 60)

    return "\n".join(lines)


def format_json_output(summary: VerificationSummary) -> str:
    """JSON 형식 출력."""
    output = {
        "verified_at": utc_now().isoformat(),
        "summary": {
            "total_files": summary.total_files,
            "valid_files": summary.valid_files,
            "invalid_files": summary.invalid_files,
            "error_files": summary.error_files,
            "total_entries": summary.total_entries,
            "total_issues": summary.total_issues,
            "is_valid": summary.invalid_files == 0 and summary.error_files == 0,
        },
        "results": [
            {
                "file_path": r.file_path,
                "is_valid": r.is_valid,
                "total_entries": r.total_entries,
                "issues": r.issues,
                "error": r.error,
                "verified_at": r.verified_at,
            }
            for r in summary.results
        ],
    }
    return json.dumps(output, indent=2)


def format_summary_output(summary: VerificationSummary) -> str:
    """간단한 요약 출력."""
    status = (
        "PASS" if summary.invalid_files == 0 and summary.error_files == 0 else "FAIL"
    )
    return (
        f"{status}: {summary.valid_files}/{summary.total_files} valid, "
        f"{summary.total_entries} entries, {summary.total_issues} issues"
    )


def _create_argument_parser() -> argparse.ArgumentParser:
    """ArgumentParser 생성."""
    parser = argparse.ArgumentParser(
        description="Audit Log Integrity Verifier - Hash Chain Verification Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Verify a single audit log file
  python -m baldur.audit.verify_audit_integrity audit.jsonl

  # Verify all audit logs in a directory
  python -m baldur.audit.verify_audit_integrity /var/log/audit/ -r

  # Verify WAL files
  python -m baldur.audit.verify_audit_integrity /var/log/audit/wal/ --wal

  # Output as JSON for automation
  python -m baldur.audit.verify_audit_integrity audit.jsonl -f json

Exit Codes:
  0 - All files are valid
  1 - One or more files are invalid or have errors
  2 - Invalid arguments or other errors
        """,
    )

    parser.add_argument(
        "path",
        type=Path,
        help="File or directory to verify",
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="Recursively verify all files in directory",
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
        help="Show detailed output for all files",
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
        help="File pattern to match (e.g., '*.jsonl')",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress output, only set exit code",
    )

    return parser


def _create_summary_from_result(result: VerificationResult) -> VerificationSummary:
    """단일 VerificationResult로부터 VerificationSummary 생성."""
    return VerificationSummary(
        total_files=1,
        valid_files=1 if result.is_valid else 0,
        invalid_files=0 if result.is_valid else 1,
        error_files=1 if result.error else 0,
        total_entries=result.total_entries,
        total_issues=len(result.issues),
        results=[result],
    )


def _verify_path(
    verifier: AuditIntegrityVerifier,
    path: Path,
    wal_mode: bool,
    recursive: bool,
    pattern: str | None,
) -> VerificationSummary | None:
    """
    경로 타입에 따라 적절한 검증 수행.

    Returns:
        VerificationSummary 또는 None (경로가 유효하지 않은 경우)
    """
    if wal_mode:
        result = verifier.verify_wal_directory(path)
        return _create_summary_from_result(result)

    if path.is_file():
        result = verifier.verify_file(path)
        return _create_summary_from_result(result)

    if path.is_dir():
        return verifier.verify_directory(path, recursive=recursive, pattern=pattern)

    return None


def _format_output(
    summary: VerificationSummary, format_type: str, verbose: bool
) -> str:
    """출력 형식에 따른 포맷팅."""
    if format_type == "json":
        return format_json_output(summary)
    if format_type == "summary":
        return format_summary_output(summary)
    return format_text_output(summary, verbose=verbose)


def _get_exit_code(summary: VerificationSummary) -> int:
    """검증 결과에 따른 종료 코드 반환."""
    if summary.invalid_files == 0 and summary.error_files == 0:
        return 0
    return 1


def main() -> None:
    """CLI 메인 함수."""
    parser = _create_argument_parser()
    args = parser.parse_args()

    verifier = AuditIntegrityVerifier(verbose=args.verbose)

    try:
        summary = _verify_path(
            verifier=verifier,
            path=args.path,
            wal_mode=args.wal,
            recursive=args.recursive,
            pattern=args.pattern,
        )

        if summary is None:
            print(f"Error: Path not found: {args.path}", file=sys.stderr)
            sys.exit(2)

        if not args.quiet:
            print(_format_output(summary, args.format, args.verbose))

        sys.exit(_get_exit_code(summary))

    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
