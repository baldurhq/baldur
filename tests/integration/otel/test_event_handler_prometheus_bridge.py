"""In-process OTel SDK -> PrometheusMetricReader bridge for event-handler metrics.

645 SC5. The backend-parity unit test
(tests/unit/metrics/test_event_handler_backend_parity.py) MagicMocks
``baldur.observability.get_meter``, so the real OTel SDK aggregation and the
PrometheusMetricReader bridge are stubbed. This test closes that layer: it builds
a real MeterProvider with a real PrometheusMetricReader, injects OTELBaldurMetrics
into the event-handler module, drives CircuitBreakerEventHandler.on_state_changed,
and asserts the ``baldur_circuit_breaker_state`` series appears (non-empty) in the
prometheus_client exposition.

No live OTel Collector is needed — the bridge is fully in-process — so the test is
guarded by ``importorskip`` on the exporter package, NOT the ``requires_otel``
marker (which auto-skips unless a Collector health endpoint answers; see
tests/integration/conftest.py ``_check_otel_connection``).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("opentelemetry.exporter.prometheus")

from opentelemetry.exporter.prometheus import PrometheusMetricReader  # noqa: E402
from opentelemetry.sdk.metrics import MeterProvider  # noqa: E402
from prometheus_client import (  # noqa: E402
    REGISTRY,
    CollectorRegistry,
    generate_latest,
)

from baldur.metrics import event_handlers  # noqa: E402
from baldur.metrics.event_handlers import (  # noqa: E402
    CircuitBreakerEventHandler,
    reset_event_handler_cache,
)
from baldur.metrics.otel_backend import OTELBaldurMetrics  # noqa: E402


@pytest.fixture
def otel_prometheus_bridge():
    """A real in-process OTel MeterProvider + PrometheusMetricReader.

    PrometheusMetricReader auto-registers its collector with the global prometheus
    REGISTRY; move it to an isolated CollectorRegistry so the
    ``baldur_circuit_breaker_state`` series the OTel backend exposes cannot collide
    (duplicate-timeseries error) with the prometheus-backend recorder that
    registers the same metric name in the global default registry. Yields
    ``(metrics, isolated_registry)``.
    """
    reader = PrometheusMetricReader()
    REGISTRY.unregister(reader._collector)
    isolated = CollectorRegistry()
    isolated.register(reader._collector)
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("baldur-bridge-test")

    reset_event_handler_cache()
    with patch("baldur.observability.get_meter", return_value=meter):
        metrics = OTELBaldurMetrics(prefix="baldur")
    assert metrics._initialized, "OTEL backend failed to initialize with real meter"
    event_handlers._metrics_instance = metrics

    try:
        yield metrics, isolated
    finally:
        reset_event_handler_cache()
        try:
            isolated.unregister(reader._collector)
        except Exception:
            pass
        # PrometheusMetricReader.shutdown() unregisters its collector from the
        # global default REGISTRY, which no longer holds it (moved to isolated
        # above) — that KeyError on a pull-based reader is benign at teardown.
        try:
            provider.shutdown()
        except Exception:
            pass


class TestEventHandlerPrometheusBridge:
    """SC5: event-handler metrics traverse the real OTel SDK -> prometheus bridge."""

    def test_on_state_changed_appears_in_prometheus_exposition(
        self, otel_prometheus_bridge
    ):
        """on_state_changed -> baldur_circuit_breaker_state present and non-empty."""
        _metrics, isolated = otel_prometheus_bridge

        CircuitBreakerEventHandler.on_state_changed("bridge_test_svc", "closed", "open")

        output = generate_latest(isolated).decode()

        assert "baldur_circuit_breaker_state" in output
        # Non-empty: the driven service's series carries the open value (1.0).
        state_lines = [
            line
            for line in output.splitlines()
            if line.startswith("baldur_circuit_breaker_state")
            and "bridge_test_svc" in line
        ]
        assert state_lines, "cb-state series for the driven service is missing"
        assert state_lines[0].endswith(" 1.0")

    def test_state_change_records_without_swallowed_failure(
        self, otel_prometheus_bridge
    ):
        """Driving the handler under the real meter emits no *_failed event."""
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            CircuitBreakerEventHandler.on_state_changed(
                "bridge_test_svc", "closed", "open"
            )

        failed = [e for e in logs if str(e.get("event", "")).endswith("_failed")]
        assert failed == []
