"""
OpenTelemetry Settings Configuration.

Provides environment-based configuration for OpenTelemetry SDK.
Supports adaptive sampling based on EmergencyLevel.
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import COMMON_SETTINGS_CONFIG


class OpenTelemetrySettings(BaseSettings):
    """
    OpenTelemetry SDK configuration via environment variables.

    All settings follow OTEL_* naming convention for consistency
    with OpenTelemetry SDK environment variable standards.
    """

    model_config = COMMON_SETTINGS_CONFIG

    # Core Settings
    service_name: str = Field(
        default="baldur",
        validation_alias="OTEL_SERVICE_NAME",
        description="Service name for telemetry identification",
    )

    # Exporter Settings
    exporter_otlp_endpoint: str = Field(
        default="http://localhost:4317",
        validation_alias="OTEL_EXPORTER_OTLP_ENDPOINT",
        description="OTLP Collector endpoint URL",
    )

    exporter_otlp_timeout_ms: int = Field(
        default=5000,
        validation_alias="OTEL_EXPORTER_OTLP_TIMEOUT",
        description="OTLP exporter timeout in milliseconds",
    )

    traces_exporter: Literal["otlp", "console", "none"] = Field(
        default="otlp",
        validation_alias="OTEL_TRACES_EXPORTER",
        description=(
            "Span exporter selection. 'otlp' (default) batches to the OTLP "
            "collector endpoint; 'console' writes spans synchronously to stdout "
            "(dev visibility without a collector); 'none' keeps the "
            "TracerProvider, sampler, and composite propagator live but exports "
            "nothing (silences no-collector failure noise while preserving "
            "in-process span context and trace_id-in-logs correlation)."
        ),
    )

    # Sampling Settings
    traces_sampler: Literal[
        "always_on",
        "always_off",
        "traceidratio",
        "parentbased_always_on",
        "parentbased_always_off",
        "parentbased_traceidratio",
    ] = Field(
        default="parentbased_traceidratio",
        validation_alias="OTEL_TRACES_SAMPLER",
        description="Sampling strategy",
    )

    traces_sampler_arg: float = Field(
        default=0.01,
        validation_alias="OTEL_TRACES_SAMPLER_ARG",
        ge=0.0,
        le=1.0,
        description="Sampling ratio (0.0-1.0). Default 1% (0.01)",
    )

    # Adaptive Sampling
    adaptive_sampling_enabled: bool = Field(
        default=True,
        validation_alias="OTEL_ADAPTIVE_SAMPLING_ENABLED",
        description="Enable emergency-level based adaptive sampling",
    )

    # Resource Attributes
    resource_attributes: str = Field(
        default="",
        validation_alias="OTEL_RESOURCE_ATTRIBUTES",
        description="Comma-separated key=value pairs for resource attributes",
    )

    # Django Instrumentation Settings
    django_instrument_enabled: bool = Field(
        default=True,
        validation_alias="OTEL_DJANGO_INSTRUMENT_ENABLED",
        description="Enable automatic Django request/response instrumentation",
    )

    # Excluded URLs (for health checks, etc.)
    excluded_urls: str = Field(
        default="/health,/health/,/health/ready,/health/live,/health/l3,/metrics",
        validation_alias="OTEL_EXCLUDED_URLS",
        description="Comma-separated URL paths to exclude from tracing",
    )

    def get_excluded_urls_list(self) -> list[str]:
        """Get excluded URLs as a list."""
        if not self.excluded_urls:
            return []
        return [url.strip() for url in self.excluded_urls.split(",") if url.strip()]

    def get_resource_attributes_dict(self) -> dict[str, str]:
        """Parse resource_attributes string into a dictionary."""
        if not self.resource_attributes:
            return {}

        result = {}
        for pair in self.resource_attributes.split(","):
            if "=" in pair:
                key, value = pair.split("=", 1)
                result[key.strip()] = value.strip()
        return result


# Global settings instance (cached)


def get_otel_settings() -> "OpenTelemetrySettings":
    from baldur.settings.root import get_config

    return get_config().obs.otel


def reset_otel_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().obs.__dict__["otel"]
    except KeyError:
        pass
