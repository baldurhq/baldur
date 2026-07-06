"""Unit tests for ``baldur.api.middleware.http_metrics`` (649).

Scope:
    - ``record_http_red``: the framework-free HTTP RED (Rate/Errors/Duration)
      recorder the Flask and FastAPI adapters call for Django parity on the
      ``baldur_http_request_duration_seconds`` histogram. Covers the 500 error
      cutoff (Rate+Duration always, Errors only on 5xx), the ``error_type``
      override the unhandled-exception path uses, the metrics-enabled gate
      (including the settings-lookup fail-open default-``True``), the recorder
      fail-open swallow, and backend dispatch onto the OTel backend's ``infra``
      recorder (SC4).

``record_http_red`` resolves both the layered metrics snapshot
(``get_layered_settings_cached``) and the ``record_http_request`` /
``record_http_error`` convenience functions via lazy imports inside the function
body, so the gate is toggled by patching
``baldur.settings.layered_provider.get_layered_settings_cached`` and the recorders
by patching ``baldur.metrics.prometheus.record_http_{request,error}``. The backend-dispatch
tests instead let the real convenience functions run and route them onto an
``OTELBaldurMetrics`` instance configured as the active metrics backend, proving
the series populate under OTel (not just prometheus) — the backend-agnostic
guarantee that decouples this work from doc 648.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.api.middleware.http_metrics import record_http_red

# =============================================================================
# record_http_red — Behavior
# =============================================================================


class TestRecordHttpRedBehavior:
    """The RED triplet: Rate+Duration always, Errors only on 5xx, fail-open."""

    @pytest.mark.parametrize("status_code", [200, 201, 204, 301, 404, 499])
    def test_below_500_records_request_only(self, status_code):
        """2xx/3xx/4xx feed Rate+Duration but never the Errors series.

        Boundary: 499 is the last status below the 5xx error cutoff.
        """
        with (
            patch("baldur.metrics.prometheus.record_http_request") as m_req,
            patch("baldur.metrics.prometheus.record_http_error") as m_err,
        ):
            record_http_red("GET", "/users/<int:uid>", status_code, 0.5)

        m_req.assert_called_once_with("GET", "/users/<int:uid>", status_code, 0.5)
        m_err.assert_not_called()

    @pytest.mark.parametrize("status_code", [500, 502, 503, 599])
    def test_5xx_records_request_and_error(self, status_code):
        """5xx feeds both Rate+Duration and the Errors series (default HTTP_<code>).

        Boundary: 500 is the first status at/above the error cutoff.
        """
        with (
            patch("baldur.metrics.prometheus.record_http_request") as m_req,
            patch("baldur.metrics.prometheus.record_http_error") as m_err,
        ):
            record_http_red("POST", "/pay", status_code, 1.0)

        m_req.assert_called_once_with("POST", "/pay", status_code, 1.0)
        m_err.assert_called_once_with("POST", "/pay", f"HTTP_{status_code}")

    def test_explicit_error_type_overrides_http_code_default(self):
        """The unhandled-exception caller passes type(exc).__name__ as the label."""
        with (
            patch("baldur.metrics.prometheus.record_http_request"),
            patch("baldur.metrics.prometheus.record_http_error") as m_err,
        ):
            record_http_red("GET", "/x", 500, 0.2, error_type="ValueError")

        m_err.assert_called_once_with("GET", "/x", "ValueError")

    def test_disabled_gate_makes_no_record(self):
        """MetricsSettings.enabled=False silences the recorder (Django parity)."""
        with (
            patch("baldur.metrics.prometheus.record_http_request") as m_req,
            patch("baldur.metrics.prometheus.record_http_error") as m_err,
            patch(
                "baldur.settings.layered_provider.get_layered_settings_cached",
                return_value=MagicMock(enabled=False),
            ),
        ):
            record_http_red("GET", "/x", 200, 0.5)

        m_req.assert_not_called()
        m_err.assert_not_called()

    def test_enabled_gate_records(self):
        """enabled=True (the default) lets the record through (gate paired)."""
        with (
            patch("baldur.metrics.prometheus.record_http_request") as m_req,
            patch(
                "baldur.settings.layered_provider.get_layered_settings_cached",
                return_value=MagicMock(enabled=True),
            ),
        ):
            record_http_red("GET", "/x", 200, 0.5)

        m_req.assert_called_once()

    def test_settings_lookup_failure_defaults_to_enabled(self):
        """A settings-layer error must not silence metrics (fail-open default-True)."""
        with (
            patch("baldur.metrics.prometheus.record_http_request") as m_req,
            patch(
                "baldur.settings.layered_provider.get_layered_settings_cached",
                side_effect=RuntimeError("settings down"),
            ),
        ):
            record_http_red("GET", "/x", 200, 0.5)

        m_req.assert_called_once()

    def test_recorder_failure_is_swallowed(self):
        """A recorder error must never propagate to the request (fail-open)."""
        with patch(
            "baldur.metrics.prometheus.record_http_request",
            side_effect=RuntimeError("recorder down"),
        ):
            # Must not raise — metric recording is best-effort.
            record_http_red("GET", "/x", 200, 0.5)

    def test_error_recorder_failure_is_swallowed(self):
        """A 5xx whose error recorder raises is also swallowed (no propagation)."""
        with (
            patch("baldur.metrics.prometheus.record_http_request"),
            patch(
                "baldur.metrics.prometheus.record_http_error",
                side_effect=RuntimeError("error recorder down"),
            ),
        ):
            # Must not raise even though the Errors-series write failed.
            record_http_red("GET", "/x", 500, 0.5)


# =============================================================================
# record_http_red — backend dispatch (SC4)
# =============================================================================


class TestRecordHttpRedBackendDispatch:
    """SC4: the RED series populate under the OTel backend, not just prometheus.

    ``record_http_red`` routes through the ``baldur.metrics.prometheus``
    convenience functions, which dispatch to whatever ``get_metrics()`` returns.
    Configuring an ``OTELBaldurMetrics`` instance as the active backend and
    spying on its ``infra`` recorder proves the call reaches the OTel family —
    the backend-agnostic guarantee that decouples this from doc 648.
    """

    @pytest.fixture
    def otel_backend(self):
        """Configure an initialized OTel backend as the active metrics singleton.

        A MagicMock meter drives ``_initialized=True`` (the reused prometheus
        recorders never touch the meter), and ``reset_metrics`` restores the lazy
        default singleton afterwards so the override never leaks into other tests.
        """
        from baldur.metrics import prometheus as prom
        from baldur.metrics.otel_backend import OTELBaldurMetrics

        with patch("baldur.observability.get_meter", return_value=MagicMock()):
            backend = OTELBaldurMetrics()
        prom.configure_metrics(backend)
        try:
            yield backend
        finally:
            prom.reset_metrics()

    def test_2xx_dispatches_to_otel_infra_recorder(self, otel_backend):
        """A 2xx advances the OTel infra Rate+Duration series (not the Errors one)."""
        with (
            patch.object(otel_backend.infra, "record_http_request") as m_req,
            patch.object(otel_backend.infra, "record_http_error") as m_err,
        ):
            record_http_red("GET", "/items/{item_id}", 200, 0.3)

        m_req.assert_called_once_with("GET", "/items/{item_id}", 200, 0.3)
        m_err.assert_not_called()

    def test_5xx_dispatches_error_to_otel_infra_recorder(self, otel_backend):
        """A 5xx advances both OTel infra Rate+Duration and Errors series."""
        with (
            patch.object(otel_backend.infra, "record_http_request") as m_req,
            patch.object(otel_backend.infra, "record_http_error") as m_err,
        ):
            record_http_red("GET", "/items/{item_id}", 500, 0.3)

        m_req.assert_called_once_with("GET", "/items/{item_id}", 500, 0.3)
        m_err.assert_called_once_with("GET", "/items/{item_id}", "HTTP_500")
