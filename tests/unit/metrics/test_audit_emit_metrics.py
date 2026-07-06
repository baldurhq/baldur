"""Unit tests for baldur.metrics.audit_emit_metrics (587 — D8).

Covers the ``audit_emit_dropped_total{site}`` counter and the
``record_audit_emit_dropped()`` helper added so the recovered (still
fail-open) best-effort audit emissions are verifiable in production rather
than only in tests:

- Contract: the counter exports under the design-doc name with a single
  ``site`` label; ``record_audit_emit_dropped`` increments the right series.
- No-prometheus branch: when ``prometheus_client`` is absent the module
  falls back to a dummy metric whose ``record_audit_emit_dropped()`` never
  raises (preserving the callers' fail-open guarantee). Verified in a
  subprocess with ``prometheus_client`` poisoned before import — in-process
  ``delitem`` is insufficient because already-imported modules cache the
  resolved symbols and would mask the regression.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest


# tests/unit/metrics/conftest.py defines an autouse fixture that skips the
# whole module when prometheus_client is absent in the parent. Override it
# here: the subprocess test poisons prometheus_client inside the child, so the
# parent's installation status must not gate it. The in-process contract tests
# guard themselves explicitly via METRICS_AVAILABLE.
@pytest.fixture(autouse=True)
def _check_prometheus():
    return


def _run_poisoned(snippet: str) -> subprocess.CompletedProcess:
    """Run a Python snippet in a subprocess with prometheus_client poisoned."""
    script = "import sys\nsys.modules['prometheus_client'] = None\n" + textwrap.dedent(
        snippet
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )


# The complete best-effort, no-backstop emitter set the design doc (D8)
# declares as valid ``site`` label values.
_VALID_SITES = ["unified_notification", "celery_notifying_task", "forensic_recorder"]


class TestAuditEmitMetricsContract:
    """The counter and helper behave per the D8 contract (prometheus present)."""

    @pytest.fixture(autouse=True)
    def _require_prometheus(self):
        from baldur.metrics.audit_emit_metrics import METRICS_AVAILABLE

        if not METRICS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

    def test_counter_exports_under_design_name_with_site_label(self):
        # Given
        from prometheus_client import REGISTRY

        from baldur.metrics.audit_emit_metrics import record_audit_emit_dropped

        # When — Counter("audit_emit_dropped_total", ...) exports the sample
        # "audit_emit_dropped_total" (prometheus strips/re-appends the _total
        # suffix) carrying a single "site" label.
        record_audit_emit_dropped("unified_notification")

        # Then
        value = REGISTRY.get_sample_value(
            "audit_emit_dropped_total", {"site": "unified_notification"}
        )
        assert value is not None
        assert value >= 1.0

    def test_labelnames_is_exactly_site(self):
        from baldur.metrics.audit_emit_metrics import audit_emit_dropped_total

        assert audit_emit_dropped_total._labelnames == ("site",)

    @pytest.mark.parametrize("site", _VALID_SITES)
    def test_record_audit_emit_dropped_increments_labeled_series(self, site):
        # Given
        from baldur.metrics.audit_emit_metrics import (
            audit_emit_dropped_total,
            record_audit_emit_dropped,
        )

        before = audit_emit_dropped_total.labels(site=site)._value.get()

        # When
        record_audit_emit_dropped(site)

        # Then
        after = audit_emit_dropped_total.labels(site=site)._value.get()
        assert after - before == 1.0

    def test_distinct_sites_track_independent_series(self):
        # Given
        from baldur.metrics.audit_emit_metrics import (
            audit_emit_dropped_total,
            record_audit_emit_dropped,
        )

        a_before = audit_emit_dropped_total.labels(
            site="unified_notification"
        )._value.get()
        b_before = audit_emit_dropped_total.labels(
            site="forensic_recorder"
        )._value.get()

        # When — one site only
        record_audit_emit_dropped("unified_notification")

        # Then — the other site's series is untouched (no cardinality bleed)
        a_after = audit_emit_dropped_total.labels(
            site="unified_notification"
        )._value.get()
        b_after = audit_emit_dropped_total.labels(site="forensic_recorder")._value.get()
        assert a_after - a_before == 1.0
        assert b_after == b_before


class TestAuditEmitMetricsNoPrometheusContract:
    """Without prometheus_client the module degrades to a no-raise dummy."""

    def test_metrics_available_false_when_prometheus_absent(self):
        # When
        result = _run_poisoned(
            """
            from baldur.metrics.audit_emit_metrics import METRICS_AVAILABLE
            assert METRICS_AVAILABLE is False, METRICS_AVAILABLE
            print('OK')
            """
        )
        # Then
        assert result.returncode == 0, f"stderr={result.stderr}"
        assert "OK" in result.stdout

    def test_record_audit_emit_dropped_never_raises_without_prometheus(self):
        # When — the dummy .inc() is a no-op that must never raise inside the
        # callers' fail-open except blocks.
        result = _run_poisoned(
            """
            from baldur.metrics.audit_emit_metrics import record_audit_emit_dropped
            record_audit_emit_dropped("unified_notification")
            record_audit_emit_dropped("celery_notifying_task")
            record_audit_emit_dropped("forensic_recorder")
            print('OK')
            """
        )
        # Then
        assert result.returncode == 0, f"stderr={result.stderr}"
        assert "OK" in result.stdout
