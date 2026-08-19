"""The footprint probe, run end to end the way the documentation says to run it.

Test plan source: 760 `## Test Assessment`.

Mock-based in the sense that matters here: no infrastructure is required, and
whatever the developer running the suite has configured is what the child
measures. What cannot be faked is the shape of the run - ``import baldur``,
``baldur.init()``, a live settle loop, then process exit - and every defect
this script actually shipped with was invisible to a code read and visible on
the first real run: a report character the console could not encode, an import
stage that reads near zero, and framework logging arriving after the numbers.

The child is a subprocess rather than an in-suite call for two reasons.
``init()`` starts daemon threads and an outbox worker that would leak into the
xdist worker running this test, and the exit path is half of what is being
measured. Capture is pinned to UTF-8: on a legacy code page, text-mode capture
returns empty output instead of raising, and every assertion below would then
fail for the wrong reason.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from baldur.scripts import measure_footprint as mf

_PROBE_MODULE = "baldur.scripts.measure_footprint"

# The probe waits at least ten seconds past init()'s return and gives up at
# sixty, then pays whatever the exit path costs. The budget is deliberately far
# above that: a timeout here should mean "hung", never "slower runner".
_CHILD_TIMEOUT_SECONDS = 300.0

_SETTLED_STAGE_LABEL = "settled"


@pytest.fixture(scope="module")
def probe_run() -> subprocess.CompletedProcess[str]:
    """Run the documented command once - a child that settles is not cheap."""
    env = dict(os.environ)
    # init() refuses to start in production without shared storage, and that
    # refusal is the one exit path this smoke is not about. Nothing else is
    # touched, so the child measures the posture this machine actually has.
    env.pop("BALDUR_ENVIRONMENT", None)

    return subprocess.run(
        [sys.executable, "-m", _PROBE_MODULE],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=_CHILD_TIMEOUT_SECONDS,
        check=False,
    )


class TestMeasureFootprintProbeSmoke:
    """One child run, several questions asked of its output."""

    @staticmethod
    def _posture_keys(stdout: str) -> set[str]:
        """The left column of the posture echo, read back out of the report."""
        lines = stdout.splitlines()
        block = lines[lines.index("Posture") + 1 : lines.index("Stages")]
        return {line.split("  ")[1] for line in block if line.strip()}

    def test_the_documented_command_exits_clean(self, probe_run):
        """Exit 0 means a citable sample was found; 1 means it never settled."""
        assert probe_run.returncode == 0, probe_run.stdout + probe_run.stderr

    @pytest.mark.parametrize(
        "label",
        [mf._BASELINE_LABEL, mf._IMPORT_LABEL, mf._INIT_LABEL, _SETTLED_STAGE_LABEL],
        ids=["baseline", "import", "init_returned", "settled"],
    )
    def test_every_stage_is_reported(self, probe_run, label):
        """A missing stage is a missing layer of the cost breakdown."""
        assert label in probe_run.stdout

    def test_the_import_stage_explains_why_it_reads_near_zero(self, probe_run):
        """Without the note a reader takes a near-zero import stage for
        "importing Baldur is free" - the package defers its submodules, so the
        cost lands in the stage below."""
        assert "near zero by design" in probe_run.stdout

    def test_the_init_return_reading_is_labelled_a_transient_peak(self, probe_run):
        """The reason the settle stage exists at all."""
        assert "transient peak" in probe_run.stdout

    def test_the_citable_figure_is_reported_after_the_peak(self, probe_run):
        """Order is the message: the quotable number is the later one."""
        stdout = probe_run.stdout

        assert "settled, citable" in stdout
        assert stdout.index("transient peak") < stdout.index("settled, citable")

    def test_the_posture_echo_carries_the_host_axes(self, probe_run):
        """Resident memory does not transfer across OS or interpreter version,
        so a reader has to be able to tell whether their number is comparable."""
        assert {"os", "python", "cpu count"} <= self._posture_keys(probe_run.stdout)

    def test_the_posture_echo_carries_the_tier_axes(self, probe_run):
        """Which code paths the numbers include is the other half of provenance."""
        assert {"baldur_pro", "entitlement", "storage backend"} <= self._posture_keys(
            probe_run.stdout
        )

    def test_the_completion_boundary_closes_the_measurement(self, probe_run):
        assert mf._COMPLETE_BOUNDARY in probe_run.stdout

    def test_the_shutdown_notice_follows_the_boundary(self, probe_run):
        """Framework and exporter logging keeps arriving after the numbers; the
        reader is told once, in the output itself, that it is not measured cost."""
        stdout = probe_run.stdout

        assert stdout.index("shutdown work") > stdout.index(mf._COMPLETE_BOUNDARY)
