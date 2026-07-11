"""
Unit tests for bootstrap Prometheus metrics-provider registration.

``_register_metrics_provider_if_configured()`` is the settings-gated init step
that auto-registers the remote-Prometheus TimeSeriesMetricsProvider when
``BALDUR_PROMETHEUS_URL`` is set. Unset URL is the off switch (honest skip, no
provider, no new WARNING); a set URL registers the provider (with a scoping
WARNING when no static selector is configured); a malformed config fails open
with a WARNING rather than aborting startup.

Target: baldur.bootstrap._register_metrics_provider_if_configured
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import structlog

from baldur.bootstrap import _register_metrics_provider_if_configured
from baldur.services.config_shadow.metrics_provider import (
    MockTimeSeriesProvider,
    get_metrics_provider,
    is_metrics_provider_registered,
    reset_metrics_provider,
)
from baldur.services.config_shadow.providers.prometheus import (
    PrometheusTimeSeriesProvider,
)
from baldur.settings.prometheus import PrometheusSettings

_SETTINGS_GETTER = "baldur.settings.prometheus.get_prometheus_settings"


def _events(logs) -> list[str]:
    return [entry["event"] for entry in logs]


def _levels(logs) -> list[str]:
    return [entry["log_level"] for entry in logs]


@pytest.fixture(autouse=True)
def _reset_provider():
    reset_metrics_provider()
    yield
    reset_metrics_provider()


class TestPrometheusBootstrapRegistrationBehavior:
    """_register_metrics_provider_if_configured() flag/registration behavior."""

    def test_no_url_configured_skips_without_registration_or_warning(self):
        """SC2 — no URL (unconfigured): honest skip, lazy Mock stays, no WARNING."""
        with (
            patch(_SETTINGS_GETTER, return_value=PrometheusSettings(url="")),
            structlog.testing.capture_logs() as logs,
        ):
            _register_metrics_provider_if_configured()

        # Not registered; get_metrics_provider() still returns the lazy Mock.
        assert is_metrics_provider_registered() is False
        assert isinstance(get_metrics_provider(), MockTimeSeriesProvider)
        # Honest skip logged; no registered event, no WARNING-level event.
        assert "prometheus_provider.registration_skipped" in _events(logs)
        assert "prometheus_provider.registered" not in _events(logs)
        assert "warning" not in _levels(logs)

    def test_set_url_registers_prometheus_provider(self):
        settings = PrometheusSettings(
            url="http://prometheus:9090",
            extra_label_selectors={"namespace": "prod"},
        )
        with (
            patch(_SETTINGS_GETTER, return_value=settings),
            structlog.testing.capture_logs() as logs,
        ):
            _register_metrics_provider_if_configured()

        assert is_metrics_provider_registered() is True
        assert isinstance(get_metrics_provider(), PrometheusTimeSeriesProvider)
        assert "prometheus_provider.registered" in _events(logs)

    def test_set_url_without_selectors_warns_scoping_unset(self):
        settings = PrometheusSettings(url="http://prometheus:9090")
        with (
            patch(_SETTINGS_GETTER, return_value=settings),
            structlog.testing.capture_logs() as logs,
        ):
            _register_metrics_provider_if_configured()

        assert is_metrics_provider_registered() is True
        assert "prometheus_provider.scoping_unset" in _events(logs)

    def test_set_url_with_selectors_does_not_warn_scoping(self):
        settings = PrometheusSettings(
            url="http://prometheus:9090",
            extra_label_selectors={"namespace": "prod"},
        )
        with (
            patch(_SETTINGS_GETTER, return_value=settings),
            structlog.testing.capture_logs() as logs,
        ):
            _register_metrics_provider_if_configured()

        assert "prometheus_provider.scoping_unset" not in _events(logs)

    def test_malformed_config_fails_open_with_warning(self):
        """A pydantic ValidationError at settings load is surfaced as a WARNING;
        boot continues and nothing is registered (fail-open)."""

        def _raise_validation_error():
            # An invalid scheme raises a real pydantic ValidationError.
            return PrometheusSettings(url="ftp://prometheus:9090")

        with (
            patch(_SETTINGS_GETTER, side_effect=_raise_validation_error),
            structlog.testing.capture_logs() as logs,
        ):
            _register_metrics_provider_if_configured()  # must not raise

        assert is_metrics_provider_registered() is False
        assert "prometheus_provider.registration_failed" in _events(logs)
        assert "warning" in _levels(logs)
