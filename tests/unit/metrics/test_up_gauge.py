"""Unit tests for the ``baldur_up`` exporter-liveness marker (734 D5).

The bundled scrape-liveness alert rules join on this marker's *presence*, which
is what makes them framework-agnostic — they need no knowledge of the scrape
job's name. Three properties keep that join working, and each has a test class
here:

* the marker is registered at **module scope**, so every metrics backend
  exports it — including the OTel backend, which never constructs
  ``BaldurMetrics``. Only a fresh subprocess can show this: in a pytest process
  the name is already owned by the time the first test runs, so an in-process
  assertion would stay green even if registration regressed into a
  backend-specific constructor.
* the marker is **label-less and pinned at 1**; the rules read existence, never
  the value.
* the helper is **fail-open on any exception**, because ``get_or_create_gauge``
  hands back whatever collector already owns the name without a type check.

The alert file's own shape (no ``job=`` selector, no value read of the marker)
is a separate architectural gate over a static asset; these tests cover the
live-registry half it cannot see.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest
from prometheus_client import REGISTRY, Counter, Gauge
from structlog.testing import capture_logs

from baldur.metrics.registry import UP_GAUGE_NAME, ensure_up_gauge

# The collision WARNING is the only diagnostic a foreign collector produces, so
# the tests pin the literal the operator would grep for.
COLLISION_EVENT = "metrics.up_gauge_registration_failed"

_SUBPROCESS_TIMEOUT = 60


def _safe_unregister(collector) -> None:
    try:
        REGISTRY.unregister(collector)
    except KeyError:
        pass


@pytest.fixture
def released_marker_name():
    """Hand the global ``baldur_up`` name to the test, then restore Baldur's own.

    Registration happens at import of ``baldur.metrics.registry``, so by the time
    any test runs Baldur already owns the name and no collision can occur
    naturally. The surgery must target the **global** ``REGISTRY``:
    ``get_or_create_gauge`` reads only that one, so planting a foreign collector
    in a scratch ``CollectorRegistry`` would construct no collision at all and
    leave the test inert.

    Yields a list — append any collector the test plants and teardown removes it.
    Teardown is mandatory: the registry is process-wide, so a leaked foreign
    collector poisons every later ``baldur_up`` assertion in the same worker.
    """
    original = REGISTRY._names_to_collectors[UP_GAUGE_NAME]
    REGISTRY.unregister(original)
    planted: list = []
    try:
        yield planted
    finally:
        for collector in planted:
            _safe_unregister(collector)
        current = REGISTRY._names_to_collectors.get(UP_GAUGE_NAME)
        if current is not None and current is not original:
            _safe_unregister(current)
        if UP_GAUGE_NAME not in REGISTRY._names_to_collectors:
            REGISTRY.register(original)


def _run_child(snippet: str) -> subprocess.CompletedProcess:
    """Run a snippet in a fresh interpreter that never calls ``baldur.init()``."""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(snippet)],
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT,
    )


class TestUpGaugeContract:
    """Design values the shipped alert rules depend on."""

    def test_marker_name_is_the_alert_join_key(self):
        # The bundled rules select `baldur_up` by literal name; renaming the
        # constant alone silently empties both of them.
        assert UP_GAUGE_NAME == "baldur_up"

    def test_marker_gauge_carries_no_labels(self):
        # Given / When
        collector = REGISTRY._names_to_collectors[UP_GAUGE_NAME]
        # Then — a labelled marker would emit one series per label combination
        # instead of one per scrape target, and `.set(1)` on an unlabelled
        # handle would raise.
        assert collector._labelnames == ()

    def test_marker_is_exported_at_one(self):
        assert REGISTRY.get_sample_value(UP_GAUGE_NAME) == 1.0


class TestEnsureUpGaugeBehavior:
    """``ensure_up_gauge()`` registers the marker and never propagates."""

    def test_ensure_up_gauge_registers_the_marker_at_one_when_the_name_is_free(
        self, released_marker_name
    ):
        # Given — the fixture released the name
        assert UP_GAUGE_NAME not in REGISTRY._names_to_collectors

        # When
        ensure_up_gauge()

        # Then
        assert UP_GAUGE_NAME in REGISTRY._names_to_collectors
        assert REGISTRY.get_sample_value(UP_GAUGE_NAME) == 1.0

    def test_ensure_up_gauge_repeated_calls_keep_one_collector_at_one(
        self, released_marker_name
    ):
        # Given
        ensure_up_gauge()
        first = REGISTRY._names_to_collectors[UP_GAUGE_NAME]

        # When — module-scope registration runs once per process, but the helper
        # is public and the recorders re-enter the same name space
        ensure_up_gauge()
        ensure_up_gauge()

        # Then
        assert REGISTRY._names_to_collectors[UP_GAUGE_NAME] is first
        assert REGISTRY.get_sample_value(UP_GAUGE_NAME) == 1.0

    @pytest.mark.parametrize(
        ("plant", "expected_error_fragment"),
        [
            # A labelled Gauge: `.set(1)` on an unlabelled handle -> ValueError.
            (
                lambda: Gauge(UP_GAUGE_NAME, "foreign labelled gauge", ["instance"]),
                "missing label values",
            ),
            # A Counter: no `.set` at all -> AttributeError. An
            # ImportError/ValueError-only guard would let this one through.
            (
                lambda: Counter(UP_GAUGE_NAME, "foreign counter", []),
                "has no attribute 'set'",
            ),
        ],
        ids=["labelled_gauge", "counter"],
    )
    def test_ensure_up_gauge_swallows_a_foreign_collector_and_warns(
        self, released_marker_name, plant, expected_error_fragment
    ):
        """Fail-open: a foreign owner of the name must not break the import.

        This runs at module scope in production, so a propagated raise would
        take down every importer of the metrics registry. The WARNING assertion
        is what proves the fault actually fired — without it "does not raise"
        would also pass on a path where no collision was ever constructed.
        """
        # Given
        foreign = plant()
        released_marker_name.append(foreign)

        # When
        with capture_logs() as logs:
            ensure_up_gauge()  # must not raise

        # Then
        collisions = [entry for entry in logs if entry.get("event") == COLLISION_EVENT]
        assert len(collisions) == 1, logs
        assert collisions[0]["log_level"] == "warning"
        assert collisions[0]["metric"] == UP_GAUGE_NAME
        assert expected_error_fragment in collisions[0]["error"]


class TestUpGaugeRegistrationReachBehavior:
    """Module-scope registration, shown where in-process assertions cannot.

    Every check below runs in a fresh interpreter that never calls
    ``baldur.init()``. In the pytest process the marker is registered before the
    first test collects — and pytest-django's session start runs ``init()`` on
    top of that — so an in-process version of any of these would be green by
    ambient session state rather than by the property under test.
    """

    def test_importing_the_registry_module_alone_exports_the_marker(self):
        # When
        result = _run_child(
            """
            import baldur.metrics.registry  # noqa: F401

            from prometheus_client import REGISTRY

            assert REGISTRY.get_sample_value("baldur_up") == 1.0, (
                REGISTRY.get_sample_value("baldur_up")
            )
            print("OK")
            """
        )
        # Then
        assert result.returncode == 0, f"stderr={result.stderr}"
        assert "OK" in result.stdout

    def test_alert_drift_gate_snapshot_sources_resolve_the_marker(self):
        """The alert/metric drift gate builds its registry snapshot from
        ``services.metrics.definitions`` + ``...recorders`` + a bare
        ``BaldurMetrics()`` — deliberately not ``get_metrics()``. The marker has
        to resolve from those three alone, or that gate passes only where an
        ambient ``baldur.init()`` happens to have run.
        """
        # When
        result = _run_child(
            """
            import sys

            import baldur.services.metrics.definitions  # noqa: F401
            import baldur.services.metrics.recorders  # noqa: F401
            from baldur.metrics.prometheus import BaldurMetrics

            BaldurMetrics()

            from prometheus_client import REGISTRY

            assert "baldur.bootstrap" not in sys.modules, "init() ran after all"
            assert REGISTRY.get_sample_value("baldur_up") == 1.0, (
                REGISTRY.get_sample_value("baldur_up")
            )
            print("OK")
            """
        )
        # Then
        assert result.returncode == 0, f"stderr={result.stderr}"
        assert "OK" in result.stdout

    def test_otel_backend_process_exports_the_marker(self):
        """The default OTel profile never constructs ``BaldurMetrics``, so a
        backend-side registration hook would leave the marker absent and the
        bundled absence rule would page critical on a healthy deployment.
        """
        # When
        result = _run_child(
            """
            from unittest.mock import MagicMock, patch

            import baldur.settings.observability as observability_settings

            with (
                patch.object(
                    observability_settings,
                    "get_observability_settings",
                    return_value=MagicMock(effective_backend="otel"),
                ),
                patch("baldur.observability.get_meter", return_value=MagicMock()),
            ):
                from baldur.metrics.prometheus import get_metrics, reset_metrics

                reset_metrics()
                backend = get_metrics()

            from prometheus_client import REGISTRY

            assert type(backend).__name__ == "OTELBaldurMetrics", type(backend).__name__
            assert REGISTRY.get_sample_value("baldur_up") == 1.0, (
                REGISTRY.get_sample_value("baldur_up")
            )
            print("OK")
            """
        )
        # Then
        assert result.returncode == 0, f"stderr={result.stderr}"
        assert "OK" in result.stdout
