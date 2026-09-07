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


def _run_clean(snippet: str) -> subprocess.CompletedProcess:
    """Run a Python snippet in a pristine subprocess.

    Absence is a per-process fact and every other test in this file publishes
    the series, so it can only be observed from a process that has done
    nothing else.
    """
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(snippet)],
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

    def test_a_process_that_never_asked_publishes_no_sample(self):
        """The third state, and the one the other two are read against.

        Creating this gauge beside ``audit_backend_wired`` in the module body
        would export it from every audit-enabled process, because a
        label-less prometheus gauge registers a sample the moment it is
        constructed. ``0`` would then mean both "asked, and Redis answered"
        and "never asked", which is the two-state collapse this series exists
        to avoid. Run in a clean process: the module is imported and the
        *other* gauge is published, exactly as ``init()`` does on a boot with
        no distributed chain.
        """
        result = _run_clean(
            """
            from prometheus_client import REGISTRY

            from baldur.metrics.audit_backend_metrics import (
                set_audit_backend_wired,
            )

            set_audit_backend_wired(True)

            print(
                REGISTRY.get_sample_value("audit_backend_wired", {}),
                REGISTRY.get_sample_value("audit_distributed_chain_degraded", {}),
            )
            """
        )

        assert result.returncode == 0, result.stderr
        wired, degraded = result.stdout.split()
        assert wired == "1.0", "the sibling gauge must still publish"
        assert degraded == "None", (
            "a process that never asked for a distributed chain must publish "
            f"no sample at all; got {degraded}"
        )

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


class TestAuditHashChainCountersContract:
    """The two counters that make a repair visible without a verification pass.

    Both are labelled or label-less by design: a healthy chain exports nothing
    at all, so a sample's existence is itself the signal. An operator alerting
    on ``increase(...[5m]) > 0`` cannot do that against a series that is
    always present reading zero.
    """

    @pytest.fixture(autouse=True)
    def _require_prometheus(self):
        from baldur.metrics.audit_backend_metrics import METRICS_AVAILABLE

        if not METRICS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

    def test_the_source_reset_counter_exports_under_the_design_name(self):
        from prometheus_client import REGISTRY

        from baldur.metrics.audit_backend_metrics import (
            increment_audit_hash_chain_source_reset,
        )

        increment_audit_hash_chain_source_reset(manager="redis", reason="counter_reset")

        assert (
            REGISTRY.get_sample_value(
                "baldur_audit_hash_chain_source_resets_total",
                {"manager": "redis", "reason": "counter_reset"},
            )
            is not None
        )

    def test_the_source_reset_counter_is_sliced_by_manager_and_reason(self):
        """An incident review needs to tell "Redis was wiped" from "the local
        state file was truncated" without reading the ledger."""
        from baldur.metrics.audit_backend_metrics import (
            audit_hash_chain_source_resets_total,
        )

        assert audit_hash_chain_source_resets_total._labelnames == (
            "manager",
            "reason",
        )

    def test_each_manager_reason_pair_is_its_own_child(self):
        from prometheus_client import REGISTRY

        from baldur.metrics.audit_backend_metrics import (
            increment_audit_hash_chain_source_reset,
        )

        def sample(manager: str, reason: str) -> float:
            return (
                REGISTRY.get_sample_value(
                    "baldur_audit_hash_chain_source_resets_total",
                    {"manager": manager, "reason": reason},
                )
                or 0.0
            )

        before_target = sample("local", "state_unreadable")
        before_sibling = sample("local", "state_hash_stale")

        increment_audit_hash_chain_source_reset(
            manager="local", reason="state_unreadable"
        )

        assert sample("local", "state_unreadable") == before_target + 1
        assert sample("local", "state_hash_stale") == before_sibling

    def test_the_fallback_write_counter_exports_under_the_design_name(self):
        from prometheus_client import REGISTRY

        from baldur.metrics.audit_backend_metrics import (
            increment_audit_hash_chain_fallback_write,
        )

        before = (
            REGISTRY.get_sample_value(
                "baldur_audit_hash_chain_fallback_writes_total", {}
            )
            or 0.0
        )
        increment_audit_hash_chain_fallback_write()

        assert (
            REGISTRY.get_sample_value(
                "baldur_audit_hash_chain_fallback_writes_total", {}
            )
            == before + 1
        )

    def test_the_fallback_write_counter_carries_no_labels(self):
        """A process-level count of entries Redis did not sequence — no
        dimension to slice it by, and any label would fragment the series an
        alert has to watch."""
        from baldur.metrics.audit_backend_metrics import (
            audit_hash_chain_fallback_writes_total,
        )

        assert audit_hash_chain_fallback_writes_total._labelnames == ()

    def test_a_chain_that_never_repaired_exports_no_sample(self):
        """Absence is a per-process fact, so it can only be observed from a
        process that has published nothing else."""
        result = _run_clean(
            """
            from prometheus_client import REGISTRY

            import baldur.metrics.audit_backend_metrics  # noqa: F401

            print(
                REGISTRY.get_sample_value(
                    "baldur_audit_hash_chain_source_resets_total",
                    {"manager": "redis", "reason": "counter_reset"},
                ),
                REGISTRY.get_sample_value(
                    "baldur_audit_hash_chain_fallback_writes_total", {}
                ),
            )
            """
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "None 0.0"

    def test_the_degraded_gauge_help_text_describes_the_live_posture(self):
        """It is primed by the admission probe and then re-published by the
        manager on every posture change, so the old "did not answer the
        admission probe" wording described a series that no longer exists."""
        from prometheus_client import REGISTRY

        from baldur.metrics.audit_backend_metrics import (
            set_audit_distributed_chain_degraded,
        )

        set_audit_distributed_chain_degraded(False)
        gauge = REGISTRY._names_to_collectors["audit_distributed_chain_degraded"]

        assert "right now" in gauge._documentation
        assert "admission probe" not in gauge._documentation


class TestAuditHashChainCountersNoPrometheusContract:
    """Without prometheus_client the counters are inert, never absent."""

    def test_incrementing_either_counter_never_raises_without_prometheus(self):
        result = _run_poisoned(
            """
            from baldur.metrics.audit_backend_metrics import (
                METRICS_AVAILABLE,
                increment_audit_hash_chain_fallback_write,
                increment_audit_hash_chain_source_reset,
            )

            assert METRICS_AVAILABLE is False
            increment_audit_hash_chain_source_reset(
                manager="local", reason="state_unreadable"
            )
            increment_audit_hash_chain_fallback_write()
            print("ok")
            """
        )

        assert result.returncode == 0, result.stderr
        assert "ok" in result.stdout
