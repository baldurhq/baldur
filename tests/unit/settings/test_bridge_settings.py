"""Unit tests for ``baldur.settings.bridge.BridgeSettings`` (impl 451 D8).

Scope:
- Contract defaults: tenacity_enabled=False, tenacity_instrument=False, tenacity_metrics=True.
- Env var prefix ``BALDUR_BRIDGE_``.
- Singleton ``get_bridge_settings()`` / ``reset_bridge_settings()`` — runtime delegation.
"""

from __future__ import annotations

import pytest

from baldur.settings.bridge import (
    BridgeSettings,
    get_bridge_settings,
    reset_bridge_settings,
)


@pytest.fixture(autouse=True)
def _reset_settings():
    reset_bridge_settings()
    yield
    reset_bridge_settings()


# =============================================================================
# Contract — defaults from impl 451 D8
# =============================================================================


class TestBridgeSettingsContract:
    """Hardcoded defaults per impl 451 D8."""

    def test_default_values_match_documented_contract(self):
        """tenacity_enabled=False (opt-in), instrument=False, metrics=True."""
        settings = BridgeSettings()

        assert settings.tenacity_enabled is False
        assert settings.tenacity_instrument is False
        assert settings.tenacity_metrics is True

    def test_env_prefix_is_baldur_bridge(self):
        """Env prefix per D8 — ``BALDUR_BRIDGE_``."""
        prefix = BridgeSettings.model_config.get("env_prefix")

        assert prefix == "BALDUR_BRIDGE_"


# =============================================================================
# Behavior — env var binding
# =============================================================================


class TestBridgeSettingsEnvOverrideBehavior:
    """Env vars override the defaults using the ``BALDUR_BRIDGE_`` prefix."""

    @pytest.mark.parametrize(
        ("env_name", "field_name", "raw_value", "expected"),
        [
            ("BALDUR_BRIDGE_TENACITY_ENABLED", "tenacity_enabled", "true", True),
            ("BALDUR_BRIDGE_TENACITY_INSTRUMENT", "tenacity_instrument", "1", True),
            ("BALDUR_BRIDGE_TENACITY_METRICS", "tenacity_metrics", "false", False),
        ],
        ids=["enabled_true", "instrument_truthy_int", "metrics_false"],
    )
    def test_env_var_overrides_default(
        self, monkeypatch, env_name, field_name, raw_value, expected
    ):
        """Each flag respects its ``BALDUR_BRIDGE_*`` env var."""
        monkeypatch.setenv(env_name, raw_value)

        settings = BridgeSettings()

        assert getattr(settings, field_name) is expected


# =============================================================================
# Behavior — singleton get / reset
# =============================================================================


class TestBridgeSettingsSingletonBehavior:
    """``get_bridge_settings()`` / ``reset_bridge_settings()`` lifecycle."""

    def test_get_returns_same_instance_on_repeated_calls(self):
        """Cached singleton — same identity across two calls."""
        first = get_bridge_settings()
        second = get_bridge_settings()

        assert first is second

    def test_reset_forces_fresh_instance_next_call(self):
        """reset_bridge_settings() drops the cached instance."""
        first = get_bridge_settings()
        reset_bridge_settings()
        second = get_bridge_settings()

        assert first is not second
