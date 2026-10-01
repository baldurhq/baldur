"""
Export CLI Tool Tests.

AuditExporter tests.
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
    """Temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_log_file(temp_dir):
    """Create a sample log file."""
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
    """ExportOptions tests."""

    def test_default_values(self):
        """Default values."""
        options = ExportOptions(input_paths=["test.jsonl"])

        assert options.format == ExportFormat.JSONL
        assert options.target == ExportTarget.STDOUT
        assert options.verify_integrity is True

    def test_all_filters(self):
        """Every filter set."""
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


class TestAuditExporterBehavior:
    """AuditExporter tests."""

    def test_collect_input_files(self, sample_log_file):
        """Collect input files."""
        options = ExportOptions(
            input_paths=[str(sample_log_file.parent / "*.jsonl")],
        )

        exporter = AuditExporter(options)
        files = exporter._collect_input_files()

        assert len(files) == 1
        assert files[0] == sample_log_file

    def test_read_all_entries(self, sample_log_file):
        """Read every entry."""
        options = ExportOptions(
            input_paths=[str(sample_log_file)],
        )

        exporter = AuditExporter(options)
        entries = list(exporter._read_and_filter_entries([sample_log_file]))

        assert len(entries) == 3
        assert exporter._stats.total_entries == 3
        assert exporter._stats.filtered_entries == 3

    def test_filter_by_time(self, sample_log_file):
        """Time filtering."""
        start = datetime(2025, 1, 15, 0, 0, 0, tzinfo=UTC)
        end = datetime(2025, 1, 15, 23, 59, 59, tzinfo=UTC)

        options = ExportOptions(
            input_paths=[str(sample_log_file)],
            start_time=start,
            end_time=end,
        )

        exporter = AuditExporter(options)
        entries = list(exporter._read_and_filter_entries([sample_log_file]))

        # Only the January 15 entries (2)
        assert len(entries) == 2

    def test_filter_by_action(self, sample_log_file):
        """Action filtering."""
        options = ExportOptions(
            input_paths=[str(sample_log_file)],
            actions=["config_change"],
        )

        exporter = AuditExporter(options)
        entries = list(exporter._read_and_filter_entries([sample_log_file]))

        assert len(entries) == 1
        assert entries[0]["action"] == "config_change"

    def test_filter_by_actor(self, sample_log_file):
        """Actor filtering."""
        options = ExportOptions(
            input_paths=[str(sample_log_file)],
            actor_ids=["user1"],
        )

        exporter = AuditExporter(options)
        entries = list(exporter._read_and_filter_entries([sample_log_file]))

        assert len(entries) == 2
        assert all(e["actor_id"] == "user1" for e in entries)

    def test_verify_integrity(self, sample_log_file):
        """Rows carrying no integrity block are not counted as errors."""
        options = ExportOptions(
            input_paths=[str(sample_log_file)],
            verify_integrity=True,
        )

        exporter = AuditExporter(options)
        trails = exporter._new_trail_set()
        entries = list(exporter._read_and_filter_entries([sample_log_file], trails))
        exporter._finish_integrity(trails)

        assert exporter._stats.integrity_errors == 0
        assert len(entries) == 3

    def test_export_to_file_jsonl(self, sample_log_file, temp_dir):
        """Export to a JSONL file."""
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

        # Check the file content
        with open(output_file) as f:
            lines = f.readlines()
            assert len(lines) == 3

    def test_export_to_file_json(self, sample_log_file, temp_dir):
        """Export as a JSON array."""
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

        # Check the JSON array
        with open(output_file) as f:
            data = json.load(f)
            assert isinstance(data, list)
            assert len(data) == 3

    def test_export_to_file_csv(self, sample_log_file, temp_dir):
        """Export to CSV."""
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

        # Check the CSV content
        with open(output_file) as f:
            content = f.read().strip()
            lines = [line for line in content.split("\n") if line.strip()]
            assert len(lines) == 4  # header + 3 entries
            assert "timestamp" in lines[0]  # header

    def test_export_to_http(self, sample_log_file):
        """Export over HTTP."""
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
        """Empty input."""
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
    """parse_datetime tests."""

    def test_date_only(self):
        """Date only."""
        dt = parse_datetime("2025-01-15")

        assert dt.year == 2025
        assert dt.month == 1
        assert dt.day == 15
        assert dt.tzinfo == UTC

    def test_datetime_t_format(self):
        """ISO T form."""
        dt = parse_datetime("2025-01-15T10:30:00")

        assert dt.hour == 10
        assert dt.minute == 30

    def test_datetime_z_format(self):
        """ISO Z form."""
        dt = parse_datetime("2025-01-15T10:30:00Z")

        assert dt.hour == 10
        assert dt.minute == 30

    def test_datetime_space_format(self):
        """Space-separated form."""
        dt = parse_datetime("2025-01-15 10:30:00")

        assert dt.hour == 10
        assert dt.minute == 30

    def test_invalid_format(self):
        """Invalid form."""
        with pytest.raises(ValueError, match="Invalid datetime format"):
            parse_datetime("not-a-date")


# ─────────────────────────────────────────────────────────────
# CLI Tests
# ─────────────────────────────────────────────────────────────


class TestCLI:
    """CLI tests."""

    def test_main_stdout(self, sample_log_file, capsys):
        """stdout output."""
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
        """Filters applied."""
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
        """File output."""
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
        """File target without an output path."""
        result = main(
            [
                "--input",
                str(sample_log_file),
                "--target",
                "file",
            ]
        )

        assert result == 1  # error

    def test_main_verbose(self, sample_log_file, capsys):
        """Verbose mode."""
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


class TestIntegrityVerificationBehavior:
    """Integrity pass tests."""

    def test_broken_chain_detected(self, temp_dir):
        """An entry whose predecessor is not present is counted."""
        from baldur.audit.integrity import HashChainManager

        log_file = temp_dir / "audit_2025-01-15.jsonl"
        manager = HashChainManager()
        entries = [
            manager.add_integrity({"timestamp": f"2025-01-15T1{i}:00:00Z", "n": i})
            for i in range(3)
        ]
        entries[2]["integrity"]["previous_hash"] = "WRONG_HASH"

        with open(log_file, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

        options = ExportOptions(
            input_paths=[str(log_file)],
            verify_integrity=True,
        )

        exporter = AuditExporter(options)
        trails = exporter._new_trail_set()
        list(exporter._read_and_filter_entries([log_file], trails))
        exporter._finish_integrity(trails)

        # The edited previous hash is under the fingerprint, so the entry is
        # both modified and unlinked.
        assert exporter._stats.integrity_errors == 2

    def test_skip_integrity_check(self, temp_dir):
        """Skip the integrity check."""
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
            verify_integrity=False,  # no check
        )

        exporter = AuditExporter(options)
        stats = exporter.export()

        # no error counted
        assert stats.integrity_errors == 0
