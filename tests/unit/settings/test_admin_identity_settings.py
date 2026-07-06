"""AdminIdentitySettings unit tests — 537 D-C4.

Verification target: the OSS settings module carrying the proxy-forwarded
identity header name (``BALDUR_ADMIN_IDENTITY_HEADER``). It lives in OSS so the
#g15 env-prefix module-equality fitness function stays complete, even though
only the PRO resolver reads it.

Verification techniques (UNIT_TEST_GUIDELINES §8):
- Contract: default header value (``X-Forwarded-Email``, oauth2-proxy
  convention) — hardcoded design contract.
- §8.5 dependency interaction: ``BALDUR_ADMIN_IDENTITY_HEADER`` env override.
- §8.10 singleton lifecycle: ``get_*`` caches, ``reset_*`` clears, post-reset
  reads fresh env (G8 singleton-pair fitness function).
"""

from __future__ import annotations

import pytest

from baldur.settings.admin_identity import (
    AdminIdentitySettings,
    get_admin_identity_settings,
    reset_admin_identity_settings,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_admin_identity_settings()
    yield
    reset_admin_identity_settings()


class TestAdminIdentitySettingsContract:
    """Default values and env binding are the public contract of 537 D-C4."""

    def test_default_header_is_oauth2_proxy_convention(self):
        """Default identity header is X-Forwarded-Email (oauth2-proxy)."""
        settings = AdminIdentitySettings()
        assert settings.header == "X-Forwarded-Email"

    def test_header_env_override_uses_baldur_admin_identity_prefix(self, monkeypatch):
        """BALDUR_ADMIN_IDENTITY_HEADER overrides the header field.

        Verifies the #g15 module-equality binding: module ``admin_identity`` <->
        env prefix ``BALDUR_ADMIN_IDENTITY_``.
        """
        monkeypatch.setenv(
            "BALDUR_ADMIN_IDENTITY_HEADER", "X-Goog-Authenticated-User-Email"
        )
        settings = AdminIdentitySettings()
        assert settings.header == "X-Goog-Authenticated-User-Email"

    def test_unrelated_admin_prefix_env_does_not_bind(self, monkeypatch):
        """``extra="ignore"`` keeps the BALDUR_ADMIN_ prefix from colliding.

        BALDUR_ADMIN_PORT belongs to AdminServerSettings; AdminIdentitySettings
        must ignore it rather than error (537 D-C4).
        """
        monkeypatch.setenv("BALDUR_ADMIN_PORT", "7777")
        settings = AdminIdentitySettings()
        assert settings.header == "X-Forwarded-Email"


class TestAdminIdentitySettingsSingletonBehavior:
    """get_admin_identity_settings() / reset_admin_identity_settings() pair."""

    def test_get_returns_cached_instance(self):
        first = get_admin_identity_settings()
        second = get_admin_identity_settings()
        assert first is second

    def test_reset_clears_cache(self):
        first = get_admin_identity_settings()
        reset_admin_identity_settings()
        second = get_admin_identity_settings()
        assert first is not second

    def test_reset_picks_up_new_env_value(self, monkeypatch):
        """After reset, a new singleton reads the current env var."""
        monkeypatch.setenv(
            "BALDUR_ADMIN_IDENTITY_HEADER", "Cf-Access-Authenticated-User-Email"
        )
        reset_admin_identity_settings()
        settings = get_admin_identity_settings()
        assert settings.header == "Cf-Access-Authenticated-User-Email"
