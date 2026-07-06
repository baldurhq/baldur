"""Unit tests for the observability profile system (524).

Covers the single ``BALDUR_OBSERVABILITY_PROFILE`` selector that replaced the
former three-way env split (the separate Django auto-config, OTel SDK-enable,
and metrics-backend toggles):

- ``ObservabilityProfile`` enum (3 OSS members) + vendor-string rejection (D1)
- ``PROFILE_DEFAULTS`` structure — single ``otel_enabled`` key (D2)
- ``ObservabilitySettings`` resolving properties:
  ``effective_profile`` (AUTO resolution), ``effective_otel_enabled``
  (PROFILE_DEFAULTS + ``OTEL_SDK_DISABLED`` override), and the *derived*
  ``effective_backend`` (the 1.1 + AP-1 silent-metric-loss invariants) (D3–D7)
- the one-time ``observability.profile_resolved`` /
  ``observability.otel_sdk_disabled_by_standard_env`` log emission (D5/D7)
- ``_is_otel_meter_available`` import probe (D4)
- ``_create_metrics`` backend routing via ``effective_backend`` — re-homed
  from the deleted ``test_metrics_backend_settings.py`` (D6)
- ``get/reset_observability_settings`` singleton lifecycle (D8)

Resolution depends on monkeypatchable module-level probes
(``baldur.observability._is_otel_available`` / ``_is_otel_meter_available``);
every AUTO-resolution test pins both so the outcome is env-independent.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from baldur.settings.observability import (
    PROFILE_DEFAULTS,
    ObservabilityProfile,
    ObservabilitySettings,
    get_observability_settings,
    reset_observability_settings,
)


@pytest.fixture(autouse=True)
def _reset_observability_singleton():
    """Reconstruct the observability singleton before+after each test.

    Clears the ``@cached_property`` resolution caches (and re-runs the one-time
    ``model_post_init`` emission) so a profile resolved by one test cannot leak
    into the next — matches the ``tests/unit/settings/`` isolation precedent.
    """
    reset_observability_settings()
    yield
    reset_observability_settings()


# =============================================================================
# ObservabilityProfile enum — 3 OSS members (D1)
# =============================================================================
class TestObservabilityProfileContract:
    """``ObservabilityProfile`` is a 3-member ``(str, Enum)`` (design contract)."""

    def test_profile_members_are_exactly_auto_local_otel_collector(self):
        """Exactly three OSS members with the documented values (D1)."""
        assert {p.value for p in ObservabilityProfile} == {
            "auto",
            "local",
            "otel_collector",
        }

    def test_profile_value_equality_matches_contract(self):
        """Each member maps to its lowercase snake_case value (D1)."""
        assert ObservabilityProfile.AUTO.value == "auto"
        assert ObservabilityProfile.LOCAL.value == "local"
        assert ObservabilityProfile.OTEL_COLLECTOR.value == "otel_collector"

    def test_profile_is_str_subclass_for_json_serialization(self):
        """``(str, Enum)`` inheritance enables JSON serialization (project rule)."""
        assert issubclass(ObservabilityProfile, str)
        assert isinstance(ObservabilityProfile.OTEL_COLLECTOR, str)


# =============================================================================
# PROFILE_DEFAULTS — single otel_enabled key (D2)
# =============================================================================
class TestProfileDefaultsContract:
    """``PROFILE_DEFAULTS`` declares exactly the two static profiles (D2)."""

    def test_profile_defaults_keys_are_local_and_otel_collector_only(self):
        """AUTO is resolved at runtime, so only the two static profiles map (D2)."""
        assert set(PROFILE_DEFAULTS) == {
            ObservabilityProfile.LOCAL,
            ObservabilityProfile.OTEL_COLLECTOR,
        }

    def test_profile_defaults_each_entry_has_only_otel_enabled_key(self):
        """``backend`` is derived, not stored — each entry has one key (D2)."""
        assert all(set(v) == {"otel_enabled"} for v in PROFILE_DEFAULTS.values())

    def test_profile_defaults_otel_enabled_values_match_contract(self):
        """LOCAL disables OTel export; OTEL_COLLECTOR enables it (D2)."""
        assert PROFILE_DEFAULTS[ObservabilityProfile.LOCAL]["otel_enabled"] is False
        assert (
            PROFILE_DEFAULTS[ObservabilityProfile.OTEL_COLLECTOR]["otel_enabled"]
            is True
        )


# =============================================================================
# ObservabilitySettings construction — default + fail-closed vendor rejection (D1)
# =============================================================================
class TestObservabilitySettingsContract:
    """Field default + loud failure on a non-OSS (vendor) profile string."""

    def test_default_profile_is_auto(self, monkeypatch):
        """Unset ``BALDUR_OBSERVABILITY_PROFILE`` resolves the field default AUTO."""
        # The suite pins the env var to ``local`` for isolation; drop only that
        # key (surgically, not a clear=True snapshot that can unpin the suite for
        # xdist siblings) so construction observes the real field default (AUTO).
        monkeypatch.delenv("BALDUR_OBSERVABILITY_PROFILE", raising=False)
        settings = ObservabilitySettings()

        assert settings.profile is ObservabilityProfile.AUTO

    @pytest.mark.parametrize(
        "vendor_value",
        ["datadog", "grafana_cloud", "not_a_profile"],
        ids=["datadog", "grafana_cloud", "garbage"],
    )
    def test_vendor_or_unknown_profile_string_raises_validation_error(
        self, vendor_value
    ):
        """A non-OSS profile string fails loudly at construction (fail-closed, D1)."""
        with pytest.raises(ValidationError):
            ObservabilitySettings(profile=vendor_value)


# =============================================================================
# effective_profile — AUTO resolution truth table + passthrough (D4)
# =============================================================================
class TestEffectiveProfileBehavior:
    """``effective_profile`` resolves AUTO via the two import probes (D4)."""

    @pytest.mark.parametrize(
        ("otel_available", "meter_available", "expected"),
        [
            (True, True, ObservabilityProfile.OTEL_COLLECTOR),
            (True, False, ObservabilityProfile.LOCAL),
            (False, True, ObservabilityProfile.LOCAL),
            (False, False, ObservabilityProfile.LOCAL),
        ],
        ids=["both_present", "no_meter", "no_trace", "neither"],
    )
    def test_auto_resolution_requires_both_probes_for_otel_collector(
        self, otel_available, meter_available, expected
    ):
        """AUTO resolves to OTEL_COLLECTOR only when both probes pass (D4)."""
        # Given the probes report a specific availability combination
        with (
            patch(
                "baldur.observability._is_otel_available",
                return_value=otel_available,
            ),
            patch(
                "baldur.observability._is_otel_meter_available",
                return_value=meter_available,
            ),
        ):
            # When AUTO is resolved (cached at construction via model_post_init)
            settings = ObservabilitySettings(profile=ObservabilityProfile.AUTO)

            # Then the resolution follows the both-probe AND gate
            assert settings.effective_profile is expected

    @pytest.mark.parametrize(
        "explicit",
        [ObservabilityProfile.LOCAL, ObservabilityProfile.OTEL_COLLECTOR],
        ids=["local", "otel_collector"],
    )
    def test_explicit_profile_passes_through_without_probing(self, explicit):
        """A non-AUTO profile is returned unchanged and never consults a probe."""
        # Given the availability probes would raise if consulted
        with (
            patch("baldur.observability._is_otel_available") as m_avail,
            patch("baldur.observability._is_otel_meter_available") as m_meter,
        ):
            # When an explicit profile is resolved
            settings = ObservabilitySettings(profile=explicit)

            # Then it passes through and short-circuits before the probes
            assert settings.effective_profile is explicit
            m_avail.assert_not_called()
            m_meter.assert_not_called()


# =============================================================================
# effective_otel_enabled — PROFILE_DEFAULTS lookup + OTEL_SDK_DISABLED override (D7)
# =============================================================================
class TestEffectiveOtelEnabledBehavior:
    """``effective_otel_enabled`` = profile default, forced False when SDK muted."""

    def test_local_profile_disables_otel_export(self, monkeypatch):
        """LOCAL mirrors its PROFILE_DEFAULTS ``otel_enabled`` (False)."""
        monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
        settings = ObservabilitySettings(profile=ObservabilityProfile.LOCAL)

        assert (
            settings.effective_otel_enabled
            is PROFILE_DEFAULTS[ObservabilityProfile.LOCAL]["otel_enabled"]
        )

    def test_otel_collector_profile_enables_otel_export(self, monkeypatch):
        """OTEL_COLLECTOR mirrors its PROFILE_DEFAULTS ``otel_enabled`` (True)."""
        monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
        settings = ObservabilitySettings(profile=ObservabilityProfile.OTEL_COLLECTOR)

        assert (
            settings.effective_otel_enabled
            is PROFILE_DEFAULTS[ObservabilityProfile.OTEL_COLLECTOR]["otel_enabled"]
        )

    def test_otel_sdk_disabled_env_forces_disabled(self, monkeypatch):
        """``OTEL_SDK_DISABLED=true`` overrides an otherwise-enabled profile (D7)."""
        monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
        settings = ObservabilitySettings(profile=ObservabilityProfile.OTEL_COLLECTOR)

        assert settings.effective_otel_enabled is False

    def test_otel_sdk_disabled_false_does_not_force_disabled(self, monkeypatch):
        """Only the literal ``true`` token mutes — ``false`` leaves OTel enabled."""
        monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
        settings = ObservabilitySettings(profile=ObservabilityProfile.OTEL_COLLECTOR)

        assert settings.effective_otel_enabled is True

    def test_otel_sdk_disabled_uppercase_whitespace_token_forces_disabled(
        self, monkeypatch
    ):
        """The mute token is ``strip().lower()``-normalized (boundary of D7)."""
        monkeypatch.setenv("OTEL_SDK_DISABLED", "  TRUE  ")
        settings = ObservabilitySettings(profile=ObservabilityProfile.OTEL_COLLECTOR)

        assert settings.effective_otel_enabled is False


# =============================================================================
# effective_backend — derived "otel" iff enabled AND bridge importable (D3, 1.1/AP-1)
# =============================================================================
class TestEffectiveBackendBehavior:
    """``effective_backend`` degrades to prometheus for both silent-loss reasons.

    Invariant: ``effective_backend == "otel"`` iff
    ``effective_otel_enabled and _is_otel_meter_available()``.
    """

    def test_otel_collector_with_bridge_present_selects_otel(self, monkeypatch):
        """OTEL_COLLECTOR + bridge importable + SDK live → backend ``otel``."""
        monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
        with patch("baldur.observability._is_otel_meter_available", return_value=True):
            settings = ObservabilitySettings(
                profile=ObservabilityProfile.OTEL_COLLECTOR
            )

            assert settings.effective_otel_enabled is True
            assert settings.effective_backend == "otel"

    def test_explicit_otel_collector_bridge_absent_keeps_traces_degrades_backend(
        self, monkeypatch
    ):
        """AP-1: bridge absent → traces stay on but the metrics backend degrades.

        ``effective_otel_enabled`` stays True (the OTLP span exporter needs no
        metric bridge) while ``effective_backend`` falls back to prometheus so the
        factory never builds a dead OTel recorder. Asserted on the same instance.
        """
        # Given an explicit otel_collector, the SDK live, but the bridge absent
        monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
        with patch("baldur.observability._is_otel_meter_available", return_value=False):
            settings = ObservabilitySettings(
                profile=ObservabilityProfile.OTEL_COLLECTOR
            )

            # Then traces stay enabled but metrics degrade to prometheus
            assert settings.effective_otel_enabled is True
            assert settings.effective_backend == "prometheus"

    def test_explicit_otel_collector_sdk_disabled_degrades_both(self, monkeypatch):
        """1.1: ``OTEL_SDK_DISABLED`` mutes OTel and degrades the backend too.

        The single override disables OTel export AND (because the backend is
        derived from it) selects prometheus — uniform across both selection paths.
        """
        # Given an explicit otel_collector but the SDK muted by the standard env
        monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
        with patch("baldur.observability._is_otel_meter_available", return_value=True):
            settings = ObservabilitySettings(
                profile=ObservabilityProfile.OTEL_COLLECTOR
            )

            # Then both signals degrade on the same instance
            assert settings.effective_otel_enabled is False
            assert settings.effective_backend == "prometheus"

    def test_local_profile_uses_prometheus_backend(self, monkeypatch):
        """LOCAL never enables OTel, so the backend is always prometheus."""
        monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
        with patch("baldur.observability._is_otel_meter_available", return_value=True):
            settings = ObservabilitySettings(profile=ObservabilityProfile.LOCAL)

            assert settings.effective_backend == "prometheus"


# =============================================================================
# model_post_init — one-time resolution log emission (D5, D7)
# =============================================================================
class TestResolutionLogBehavior:
    """``model_post_init`` emits the resolution log once per construction (D5)."""

    def test_explicit_profile_emits_one_resolution_log_with_reason(self):
        """An explicit profile emits one INFO log with ``reason=explicit_profile``."""
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            ObservabilitySettings(profile=ObservabilityProfile.LOCAL)

        resolved = [
            e for e in logs if e.get("event") == "observability.profile_resolved"
        ]
        assert len(resolved) == 1
        assert resolved[0]["log_level"] == "info"
        assert resolved[0]["raw_profile"] == "local"
        assert resolved[0]["resolved_profile"] == "local"
        assert resolved[0]["reason"] == "explicit_profile"

    def test_auto_resolved_to_otel_reports_sdk_available_reason(self):
        """AUTO→OTEL_COLLECTOR carries ``reason=auto_resolved_otel_sdk_available``."""
        from structlog.testing import capture_logs

        with (
            patch("baldur.observability._is_otel_available", return_value=True),
            patch("baldur.observability._is_otel_meter_available", return_value=True),
        ):
            with capture_logs() as logs:
                ObservabilitySettings(profile=ObservabilityProfile.AUTO)

        resolved = [
            e for e in logs if e.get("event") == "observability.profile_resolved"
        ]
        assert len(resolved) == 1
        assert resolved[0]["raw_profile"] == "auto"
        assert resolved[0]["resolved_profile"] == "otel_collector"
        assert resolved[0]["reason"] == "auto_resolved_otel_sdk_available"

    def test_auto_resolved_to_local_reports_fallback_reason(self):
        """AUTO→LOCAL (a probe fails) carries ``reason=auto_resolved_fallback_local``."""
        from structlog.testing import capture_logs

        with (
            patch("baldur.observability._is_otel_available", return_value=True),
            patch("baldur.observability._is_otel_meter_available", return_value=False),
        ):
            with capture_logs() as logs:
                ObservabilitySettings(profile=ObservabilityProfile.AUTO)

        resolved = [
            e for e in logs if e.get("event") == "observability.profile_resolved"
        ]
        assert len(resolved) == 1
        assert resolved[0]["resolved_profile"] == "local"
        assert resolved[0]["reason"] == "auto_resolved_fallback_local"

    def test_sdk_disabled_under_enabled_profile_emits_downgrade_warning(
        self, monkeypatch
    ):
        """``OTEL_SDK_DISABLED`` under an OTel-enabled profile emits a WARNING (D7)."""
        from structlog.testing import capture_logs

        monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
        with capture_logs() as logs:
            ObservabilitySettings(profile=ObservabilityProfile.OTEL_COLLECTOR)

        warnings = [
            e
            for e in logs
            if e.get("event") == "observability.otel_sdk_disabled_by_standard_env"
        ]
        assert len(warnings) == 1
        assert warnings[0]["log_level"] == "warning"
        assert warnings[0]["resolved_profile"] == "otel_collector"

    def test_sdk_disabled_under_local_profile_does_not_warn(self, monkeypatch):
        """LOCAL already disables OTel, so the SDK-mute downgrade warning is silent."""
        from structlog.testing import capture_logs

        monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
        with capture_logs() as logs:
            ObservabilitySettings(profile=ObservabilityProfile.LOCAL)

        assert not [
            e
            for e in logs
            if e.get("event") == "observability.otel_sdk_disabled_by_standard_env"
        ]


# =============================================================================
# _is_otel_meter_available — import probe (D4)
# =============================================================================
class TestOtelMeterAvailableBehavior:
    """``_is_otel_meter_available`` probes the OTel metric SDK + Prometheus bridge."""

    def test_returns_true_when_both_packages_present(self):
        """In the monorepo dev env both metric packages are importable (G1 closed)."""
        from baldur.observability import _is_otel_meter_available

        assert _is_otel_meter_available() is True

    @pytest.mark.parametrize(
        "missing_module",
        ["opentelemetry.exporter.prometheus", "opentelemetry.sdk.metrics"],
        ids=["no_prometheus_bridge", "no_metrics_sdk"],
    )
    def test_returns_false_when_a_metric_package_is_absent(self, missing_module):
        """A missing metric package makes the probe report False (ImportError path)."""
        from baldur.observability import _is_otel_meter_available

        # Mapping a module name to None in sys.modules makes ``import`` raise
        # ImportError, deterministically exercising the except branch.
        with patch.dict("sys.modules", {missing_module: None}):
            assert _is_otel_meter_available() is False


# =============================================================================
# _create_metrics — backend routing via effective_backend (D6)
# =============================================================================
class TestCreateMetricsRoutingBehavior:
    """``_create_metrics`` selects the backend from ``effective_backend`` (D6).

    Re-homed from the deleted ``test_metrics_backend_settings.py`` after the
    ``MetricsSettings.backend`` field was removed.
    """

    def test_effective_backend_otel_builds_otel_metrics(self):
        """``effective_backend == "otel"`` → ``OTELBaldurMetrics`` (D6)."""
        from baldur.metrics.otel_backend import OTELBaldurMetrics
        from baldur.metrics.prometheus import _create_metrics

        with (
            patch(
                "baldur.settings.observability.get_observability_settings",
                return_value=MagicMock(effective_backend="otel"),
            ),
            patch("baldur.observability.get_meter", return_value=MagicMock()),
        ):
            result = _create_metrics()

        assert isinstance(result, OTELBaldurMetrics)

    def test_effective_backend_prometheus_builds_baldur_metrics(self):
        """``effective_backend == "prometheus"`` → the live ``BaldurMetrics`` (D6)."""
        from baldur.metrics.otel_backend import OTELBaldurMetrics
        from baldur.metrics.prometheus import BaldurMetrics, _create_metrics

        with patch(
            "baldur.settings.observability.get_observability_settings",
            return_value=MagicMock(effective_backend="prometheus"),
        ):
            result = _create_metrics()

        assert isinstance(result, BaldurMetrics)
        assert not isinstance(result, OTELBaldurMetrics)

    def test_settings_resolution_failure_falls_back_to_baldur_metrics(self):
        """A settings-resolution error fails open to the prometheus facade (D6)."""
        from baldur.metrics.prometheus import BaldurMetrics, _create_metrics

        with patch(
            "baldur.settings.observability.get_observability_settings",
            side_effect=RuntimeError("config boom"),
        ):
            result = _create_metrics()

        assert isinstance(result, BaldurMetrics)


# =============================================================================
# get/reset_observability_settings — singleton lifecycle (D8)
# =============================================================================
class TestObservabilitySettingsSingletonBehavior:
    """``get_observability_settings`` caches; ``reset_*`` reconstructs (D8)."""

    def test_get_returns_same_cached_instance(self):
        """Repeated calls return the same cached instance (singleton)."""
        first = get_observability_settings()
        second = get_observability_settings()

        assert first is second

    def test_reset_creates_a_new_instance(self):
        """``reset_observability_settings`` drops the cache so a new one is built."""
        first = get_observability_settings()
        reset_observability_settings()
        second = get_observability_settings()

        assert first is not second

    def test_reset_is_idempotent_when_uncached(self):
        """Resetting twice (no cached instance) is a safe no-op (idempotency)."""
        reset_observability_settings()
        # A second reset with nothing cached must not raise.
        reset_observability_settings()

        assert isinstance(get_observability_settings(), ObservabilitySettings)
