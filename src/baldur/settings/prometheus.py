"""
Prometheus Metrics-Source Settings — Pydantic v2.

Connection surface for the remote-Prometheus time-series metrics provider
that backs canary live evaluation. This is the source-of-truth Prometheus
connection config (URL, auth headers, TLS, timeout, retry) and the query
naming/scoping surface (naming preset + per-field overrides + static label
selectors).

An empty ``url`` means "not configured": no provider is registered at
startup and behaviour is unchanged (fail-safe default).

Environment Variables:
    BALDUR_PROMETHEUS_URL=http://prometheus:9090
    BALDUR_PROMETHEUS_HEADERS='{"Authorization": "Bearer ...", "X-Scope-OrgID": "tenant-a"}'
    BALDUR_PROMETHEUS_TLS_VERIFY=true
    BALDUR_PROMETHEUS_TLS_CA_CERT=/etc/ssl/certs/prometheus-ca.pem
    BALDUR_PROMETHEUS_TIMEOUT_SECONDS=5.0
    BALDUR_PROMETHEUS_RETRY_TOTAL=1
    BALDUR_PROMETHEUS_RETRY_BACKOFF_FACTOR=0.5
    BALDUR_PROMETHEUS_METRIC_NAMING=baldur
    BALDUR_PROMETHEUS_EXTRA_LABEL_SELECTORS='{"namespace": "prod"}'
    BALDUR_PROMETHEUS_SERVICE_LABEL=
    BALDUR_PROMETHEUS_REQUESTS_TOTAL_METRIC=
    BALDUR_PROMETHEUS_DURATION_HISTOGRAM_METRIC=
    BALDUR_PROMETHEUS_STATUS_CODE_LABEL=
    BALDUR_PROMETHEUS_ERROR_STATUS_REGEX=5..
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class PrometheusSettings(BaseSettings):
    """Remote-Prometheus connection and query-mapping configuration.

    Consumed by the query client (connection fields) and the time-series
    metrics provider (naming/scoping fields). Per-feature Prometheus configs
    (cell topology, postmortem) keep their own homes; this is the connection
    surface for canary live evaluation.
    """

    model_config = make_settings_config("BALDUR_PROMETHEUS_")

    # --- Connection ---

    url: str = Field(
        default="",
        description=(
            "Prometheus (or PromQL-compatible backend) HTTP API base URL, e.g. "
            "http://prometheus:9090. Empty means not configured — no provider "
            "is registered and behaviour is unchanged."
        ),
    )

    headers: dict[str, SecretStr] = Field(
        default_factory=dict,
        description=(
            "Per-request headers sent on every query (bearer/basic auth, "
            "X-Scope-OrgID tenancy). Values are credentials — SecretStr "
            "repr-masked, never logged."
        ),
    )

    tls_verify: bool = Field(
        default=True,
        description="Verify the server TLS certificate. Disable only for self-signed dev setups.",
    )

    tls_ca_cert: str = Field(
        default="",
        description=(
            "Path to a CA bundle used to verify the server certificate. When "
            "set and tls_verify is on, it is passed as the requests verify value."
        ),
    )

    timeout_seconds: float = Field(
        default=5.0,
        ge=0.5,
        le=8.0,
        description=(
            "Per-request timeout in seconds. Bounded so the sequential queries "
            "of one evaluation fit inside the promotion-gate cadence."
        ),
    )

    retry_total: int = Field(
        default=1,
        ge=0,
        le=3,
        description=(
            "Bounded retry budget for transient upstream 5xx (502/503/504) "
            "responses only. Connection and read-timeout failures are never "
            "retried — a down or slow Prometheus waits for the next gate."
        ),
    )

    retry_backoff_factor: float = Field(
        default=0.5,
        ge=0.0,
        le=5.0,
        description="urllib3 backoff factor (seconds) between transient-5xx retries.",
    )

    # --- Query naming / scoping ---

    metric_naming: Literal["baldur", "otel"] = Field(
        default="baldur",
        description=(
            "Metric-naming preset. 'baldur' targets the baldur_http_* RED "
            "metrics recorded zero-config by the framework adapters; 'otel' "
            "targets the OTel HTTP-server semantic-convention histogram as "
            "translated by Prometheus-side OTLP ingestion."
        ),
    )

    extra_label_selectors: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Static label matchers appended to every query — the multi-service "
            "scoping mechanism. Multi-service clusters MUST set this so a "
            "verdict is not computed over blended cross-service traffic."
        ),
    )

    service_label: str = Field(
        default="",
        description=(
            "Label name the service_name argument is injected under. Empty "
            "means service_name is NOT injected (the canary evaluator passes a "
            "config-type identifier, not a Prometheus job/service label)."
        ),
    )

    requests_total_metric: str = Field(
        default="",
        description="Override for the request-counter metric name. Empty uses the preset default.",
    )

    duration_histogram_metric: str = Field(
        default="",
        description=(
            "Override for the request-duration histogram base name (without the "
            "_bucket suffix). Empty uses the preset default."
        ),
    )

    status_code_label: str = Field(
        default="",
        description="Override for the HTTP status-code label name. Empty uses the preset default.",
    )

    error_status_regex: str = Field(
        default="5..",
        description="Regex matched against the status-code label to classify an error response.",
    )

    @field_validator("url")
    @classmethod
    def _validate_url_scheme(cls, v: str) -> str:
        v = v.strip()
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("Prometheus url must start with http:// or https://")
        return v


# ==========================================================================
# Singleton management
# ==========================================================================
def get_prometheus_settings() -> PrometheusSettings:
    """Get cached PrometheusSettings instance."""
    from baldur.runtime import get_runtime

    return get_runtime().get_settings(PrometheusSettings)


def reset_prometheus_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.runtime import get_runtime

    get_runtime().reset_settings(PrometheusSettings)


__all__ = [
    "PrometheusSettings",
    "get_prometheus_settings",
    "reset_prometheus_settings",
]
