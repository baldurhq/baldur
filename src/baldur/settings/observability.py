"""
Observability Profile Settings - Pydantic v2.

A single declarative selector (``BALDUR_OBSERVABILITY_PROFILE``) chooses the
observability export backend, collapsing the former three-way env split
(``BALDUR_AUTO_CONFIG_OTEL`` / ``OTEL_ENABLED`` / ``BALDUR_METRICS_BACKEND``)
into one knob.

``auto`` resolves once at boot to the OTLP-collector backend when the
OpenTelemetry trace SDK and the Prometheus metric bridge are both importable,
and to local Prometheus otherwise. Every selection emits an explicit
``observability.profile_resolved`` log so the backend choice is never silent.

Vendor profiles (Datadog / Grafana Cloud) are PRO-only and live in a parallel
enum; selecting one in an OSS deployment fails loudly at settings construction.

Environment Variables:
    BALDUR_OBSERVABILITY_PROFILE=auto   # auto | local | otel_collector
    OTEL_SDK_DISABLED=true              # OTel SDK standard mute (honored here)
"""

from __future__ import annotations

import os
from enum import Enum
from functools import cached_property
from typing import Any

import structlog
from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

logger = structlog.get_logger()

__all__ = [
    "PROFILE_DEFAULTS",
    "ObservabilityProfile",
    "ObservabilitySettings",
    "get_observability_settings",
    "reset_observability_settings",
]


class ObservabilityProfile(str, Enum):
    """Declarative observability export backend selector (OSS members).

    AUTO is resolved at boot from SDK availability. Vendor values
    (``datadog`` / ``grafana_cloud``) are not OSS members — selecting one fails
    with a Pydantic ValidationError, which is fail-closed and consistent with
    "Silent degradation forbidden".
    """

    AUTO = "auto"  # resolved at boot from SDK availability
    LOCAL = "local"  # prometheus_client direct, no OTel export
    OTEL_COLLECTOR = "otel_collector"  # generic OTLP gRPC export


# Static per-profile expansion. AUTO is resolved at runtime, not listed here.
# Only the single ``otel_enabled`` key is declared: ``effective_backend`` is
# derived (not a static key) so the metrics backend can never diverge into the
# silent-metric-loss state where backend=="otel" while OTel is muted or the
# prometheus bridge is absent.
PROFILE_DEFAULTS: dict[ObservabilityProfile, dict[str, Any]] = {
    ObservabilityProfile.LOCAL: {"otel_enabled": False},
    ObservabilityProfile.OTEL_COLLECTOR: {"otel_enabled": True},
}


def _otel_sdk_muted_by_standard_env() -> bool:
    """True when the OTel SDK standard ``OTEL_SDK_DISABLED`` env var is set true.

    The OTel SDK honors this independently of Baldur's profile by emitting NoOp
    providers; mirroring it keeps the reported backend honest and avoids silent
    metric loss when Baldur would otherwise report an OTel metrics backend.
    """
    return os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true"


class ObservabilitySettings(BaseSettings):
    """Observability export backend configuration.

    ``profile`` is the single operator-facing knob; the ``effective_*``
    properties expand it into the concrete enable/backend signals consumed by
    the OTel init path and the metrics factory. The ``effective_*`` properties
    are cached per instance; ``reset_observability_settings()`` reconstructs the
    instance to clear them.
    """

    model_config = make_settings_config("BALDUR_OBSERVABILITY_")

    profile: ObservabilityProfile = Field(
        default=ObservabilityProfile.AUTO,
        description="Observability export backend selector",
    )

    @cached_property
    def effective_profile(self) -> ObservabilityProfile:
        """Resolve AUTO to a concrete profile; pass non-AUTO through unchanged.

        AUTO resolves to OTEL_COLLECTOR only when both the OTel trace SDK and
        the Prometheus metric bridge are importable, otherwise LOCAL. The probe
        imports are lazy (inside this body) to avoid a settings<->observability
        import cycle. Pure: no log side effect (resolution-log emission lives in
        ``model_post_init``).
        """
        if self.profile is not ObservabilityProfile.AUTO:
            return self.profile

        from baldur.observability import (
            _is_otel_available,
            _is_otel_meter_available,
        )

        if _is_otel_available() and _is_otel_meter_available():
            return ObservabilityProfile.OTEL_COLLECTOR
        return ObservabilityProfile.LOCAL

    @cached_property
    def effective_otel_enabled(self) -> bool:
        """Whether the OTel SDK export path should be initialized.

        Reads the resolved profile's ``otel_enabled``, forced False when the
        OTel SDK standard ``OTEL_SDK_DISABLED`` env var mutes the SDK. Traces
        (which need no metric bridge) stay governed by this single signal.
        """
        if _otel_sdk_muted_by_standard_env():
            return False
        return bool(PROFILE_DEFAULTS[self.effective_profile]["otel_enabled"])

    @cached_property
    def effective_backend(self) -> str:
        """Metrics backend, derived so it can never select a dead OTel meter.

        Returns ``"otel"`` only when OTel export is enabled AND the Prometheus
        metric bridge is importable; otherwise degrades to ``"prometheus"`` so
        the metrics factory builds a live recorder instead of an OTel recorder
        whose ``None`` meter would drop every record. The bridge probe is read
        at call time (lazy import) so it stays monkeypatchable.

        Invariant: ``effective_backend == "otel"`` iff
        ``effective_otel_enabled and _is_otel_meter_available()``.
        """
        from baldur.observability import _is_otel_meter_available

        if self.effective_otel_enabled and _is_otel_meter_available():
            return "otel"
        return "prometheus"

    def model_post_init(self, __context: Any) -> None:
        """Emit the one-time resolution log (+ SDK-mute warning) at construction.

        ``model_post_init`` runs exactly once after Pydantic validation, so the
        resolution log emits once per instance for every profile selection (not
        only AUTO), without the surprise side effect of a property getter firing
        on IDE/REPL inspection.
        """
        resolved = self.effective_profile
        if self.profile is ObservabilityProfile.AUTO:
            reason = (
                "auto_resolved_otel_sdk_available"
                if resolved is ObservabilityProfile.OTEL_COLLECTOR
                else "auto_resolved_fallback_local"
            )
        else:
            reason = "explicit_profile"

        logger.info(
            "observability.profile_resolved",
            raw_profile=self.profile.value,
            resolved_profile=resolved.value,
            reason=reason,
        )

        # When the SDK is muted but the resolved profile would otherwise enable
        # OTel, the metrics backend silently downgrades otel -> prometheus.
        # Surface that downgrade so operators understand the mismatch.
        if (
            _otel_sdk_muted_by_standard_env()
            and PROFILE_DEFAULTS[resolved]["otel_enabled"]
        ):
            logger.warning(
                "observability.otel_sdk_disabled_by_standard_env",
                resolved_profile=resolved.value,
            )


# =============================================================================
# Singleton management
# =============================================================================
def get_observability_settings() -> ObservabilitySettings:
    from baldur.settings.root import get_config

    return get_config().obs.profile


def reset_observability_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().obs.__dict__["profile"]
    except KeyError:
        pass
