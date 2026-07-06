"""Tests for ``RateLimitSettings.decorator_enabled`` (#458 §D5).

Verification techniques applied:
- Contract: default value is True so OSS/PRO get the decorator on out of
  the box.
- Behavior: env-var override flips the toggle; reset_rate_limit_settings
  re-reads env on next access.
"""

import pytest


class TestRateLimitDecoratorToggle:
    """Tests for the decorator_enabled toggle field added in #458 §D5."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        from baldur.settings.rate_limit import reset_rate_limit_settings

        reset_rate_limit_settings()
        yield
        reset_rate_limit_settings()

    def test_default_is_true(self):
        from baldur.settings.rate_limit import RateLimitSettings

        settings = RateLimitSettings()
        assert settings.decorator_enabled is True

    def test_env_override_to_false(self, monkeypatch):
        from baldur.settings.rate_limit import RateLimitSettings

        monkeypatch.setenv("BALDUR_RATE_LIMIT_DECORATOR_ENABLED", "false")
        settings = RateLimitSettings()
        assert settings.decorator_enabled is False

    def test_env_override_to_true(self, monkeypatch):
        from baldur.settings.rate_limit import RateLimitSettings

        monkeypatch.setenv("BALDUR_RATE_LIMIT_DECORATOR_ENABLED", "true")
        settings = RateLimitSettings()
        assert settings.decorator_enabled is True

    def test_singleton_reset_picks_up_new_env(self, monkeypatch):
        from baldur.settings.rate_limit import (
            get_rate_limit_settings,
            reset_rate_limit_settings,
        )

        # Initial: default True
        assert get_rate_limit_settings().decorator_enabled is True

        # Flip env, reset, observe new value
        monkeypatch.setenv("BALDUR_RATE_LIMIT_DECORATOR_ENABLED", "false")
        reset_rate_limit_settings()
        assert get_rate_limit_settings().decorator_enabled is False
