"""Unit tests for audit/export.py streaming/format changes (308-C)."""

import csv
import io
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from baldur.audit.export import (
    AuditExporter,
    ExportFormat,
    ExportOptions,
    ExportTarget,
    parse_datetime,
)


class TestExportFormatContract:
    """ExportFormat design contract verification."""

    def test_format_enum_has_four_members(self):
        """ExportFormat has JSONL, JSON, CSV, PARQUET."""
        assert set(ExportFormat) == {
            ExportFormat.JSONL,
            ExportFormat.JSON,
            ExportFormat.CSV,
            ExportFormat.PARQUET,
        }

    def test_jsonl_value(self):
        """JSONL value is 'jsonl'."""
        assert ExportFormat.JSONL.value == "jsonl"

    def test_csv_value(self):
        """CSV value is 'csv'."""
        assert ExportFormat.CSV.value == "csv"


class TestExportTargetContract:
    """ExportTarget design contract verification."""

    def test_target_enum_has_four_members(self):
        """ExportTarget has STDOUT, FILE, S3, HTTP."""
        assert set(ExportTarget) == {
            ExportTarget.STDOUT,
            ExportTarget.FILE,
            ExportTarget.S3,
            ExportTarget.HTTP,
        }

    def test_http_value(self):
        """HTTP value is 'http'."""
        assert ExportTarget.HTTP.value == "http"


class TestFixedAuditFieldsContract:
    """FIXED_AUDIT_FIELDS design contract verification."""

    def test_fixed_audit_fields_count(self):
        """FIXED_AUDIT_FIELDS has 9 fields."""
        from baldur.audit.constants import FIXED_AUDIT_FIELDS

        assert len(FIXED_AUDIT_FIELDS) == 9

    def test_fixed_audit_fields_contains_required_keys(self):
        """FIXED_AUDIT_FIELDS contains all required CSV column names."""
        from baldur.audit.constants import FIXED_AUDIT_FIELDS

        expected = {
            "timestamp",
            "action",
            "actor_id",
            "actor_type",
            "target_type",
            "target_id",
            "service_name",
            "reason",
            "success",
        }
        assert set(FIXED_AUDIT_FIELDS) == expected

    def test_fixed_audit_fields_order_starts_with_timestamp(self):
        """First field is 'timestamp'."""
        from baldur.audit.constants import FIXED_AUDIT_FIELDS

        assert FIXED_AUDIT_FIELDS[0] == "timestamp"


class TestExportOptionsContract:
    """ExportOptions default values contract."""

    def test_default_format_is_jsonl(self):
        """Default format is JSONL."""
        opts = ExportOptions(input_paths=["test.jsonl"])
        assert opts.format == ExportFormat.JSONL

    def test_default_target_is_stdout(self):
        """Default target is STDOUT."""
        opts = ExportOptions(input_paths=["test.jsonl"])
        assert opts.target == ExportTarget.STDOUT

    def test_default_max_entries_json_format(self):
        """Default max_entries_json_format is 50000."""
        opts = ExportOptions(input_paths=["test.jsonl"])
        assert opts.max_entries_json_format == 50000

    def test_default_verify_integrity_is_true(self):
        """Default verify_integrity is True."""
        opts = ExportOptions(input_paths=["test.jsonl"])
        assert opts.verify_integrity is True

    def test_default_s3_region(self):
        """Default s3_region is 'ap-northeast-2'."""
        opts = ExportOptions(input_paths=["test.jsonl"])
        assert opts.s3_region == "ap-northeast-2"


class TestWriteEntriesBehavior:
    """_write_entries() behavior verification for different formats."""

    def _make_exporter(self, fmt=ExportFormat.JSONL, **kwargs):
        opts = ExportOptions(
            input_paths=["dummy"],
            format=fmt,
            verify_integrity=False,
            **kwargs,
        )
        return AuditExporter(opts)

    def test_jsonl_writes_one_line_per_entry(self):
        """JSONL format writes one JSON line per entry."""
        exporter = self._make_exporter(ExportFormat.JSONL)
        output = io.StringIO()
        entries = [{"action": "test1"}, {"action": "test2"}]

        exporter._write_entries(iter(entries), output)

        lines = output.getvalue().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["action"] == "test1"
        assert json.loads(lines[1])["action"] == "test2"

    def test_json_format_writes_array(self):
        """JSON format writes a JSON array."""
        exporter = self._make_exporter(ExportFormat.JSON)
        output = io.StringIO()
        entries = [{"a": 1}, {"b": 2}]

        exporter._write_entries(iter(entries), output)

        result = json.loads(output.getvalue())
        assert isinstance(result, list)
        assert len(result) == 2

    def test_json_format_raises_when_exceeding_limit(self):
        """JSON format raises ValueError when entries exceed max_entries_json_format."""
        exporter = self._make_exporter(ExportFormat.JSON, max_entries_json_format=2)
        output = io.StringIO()
        entries = [{"i": i} for i in range(3)]

        with pytest.raises(ValueError, match="JSON format supports max"):
            exporter._write_entries(iter(entries), output)

    def test_csv_format_uses_fixed_audit_fields_as_header(self):
        """CSV format uses FIXED_AUDIT_FIELDS as header row."""
        exporter = self._make_exporter(ExportFormat.CSV)
        output = io.StringIO()
        entries = [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "action": "test",
                "actor_id": "user1",
                "actor_type": "human",
                "target_type": "service",
                "target_id": "svc-1",
                "service_name": "audit",
                "reason": "test",
                "success": True,
                "extra_field": "ignored",
            }
        ]

        exporter._write_entries(iter(entries), output)

        output.seek(0)
        reader = csv.reader(output)
        header = next(reader)
        from baldur.audit.constants import FIXED_AUDIT_FIELDS

        assert header == FIXED_AUDIT_FIELDS

    def test_csv_format_ignores_extra_fields(self):
        """CSV format ignores fields not in FIXED_AUDIT_FIELDS."""
        exporter = self._make_exporter(ExportFormat.CSV)
        output = io.StringIO()
        entries = [{"action": "test", "unknown_field": "should_be_ignored"}]

        exporter._write_entries(iter(entries), output)

        output.seek(0)
        content = output.getvalue()
        assert "unknown_field" not in content
        assert "should_be_ignored" not in content

    def test_parquet_format_raises_not_implemented(self):
        """Parquet format raises NotImplementedError."""
        exporter = self._make_exporter(ExportFormat.PARQUET)
        output = io.StringIO()

        with pytest.raises(NotImplementedError, match="pyarrow"):
            exporter._write_entries(iter([{"a": 1}]), output)

    def test_jsonl_increments_exported_entries_count(self):
        """JSONL writing increments exported_entries stat."""
        exporter = self._make_exporter(ExportFormat.JSONL)
        output = io.StringIO()
        entries = [{"a": 1}, {"b": 2}, {"c": 3}]

        exporter._write_entries(iter(entries), output)

        assert exporter._stats.exported_entries == 3


class TestExportToHttpBehavior:
    """_export_to_http() NDJSON chunked POST behavior verification."""

    def test_http_requires_endpoint(self):
        """HTTP target without endpoint raises ValueError."""
        opts = ExportOptions(
            input_paths=["dummy"],
            target=ExportTarget.HTTP,
            verify_integrity=False,
        )
        exporter = AuditExporter(opts)

        with pytest.raises(ValueError, match="http-endpoint"):
            exporter._export_to_http(iter([{"a": 1}]))

    @patch("urllib.request.urlopen", autospec=True)
    def test_http_sends_ndjson_content_type(self, mock_urlopen):
        """HTTP POST uses application/x-ndjson content type."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        opts = ExportOptions(
            input_paths=["dummy"],
            target=ExportTarget.HTTP,
            http_endpoint="http://localhost:9200/_bulk",
            verify_integrity=False,
        )
        exporter = AuditExporter(opts)
        exporter._export_to_http(iter([{"action": "test"}]))

        # Verify the request was made
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        assert request.get_header("Content-type") == "application/x-ndjson"
        assert request.method == "POST"


class TestParseDatetimeBehavior:
    """parse_datetime() behavior verification."""

    def test_iso_date_only(self):
        """Parses 'YYYY-MM-DD' format."""
        result = parse_datetime("2026-01-15")
        assert result == datetime(2026, 1, 15, tzinfo=UTC)

    def test_iso_datetime_with_t(self):
        """Parses 'YYYY-MM-DDTHH:MM:SS' format."""
        result = parse_datetime("2026-01-15T10:30:00")
        assert result == datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)

    def test_iso_datetime_with_z(self):
        """Parses 'YYYY-MM-DDTHH:MM:SSZ' format."""
        result = parse_datetime("2026-01-15T10:30:00Z")
        assert result == datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)

    def test_datetime_with_space(self):
        """Parses 'YYYY-MM-DD HH:MM:SS' format."""
        result = parse_datetime("2026-01-15 10:30:00")
        assert result == datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)

    def test_invalid_format_raises_value_error(self):
        """Invalid datetime string raises ValueError."""
        with pytest.raises(ValueError, match="Invalid datetime format"):
            parse_datetime("not-a-date")

    def test_result_has_utc_timezone(self):
        """Parsed datetime always has UTC timezone."""
        result = parse_datetime("2026-06-01")
        assert result.tzinfo == UTC


class TestMatchesFiltersBehavior:
    """_matches_filters() behavior verification."""

    def _make_exporter_with_filters(self, **kwargs):
        opts = ExportOptions(input_paths=["dummy"], **kwargs)
        return AuditExporter(opts)

    def test_no_filters_matches_all(self):
        """No filters set: all entries match."""
        exporter = self._make_exporter_with_filters()
        assert exporter._matches_filters({"action": "anything"}) is True

    def test_action_filter_matches(self):
        """Action filter matches specified action."""
        exporter = self._make_exporter_with_filters(actions=["cb_open", "dlq_store"])
        assert exporter._matches_filters({"action": "cb_open"}) is True
        assert exporter._matches_filters({"action": "other"}) is False

    def test_actor_filter_matches(self):
        """Actor filter matches specified actor_id."""
        exporter = self._make_exporter_with_filters(actor_ids=["user1"])
        assert exporter._matches_filters({"actor_id": "user1"}) is True
        assert exporter._matches_filters({"actor_id": "user2"}) is False

    def test_time_filter_excludes_before_start(self):
        """Entries before start_time are excluded."""
        start = datetime(2026, 6, 1, tzinfo=UTC)
        exporter = self._make_exporter_with_filters(start_time=start)
        entry = {"timestamp": "2026-05-01T00:00:00+00:00"}
        assert exporter._matches_filters(entry) is False

    def test_time_filter_includes_after_start(self):
        """Entries after start_time are included."""
        start = datetime(2026, 1, 1, tzinfo=UTC)
        exporter = self._make_exporter_with_filters(start_time=start)
        entry = {"timestamp": "2026-06-01T00:00:00+00:00"}
        assert exporter._matches_filters(entry) is True
