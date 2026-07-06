"""
Export CLI Tool Tests.

AuditExporter 테스트.
"""

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from baldur.audit.export import (
    AuditExporter,
    ExportFormat,
    ExportOptions,
    ExportTarget,
    main,
    parse_datetime,
)

# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def temp_dir():
    """임시 디렉토리."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_log_file(temp_dir):
    """샘플 로그 파일 생성."""
    log_file = temp_dir / "audit.jsonl"

    entries = [
        {
            "audit_id": "audit-001",
            "timestamp": "2025-01-15T10:00:00Z",
            "action": "config_change",
            "actor_id": "user1",
            "checksum": "abc123",
        },
        {
            "audit_id": "audit-002",
            "timestamp": "2025-01-15T11:00:00Z",
            "action": "governance_blocked",
            "actor_id": "user2",
            "prev_hash": "abc123",
            "checksum": "def456",
        },
        {
            "audit_id": "audit-003",
            "timestamp": "2025-01-16T09:00:00Z",
            "action": "cb_force_open",
            "actor_id": "user1",
            "prev_hash": "def456",
            "checksum": "ghi789",
        },
    ]

    with open(log_file, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")

    return log_file


# ─────────────────────────────────────────────────────────────
# ExportOptions Tests
# ─────────────────────────────────────────────────────────────


class TestExportOptions:
    """ExportOptions 테스트."""

    def test_default_values(self):
        """기본값 확인."""
        options = ExportOptions(input_paths=["test.jsonl"])

        assert options.format == ExportFormat.JSONL
        assert options.target == ExportTarget.STDOUT
        assert options.verify_integrity is True

    def test_all_filters(self):
        """모든 필터 설정."""
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = datetime(2025, 1, 31, tzinfo=UTC)

        options = ExportOptions(
            input_paths=["*.jsonl"],
            start_time=start,
            end_time=end,
            actions=["config_change", "governance_blocked"],
            actor_ids=["user1"],
        )

        assert options.start_time == start
        assert options.actions == ["config_change", "governance_blocked"]


# ─────────────────────────────────────────────────────────────
# AuditExporter Tests
# ─────────────────────────────────────────────────────────────


class TestAuditExporter:
    """AuditExporter 테스트."""

    def test_collect_input_files(self, sample_log_file):
        """입력 파일 수집."""
        options = ExportOptions(
            input_paths=[str(sample_log_file.parent / "*.jsonl")],
        )

        exporter = AuditExporter(options)
        files = exporter._collect_input_files()

        assert len(files) == 1
        assert files[0] == sample_log_file

    def test_read_all_entries(self, sample_log_file):
        """모든 엔트리 읽기."""
        options = ExportOptions(
            input_paths=[str(sample_log_file)],
        )

        exporter = AuditExporter(options)
        entries = list(exporter._read_and_filter_entries([sample_log_file]))

        assert len(entries) == 3
        assert exporter._stats.total_entries == 3
        assert exporter._stats.filtered_entries == 3

    def test_filter_by_time(self, sample_log_file):
        """시간 필터링."""
        start = datetime(2025, 1, 15, 0, 0, 0, tzinfo=UTC)
        end = datetime(2025, 1, 15, 23, 59, 59, tzinfo=UTC)

        options = ExportOptions(
            input_paths=[str(sample_log_file)],
            start_time=start,
            end_time=end,
        )

        exporter = AuditExporter(options)
        entries = list(exporter._read_and_filter_entries([sample_log_file]))

        # 1월 15일 엔트리만 (2개)
        assert len(entries) == 2

    def test_filter_by_action(self, sample_log_file):
        """액션 필터링."""
        options = ExportOptions(
            input_paths=[str(sample_log_file)],
            actions=["config_change"],
        )

        exporter = AuditExporter(options)
        entries = list(exporter._read_and_filter_entries([sample_log_file]))

        assert len(entries) == 1
        assert entries[0]["action"] == "config_change"

    def test_filter_by_actor(self, sample_log_file):
        """Actor 필터링."""
        options = ExportOptions(
            input_paths=[str(sample_log_file)],
            actor_ids=["user1"],
        )

        exporter = AuditExporter(options)
        entries = list(exporter._read_and_filter_entries([sample_log_file]))

        assert len(entries) == 2
        assert all(e["actor_id"] == "user1" for e in entries)

    def test_verify_integrity(self, sample_log_file):
        """무결성 검증."""
        options = ExportOptions(
            input_paths=[str(sample_log_file)],
            verify_integrity=True,
        )

        exporter = AuditExporter(options)
        entries = list(exporter._read_and_filter_entries([sample_log_file]))
        verified = list(exporter._verify_integrity(iter(entries)))

        # 체인이 연결되어 있으므로 에러 없음
        assert exporter._stats.integrity_errors == 0
        assert len(verified) == 3

    def test_export_to_file_jsonl(self, sample_log_file, temp_dir):
        """JSONL 파일로 내보내기."""
        output_file = temp_dir / "output.jsonl"

        options = ExportOptions(
            input_paths=[str(sample_log_file)],
            format=ExportFormat.JSONL,
            target=ExportTarget.FILE,
            output_path=str(output_file),
        )

        exporter = AuditExporter(options)
        stats = exporter.export()

        assert output_file.exists()
        assert stats.exported_entries == 3

        # 파일 내용 확인
        with open(output_file) as f:
            lines = f.readlines()
            assert len(lines) == 3

    def test_export_to_file_json(self, sample_log_file, temp_dir):
        """JSON Array로 내보내기."""
        output_file = temp_dir / "output.json"

        options = ExportOptions(
            input_paths=[str(sample_log_file)],
            format=ExportFormat.JSON,
            target=ExportTarget.FILE,
            output_path=str(output_file),
        )

        exporter = AuditExporter(options)
        stats = exporter.export()

        assert output_file.exists()
        assert stats.exported_entries == 3

        # JSON 배열 확인
        with open(output_file) as f:
            data = json.load(f)
            assert isinstance(data, list)
            assert len(data) == 3

    def test_export_to_file_csv(self, sample_log_file, temp_dir):
        """CSV로 내보내기."""
        output_file = temp_dir / "output.csv"

        options = ExportOptions(
            input_paths=[str(sample_log_file)],
            format=ExportFormat.CSV,
            target=ExportTarget.FILE,
            output_path=str(output_file),
        )

        exporter = AuditExporter(options)
        stats = exporter.export()

        assert output_file.exists()
        assert stats.exported_entries == 3

        # CSV 내용 확인
        with open(output_file) as f:
            content = f.read().strip()
            lines = [line for line in content.split("\n") if line.strip()]
            assert len(lines) == 4  # 헤더 + 3 엔트리
            assert "timestamp" in lines[0]  # 헤더

    def test_export_to_http(self, sample_log_file):
        """HTTP로 내보내기."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_response

            options = ExportOptions(
                input_paths=[str(sample_log_file)],
                target=ExportTarget.HTTP,
                http_endpoint="https://logs.example.com/ingest",
            )

            exporter = AuditExporter(options)
            stats = exporter.export()

            mock_urlopen.assert_called_once()
            assert stats.exported_entries == 3

    def test_export_empty_input(self, temp_dir):
        """빈 입력 처리."""
        options = ExportOptions(
            input_paths=[str(temp_dir / "nonexistent.jsonl")],
        )

        exporter = AuditExporter(options)
        stats = exporter.export()

        assert stats.total_files == 0
        assert stats.total_entries == 0


# ─────────────────────────────────────────────────────────────
# parse_datetime Tests
# ─────────────────────────────────────────────────────────────


class TestParseDatetime:
    """parse_datetime 테스트."""

    def test_date_only(self):
        """날짜만."""
        dt = parse_datetime("2025-01-15")

        assert dt.year == 2025
        assert dt.month == 1
        assert dt.day == 15
        assert dt.tzinfo == UTC

    def test_datetime_t_format(self):
        """ISO T 형식."""
        dt = parse_datetime("2025-01-15T10:30:00")

        assert dt.hour == 10
        assert dt.minute == 30

    def test_datetime_z_format(self):
        """ISO Z 형식."""
        dt = parse_datetime("2025-01-15T10:30:00Z")

        assert dt.hour == 10
        assert dt.minute == 30

    def test_datetime_space_format(self):
        """공백 형식."""
        dt = parse_datetime("2025-01-15 10:30:00")

        assert dt.hour == 10
        assert dt.minute == 30

    def test_invalid_format(self):
        """잘못된 형식."""
        with pytest.raises(ValueError, match="Invalid datetime format"):
            parse_datetime("not-a-date")


# ─────────────────────────────────────────────────────────────
# CLI Tests
# ─────────────────────────────────────────────────────────────


class TestCLI:
    """CLI 테스트."""

    def test_main_stdout(self, sample_log_file, capsys):
        """stdout 출력."""
        result = main(
            [
                "--input",
                str(sample_log_file),
            ]
        )

        assert result == 0

        captured = capsys.readouterr()
        assert "audit-001" in captured.out

    def test_main_with_filters(self, sample_log_file, capsys):
        """필터 적용."""
        result = main(
            [
                "--input",
                str(sample_log_file),
                "--actions",
                "config_change",
            ]
        )

        assert result == 0

        captured = capsys.readouterr()
        assert "audit-001" in captured.out
        assert "audit-002" not in captured.out

    def test_main_to_file(self, sample_log_file, temp_dir):
        """파일 출력."""
        output_file = temp_dir / "output.jsonl"

        result = main(
            [
                "--input",
                str(sample_log_file),
                "--target",
                "file",
                "--output",
                str(output_file),
            ]
        )

        assert result == 0
        assert output_file.exists()

    def test_main_missing_output(self, sample_log_file, capsys):
        """파일 타겟인데 출력 경로 없음."""
        result = main(
            [
                "--input",
                str(sample_log_file),
                "--target",
                "file",
            ]
        )

        assert result == 1  # 에러

    def test_main_verbose(self, sample_log_file, capsys):
        """Verbose 모드."""
        result = main(
            [
                "--input",
                str(sample_log_file),
                "-v",
            ]
        )

        assert result == 0

        captured = capsys.readouterr()
        assert "Export Statistics" in captured.err


# ─────────────────────────────────────────────────────────────
# Integrity Verification Tests
# ─────────────────────────────────────────────────────────────


class TestIntegrityVerification:
    """무결성 검증 테스트."""

    def test_broken_chain_detected(self, temp_dir):
        """끊어진 체인 감지."""
        log_file = temp_dir / "broken.jsonl"

        entries = [
            {
                "audit_id": "audit-001",
                "timestamp": "2025-01-15T10:00:00Z",
                "checksum": "abc123",
            },
            {
                "audit_id": "audit-002",
                "timestamp": "2025-01-15T11:00:00Z",
                "prev_hash": "WRONG_HASH",  # 잘못된 prev_hash
                "checksum": "def456",
            },
        ]

        with open(log_file, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

        options = ExportOptions(
            input_paths=[str(log_file)],
            verify_integrity=True,
        )

        exporter = AuditExporter(options)
        entries_iter = exporter._read_and_filter_entries([log_file])
        list(exporter._verify_integrity(entries_iter))

        # 체인 끊김 감지
        assert exporter._stats.integrity_errors == 1

    def test_skip_integrity_check(self, temp_dir):
        """무결성 검증 건너뛰기."""
        log_file = temp_dir / "broken.jsonl"

        entries = [
            {
                "audit_id": "audit-001",
                "checksum": "abc123",
            },
            {
                "audit_id": "audit-002",
                "prev_hash": "WRONG",
                "checksum": "def456",
            },
        ]

        with open(log_file, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

        options = ExportOptions(
            input_paths=[str(log_file)],
            verify_integrity=False,  # 검증 안 함
        )

        exporter = AuditExporter(options)
        stats = exporter.export()

        # 에러 카운트 없음
        assert stats.integrity_errors == 0
