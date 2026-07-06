"""
Tests for Emergency Level Adaptive Sampler.
"""

from unittest.mock import MagicMock, patch

import pytest


def _is_otel_available() -> bool:
    """Check if OpenTelemetry SDK is installed."""
    try:
        import opentelemetry.sdk.trace  # noqa: F401

        return True
    except ImportError:
        return False


@pytest.mark.skipif(
    not _is_otel_available(),
    reason="OpenTelemetry SDK not installed",
)
class TestEmergencyLevelAdaptiveSampler:
    """Tests for EmergencyLevelAdaptiveSampler."""

    def test_sampler_initialization(self):
        """Test sampler initializes with correct defaults."""
        from baldur.observability.sampler import EmergencyLevelAdaptiveSampler

        sampler = EmergencyLevelAdaptiveSampler()
        assert sampler._base_ratio == 0.01

    def test_sampler_initialization_with_custom_values(self):
        """Test sampler initializes with custom values."""
        from baldur.observability.sampler import EmergencyLevelAdaptiveSampler

        sampler = EmergencyLevelAdaptiveSampler(base_ratio=0.1)
        assert sampler._base_ratio == 0.1

    def test_sampler_ratio_clamped(self):
        """Test that base_ratio is clamped to 0.0-1.0."""
        from baldur.observability.sampler import EmergencyLevelAdaptiveSampler

        # Below 0
        sampler = EmergencyLevelAdaptiveSampler(base_ratio=-0.5)
        assert sampler._base_ratio == 0.0

        # Above 1
        sampler = EmergencyLevelAdaptiveSampler(base_ratio=1.5)
        assert sampler._base_ratio == 1.0

    def test_get_description(self):
        """Test sampler description includes current state."""
        from baldur.observability.sampler import EmergencyLevelAdaptiveSampler

        sampler = EmergencyLevelAdaptiveSampler(base_ratio=0.01)
        description = sampler.get_description()

        assert "EmergencyLevelAdaptiveSampler" in description
        assert "base_ratio=1.00%" in description


class TestEmergencyLevelSamplingRatios:
    """Tests for emergency level to sampling ratio mapping.

    643 D6 — NORMAL (level 0) is intentionally absent so ``_get_current_ratio``
    falls through to ``base_ratio``; the table defines escalation levels only.
    """

    def test_table_defines_escalation_levels_only(self):
        """The ratio table covers escalation levels 1-3 only; NORMAL is absent."""
        from baldur.observability.sampler import EMERGENCY_LEVEL_SAMPLING_RATIOS

        assert set(EMERGENCY_LEVEL_SAMPLING_RATIOS) == {1, 2, 3}

    def test_normal_level_absent_so_base_ratio_is_honored(self):
        """Level 0 (NORMAL) is not in the dict — base_ratio fallback applies."""
        from baldur.observability.sampler import EMERGENCY_LEVEL_SAMPLING_RATIOS

        assert 0 not in EMERGENCY_LEVEL_SAMPLING_RATIOS

    def test_level_1_has_moderate_sampling(self):
        """Test LEVEL_1 uses 10% sampling."""
        from baldur.observability.sampler import EMERGENCY_LEVEL_SAMPLING_RATIOS

        assert EMERGENCY_LEVEL_SAMPLING_RATIOS[1] == 0.10

    def test_level_2_has_full_sampling(self):
        """Test LEVEL_2 uses 100% sampling."""
        from baldur.observability.sampler import EMERGENCY_LEVEL_SAMPLING_RATIOS

        assert EMERGENCY_LEVEL_SAMPLING_RATIOS[2] == 1.00

    def test_level_3_has_full_sampling(self):
        """Test LEVEL_3 uses 100% sampling."""
        from baldur.observability.sampler import EMERGENCY_LEVEL_SAMPLING_RATIOS

        assert EMERGENCY_LEVEL_SAMPLING_RATIOS[3] == 1.00


@pytest.mark.skipif(
    not _is_otel_available(),
    reason="OpenTelemetry SDK not installed",
)
class TestStaticRatioSampler:
    """Tests for StaticRatioSampler."""

    def test_static_sampler_initialization(self):
        """Test static sampler initializes correctly."""
        from baldur.observability.sampler import StaticRatioSampler

        sampler = StaticRatioSampler(ratio=0.5)
        assert sampler._ratio == 0.5

    def test_static_sampler_ratio_clamped(self):
        """Test static sampler ratio is clamped."""
        from baldur.observability.sampler import StaticRatioSampler

        sampler = StaticRatioSampler(ratio=-0.5)
        assert sampler._ratio == 0.0

        sampler = StaticRatioSampler(ratio=1.5)
        assert sampler._ratio == 1.0

    def test_static_sampler_description(self):
        """Test static sampler description."""
        from baldur.observability.sampler import StaticRatioSampler

        sampler = StaticRatioSampler(ratio=0.5)
        description = sampler.get_description()

        assert "StaticRatioSampler" in description
        assert "50.00%" in description


class TestBaseRatioHonoredAtNormal:
    """643 D6 — OTEL_TRACES_SAMPLER_ARG (base_ratio) controls the NORMAL rate.

    With level 0 absent from the ratio table, ``_get_current_ratio`` falls
    through to ``base_ratio`` during normal operation, so a custom
    ``OTEL_TRACES_SAMPLER_ARG`` is no longer shadowed by a hardcoded 1%.
    """

    def test_base_ratio_honored_at_normal(self):
        """At NORMAL (level 0), the ratio equals base_ratio, not a hardcoded 1%."""
        from baldur.observability.sampler import EmergencyLevelAdaptiveSampler

        with patch(
            "baldur.observability.sampler._get_current_emergency_level",
            return_value=0,
        ):
            sampler = EmergencyLevelAdaptiveSampler(base_ratio=0.5)
            assert sampler._get_current_ratio() == 0.5

    def test_escalation_level_overrides_base_ratio(self):
        """At an escalation level, the table ratio (not base_ratio) wins."""
        from baldur.observability.sampler import EmergencyLevelAdaptiveSampler

        with patch(
            "baldur.observability.sampler._get_current_emergency_level",
            return_value=2,
        ):
            sampler = EmergencyLevelAdaptiveSampler(base_ratio=0.5)
            assert sampler._get_current_ratio() == 1.00


class TestGetCurrentEmergencyLevel:
    """Tests for _get_current_emergency_level helper.

    643 D7 — patches the real production seam
    (``ProviderRegistry.emergency_manager.safe_get``), not the inert private
    ``get_emergency_manager`` symbol the production code never reads. The mock
    sits one level below the consumer (``get_current_level().severity``) so the
    ``.severity`` extraction stays exercised, and no private-tier import is
    needed.
    """

    def test_returns_0_when_manager_absent(self):
        """safe_get() returns None → OSS fallback contract is 0 (NORMAL)."""
        from baldur.factory.registry import ProviderRegistry
        from baldur.observability.sampler import _get_current_emergency_level

        with patch.object(
            ProviderRegistry.emergency_manager, "safe_get", return_value=None
        ):
            assert _get_current_emergency_level() == 0

    def test_returns_severity_from_manager(self):
        """A manager whose get_current_level().severity is 2 → returns 2."""
        from baldur.factory.registry import ProviderRegistry
        from baldur.observability.sampler import _get_current_emergency_level

        mock_manager = MagicMock()
        mock_manager.get_current_level.return_value.severity = 2

        with patch.object(
            ProviderRegistry.emergency_manager, "safe_get", return_value=mock_manager
        ):
            assert _get_current_emergency_level() == 2


@pytest.mark.skipif(
    not _is_otel_available(),
    reason="OpenTelemetry SDK not installed",
)
class TestSelectSampler:
    """643 D4 — strategy-first ``_select_sampler`` dispatch.

    Covers all six ``traces_sampler`` values x {adaptive on, off}. Absolute
    strategies (always_on/always_off/parentbased_always_*) override the adaptive
    flag and map to OTEL public building blocks; the ratio strategies route to
    the adaptive sampler (adaptive on) or the plain OTEL ratio samplers (off).

    Settings are constructed via the validation aliases (``OTEL_*`` kwargs);
    field-name kwargs are silently ignored without ``populate_by_name`` and
    would yield the defaults, masking the case under test.
    """

    @pytest.mark.parametrize(
        ("strategy", "adaptive", "expected_const"),
        [
            pytest.param("always_on", True, "ALWAYS_ON", id="always_on-adaptive_on"),
            pytest.param("always_on", False, "ALWAYS_ON", id="always_on-adaptive_off"),
            pytest.param("always_off", True, "ALWAYS_OFF", id="always_off-adaptive_on"),
            pytest.param(
                "always_off", False, "ALWAYS_OFF", id="always_off-adaptive_off"
            ),
            pytest.param(
                "parentbased_always_on",
                True,
                "DEFAULT_ON",
                id="parentbased_always_on-adaptive_on",
            ),
            pytest.param(
                "parentbased_always_on",
                False,
                "DEFAULT_ON",
                id="parentbased_always_on-adaptive_off",
            ),
            pytest.param(
                "parentbased_always_off",
                True,
                "DEFAULT_OFF",
                id="parentbased_always_off-adaptive_on",
            ),
            pytest.param(
                "parentbased_always_off",
                False,
                "DEFAULT_OFF",
                id="parentbased_always_off-adaptive_off",
            ),
        ],
    )
    def test_sampler_selection_absolute_strategies(
        self, strategy, adaptive, expected_const
    ):
        """Absolute strategies map to the OTEL public constant, ignoring adaptive."""
        from opentelemetry.sdk.trace import sampling

        from baldur.observability import _select_sampler
        from baldur.settings.otel import OpenTelemetrySettings

        settings = OpenTelemetrySettings(
            OTEL_TRACES_SAMPLER=strategy,
            OTEL_ADAPTIVE_SAMPLING_ENABLED=adaptive,
        )
        sampler = _select_sampler(settings)

        assert sampler is getattr(sampling, expected_const)

        # always_on / always_off carry an unambiguous 100% / 0% contract.
        if expected_const == "ALWAYS_ON":
            decision = sampler.should_sample(None, 0x1, "x").decision
            assert decision == sampling.Decision.RECORD_AND_SAMPLE
        elif expected_const == "ALWAYS_OFF":
            decision = sampler.should_sample(None, 0x1, "x").decision
            assert decision == sampling.Decision.DROP

    @pytest.mark.parametrize(
        ("strategy", "adaptive", "expected_type"),
        [
            pytest.param(
                "traceidratio",
                True,
                "EmergencyLevelAdaptiveSampler",
                id="traceidratio-adaptive_on",
            ),
            pytest.param(
                "traceidratio",
                False,
                "TraceIdRatioBased",
                id="traceidratio-adaptive_off",
            ),
            pytest.param(
                "parentbased_traceidratio",
                True,
                "EmergencyLevelAdaptiveSampler",
                id="parentbased_traceidratio-adaptive_on",
            ),
            pytest.param(
                "parentbased_traceidratio",
                False,
                "ParentBasedTraceIdRatio",
                id="parentbased_traceidratio-adaptive_off",
            ),
        ],
    )
    def test_sampler_selection_ratio_strategies(
        self, strategy, adaptive, expected_type
    ):
        """Ratio strategies route to the adaptive sampler (on) or plain ratio (off)."""
        from baldur.observability import _select_sampler
        from baldur.settings.otel import OpenTelemetrySettings

        settings = OpenTelemetrySettings(
            OTEL_TRACES_SAMPLER=strategy,
            OTEL_ADAPTIVE_SAMPLING_ENABLED=adaptive,
            OTEL_TRACES_SAMPLER_ARG=0.25,
        )
        sampler = _select_sampler(settings)

        assert type(sampler).__name__ == expected_type


@pytest.mark.skipif(
    not _is_otel_available(),
    reason="OpenTelemetry SDK not installed",
)
class TestAdaptiveSamplingBypassedLog:
    """643 D1 — ``_select_sampler`` emits ``otel.adaptive_sampling_bypassed`` at
    DEBUG when ``adaptive_sampling_enabled`` is True but an absolute strategy
    wins (the adaptive sampler is inert; the log surfaces the precedence to
    operators without raising).

    The log fires only on the (absolute strategy AND adaptive) intersection:
    silent when adaptive is off (nothing to bypass) or when a ratio strategy
    keeps the adaptive sampler active.
    """

    def test_absolute_strategy_with_adaptive_logs_bypass(self):
        """always_on + adaptive on → one DEBUG bypass log naming the strategy."""
        from structlog.testing import capture_logs

        from baldur.observability import _select_sampler
        from baldur.settings.otel import OpenTelemetrySettings

        settings = OpenTelemetrySettings(
            OTEL_TRACES_SAMPLER="always_on",
            OTEL_ADAPTIVE_SAMPLING_ENABLED=True,
        )
        with capture_logs() as logs:
            _select_sampler(settings)

        bypassed = [
            e for e in logs if e.get("event") == "otel.adaptive_sampling_bypassed"
        ]
        assert len(bypassed) == 1
        assert bypassed[0]["log_level"] == "debug"
        assert bypassed[0]["traces_sampler"] == "always_on"

    def test_absolute_strategy_without_adaptive_does_not_log(self):
        """always_on + adaptive off → no bypass log (adaptive was never enabled)."""
        from structlog.testing import capture_logs

        from baldur.observability import _select_sampler
        from baldur.settings.otel import OpenTelemetrySettings

        settings = OpenTelemetrySettings(
            OTEL_TRACES_SAMPLER="always_on",
            OTEL_ADAPTIVE_SAMPLING_ENABLED=False,
        )
        with capture_logs() as logs:
            _select_sampler(settings)

        assert not [
            e for e in logs if e.get("event") == "otel.adaptive_sampling_bypassed"
        ]

    def test_ratio_strategy_with_adaptive_does_not_log(self):
        """traceidratio + adaptive on → adaptive sampler is active, not bypassed."""
        from structlog.testing import capture_logs

        from baldur.observability import _select_sampler
        from baldur.settings.otel import OpenTelemetrySettings

        settings = OpenTelemetrySettings(
            OTEL_TRACES_SAMPLER="traceidratio",
            OTEL_ADAPTIVE_SAMPLING_ENABLED=True,
        )
        with capture_logs() as logs:
            _select_sampler(settings)

        assert not [
            e for e in logs if e.get("event") == "otel.adaptive_sampling_bypassed"
        ]
