"""
Unit tests for PrometheusSettings — remote-Prometheus metrics-source config.

Test classification (UNIT_TEST_GUIDELINES §0):
- Contract: hardcoded default/constraint values from the design spec.
- Behavior: url scheme validation, credential masking, singleton lifecycle,
  and env-var overrides computed against source behavior.

Target: baldur.settings.prometheus
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings.prometheus import (
    PrometheusSettings,
    get_prometheus_settings,
    reset_prometheus_settings,
)


@pytest.fixture(autouse=True)
def _reset_settings():
    """Reset the runtime-cached singleton before and after each test."""
    reset_prometheus_settings()
    yield
    reset_prometheus_settings()


class TestPrometheusSettingsContract:
    """PrometheusSettings design-contract default values."""

    def test_url_default_empty(self):
        """url default '' means not configured (fail-safe: no provider wired)."""
        assert PrometheusSettings().url == ""

    def test_headers_default_empty(self):
        """headers default is an empty dict."""
        assert PrometheusSettings().headers == {}

    def test_tls_verify_default_true(self):
        """tls_verify default True (verify server certificate)."""
        assert PrometheusSettings().tls_verify is True

    def test_tls_ca_cert_default_empty(self):
        """tls_ca_cert default ''."""
        assert PrometheusSettings().tls_ca_cert == ""

    def test_timeout_seconds_default_5(self):
        """timeout_seconds default 5.0."""
        assert PrometheusSettings().timeout_seconds == pytest.approx(5.0)

    def test_retry_total_default_1(self):
        """retry_total default 1 (bounded transient-5xx retry budget)."""
        assert PrometheusSettings().retry_total == 1

    def test_retry_backoff_factor_default_0_5(self):
        """retry_backoff_factor default 0.5."""
        assert PrometheusSettings().retry_backoff_factor == pytest.approx(0.5)

    def test_metric_naming_default_baldur(self):
        """metric_naming default 'baldur' preset."""
        assert PrometheusSettings().metric_naming == "baldur"

    def test_extra_label_selectors_default_empty(self):
        """extra_label_selectors default is an empty dict."""
        assert PrometheusSettings().extra_label_selectors == {}

    def test_service_label_default_empty(self):
        """service_label default '' (service_name not injected — config_type trap)."""
        assert PrometheusSettings().service_label == ""

    def test_requests_total_metric_default_empty(self):
        """requests_total_metric default '' (uses preset default)."""
        assert PrometheusSettings().requests_total_metric == ""

    def test_duration_histogram_metric_default_empty(self):
        """duration_histogram_metric default '' (uses preset default)."""
        assert PrometheusSettings().duration_histogram_metric == ""

    def test_status_code_label_default_empty(self):
        """status_code_label default '' (uses preset default)."""
        assert PrometheusSettings().status_code_label == ""

    def test_error_status_regex_default_5xx(self):
        """error_status_regex default '5..'."""
        assert PrometheusSettings().error_status_regex == "5.."

    def test_env_prefix_is_baldur_prometheus(self):
        """env_prefix contract: BALDUR_PROMETHEUS_."""
        assert PrometheusSettings.model_config["env_prefix"] == "BALDUR_PROMETHEUS_"


class TestPrometheusSettingsBoundary:
    """Field constraint boundary analysis (just-inside pass / just-outside fail)."""

    # --- timeout_seconds: ge=0.5, le=8.0 ---

    def test_timeout_below_minimum_raises(self):
        with pytest.raises(ValidationError):
            PrometheusSettings(timeout_seconds=0.49)

    def test_timeout_at_minimum_succeeds(self):
        assert PrometheusSettings(timeout_seconds=0.5).timeout_seconds == pytest.approx(
            0.5
        )

    def test_timeout_at_maximum_succeeds(self):
        assert PrometheusSettings(timeout_seconds=8.0).timeout_seconds == pytest.approx(
            8.0
        )

    def test_timeout_above_maximum_raises(self):
        with pytest.raises(ValidationError):
            PrometheusSettings(timeout_seconds=8.01)

    # --- retry_total: ge=0, le=3 ---

    def test_retry_total_below_minimum_raises(self):
        with pytest.raises(ValidationError):
            PrometheusSettings(retry_total=-1)

    def test_retry_total_at_zero_succeeds(self):
        assert PrometheusSettings(retry_total=0).retry_total == 0

    def test_retry_total_at_maximum_succeeds(self):
        assert PrometheusSettings(retry_total=3).retry_total == 3

    def test_retry_total_above_maximum_raises(self):
        with pytest.raises(ValidationError):
            PrometheusSettings(retry_total=4)

    # --- retry_backoff_factor: ge=0.0, le=5.0 ---

    def test_retry_backoff_below_minimum_raises(self):
        with pytest.raises(ValidationError):
            PrometheusSettings(retry_backoff_factor=-0.1)

    def test_retry_backoff_at_zero_succeeds(self):
        assert PrometheusSettings(
            retry_backoff_factor=0.0
        ).retry_backoff_factor == pytest.approx(0.0)

    def test_retry_backoff_at_maximum_succeeds(self):
        assert PrometheusSettings(
            retry_backoff_factor=5.0
        ).retry_backoff_factor == pytest.approx(5.0)

    def test_retry_backoff_above_maximum_raises(self):
        with pytest.raises(ValidationError):
            PrometheusSettings(retry_backoff_factor=5.1)


class TestPrometheusSettingsUrlValidatorBehavior:
    """url scheme validator (_validate_url_scheme)."""

    def test_http_scheme_accepted(self):
        assert PrometheusSettings(url="http://prometheus:9090").url == (
            "http://prometheus:9090"
        )

    def test_https_scheme_accepted(self):
        assert PrometheusSettings(url="https://prom.example:9090").url == (
            "https://prom.example:9090"
        )

    def test_empty_url_accepted(self):
        """Empty url stays empty (the not-configured sentinel)."""
        assert PrometheusSettings(url="").url == ""

    def test_missing_scheme_rejected(self):
        """A bare host without scheme is rejected."""
        with pytest.raises(ValidationError):
            PrometheusSettings(url="prometheus:9090")

    def test_non_http_scheme_rejected(self):
        with pytest.raises(ValidationError):
            PrometheusSettings(url="ftp://prometheus:9090")

    def test_surrounding_whitespace_stripped(self):
        """The validator strips surrounding whitespace before the scheme check."""
        assert PrometheusSettings(url="  http://prometheus:9090  ").url == (
            "http://prometheus:9090"
        )


class TestPrometheusSettingsBehavior:
    """Naming presets, credential masking, and singleton lifecycle."""

    def test_metric_naming_otel_accepted(self):
        assert PrometheusSettings(metric_naming="otel").metric_naming == "otel"

    def test_metric_naming_invalid_rejected(self):
        """metric_naming is a Literal['baldur','otel'] — other values reject."""
        with pytest.raises(ValidationError):
            PrometheusSettings(metric_naming="datadog")

    def test_header_values_repr_masked(self):
        """SecretStr header values never appear in the repr (credential safety)."""
        settings = PrometheusSettings(headers={"Authorization": "Bearer topsecret"})
        assert "topsecret" not in repr(settings)

    def test_header_secret_value_recoverable(self):
        """get_secret_value() recovers the header credential for transport."""
        settings = PrometheusSettings(headers={"Authorization": "Bearer topsecret"})
        assert settings.headers["Authorization"].get_secret_value() == (
            "Bearer topsecret"
        )

    def test_env_override_url(self, monkeypatch):
        monkeypatch.setenv("BALDUR_PROMETHEUS_URL", "http://prom-env:9090")
        assert PrometheusSettings().url == "http://prom-env:9090"

    def test_env_override_timeout_seconds(self, monkeypatch):
        monkeypatch.setenv("BALDUR_PROMETHEUS_TIMEOUT_SECONDS", "2.5")
        assert PrometheusSettings().timeout_seconds == pytest.approx(2.5)

    def test_env_override_metric_naming(self, monkeypatch):
        monkeypatch.setenv("BALDUR_PROMETHEUS_METRIC_NAMING", "otel")
        assert PrometheusSettings().metric_naming == "otel"

    def test_env_override_headers_parsed_as_secret(self, monkeypatch):
        """A JSON headers env var parses into SecretStr-wrapped values."""
        monkeypatch.setenv("BALDUR_PROMETHEUS_HEADERS", '{"X-Scope-OrgID": "tenant-a"}')
        settings = PrometheusSettings()
        assert settings.headers["X-Scope-OrgID"].get_secret_value() == "tenant-a"

    def test_env_override_extra_label_selectors(self, monkeypatch):
        monkeypatch.setenv(
            "BALDUR_PROMETHEUS_EXTRA_LABEL_SELECTORS", '{"namespace": "prod"}'
        )
        assert PrometheusSettings().extra_label_selectors == {"namespace": "prod"}

    def test_singleton_returns_same_instance(self):
        assert get_prometheus_settings() is get_prometheus_settings()

    def test_reset_clears_singleton(self):
        first = get_prometheus_settings()
        reset_prometheus_settings()
        assert get_prometheus_settings() is not first
