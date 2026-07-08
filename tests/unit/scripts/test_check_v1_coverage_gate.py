"""Unit tests for scripts/check_v1_coverage_gate.py - v1.0 coverage gate.

Test plan source: 523 `## Test Assessment`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_v1_coverage_gate as gate  # noqa: E402

# =============================================================================
# Helpers / fixtures
# =============================================================================


def _file_entry(num_statements: int, covered_lines: int) -> dict:
    """Build a minimal coverage.py file-entry shape used by the gate."""
    return {
        "summary": {
            "num_statements": num_statements,
            "covered_lines": covered_lines,
        }
    }


def _passing_files() -> dict[str, dict]:
    """Build a synthesised `files` dict where all 5 TARGETS hit 100%.

    The gate reads per-target paths from `gate.TARGETS`, so we synthesise
    one entry under each directory target and one entry at each file
    target. 100% is used so every target satisfies the default threshold
    while leaving headroom for boundary tests to subtract coverage.
    """
    files: dict[str, dict] = {}
    for target in gate.TARGETS:
        if target["type"] == "dir":
            # Place a single file under the directory prefix.
            path = f"{target['path']}/synthetic.py"
        else:
            path = target["path"]
        files[path] = _file_entry(num_statements=100, covered_lines=100)
    return files


@pytest.fixture
def write_coverage_json(tmp_path):
    """Return a helper that writes a coverage JSON file and returns its path."""

    def _write(files: dict[str, dict] | None = None, *, raw: str | None = None) -> Path:
        path = tmp_path / "cov.json"
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
        else:
            path.write_text(json.dumps({"files": files or {}}), encoding="utf-8")
        return path

    return _write


# =============================================================================
# TestMeasureBehavior - _measure() aggregation + matching
# =============================================================================


class TestMeasureBehavior:
    def test_measure_dir_target_aggregates_all_nested_files(self):
        # Given a directory target with two nested files
        target = {"name": "bulkhead", "type": "dir", "path": "src/pkg/bulkhead"}
        files = {
            "src/pkg/bulkhead/a.py": _file_entry(num_statements=50, covered_lines=40),
            "src/pkg/bulkhead/sub/b.py": _file_entry(
                num_statements=30, covered_lines=15
            ),
        }

        # When measuring
        covered, total, matched = gate._measure(files, target)

        # Then both files are summed and reported
        assert (covered, total) == (55, 80)
        assert sorted(matched) == [
            "src/pkg/bulkhead/a.py",
            "src/pkg/bulkhead/sub/b.py",
        ]

    def test_measure_dir_target_excludes_files_outside_prefix(self):
        target = {"name": "bulkhead", "type": "dir", "path": "src/pkg/bulkhead"}
        files = {
            "src/pkg/bulkhead/in.py": _file_entry(num_statements=10, covered_lines=10),
            "src/pkg/other/out.py": _file_entry(num_statements=10, covered_lines=10),
        }

        covered, total, matched = gate._measure(files, target)

        assert (covered, total) == (10, 10)
        assert matched == ["src/pkg/bulkhead/in.py"]

    def test_measure_dir_target_rejects_partial_prefix_match(self):
        # Regression guard: `services/bulkhead` must NOT match
        # `services/bulkhead_helper/...` - dir match requires a trailing slash.
        target = {"name": "bulkhead", "type": "dir", "path": "src/pkg/bulkhead"}
        files = {
            "src/pkg/bulkhead_helper/x.py": _file_entry(
                num_statements=10, covered_lines=10
            ),
        }

        covered, total, matched = gate._measure(files, target)

        assert (covered, total, matched) == (0, 0, [])

    def test_measure_file_target_matches_only_exact_path(self):
        target = {"name": "f", "type": "file", "path": "src/pkg/file.py"}
        files = {
            "src/pkg/file.py": _file_entry(num_statements=20, covered_lines=15),
            "src/pkg/file_other.py": _file_entry(num_statements=20, covered_lines=20),
            "src/pkg/sub/file.py": _file_entry(num_statements=20, covered_lines=20),
        }

        covered, total, matched = gate._measure(files, target)

        assert (covered, total, matched) == (15, 20, ["src/pkg/file.py"])

    def test_measure_normalises_windows_backslash_paths(self):
        # coverage.py emits OS-native separators - the gate must match
        # backslash-keyed entries against a forward-slash target path.
        target = {"name": "f", "type": "file", "path": "src/pkg/file.py"}
        files = {
            "src\\pkg\\file.py": _file_entry(num_statements=20, covered_lines=12),
        }

        covered, total, matched = gate._measure(files, target)

        assert (covered, total) == (12, 20)
        assert matched == ["src/pkg/file.py"]

    def test_measure_missing_target_returns_zero(self):
        target = {"name": "missing", "type": "file", "path": "src/pkg/missing.py"}
        files = {
            "src/pkg/other.py": _file_entry(num_statements=10, covered_lines=10),
        }

        covered, total, matched = gate._measure(files, target)

        assert (covered, total, matched) == (0, 0, [])

    def test_measure_entry_without_summary_treated_as_zero(self):
        # Defensive: coverage.py always emits `summary`, but the gate
        # uses `.get(...)` to avoid KeyError. Verify the fallback path.
        target = {"name": "f", "type": "file", "path": "src/pkg/file.py"}
        files = {"src/pkg/file.py": {}}

        covered, total, matched = gate._measure(files, target)

        assert (covered, total) == (0, 0)
        assert matched == ["src/pkg/file.py"]


# =============================================================================
# TestGateContract - main() exit codes and threshold semantics
# =============================================================================


class TestGateContract:
    def test_main_returns_zero_when_all_targets_meet_threshold(
        self, write_coverage_json, capsys
    ):
        path = write_coverage_json(_passing_files())

        exit_code = gate.main([str(path)])

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "OK: all 5 targets" in out

    def test_main_returns_zero_at_exact_threshold_boundary(self, write_coverage_json):
        # Boundary: 80.0% exactly should PASS (the gate uses
        # `percent + 1e-9 < threshold`, so equal does not fail).
        files = _passing_files()
        for path in list(files):
            files[path] = _file_entry(num_statements=100, covered_lines=80)
        coverage_path = write_coverage_json(files)

        assert gate.main([str(coverage_path)]) == 0

    def test_main_returns_one_when_one_target_below_threshold(
        self, write_coverage_json, capsys
    ):
        files = _passing_files()
        # Drop the first target below 80%.
        first_target = gate.TARGETS[0]
        if first_target["type"] == "dir":
            offender_path = f"{first_target['path']}/synthetic.py"
        else:
            offender_path = first_target["path"]
        files[offender_path] = _file_entry(num_statements=100, covered_lines=79)
        coverage_path = write_coverage_json(files)

        exit_code = gate.main([str(coverage_path)])

        assert exit_code == 1
        err = capsys.readouterr().err
        assert first_target["name"] in err
        assert "79.0%" in err

    def test_main_returns_one_when_target_missing_from_report(
        self, write_coverage_json, capsys
    ):
        # All present targets pass; one is simply absent.
        files = _passing_files()
        missing_target = gate.TARGETS[0]
        absent_key = (
            f"{missing_target['path']}/synthetic.py"
            if missing_target["type"] == "dir"
            else missing_target["path"]
        )
        files.pop(absent_key)
        coverage_path = write_coverage_json(files)

        exit_code = gate.main([str(coverage_path)])

        assert exit_code == 1
        err = capsys.readouterr().err
        assert missing_target["name"] in err
        assert "missing from coverage report" in err

    def test_main_returns_two_when_coverage_file_missing(self, tmp_path, capsys):
        missing = tmp_path / "does_not_exist.json"

        exit_code = gate.main([str(missing)])

        assert exit_code == 2
        assert "coverage report not found" in capsys.readouterr().err

    def test_main_returns_two_when_json_malformed(self, write_coverage_json, capsys):
        path = write_coverage_json(raw="{not valid json")

        exit_code = gate.main([str(path)])

        assert exit_code == 2
        assert "malformed JSON" in capsys.readouterr().err

    def test_main_returns_two_when_files_dict_empty(self, write_coverage_json, capsys):
        path = write_coverage_json({})

        exit_code = gate.main([str(path)])

        assert exit_code == 2
        assert "no 'files' entries" in capsys.readouterr().err

    def test_main_threshold_argument_propagates_to_comparison(
        self, write_coverage_json
    ):
        # All targets at 50% — fails default 80, passes --threshold 50.
        files = _passing_files()
        for path in list(files):
            files[path] = _file_entry(num_statements=100, covered_lines=50)
        coverage_path = write_coverage_json(files)

        assert gate.main([str(coverage_path)]) == 1
        assert gate.main([str(coverage_path), "--threshold", "50"]) == 0

    def test_main_dunder_main_invocation_returns_via_sys_exit(
        self, write_coverage_json, monkeypatch
    ):
        # Smoke: the `if __name__ == "__main__"` path wraps main() in
        # sys.exit(). We exercise main() with patched argv to confirm
        # the script-mode entry point still returns the int contract.
        path = write_coverage_json(_passing_files())
        monkeypatch.setattr(sys, "argv", ["check_v1_coverage_gate.py", str(path)])

        assert gate.main() == 0


# =============================================================================
# TestTargetsContract - hardcoded inventory of the 5 OSS launch-surface files
# =============================================================================


class TestTargetsContract:
    def test_targets_have_five_entries(self):
        assert len(gate.TARGETS) == 5

    def test_targets_are_all_files(self):
        dirs = [t for t in gate.TARGETS if t["type"] == "dir"]
        files = [t for t in gate.TARGETS if t["type"] == "file"]
        assert len(dirs) == 0
        assert len(files) == 5

    def test_targets_match_oss_launch_surface_inventory(self):
        # Hardcoded inventory of the 5 OSS v1.0 launch-surface files (523).
        expected_paths = {
            "src/baldur/api/django/reauthentication.py",
            "src/baldur/api/django/throttle_adapter.py",
            "src/baldur/api/django/serializers/pydantic_integration.py",
            "src/baldur/api/handlers/canary.py",
            "src/baldur/api/handlers/compliance.py",
        }
        actual_paths = {t["path"] for t in gate.TARGETS}
        assert actual_paths == expected_paths

    def test_default_threshold_is_eighty(self):
        # Contract from 523 D6.
        assert gate.DEFAULT_THRESHOLD == 80.0

    def test_default_coverage_json_is_dist_cov_v1(self):
        # Contract from doc 523 § D3.
        assert gate.DEFAULT_COVERAGE_JSON.name == "cov-v1.json"
        assert gate.DEFAULT_COVERAGE_JSON.parent.name == "dist"


# =============================================================================
# TestStepSummaryWriterBehavior - _write_step_summary() markdown shape
# Test plan source: 525 § Test Assessment
# =============================================================================


class TestStepSummaryWriterBehavior:
    def test_writer_emits_header_and_table_skeleton(self, tmp_path):
        summary_path = tmp_path / "summary.md"
        records: list[tuple[str, float, int, int, str]] = [
            ("api/handlers/canary.py", 95.0, 19, 20, "PASS"),
        ]

        gate._write_step_summary(summary_path, 80.0, records)
        content = summary_path.read_text(encoding="utf-8")

        # Header + markdown table skeleton must be present.
        assert "### v1.0 coverage gate (threshold 80.0%)" in content
        assert "| target | coverage | covered/total | status |" in content
        assert "|---|---:|---:|---|" in content

    def test_pass_row_formats_percent_to_one_decimal(self, tmp_path):
        summary_path = tmp_path / "summary.md"
        records = [("api/handlers/canary.py", 95.0, 19, 20, "PASS")]

        gate._write_step_summary(summary_path, 80.0, records)
        content = summary_path.read_text(encoding="utf-8")

        # Target name wrapped in backticks, percent has one decimal,
        # statements rendered as `covered/total`.
        assert "| `api/handlers/canary.py` | 95.0% | 19/20 | PASS |" in content

    def test_fail_row_keeps_percent_cell(self, tmp_path):
        summary_path = tmp_path / "summary.md"
        records = [("services/audit", 65.5, 131, 200, "FAIL")]

        gate._write_step_summary(summary_path, 80.0, records)
        content = summary_path.read_text(encoding="utf-8")

        # FAIL rows still emit the measured percent (operator needs the gap size).
        assert "| `services/audit` | 65.5% | 131/200 | FAIL |" in content

    def test_miss_row_renders_percent_as_na(self, tmp_path):
        # MISS = target absent from coverage report. The percent cell
        # should be "n/a" rather than "0.0%" so the operator can tell a
        # missing target apart from a truly zero-covered one.
        summary_path = tmp_path / "summary.md"
        records = [("services/bulkhead", 0.0, 0, 0, "MISS")]

        gate._write_step_summary(summary_path, 80.0, records)
        content = summary_path.read_text(encoding="utf-8")

        assert "| `services/bulkhead` | n/a | 0/0 | MISS |" in content
        # Defensive: must not silently coerce MISS into "0.0%".
        assert "| `services/bulkhead` | 0.0%" not in content

    def test_writer_appends_to_existing_content(self, tmp_path):
        # GitHub Actions concatenates multiple steps' summaries; the
        # writer must append rather than overwrite.
        summary_path = tmp_path / "summary.md"
        summary_path.write_text("preexisting line\n", encoding="utf-8")
        records = [("api/handlers/canary.py", 95.0, 19, 20, "PASS")]

        gate._write_step_summary(summary_path, 80.0, records)
        content = summary_path.read_text(encoding="utf-8")

        assert content.startswith("preexisting line\n")
        assert "### v1.0 coverage gate" in content

    def test_writer_respects_custom_threshold_in_header(self, tmp_path):
        summary_path = tmp_path / "summary.md"

        gate._write_step_summary(summary_path, 90.0, [])
        content = summary_path.read_text(encoding="utf-8")

        assert "### v1.0 coverage gate (threshold 90.0%)" in content

    def test_writer_emits_only_header_when_records_empty(self, tmp_path):
        summary_path = tmp_path / "summary.md"

        gate._write_step_summary(summary_path, 80.0, [])
        content = summary_path.read_text(encoding="utf-8")

        # Header + table skeleton present, but no data rows.
        assert "### v1.0 coverage gate" in content
        assert "| target | coverage | covered/total | status |" in content
        # No data row -> no backtick-wrapped target cells.
        assert "| `" not in content


# =============================================================================
# TestStepSummaryEnvIntegration - GITHUB_STEP_SUMMARY env wiring in main()
# =============================================================================


class TestStepSummaryEnvIntegration:
    def test_main_writes_summary_when_env_var_set(
        self, write_coverage_json, monkeypatch, tmp_path
    ):
        # Given GITHUB_STEP_SUMMARY points at a writable file
        summary_path = tmp_path / "github_step_summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
        coverage_path = write_coverage_json(_passing_files())

        # When main() runs (all targets PASS)
        assert gate.main([str(coverage_path)]) == 0

        # Then the summary file picks up the markdown table
        content = summary_path.read_text(encoding="utf-8")
        assert "### v1.0 coverage gate" in content
        # All 5 targets should appear as rows.
        for target in gate.TARGETS:
            assert f"| `{target['name']}` |" in content

    def test_main_skips_summary_when_env_var_missing(
        self, write_coverage_json, monkeypatch, tmp_path
    ):
        # Given no GITHUB_STEP_SUMMARY env var (delenv is no-op if absent)
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        coverage_path = write_coverage_json(_passing_files())

        # When main() runs and tmp_path has no pre-existing summary file
        assert gate.main([str(coverage_path)]) == 0

        # Then no summary file is created in tmp_path
        assert list(tmp_path.glob("*.md")) == []

    def test_main_summary_records_fail_target_with_percent(
        self, write_coverage_json, monkeypatch, tmp_path
    ):
        summary_path = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))

        # Drop the first target below threshold so it shows up as FAIL.
        files = _passing_files()
        first_target = gate.TARGETS[0]
        offender_path = (
            f"{first_target['path']}/synthetic.py"
            if first_target["type"] == "dir"
            else first_target["path"]
        )
        files[offender_path] = _file_entry(num_statements=100, covered_lines=50)
        coverage_path = write_coverage_json(files)

        assert gate.main([str(coverage_path)]) == 1

        content = summary_path.read_text(encoding="utf-8")
        # FAIL row carries the actual percent (50.0%) so the operator
        # sees the gap without scrolling the raw log.
        assert f"| `{first_target['name']}` | 50.0% | 50/100 | FAIL |" in content

    def test_main_summary_records_miss_target_with_na(
        self, write_coverage_json, monkeypatch, tmp_path
    ):
        summary_path = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))

        files = _passing_files()
        missing_target = gate.TARGETS[0]
        absent_key = (
            f"{missing_target['path']}/synthetic.py"
            if missing_target["type"] == "dir"
            else missing_target["path"]
        )
        files.pop(absent_key)
        coverage_path = write_coverage_json(files)

        assert gate.main([str(coverage_path)]) == 1

        content = summary_path.read_text(encoding="utf-8")
        assert f"| `{missing_target['name']}` | n/a | 0/0 | MISS |" in content
