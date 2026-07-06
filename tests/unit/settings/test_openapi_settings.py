"""OpenAPISettings unit tests — 530 Wave 6F (API Discoverability).

Verification targets:
- Default values (Contract) — BALDUR_OPENAPI_* env prefix, enabled=True
- Environment override propagation (BALDUR_OPENAPI_*)
- Singleton lifecycle (get_*/reset_* pair)

Techniques applied (UNIT_TEST_GUIDELINES §8):
- Boundary analysis — bool round-trip on `enabled`
- Dependency interaction — env_var binding via BaseSettings
- Singleton/lifecycle — caching behavior
"""

from __future__ import annotations

import pytest

from baldur.settings.openapi import (
    OpenAPISettings,
    get_openapi_settings,
    reset_openapi_settings,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_openapi_settings()
    yield
    reset_openapi_settings()


# =============================================================================
# Contract — defaults are the public 530 D4 contract
# =============================================================================


class TestOpenAPISettingsContract:
    """Defaults are documented in 530 D4 and consumed by `urls/schema.py`."""

    def test_defaults_match_530_d4(self):
        """enabled=True default reflects OSS discoverability (530 D4)."""
        settings = OpenAPISettings()

        assert settings.enabled is True
        assert settings.title == "Baldur Reliability API"
        assert settings.version == "1.0.0"
        # description is long; just confirm it's a non-empty string.
        assert isinstance(settings.description, str)
        assert settings.description

    def test_env_prefix_is_baldur_openapi(self, monkeypatch):
        """BaseSettings reads BALDUR_OPENAPI_* env vars (530 D4)."""
        monkeypatch.setenv("BALDUR_OPENAPI_ENABLED", "false")
        monkeypatch.setenv("BALDUR_OPENAPI_TITLE", "Acme API")
        monkeypatch.setenv("BALDUR_OPENAPI_VERSION", "2.5.0")
        monkeypatch.setenv("BALDUR_OPENAPI_DESCRIPTION", "Custom override.")

        settings = OpenAPISettings()

        assert settings.enabled is False
        assert settings.title == "Acme API"
        assert settings.version == "2.5.0"
        assert settings.description == "Custom override."

    @pytest.mark.parametrize(
        ("env_value", "expected"),
        [
            ("true", True),
            ("True", True),
            ("1", True),
            ("false", False),
            ("False", False),
            ("0", False),
        ],
    )
    def test_enabled_parses_boolean_strings(self, monkeypatch, env_value, expected):
        """Pydantic BaseSettings parses string env values to bool."""
        monkeypatch.setenv("BALDUR_OPENAPI_ENABLED", env_value)
        settings = OpenAPISettings()
        assert settings.enabled is expected


# =============================================================================
# Behavior — singleton lifecycle
# =============================================================================


class TestOpenAPISettingsSingletonBehavior:
    """get_openapi_settings() / reset_openapi_settings() lifecycle."""

    def test_get_returns_cached_instance(self):
        first = get_openapi_settings()
        second = get_openapi_settings()
        assert first is second

    def test_reset_clears_cache(self):
        first = get_openapi_settings()
        reset_openapi_settings()
        second = get_openapi_settings()
        assert first is not second

    def test_reset_picks_up_new_env_values(self, monkeypatch):
        """After reset, the new singleton reads current env state."""
        monkeypatch.setenv("BALDUR_OPENAPI_VERSION", "9.9.9")
        reset_openapi_settings()
        assert get_openapi_settings().version == "9.9.9"
