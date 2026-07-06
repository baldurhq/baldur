"""
Unit tests for EntitlementSettings (427 §4.1, 508 D2 rename).

Verification techniques:
- Contract: default values, env prefix
- Singleton/lifecycle: get/reset pair
"""

from __future__ import annotations

from baldur.settings.license import (
    EntitlementSettings,
    get_entitlement_settings,
    reset_entitlement_settings,
)


class TestEntitlementSettingsContract:
    """Design contract values from 427 §4.1."""

    def test_license_key_default_empty(self):
        """BALDUR_LICENSE_KEY defaults to empty string (OSS mode)."""
        settings = EntitlementSettings()
        assert settings.key == ""

    def test_license_file_default_empty(self):
        """BALDUR_LICENSE_FILE defaults to empty string."""
        settings = EntitlementSettings()
        assert settings.file == ""

    def test_env_prefix_is_baldur_license(self):
        """Env prefix must be BALDUR_LICENSE_ per 508 D2 (was BALDUR_)."""
        prefix = EntitlementSettings.model_config.get("env_prefix")
        assert prefix == "BALDUR_LICENSE_"


class TestEntitlementSettingsBehavior:
    """Behavior tests for env var override and singleton."""

    def test_license_key_overridden_by_env(self, monkeypatch):
        """BALDUR_LICENSE_KEY env var overrides default."""
        monkeypatch.setenv("BALDUR_LICENSE_KEY", "test-token-123")
        settings = EntitlementSettings()
        assert settings.key == "test-token-123"

    def test_license_file_overridden_by_env(self, monkeypatch):
        """BALDUR_LICENSE_FILE env var overrides default."""
        monkeypatch.setenv("BALDUR_LICENSE_FILE", "/etc/baldur/license.key")
        settings = EntitlementSettings()
        assert settings.file == "/etc/baldur/license.key"

    def test_singleton_returns_same_instance(self):
        """get_entitlement_settings returns cached singleton."""
        reset_entitlement_settings()
        try:
            first = get_entitlement_settings()
            second = get_entitlement_settings()
            assert first is second
        finally:
            reset_entitlement_settings()

    def test_reset_clears_singleton(self):
        """reset_entitlement_settings clears the cached instance."""
        reset_entitlement_settings()
        try:
            first = get_entitlement_settings()
            reset_entitlement_settings()
            second = get_entitlement_settings()
            assert first is not second
        finally:
            reset_entitlement_settings()
