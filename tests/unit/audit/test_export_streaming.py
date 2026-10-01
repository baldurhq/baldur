"""Unit tests for audit/export.py streaming/format changes (308-C).

The integrity pass (804) walks every entry read, before the time / action /
actor filters, from the lowest entry present, and says where each trail's
check began. It used to compare fields the ledger never writes, so an altered
trail exported with zero integrity errors.
"""

import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.audit.export import (
    AuditExporter,
    ExportFormat,
    ExportOptions,
    ExportStats,
    ExportTarget,
    integrity_summary_lines,
    main,
    parse_datetime,
)
from baldur.audit.integrity import HashChainManager
from baldur.settings.secrets import reset_secrets_settings


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


# =============================================================================
# The integrity pass over a hash-chain ledger
# =============================================================================

_KEY_ENV = "BALDUR_SECRETS_AUDIT_SIGNING_KEY"
_DAY_FILES = ("audit_2025-01-15.jsonl", "audit_2025-01-16.jsonl")
_PER_DAY = 4
# One entry per day carries this action; the rest are config changes.
_FILTERED_ACTION = "cb_force_open"


@pytest.fixture
def _writer_key():
    """Pin the signing key the trail is written and verified with."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(_KEY_ENV, "export-integrity-writer-key")
        reset_secrets_settings()
        yield
    reset_secrets_settings()


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return path


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _export_trail(directory: Path) -> list[Path]:
    """An untouched chain over two daily files, written by the chain manager."""
    manager = HashChainManager()
    paths = []
    for day, name in enumerate(_DAY_FILES):
        rows = [
            manager.add_integrity(
                {
                    "timestamp": f"2025-01-{15 + day}T1{n}:00:00+00:00",
                    "action": _FILTERED_ACTION if n == 1 else "config_change",
                    "actor_id": "user1",
                }
            )
            for n in range(_PER_DAY)
        ]
        paths.append(_write_jsonl(directory / name, rows))
    return paths


def _untouched(paths: list[Path]) -> None:
    """Leave the trail as written."""


def _alter_an_entry_the_filter_leaves_out(paths: list[Path]) -> None:
    """Edit day two's first entry, a config change, its hash kept."""
    rows = _read_jsonl(paths[1])
    rows[0]["actor_id"] = "mallory"
    _write_jsonl(paths[1], rows)


def _export(paths: list[Path], output: Path, **options) -> ExportStats:
    return AuditExporter(
        ExportOptions(
            input_paths=[str(path) for path in paths],
            target=ExportTarget.FILE,
            output_path=str(output),
            **options,
        )
    ).export()


@pytest.mark.usefixtures("_writer_key")
class TestExportIntegrityBehavior:
    """The export's integrity pass sees every entry read, before the filters."""

    @pytest.mark.parametrize(
        ("actions", "expected_exported"),
        [(None, len(_DAY_FILES) * _PER_DAY), ([_FILTERED_ACTION], len(_DAY_FILES))],
        ids=["no_filter", "action_filter"],
    )
    @pytest.mark.parametrize(
        ("mutate", "expected_errors"),
        [(_untouched, 0), (_alter_an_entry_the_filter_leaves_out, 1)],
        ids=["untouched", "altered"],
    )
    def test_integrity_errors_count_tampering_with_and_without_an_action_filter(
        self, tmp_path, mutate, expected_errors, actions, expected_exported
    ):
        # Given a two-day trail, untouched or with one entry altered that the
        # action filter does not export
        paths = _export_trail(tmp_path)
        mutate(paths)

        # When it is exported, with or without the action filter
        stats = _export(paths, tmp_path / "out.jsonl", actions=actions)

        # Then the pass counted what it read, not what the filter kept
        assert stats.exported_entries == expected_exported
        assert stats.integrity_errors == expected_errors

    def test_integrity_partial_input_reports_zero_errors_and_prints_its_first_sequence(
        self, tmp_path, capsys
    ):
        # Given only the later day's file of an untouched trail as input
        paths = _export_trail(tmp_path)
        capsys.readouterr()

        # When the CLI exports it
        code = main(
            [
                "--input",
                str(paths[1]),
                "--target",
                "file",
                "--output",
                str(tmp_path / "out.jsonl"),
            ]
        )

        # Then nothing is an error, and the summary says where the check began
        err = capsys.readouterr().err
        assert code == 0
        assert "Integrity errors" not in err
        assert f"Integrity checked from sequence {_PER_DAY + 1};" in err

    def test_integrity_full_input_with_start_reports_a_removed_first_of_day_entry(
        self, tmp_path
    ):
        # Given every file as input and day two's first entry removed
        paths = _export_trail(tmp_path)
        _write_jsonl(paths[1], _read_jsonl(paths[1])[1:])

        # When the export starts at day two
        with capture_logs() as logs:
            stats = _export(
                paths,
                tmp_path / "out.jsonl",
                start_time=datetime(2025, 1, 16, tzinfo=UTC),
            )

        # Then the time filter did not begin the walk mid-chain: the removal
        # is a missing entry, reported once in the WARNING
        assert stats.integrity_errors == 1
        assert stats.exported_entries == _PER_DAY - 1
        (warning,) = [
            log for log in logs if log["event"] == "audit_export.integrity_check_failed"
        ]
        assert warning["log_level"] == "warning"
        assert [
            (issue["type"], issue["sequence"]) for issue in warning["first_issues"]
        ] == [("missing_entry", _PER_DAY + 1)]

    def test_integrity_input_globs_reaching_one_file_read_it_once(self, tmp_path):
        # Read twice, every entry of the file would be a duplicate_entry.
        paths = _export_trail(tmp_path)
        (tmp_path / "sub").mkdir()
        same_file_spelled_differently = tmp_path / "sub" / ".." / paths[0].name

        stats = AuditExporter(
            ExportOptions(
                input_paths=[
                    str(tmp_path / "*.jsonl"),
                    str(same_file_spelled_differently),
                ],
                target=ExportTarget.FILE,
                output_path=str(tmp_path / "out.json"),
            )
        ).export()

        assert stats.total_entries == len(_DAY_FILES) * _PER_DAY
        assert stats.integrity_errors == 0

    def test_skip_integrity_runs_no_walk_over_an_altered_trail(self, tmp_path):
        paths = _export_trail(tmp_path)
        _alter_an_entry_the_filter_leaves_out(paths)

        stats = _export(paths, tmp_path / "out.jsonl", verify_integrity=False)

        assert stats.integrity_errors == 0
        assert stats.integrity_first_sequences == {}

    def test_integrity_summary_lines_name_errors_and_each_late_trail_start(self):
        stats = ExportStats(
            integrity_errors=2,
            integrity_first_sequences={"": 1, "worker": 7},
        )

        assert integrity_summary_lines(stats) == [
            "!! Integrity errors: 2",
            "Integrity checked from sequence 7 (partition worker); its link to 6 "
            "was not checked - add the earlier files to --input to check it",
        ]
