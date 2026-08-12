"""752 — the zero-config first-contact posture, composed across subsystems.

The posture announcement is the one place in this change where three
independently-owned pieces of state have to line up in one process:

- what ``get_runtime_posture()`` derives (settings + the metrics registry's
  import-time flag + bootstrap's ``_init_done``);
- whether the line is *visible*, which depends on ``configure_structlog()``
  having installed the posture logger's INFO floor first;
- which entry point got there first — ``init()`` at the end of its step
  sequence, or the protect prelude on the first protected call.

None of that is observable from inside the test session: the suite has
already imported every optional extra, configured logging, and run
``init()``. So each case measures a real child interpreter through the
committed harness rather than re-deriving the recipe — the same artifact the
public CI job runs, which is what keeps this from drifting away from the
gate.

Mock-based in the sense that matters here: no infrastructure is configured,
because "nothing is configured" IS the scenario.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_HARNESS_RELATIVE = ("scripts", "check_zero_config_log_posture.py")


def _harness_path() -> Path | None:
    """Locate the committed harness from the installed package, not the tree.

    ``baldur.__file__`` resolves to ``<repo>/src/baldur/__init__.py`` in the
    public repo and to the sibling clone's copy in the private one, so the
    same expression finds the script from either test tree. A non-editable
    install has no ``scripts/`` directory and yields None.
    """
    import baldur

    candidate = Path(baldur.__file__).resolve().parents[2].joinpath(*_HARNESS_RELATIVE)
    return candidate if candidate.is_file() else None


@pytest.fixture(scope="module")
def harness():
    path = _harness_path()
    if path is None:
        pytest.skip("zero-config posture harness not present (non-editable install)")

    spec = importlib.util.spec_from_file_location("zero_config_posture_harness", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before executing: the harness defines a @dataclass, and
    # dataclasses resolves annotations through ``sys.modules[cls.__module__]``.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    yield module
    sys.modules.pop(spec.name, None)


@pytest.fixture(scope="module")
def measured(harness):
    """Measure each documented path once — a child interpreter is not cheap."""
    return {path: harness._measure(path, False) for path in harness.PATHS}


class TestZeroConfigFirstContactPosture:
    """A first-contact run says one thing, at INFO, and nothing louder."""

    def test_the_documented_paths_are_the_two_that_emit(self, harness):
        """Guards the parametrized cases below from shrinking silently."""
        assert set(harness.PATHS) == {"decorator", "init"}

    @pytest.mark.parametrize("path", ["decorator", "init"])
    def test_the_child_run_completes(self, measured, path):
        assert measured[path].returncode == 0

    @pytest.mark.parametrize("path", ["decorator", "init"])
    def test_no_baldur_line_reaches_warning_or_above(self, harness, measured, path):
        """The goal of the whole change, measured on a real console stream."""
        result = measured[path]
        offending = harness.baldur_lines_at_warning_or_above(result.lines)

        assert offending == [], "\n".join(offending)

    @pytest.mark.parametrize("path", ["decorator", "init"])
    def test_the_posture_line_appears_exactly_once(self, measured, path):
        """Once — so the INFO floor worked AND the latch held.

        A count of zero would mean the line was derived and dropped by the
        default WARNING root level, which is invisible to any in-process
        record assertion.
        """
        assert measured[path].posture_lines == 1

    def test_the_init_path_installs_the_level_filter(self, measured):
        """``configure_structlog()`` runs as ``init()``'s first statement, so
        no step of the sequence can emit into the unfiltered default."""
        assert measured["init"].counts["debug"] == 0


class TestTheDetectorSeesEveryRendering:
    """The gate's own falsifiability, in the renderings CI actually produces.

    Everything above trusts one predicate to decide "was that a baldur
    WARNING?". It has to hold for all three shapes a line can arrive in:
    JSON (the configured default), plain console (pre-configuration), and
    **coloured** console. The third is the one that bites — structlog's
    ConsoleRenderer colours by default and does not gate that on a tty, so
    on a Linux runner the level word arrives wrapped in escapes even though
    stdout is a pipe. A detector that a terminal setting can silence reports
    a clean run on a noisy one, which is the only failure mode of a gate
    that nobody notices.
    """

    @staticmethod
    def _console_line(colors: bool) -> str:
        import structlog

        return structlog.dev.ConsoleRenderer(colors=colors)(
            None,
            "warning",
            {"event": "resilient_storage.lazy_redis_probe_failed", "level": "warning"},
        )

    def test_the_coloured_line_is_actually_coloured(self):
        """Guards the case below from passing on an uncoloured fixture."""
        assert "\x1b[" in self._console_line(colors=True)

    @pytest.mark.parametrize("colors", [False, True], ids=["plain", "coloured"])
    def test_a_console_warning_is_detected_in_either_rendering(self, harness, colors):
        line = self._console_line(colors)

        assert harness.baldur_lines_at_warning_or_above([line]) != []

    def test_a_json_warning_is_detected(self, harness):
        line = (
            '{"event": "resilient_storage.lazy_redis_probe_failed", '
            '"level": "warning", "logger": "baldur.adapters.resilient.backend"}'
        )

        assert harness.baldur_lines_at_warning_or_above([line]) != []

    def test_someone_elses_warning_is_not_ours(self, harness):
        """The gate must not fail on the host framework's own lines."""
        line = '{"event": "startup", "level": "warning", "logger": "uvicorn.error"}'

        assert harness.baldur_lines_at_warning_or_above([line]) == []


class TestPostureAcrossInitAndProtect:
    """The latch spans entry points, and the report agrees with the line.

    The harness measures each entry point alone. This drives both in one
    process — the only shape where the "whichever runs first is the only
    emitter" guarantee can actually fail.
    """

    @staticmethod
    def _run_child(harness, body: str) -> str:
        script = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('zc', r'{harness.__file__}')\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "sys.modules['zc'] = mod\n"  # the harness defines a @dataclass
            "spec.loader.exec_module(mod)\n"
            "mod._install_absence(mod.blocked_module_names())\n" + body
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            env=harness._child_environment(False),
            timeout=harness._CHILD_TIMEOUT_SECONDS,
            check=False,
        )
        output = (completed.stdout + completed.stderr).decode("utf-8", "replace")
        assert completed.returncode == 0, output
        return output

    @pytest.fixture(scope="class")
    def init_then_protect(self, harness):
        """``init()`` first, then protected calls — one child, several asks.

        The bootstrap logger is lifted to INFO so the startup report reaches
        the stream; the posture line needs no such help, which is the point
        of its own floor.
        """
        return self._run_child(
            harness,
            "import logging, baldur\n"
            "logging.getLogger('baldur.bootstrap').setLevel(logging.INFO)\n"
            "baldur.init()\n"
            "baldur.protect('d752', lambda: 1)\n"
            "baldur.protect('d752', lambda: 2)\n",
        )

    def test_a_protected_call_after_init_does_not_announce_again(
        self, harness, init_then_protect
    ):
        assert init_then_protect.count(harness.POSTURE_EVENT) == 1

    def test_the_combined_run_stays_below_warning(self, harness, init_then_protect):
        offending = harness.baldur_lines_at_warning_or_above(
            init_then_protect.splitlines()
        )

        assert offending == [], "\n".join(offending)

    def test_the_startup_report_agrees_with_the_posture_line(self, init_then_protect):
        """Both read one derivation, so a zero-config boot reports memory
        storage in the long report too — not just in the short line."""
        report_lines = [
            line
            for line in init_then_protect.splitlines()
            if "baldur.startup_report" in line
        ]

        assert len(report_lines) == 1, init_then_protect
        assert '"storage_backend": "memory"' in report_lines[0]
        assert '"metrics_backend": "disabled"' in report_lines[0]

    def test_init_after_a_protected_call_does_not_announce_again(self, harness):
        """The reverse order: the protect prelude got there first."""
        output = self._run_child(
            harness,
            "import baldur\nbaldur.protect('d752', lambda: 1)\nbaldur.init()\n",
        )

        assert output.count(harness.POSTURE_EVENT) == 1
