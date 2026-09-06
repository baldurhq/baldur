"""Unit tests for ``baldur.metrics.audit_backend_metrics``.

``audit_backend_wired`` is 0 exactly when the audit master switch is on while
the resolved default provider is the no-op adapter — records are written,
accepted, and reach nothing. It is primed from ``init()`` so a deployment can
alert on ``audit_backend_wired == 0`` without waiting for the first audited
event, which means both verdicts have to be observable on the series: a gauge
that emits only on failure cannot distinguish "healthy" from "never booted".

Companion file:
``tests/unit/test_bootstrap_audit_backend_wired.py`` — the ``init()`` Step-5
priming that decides which verdict is published.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.1 Contract (series name, label set, both values).
- §8.2 Exception/edge cases (prometheus_client absent → no-raise dummy).
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


class TestAuditBackendMetricsContract:
    """The gauge exports under the design name with no labels."""

    @pytest.fixture(autouse=True)
    def _require_prometheus(self):
        from baldur.metrics.audit_backend_metrics import METRICS_AVAILABLE

        if not METRICS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

    def test_gauge_exports_under_the_design_name(self):
        # Given
        from prometheus_client import REGISTRY

        from baldur.metrics.audit_backend_metrics import set_audit_backend_wired

        # When
        set_audit_backend_wired(True)

        # Then
        assert REGISTRY.get_sample_value("audit_backend_wired", {}) == 1.0

    def test_labelnames_is_empty(self):
        """A process-level verdict — no dimension to slice it by, and any
        label would fragment the series an alert has to watch."""
        from baldur.metrics.audit_backend_metrics import audit_backend_wired

        assert audit_backend_wired._labelnames == ()

    def test_unwired_verdict_publishes_zero(self):
        from prometheus_client import REGISTRY

        from baldur.metrics.audit_backend_metrics import set_audit_backend_wired

        set_audit_backend_wired(False)

        assert REGISTRY.get_sample_value("audit_backend_wired", {}) == 0.0

    def test_series_follows_the_latest_verdict_in_both_directions(self):
        """Both values on one series. A gauge only ever written with 0 would
        make ``audit_backend_wired == 0`` unusable as an alert, because the
        healthy state would be indistinguishable from the absent one."""
        from prometheus_client import REGISTRY

        from baldur.metrics.audit_backend_metrics import set_audit_backend_wired

        set_audit_backend_wired(False)
        assert REGISTRY.get_sample_value("audit_backend_wired", {}) == 0.0

        set_audit_backend_wired(True)
        assert REGISTRY.get_sample_value("audit_backend_wired", {}) == 1.0

        set_audit_backend_wired(False)
        assert REGISTRY.get_sample_value("audit_backend_wired", {}) == 0.0


class TestAuditDistributedChainDegradedContract:
    """``audit_distributed_chain_degraded`` is a three-state answer.

    Records still land and ``audit_backend_wired`` still reads 1 — this is a
    different axis: the chain sequencing those records is not the cross-host
    one the deployment asked for. Only a process that wanted a distributed
    chain publishes the series at all, so absence means "nobody asked" and
    never "everything is fine".
    """

    @pytest.fixture(autouse=True)
    def _require_prometheus(self):
        from baldur.metrics.audit_backend_metrics import METRICS_AVAILABLE

        if not METRICS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

    def test_gauge_exports_under_the_design_name(self):
        from prometheus_client import REGISTRY

        from baldur.metrics.audit_backend_metrics import (
            set_audit_distributed_chain_degraded,
        )

        set_audit_distributed_chain_degraded(True)

        assert REGISTRY.get_sample_value("audit_distributed_chain_degraded", {}) == 1.0

    def test_labelnames_is_empty(self):
        """A process-level posture — any label would fragment the series the
        alert has to watch."""
        from baldur.metrics.audit_backend_metrics import (
            audit_distributed_chain_degraded,
        )

        assert audit_distributed_chain_degraded._labelnames == ()

    def test_healthy_verdict_publishes_zero(self):
        from prometheus_client import REGISTRY

        from baldur.metrics.audit_backend_metrics import (
            set_audit_distributed_chain_degraded,
        )

        set_audit_distributed_chain_degraded(False)

        assert REGISTRY.get_sample_value("audit_distributed_chain_degraded", {}) == 0.0

    def test_series_follows_the_latest_verdict_in_both_directions(self):
        """Both values on one series. Written only with 1, the healthy state
        would be indistinguishable from the never-asked one, and the third
        state the docstring promises would not exist."""
        from prometheus_client import REGISTRY

        from baldur.metrics.audit_backend_metrics import (
            set_audit_distributed_chain_degraded,
        )

        set_audit_distributed_chain_degraded(True)
        assert REGISTRY.get_sample_value("audit_distributed_chain_degraded", {}) == 1.0

        set_audit_distributed_chain_degraded(False)
        assert REGISTRY.get_sample_value("audit_distributed_chain_degraded", {}) == 0.0

    def test_the_two_audit_gauges_are_independent_series(self):
        """A degraded chain is not an unwired backend: records land, so
        ``audit_backend_wired`` must stay 1 while the chain gauge reads 1."""
        from prometheus_client import REGISTRY

        from baldur.metrics.audit_backend_metrics import (
            set_audit_backend_wired,
            set_audit_distributed_chain_degraded,
        )

        set_audit_backend_wired(True)
        set_audit_distributed_chain_degraded(True)

        assert REGISTRY.get_sample_value("audit_backend_wired", {}) == 1.0
        assert REGISTRY.get_sample_value("audit_distributed_chain_degraded", {}) == 1.0


class TestAuditBackendMetricsNoPrometheusContract:
    """Without prometheus_client the module degrades to a no-raise dummy."""

    def test_metrics_available_false_when_prometheus_absent(self):
        # When
        result = _run_poisoned(
            """
            from baldur.metrics.audit_backend_metrics import METRICS_AVAILABLE
            assert METRICS_AVAILABLE is False, METRICS_AVAILABLE
            print('OK')
            """
        )
        # Then
        assert result.returncode == 0, f"stderr={result.stderr}"
        assert "OK" in result.stdout

    def test_set_audit_backend_wired_never_raises_without_prometheus(self):
        # When — the caller wraps this in a fail-open except that logs at
        # DEBUG, so a raising dummy would silently drop the Step-5 verdict.
        result = _run_poisoned(
            """
            from baldur.metrics.audit_backend_metrics import (
                set_audit_backend_wired,
                set_audit_distributed_chain_degraded,
            )
            set_audit_backend_wired(True)
            set_audit_backend_wired(False)
            set_audit_distributed_chain_degraded(True)
            set_audit_distributed_chain_degraded(False)
            print('OK')
            """
        )
        # Then
        assert result.returncode == 0, f"stderr={result.stderr}"
        assert "OK" in result.stdout

    def test_dummy_satisfies_the_gauge_metric_protocol(self):
        """The module annotates the gauge as ``GaugeMetric``, which is labels +
        set + inc. A dummy missing one of the three type-checks as a mismatched
        assignment and, worse, raises AttributeError on the branch prometheus
        is absent — the exact branch the fallback exists to keep quiet."""
        # When
        result = _run_poisoned(
            """
            from baldur.metrics._metric_protocol import GaugeMetric
            from baldur.metrics.audit_backend_metrics import (
                audit_backend_wired,
                audit_distributed_chain_degraded,
            )

            for gauge in (audit_backend_wired, audit_distributed_chain_degraded):
                assert isinstance(gauge, GaugeMetric), type(gauge)
                gauge.labels().inc()
            print('OK')
            """
        )
        # Then
        assert result.returncode == 0, f"stderr={result.stderr}"
        assert "OK" in result.stdout
