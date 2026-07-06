"""Unit tests for ``baldur.settings.protect`` — ProtectSettings (429 Part 1, C1 + 449 + 482).

Scope:
- Contract defaults (enabled=True, default_circuit_breaker=True,
  default_retry=False, default_dlq=False, default_timeout_seconds=None).
- BALDUR_PROTECT_* env var prefix override.
- Singleton lifecycle (get / reset).
- Timeout field boundary analysis (449 → flipped to None by 482).
"""

from __future__ import annotations

import pytest

from baldur.settings.protect import (
    ProtectSettings,
    get_protect_settings,
    reset_protect_settings,
)


@pytest.fixture(autouse=True)
def _reset_settings():
    reset_protect_settings()
    yield
    reset_protect_settings()


# =============================================================================
# Contract — default values per 429 Part 1 Implementation Decisions
# =============================================================================


class TestProtectSettingsContract:
    """Contract defaults for ProtectSettings — hardcoded per design doc."""

    def test_default_values_match_documented_contract(self):
        """429 C1: enabled=True, CB on, retry off, dlq off by default."""
        settings = ProtectSettings()

        assert settings.enabled is True
        assert settings.default_circuit_breaker is True
        assert settings.default_retry is False
        assert settings.default_dlq is False

    def test_env_prefix_is_baldur_protect(self):
        """Contract: env var prefix is ``BALDUR_PROTECT_`` per Part 1 C1 wording."""
        prefix = ProtectSettings.model_config.get("env_prefix")

        assert prefix == "BALDUR_PROTECT_"


# =============================================================================
# Behavior — env var override + singleton lifecycle
# =============================================================================


class TestProtectSettingsEnvOverrideBehavior:
    """``BALDUR_PROTECT_*`` env vars must override the defaults."""

    def test_enabled_overridable_via_env(self, monkeypatch):
        """Setting BALDUR_PROTECT_ENABLED=false flips ``enabled`` to False."""
        monkeypatch.setenv("BALDUR_PROTECT_ENABLED", "false")

        settings = ProtectSettings()

        assert settings.enabled is False

    def test_default_retry_overridable_via_env(self, monkeypatch):
        """BALDUR_PROTECT_DEFAULT_RETRY=true opts the facade into retry defaults."""
        monkeypatch.setenv("BALDUR_PROTECT_DEFAULT_RETRY", "true")

        settings = ProtectSettings()

        assert settings.default_retry is True


class TestProtectSettingsSingletonBehavior:
    """get/reset lifecycle — same instance returned until reset."""

    def test_get_returns_same_instance_on_repeated_calls(self):
        """Cached singleton — two successive get_ calls return the same object."""
        first = get_protect_settings()
        second = get_protect_settings()

        assert first is second

    def test_reset_forces_fresh_instance_next_call(self):
        """reset_protect_settings() clears the cache; the next get_ returns a new obj."""
        first = get_protect_settings()
        reset_protect_settings()
        second = get_protect_settings()

        assert first is not second


# =============================================================================
# Contract — timeout field (449)
# =============================================================================


class TestProtectSettingsTimeoutContract:
    """default_timeout_seconds design contract — flipped to None per 482 D1."""

    def test_default_timeout_seconds_is_none(self):
        """482 D1: explicit-opt-in timeout — I/O-layer timeouts (httpx /
        psycopg / redis-py) are the enforced safety net for default callers.
        Set BALDUR_PROTECT_DEFAULT_TIMEOUT_SECONDS=30 to restore pre-#482."""
        settings = ProtectSettings()
        assert settings.default_timeout_seconds is None

    def test_timeout_none_disables_timeout(self):
        """482 D1: None — the new default — disables timeout entirely."""
        settings = ProtectSettings(default_timeout_seconds=None)
        assert settings.default_timeout_seconds is None

    @pytest.mark.parametrize(
        "value",
        [0, -1, -0.001],
        ids=["zero", "negative_int", "negative_float"],
    )
    def test_timeout_rejects_non_positive_values(self, value):
        """449 D7: Field(gt=0) rejects zero and negative values."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ProtectSettings(default_timeout_seconds=value)

    def test_timeout_accepts_small_positive_value(self):
        """449 D7: smallest positive float is accepted."""
        settings = ProtectSettings(default_timeout_seconds=0.001)
        assert settings.default_timeout_seconds == 0.001

    def test_timeout_overridable_via_env(self, monkeypatch):
        """BALDUR_PROTECT_DEFAULT_TIMEOUT_SECONDS env var overrides the default."""
        monkeypatch.setenv("BALDUR_PROTECT_DEFAULT_TIMEOUT_SECONDS", "10.0")
        settings = ProtectSettings()
        assert settings.default_timeout_seconds == 10.0
