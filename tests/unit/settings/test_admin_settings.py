"""AdminServerSettings unit tests — 429 PR3-runtime.

Verification targets:
- Default values (Contract) — BALDUR_ADMIN_* env prefix, fail-safe defaults
- Field boundary constraints (port, request_timeout_seconds, max_body_bytes)
- Environment override propagation (BALDUR_ADMIN_*)
- Derived properties (is_localhost_bind, api_key_plain)
- Singleton lifecycle (get_*/reset_* pair)
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from baldur.settings.admin import (
    AdminServerSettings,
    get_admin_server_settings,
    reset_admin_server_settings,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_admin_server_settings()
    yield
    reset_admin_server_settings()


# =============================================================================
# Contract — default values
# =============================================================================


class TestAdminServerSettingsContract:
    """Default values are the public contract of 429 PR3-runtime."""

    def test_defaults_match_pr3_design_contract(self, monkeypatch):
        """Defaults mirror the 429 Part 2 design: localhost-only, auto-start,
        fail-closed unlock, no API key, plain HTTP (trust_proxy off).

        ``tests/conftest.py`` sets ``BALDUR_ADMIN_AUTOSTART=0`` so the admin
        server does not bind a real port during the test process; that env
        var must be cleared here to verify the production default.
        """
        monkeypatch.delenv("BALDUR_ADMIN_AUTOSTART", raising=False)
        settings = AdminServerSettings()

        assert settings.enabled is True
        assert settings.autostart is True
        assert settings.bind == "127.0.0.1"
        assert settings.port == 9090
        assert settings.api_key is None
        assert settings.trust_proxy is False
        assert settings.unlock is False
        assert settings.request_timeout_seconds == 30.0
        assert settings.max_body_bytes == 1_048_576

    def test_env_prefix_is_baldur_admin(self, monkeypatch):
        """Environment variables are read from the BALDUR_ADMIN_ prefix."""
        monkeypatch.setenv("BALDUR_ADMIN_BIND", "10.0.0.5")
        monkeypatch.setenv("BALDUR_ADMIN_PORT", "7777")
        monkeypatch.setenv("BALDUR_ADMIN_KEY", "sekret-from-env")
        monkeypatch.setenv("BALDUR_ADMIN_TRUST_PROXY", "1")
        monkeypatch.setenv("BALDUR_ADMIN_UNLOCK", "true")
        monkeypatch.setenv("BALDUR_ADMIN_AUTOSTART", "0")
        monkeypatch.setenv("BALDUR_ADMIN_ENABLED", "false")

        settings = AdminServerSettings()

        assert settings.bind == "10.0.0.5"
        assert settings.port == 7777
        assert settings.api_key is not None
        assert settings.api_key.get_secret_value() == "sekret-from-env"
        assert settings.trust_proxy is True
        assert settings.unlock is True
        assert settings.autostart is False
        assert settings.enabled is False

    def test_console_fields_default_to_enabled_and_empty_allowlist(self):
        """536 D4/D6: the web console is on by default (safe — localhost bind +
        origin gate) and the extra-origins allowlist starts empty."""
        settings = AdminServerSettings()

        assert settings.console_enabled is True
        assert settings.allowed_origins == []

    def test_console_enabled_env_override(self, monkeypatch):
        """BALDUR_ADMIN_CONSOLE_ENABLED toggles the console off."""
        monkeypatch.setenv("BALDUR_ADMIN_CONSOLE_ENABLED", "false")
        settings = AdminServerSettings()
        assert settings.console_enabled is False

    def test_allowed_origins_env_parses_as_json_list(self, monkeypatch):
        """536 D6: pydantic-settings parses BALDUR_ADMIN_ALLOWED_ORIGINS as a
        JSON list (not comma-split), e.g. '["a.example.com", "b.example.com"]'."""
        monkeypatch.setenv(
            "BALDUR_ADMIN_ALLOWED_ORIGINS",
            '["admin.example.com", "ops.example.com"]',
        )
        settings = AdminServerSettings()
        assert settings.allowed_origins == ["admin.example.com", "ops.example.com"]


# =============================================================================
# Contract — field boundary constraints
# =============================================================================


class TestAdminServerSettingsBoundaryContract:
    """Pydantic Field(ge=, le=) boundaries declared in settings/admin.py."""

    def test_port_minimum_boundary_accepts_zero_for_ephemeral(self):
        """port=0 is accepted (OS-assigned ephemeral; used by tests)."""
        settings = AdminServerSettings(port=0)
        assert settings.port == 0

    def test_port_rejects_negative(self):
        """port < 0 violates ge=0."""
        with pytest.raises(ValidationError):
            AdminServerSettings(port=-1)

    def test_port_maximum_boundary_accepts_65535(self):
        """port=65535 is the TCP upper bound."""
        settings = AdminServerSettings(port=65535)
        assert settings.port == 65535

    def test_port_rejects_above_65535(self):
        """port > 65535 violates le=65535."""
        with pytest.raises(ValidationError):
            AdminServerSettings(port=65536)

    def test_request_timeout_minimum_boundary(self):
        """request_timeout_seconds ge=0.1."""
        with pytest.raises(ValidationError):
            AdminServerSettings(request_timeout_seconds=0.0)
        settings = AdminServerSettings(request_timeout_seconds=0.1)
        assert settings.request_timeout_seconds == 0.1

    def test_request_timeout_maximum_boundary(self):
        """request_timeout_seconds le=300.0."""
        settings = AdminServerSettings(request_timeout_seconds=300.0)
        assert settings.request_timeout_seconds == 300.0
        with pytest.raises(ValidationError):
            AdminServerSettings(request_timeout_seconds=300.1)

    def test_max_body_bytes_minimum_boundary(self):
        """max_body_bytes ge=1024."""
        with pytest.raises(ValidationError):
            AdminServerSettings(max_body_bytes=1023)
        settings = AdminServerSettings(max_body_bytes=1024)
        assert settings.max_body_bytes == 1024

    def test_max_body_bytes_maximum_boundary(self):
        """max_body_bytes le=16 MiB."""
        max_allowed = 16 * 1024 * 1024
        settings = AdminServerSettings(max_body_bytes=max_allowed)
        assert settings.max_body_bytes == max_allowed
        with pytest.raises(ValidationError):
            AdminServerSettings(max_body_bytes=max_allowed + 1)


# =============================================================================
# Behavior — derived properties
# =============================================================================


class TestAdminServerSettingsBehavior:
    """Derived properties compute from field state."""

    @pytest.mark.parametrize(
        "bind",
        ["127.0.0.1", "::1", "localhost"],
    )
    def test_is_localhost_bind_true_for_loopback_addresses(self, bind):
        """All three canonical loopback addresses are recognized as localhost."""
        assert AdminServerSettings(bind=bind).is_localhost_bind is True

    @pytest.mark.parametrize(
        "bind",
        ["0.0.0.0", "10.0.0.5", "192.168.1.1", "::", "example.com"],
    )
    def test_is_localhost_bind_false_for_non_loopback(self, bind):
        """Any non-loopback bind address is treated as non-localhost."""
        # api_key must be set for non-localhost in production, but the
        # property itself is independent of auth state.
        settings = AdminServerSettings(bind=bind, api_key="placeholder")
        assert settings.is_localhost_bind is False

    def test_bind_whitespace_is_stripped(self):
        """Leading/trailing whitespace is stripped before comparison."""
        settings = AdminServerSettings(bind="  127.0.0.1  ")
        assert settings.bind == "127.0.0.1"
        assert settings.is_localhost_bind is True

    def test_api_key_plain_returns_secret_string(self):
        """api_key_plain unwraps the SecretStr."""
        settings = AdminServerSettings(api_key="top-secret")
        assert settings.api_key_plain == "top-secret"

    def test_api_key_plain_returns_none_when_unset(self):
        """No api_key configured → api_key_plain is None."""
        settings = AdminServerSettings()
        assert settings.api_key_plain is None

    def test_api_key_plain_returns_none_for_empty_string(self):
        """Empty SecretStr behaves as "no key configured"."""
        settings = AdminServerSettings(api_key=SecretStr(""))
        assert settings.api_key_plain is None


# =============================================================================
# Contract — read-only (VIEWER) credential: readonly_key_plain + equal-key guard
# =============================================================================


class TestAdminReadonlyKeyContract:
    """readonly_key_plain accessor (D3) and the equal-key fail-loud guard (D4).

    The read-only key is a strictly-less-privileged VIEWER credential carried on
    the same X-Baldur-Admin-Key header. readonly_key_plain mirrors api_key_plain's
    empty-string -> None normalization; _reject_equal_keys fails loud at load when
    the two secrets collide (which would make the shared value's effective
    permission level ambiguous).
    """

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"readonly_key": "ro-secret"}, "ro-secret"),
            ({}, None),
            ({"readonly_key": SecretStr("")}, None),
        ],
        ids=["non_empty_returns_value", "unset_returns_none", "empty_returns_none"],
    )
    def test_readonly_key_plain_normalizes_like_api_key_plain(
        self, monkeypatch, kwargs, expected
    ):
        """readonly_key_plain unwraps the SecretStr and normalizes empty -> None.

        Parity with api_key_plain: a configured secret returns its plaintext, an
        unset key returns None, and an empty-string secret reads as "no key
        configured" (None) rather than a usable empty credential.
        """
        # Defensive: an exported BALDUR_ADMIN_READONLY_KEY must not leak into the
        # unset case (init kwargs win, but the no-kwarg case reads the env).
        monkeypatch.delenv("BALDUR_ADMIN_READONLY_KEY", raising=False)
        assert AdminServerSettings(**kwargs).readonly_key_plain == expected

    def test_readonly_key_env_alias_populates_field(self, monkeypatch):
        """BALDUR_ADMIN_READONLY_KEY is the operator-facing alias for the field."""
        monkeypatch.setenv("BALDUR_ADMIN_READONLY_KEY", "ro-from-env")
        settings = AdminServerSettings()
        assert settings.readonly_key is not None
        assert settings.readonly_key.get_secret_value() == "ro-from-env"
        assert settings.readonly_key_plain == "ro-from-env"

    @pytest.mark.parametrize(
        ("kwargs", "expected_plain"),
        [
            ({}, (None, None)),
            ({"api_key": "op"}, ("op", None)),
            ({"readonly_key": "ro"}, (None, "ro")),
            ({"api_key": "op", "readonly_key": "ro"}, ("op", "ro")),
            ({"api_key": "", "readonly_key": ""}, (None, None)),
        ],
        ids=[
            "both_unset",
            "operator_only",
            "readonly_only",
            "distinct_keys",
            "both_empty",
        ],
    )
    def test_equal_key_guard_allows_non_conflicting_configs(
        self, monkeypatch, kwargs, expected_plain
    ):
        """The guard fires only on two non-None equal keys — every other
        partition constructs cleanly.

        The both_empty row is the boundary that proves the guard compares the
        _plain accessors (which normalize "" -> None), not the raw secrets: two
        empty strings are equal yet must NOT be rejected, because neither is a
        configured key.
        """
        # Clear ambient admin-key env so the explicit kwargs are the only source.
        monkeypatch.delenv("BALDUR_ADMIN_KEY", raising=False)
        monkeypatch.delenv("BALDUR_ADMIN_READONLY_KEY", raising=False)

        settings = AdminServerSettings(**kwargs)

        assert (settings.api_key_plain, settings.readonly_key_plain) == expected_plain

    def test_equal_keys_raise_validation_error(self, monkeypatch):
        """Configuring both secrets to the same value fails loud at load (D4).

        Identical operator and readonly keys make the shared value's effective
        permission level ambiguous; the model_validator raises ValueError which
        pydantic surfaces as ValidationError. The message names both env vars so
        an operator can fix the misconfiguration.
        """
        monkeypatch.delenv("BALDUR_ADMIN_KEY", raising=False)
        monkeypatch.delenv("BALDUR_ADMIN_READONLY_KEY", raising=False)

        with pytest.raises(ValidationError) as excinfo:
            AdminServerSettings(api_key="same-secret", readonly_key="same-secret")

        message = str(excinfo.value)
        assert "BALDUR_ADMIN_READONLY_KEY" in message
        assert "BALDUR_ADMIN_KEY" in message


# =============================================================================
# Behavior — singleton lifecycle
# =============================================================================


class TestAdminServerSettingsSingletonBehavior:
    """get_admin_server_settings() / reset_admin_server_settings() pair."""

    def test_get_returns_cached_instance(self):
        first = get_admin_server_settings()
        second = get_admin_server_settings()
        assert first is second

    def test_reset_clears_cache(self):
        first = get_admin_server_settings()
        reset_admin_server_settings()
        second = get_admin_server_settings()
        assert first is not second

    def test_reset_picks_up_new_env_values(self, monkeypatch):
        """After reset, a new singleton reads the current env vars."""
        monkeypatch.setenv("BALDUR_ADMIN_PORT", "12345")
        reset_admin_server_settings()
        settings = get_admin_server_settings()
        assert settings.port == 12345
