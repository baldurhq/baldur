"""The system snapshot and the CB-OPEN snapshot task without Django REST framework.

``collect_system_snapshot()`` used to live in a Django REST framework (DRF) view
module, so on an install without ``[django-api]`` every caller failed to import
it: the circuit-breaker OPEN snapshot task logged ``collect_cb_open_snapshot.failed``
on each OPEN and handed the save hook nothing. The helper lives in
``baldur.services.system_snapshot`` and imports no web framework.

Each case runs in a subprocess with ``sys.modules["rest_framework"] = None`` set
before any Baldur import — the unit session has DRF loaded. The child drops
``DJANGO_SETTINGS_MODULE``: a Celery worker that takes the snapshot need not
configure Django.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

# Below the gate's per-test --timeout so a hung child fails as TimeoutExpired.
_CHILD_TIMEOUT_S = 25


def _run_without_rest_framework(snippet: str, report_dir: Path) -> dict:
    """Run ``snippet`` with DRF absent; return the JSON report it writes to argv[1]."""
    report_path = report_dir / "report.json"
    script = "import sys\nsys.modules['rest_framework'] = None\n" + (
        textwrap.dedent(snippet)
    )
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    result = subprocess.run(
        [sys.executable, "-c", script, str(report_path)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=_CHILD_TIMEOUT_S,
        env=env,
        cwd=report_dir,
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout[-4000:]}\nstderr={result.stderr[-4000:]}"
    )
    return json.loads(report_path.read_text(encoding="utf-8"))


_COLLECT = """
import json
from baldur.services.system_snapshot import collect_system_snapshot

with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump({"snapshot": collect_system_snapshot()}, f, default=str)
"""

# The PRO save hook is replaced by a recorder installed after the task module
# is imported; the task imports the hook lazily at call time, and a module
# already in sys.modules is returned without importing its parent packages.
_CB_OPEN_TASK = """
import json
import types
from structlog.testing import capture_logs
from baldur.celery_tasks.circuit_breaker_tasks import collect_cb_open_snapshot

saved = []


def save_open_snapshot_to_redis(service_name, snapshot):
    saved.append({"service_name": service_name, "snapshot": snapshot})
    return True


hook = types.ModuleType("baldur_pro.services.postmortem.snapshot_builder")
hook.save_open_snapshot_to_redis = save_open_snapshot_to_redis
sys.modules[hook.__name__] = hook

with capture_logs() as logs:
    result = collect_cb_open_snapshot.run(
        service_name="payments",
        event_timestamp="2026-01-01T00:00:00+00:00",
    )
report = {
    "result": result,
    "saved": saved,
    "events": [{"event": r["event"], "log_level": r["log_level"]} for r in logs],
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(report, f, default=str)
"""


class TestSystemSnapshotWithoutRestFrameworkBehavior:
    """The snapshot is taken on an install without DRF."""

    def test_collect_system_snapshot_without_rest_framework_returns_metrics(
        self, tmp_path
    ):
        """The helper imports and returns CPU and memory figures, not the error form."""
        # When
        snapshot = _run_without_rest_framework(_COLLECT, tmp_path)["snapshot"]

        # Then
        assert "error" not in snapshot
        assert isinstance(snapshot["cpu_percent"], float)
        assert isinstance(snapshot["memory_percent"], float)

    def test_cb_open_snapshot_task_without_rest_framework_hands_snapshot_to_save_hook(
        self, tmp_path
    ):
        """On a circuit-breaker OPEN the task saves a real snapshot instead of failing."""
        # When
        report = _run_without_rest_framework(_CB_OPEN_TASK, tmp_path)

        # Then
        assert report["result"]["success"] is True
        assert [entry["service_name"] for entry in report["saved"]] == ["payments"]
        snapshot = report["saved"][0]["snapshot"]
        assert "error" not in snapshot
        assert snapshot["captured_at"] == "open"
        assert "cpu_percent" in snapshot
        assert [
            r
            for r in report["events"]
            if r["event"] == "collect_cb_open_snapshot.failed"
        ] == []
