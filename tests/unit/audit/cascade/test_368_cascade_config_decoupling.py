"""
368 Cascade Config Decoupling unit tests.

Tests for get_cascade_chain_config() and get_audit_backpressure_config()
after migration from Django settings to Pydantic Settings.

Test Categories:
    A. Behavior: Settings → dataclass mapping, fallback on exception
"""

from unittest.mock import patch

from baldur.audit.cascade_config import (
    DEFAULT_BACKPRESSURE_CONFIG,
    DEFAULT_CASCADE_CHAIN_CONFIG,
    AuditBackpressureConfig,
    CascadeChainConfig,
    get_audit_backpressure_config,
    get_cascade_chain_config,
)
from baldur.settings.audit import AuditSettings
from baldur.settings.cascade import CascadeSettings

# =============================================================================
# A. Behavior Tests
# =============================================================================


class TestGetCascadeChainConfigBehavior:
    """get_cascade_chain_config() maps CascadeSettings → CascadeChainConfig."""

    @patch(
        "baldur.settings.cascade.get_cascade_settings",
        autospec=True,
    )
    def test_maps_settings_to_chain_config(self, mock_get_settings):
        """CascadeSettings fields are correctly mapped to CascadeChainConfig."""
        mock_get_settings.return_value = CascadeSettings(
            max_depth=20,
            warn_depth=15,
            block_on_exceed=False,
            detect_cycles=False,
        )

        result = get_cascade_chain_config()

        assert isinstance(result, CascadeChainConfig)
        assert result.max_chain_depth == 20
        assert result.warn_at_depth == 15
        assert result.block_on_exceed is False
        assert result.detect_cycles is False

    @patch(
        "baldur.settings.cascade.get_cascade_settings",
        autospec=True,
    )
    def test_default_settings_produce_default_config(self, mock_get_settings):
        """Default CascadeSettings produce default CascadeChainConfig values."""
        mock_get_settings.return_value = CascadeSettings()

        result = get_cascade_chain_config()

        default = CascadeSettings()
        assert result.max_chain_depth == default.max_depth
        assert result.warn_at_depth == default.warn_depth
        assert result.block_on_exceed == default.block_on_exceed
        assert result.detect_cycles == default.detect_cycles

    @patch(
        "baldur.settings.cascade.get_cascade_settings",
        autospec=True,
        side_effect=Exception("settings unavailable"),
    )
    def test_fallback_to_default_on_exception(self, mock_get_settings):
        """Returns DEFAULT_CASCADE_CHAIN_CONFIG when settings raise."""
        result = get_cascade_chain_config()

        assert result.max_chain_depth == DEFAULT_CASCADE_CHAIN_CONFIG.max_chain_depth
        assert result.warn_at_depth == DEFAULT_CASCADE_CHAIN_CONFIG.warn_at_depth


class TestGetAuditBackpressureConfigBehavior:
    """get_audit_backpressure_config() maps AuditSettings → AuditBackpressureConfig."""

    @patch(
        "baldur.settings.audit.get_audit_settings",
        autospec=True,
    )
    def test_maps_settings_to_backpressure_config(self, mock_get_settings):
        """AuditSettings backpressure fields map to AuditBackpressureConfig."""
        mock_get_settings.return_value = AuditSettings(
            load_shedding_enabled=False,
            buffer_warning_threshold=0.5,
            buffer_critical_threshold=0.8,
            max_events_per_second=500,
            fallback_enabled=False,
            metrics_enabled=False,
        )

        result = get_audit_backpressure_config()

        assert isinstance(result, AuditBackpressureConfig)
        assert result.load_shedding_enabled is False
        assert result.buffer_warning_threshold == 0.5
        assert result.buffer_critical_threshold == 0.8
        assert result.max_events_per_second == 500
        assert result.fallback_enabled is False
        assert result.metrics_enabled is False

    @patch(
        "baldur.settings.audit.get_audit_settings",
        autospec=True,
        side_effect=Exception("settings unavailable"),
    )
    def test_fallback_to_default_on_exception(self, mock_get_settings):
        """Returns DEFAULT_BACKPRESSURE_CONFIG when settings raise."""
        result = get_audit_backpressure_config()

        assert (
            result.load_shedding_enabled
            == DEFAULT_BACKPRESSURE_CONFIG.load_shedding_enabled
        )
        assert (
            result.max_events_per_second
            == DEFAULT_BACKPRESSURE_CONFIG.max_events_per_second
        )
