"""``baldur.scaling.deadline_context`` without prometheus-client.

``baldur.metrics.registry`` imports without prometheus-client (it carries its
own fallback) and exposes ``PROMETHEUS_AVAILABLE``; its ``get_or_create_*``
helpers raise ``ImportError`` when the extra is absent. ``deadline_context``
builds its metrics at import, so it must key them on that flag, not on whether
the registry import succeeded. Keyed on the import, the module raised at import
on every prometheus-absent install: ``configure_baldur()``'s default
``TieringMiddleware`` failed to load, and the framework-free ``check_deadline``
(which imports the module under ``except ImportError``) silently stopped
fast-failing.

Each case runs in a subprocess with ``sys.modules["prometheus_client"] = None``
set before any Baldur import — the unit session has prometheus-client loaded,
and ``deadline_context`` reads the flag once, at import.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# Below the gate's per-test --timeout so a hung child fails as TimeoutExpired.
_CHILD_TIMEOUT_S = 25


def _run_without_prometheus(snippet: str, report_dir: Path, *args: str) -> dict:
    """Run ``snippet`` with prometheus-client absent; return its JSON report.

    The snippet writes its report to ``sys.argv[1]``; ``args`` follow it.
    """
    report_path = report_dir / "report.json"
    script = "import sys\nsys.modules['prometheus_client'] = None\n" + (
        textwrap.dedent(snippet)
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(report_path), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=_CHILD_TIMEOUT_S,
        cwd=report_dir,
    )
    assert result.returncode == 0, (
        f"stdout={result.stdout[-4000:]}\nstderr={result.stderr[-4000:]}"
    )
    return json.loads(report_path.read_text(encoding="utf-8"))


_IMPORT_CONSUMERS = """
import json
import baldur.scaling  # noqa: F401
from baldur.api.django.tiering import TieringMiddleware  # noqa: F401
from baldur.scaling import deadline_context

report = {"has_prometheus": deadline_context._HAS_PROMETHEUS}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(report, f)
"""

_RECORD_HELPERS = """
import json
from baldur.scaling import deadline_context

report = {
    "has_prometheus": deadline_context._HAS_PROMETHEUS,
    "returns": [
        deadline_context.record_fast_fail(tier="standard", path_prefix="orders"),
        deadline_context.record_remaining_ms(10.0, tier="standard"),
        deadline_context.record_exhausted_on_arrival(path_prefix="orders"),
    ],
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(report, f)
"""

_CHECK_DEADLINE = """
import json
from baldur.api.middleware.deadline import check_deadline
from baldur.interfaces.web_framework import HttpMethod, RequestContext
from baldur.scaling.deadline_context import (
    DEADLINE_HEADER,
    DEFAULT_MINIMUM_USEFUL_TIME_MS,
)

remaining_ms = DEFAULT_MINIMUM_USEFUL_TIME_MS + float(sys.argv[2])
response = check_deadline(
    RequestContext(
        method=HttpMethod.GET,
        path="/orders/1",
        headers={DEADLINE_HEADER: f"{remaining_ms}ms"},
    )
)
report = {
    "response": None
    if response is None
    else {"status_code": response.status_code, "headers": response.headers}
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(report, f)
"""


class TestDeadlineContextWithoutPrometheusBehavior:
    """Deadline propagation works, with no-op metrics, when prometheus is absent."""

    def test_scaling_and_tiering_middleware_import_without_prometheus_client(
        self, tmp_path
    ):
        """The module and its middleware consumer import; the metrics flag is off."""
        # When
        report = _run_without_prometheus(_IMPORT_CONSUMERS, tmp_path)

        # Then
        assert report["has_prometheus"] is False

    def test_record_helpers_without_prometheus_client_are_noops(self, tmp_path):
        """Each metric helper returns without recording or raising."""
        # When
        report = _run_without_prometheus(_RECORD_HELPERS, tmp_path)

        # Then — the absent branch was the one taken
        assert report["has_prometheus"] is False
        assert report["returns"] == [None, None, None]

    @pytest.mark.parametrize(
        ("offset_ms", "rejected"),
        [(-1.0, True), (0.0, False), (1.0, False)],
        ids=["below_minimum", "at_minimum", "above_minimum"],
    )
    def test_check_deadline_without_prometheus_client_fast_fails_below_minimum(
        self, tmp_path, offset_ms, rejected
    ):
        """Remaining time below the minimum useful time gets a 503; at or above passes."""
        # When
        report = _run_without_prometheus(_CHECK_DEADLINE, tmp_path, str(offset_ms))

        # Then
        if rejected:
            assert report["response"]["status_code"] == 503
            assert report["response"]["headers"]["X-Baldur-Deadline-Rejected"] == (
                "true"
            )
        else:
            assert report["response"] is None
