"""
Control-API rate-limit config source unification — tier-consistency guard.

``get_rate_limit_config()`` must source the Control-API limit / window /
emergency values from the single canonical ``RateLimitSettings`` surface
(``BALDUR_RATE_LIMIT_*``) so the same env var takes effect identically with
and without a runtime-config manager registered — the divergence that made
``BALDUR_RATE_LIMIT_CONTROL_API_RATE_LIMIT`` a silent no-op in OSS-only while
a separate free-tier variable family was silently shadowed once PRO was
present.

Also pins:
- the emergency cap narrowing to 100 (``MediumCount``) against re-widening;
- PRO-absent as the normal path (no per-request WARNING), while a *registered*
  manager that raises still WARNs and falls back to settings.

The registry is driven by patching the ``_get_runtime_config_manager`` seam so
the two branches are exercised deterministically regardless of ambient
registry state under ``-n`` parallelism.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import ValidationError

# Canonical env values used across the reflection tests. Distinct from the
# defaults so a passing assertion proves the env var actually flowed through.
_CANON = {
    "BALDUR_RATE_LIMIT_CONTROL_API_RATE_LIMIT": "137",
    "BALDUR_RATE_LIMIT_CONTROL_API_WINDOW_SECONDS": "45",
    "BALDUR_RATE_LIMIT_EMERGENCY_RATE_LIMIT": "7",
    "BALDUR_RATE_LIMIT_EMERGENCY_WINDOW_SECONDS": "30",
}
_EXPECTED = {
    "control_api_rate_limit": 137,
    "control_api_window_seconds": 45,
    "emergency_rate_limit": 7,
    "emergency_window_seconds": 30,
}


@pytest.fixture(autouse=True)
def _reset_rate_limit_settings():
    """Reset the canonical RateLimitSettings singleton around each test."""
    from baldur.settings.rate_limit import reset_rate_limit_settings

    reset_rate_limit_settings()
    yield
    reset_rate_limit_settings()


def _set_canonical_env(monkeypatch) -> None:
    from baldur.settings.rate_limit import reset_rate_limit_settings

    for name, value in _CANON.items():
        monkeypatch.setenv(name, value)
    reset_rate_limit_settings()


def _seeded_stub_manager():
    """A stub RuntimeConfigManager that seeds from RateLimitSettings, like PRO.

    The real PRO RuntimeConfigManager seeds its ``rate_limit`` defaults from
    ``to_dict(RateLimitSettings())``, so its config reflects the same canonical
    env var. This stub mirrors that so the "manager registered" path is proven
    tier-consistent with the "no manager" path.
    """
    from baldur.settings.rate_limit import get_rate_limit_settings

    class _StubManager:
        def get_rate_limit_config(self) -> dict:
            settings = get_rate_limit_settings()
            return {
                "control_api_rate_limit": settings.control_api_rate_limit,
                "control_api_window_seconds": settings.control_api_window_seconds,
                "emergency_rate_limit": settings.emergency_rate_limit,
                "emergency_window_seconds": settings.emergency_window_seconds,
            }

    return _StubManager()


class TestConfigSourceUnification:
    """The canonical env var governs identically with and without a manager."""

    def test_reflected_without_registered_manager(self, monkeypatch):
        """PRO absent (safe_get -> None): settings-sourced dict governs."""
        from baldur.api.django.rate_limit import config

        _set_canonical_env(monkeypatch)
        monkeypatch.setattr(config, "_get_runtime_config_manager", lambda: None)

        result = config.get_rate_limit_config()

        assert result == _EXPECTED

    def test_reflected_with_registered_manager(self, monkeypatch):
        """A registered (PRO-like) manager seeds from the same canonical var."""
        from baldur.api.django.rate_limit import config

        _set_canonical_env(monkeypatch)
        monkeypatch.setattr(
            config, "_get_runtime_config_manager", lambda: _seeded_stub_manager()
        )

        result = config.get_rate_limit_config()

        # Identical to the no-manager path — the tier-divergence is gone.
        assert result == _EXPECTED

    def test_both_paths_agree(self, monkeypatch):
        """The reflected limit is the same value in both tiers (G1 resolved)."""
        from baldur.api.django.rate_limit import config

        _set_canonical_env(monkeypatch)

        monkeypatch.setattr(config, "_get_runtime_config_manager", lambda: None)
        without = config.get_rate_limit_config()

        monkeypatch.setattr(
            config, "_get_runtime_config_manager", lambda: _seeded_stub_manager()
        )
        with_manager = config.get_rate_limit_config()

        assert without == with_manager == _EXPECTED

    def test_registered_manager_overrides_present_keys_and_falls_back_for_absent(
        self, monkeypatch
    ):
        """A manager's returned keys win; omitted keys fall back to settings.

        The seeded stub returns settings-equal values, so it cannot tell
        "manager path taken" from "settings used". A manager returning a
        *distinct* value for one key and omitting the other three proves both
        the manager-override precedence AND the per-key settings fallback
        (``config.get(key, settings_config[key])``) that the seeded stub never
        exercises.
        """
        from baldur.api.django.rate_limit import config

        # Given: canonical settings (137/45/7/30) plus a manager overriding
        # only the normal limit with a distinct value, omitting the rest.
        _set_canonical_env(monkeypatch)

        class _PartialManager:
            def get_rate_limit_config(self) -> dict:
                return {"control_api_rate_limit": 999}

        monkeypatch.setattr(
            config, "_get_runtime_config_manager", lambda: _PartialManager()
        )

        # When
        result = config.get_rate_limit_config()

        # Then: present key reflects the manager (proves the manager path is
        # actually taken); absent keys fall back to the canonical settings.
        assert result["control_api_rate_limit"] == 999
        assert result["control_api_window_seconds"] == 45
        assert result["emergency_rate_limit"] == 7
        assert result["emergency_window_seconds"] == 30


class TestProAbsentIsNormalPath:
    """Manager-absent is expected; only a registered-manager failure WARNs."""

    def test_absent_manager_logs_no_warning(self, monkeypatch):
        from baldur.api.django.rate_limit import config

        _set_canonical_env(monkeypatch)
        monkeypatch.setattr(config, "_get_runtime_config_manager", lambda: None)

        with patch.object(config, "logger") as mock_logger:
            config.get_rate_limit_config()

        mock_logger.warning.assert_not_called()

    def test_registered_manager_failure_warns_and_falls_back(self, monkeypatch):
        from baldur.api.django.rate_limit import config

        _set_canonical_env(monkeypatch)

        class _BoomManager:
            def get_rate_limit_config(self):
                raise RuntimeError("runtime config unavailable")

        monkeypatch.setattr(
            config, "_get_runtime_config_manager", lambda: _BoomManager()
        )

        with patch.object(config, "logger") as mock_logger:
            result = config.get_rate_limit_config()

        mock_logger.warning.assert_called_once()
        # Settings fallback still governs the returned values.
        assert result == _EXPECTED


class TestEmergencyCapNarrowing:
    """The sole canonical emergency surface is capped at 100 (MediumCount)."""

    def test_emergency_rate_limit_above_100_is_rejected(self, monkeypatch):
        from baldur.settings.rate_limit import (
            get_rate_limit_settings,
            reset_rate_limit_settings,
        )

        monkeypatch.setenv("BALDUR_RATE_LIMIT_EMERGENCY_RATE_LIMIT", "101")
        reset_rate_limit_settings()

        with pytest.raises(ValidationError):
            get_rate_limit_settings()

    def test_emergency_rate_limit_at_100_is_accepted(self, monkeypatch):
        from baldur.settings.rate_limit import (
            get_rate_limit_settings,
            reset_rate_limit_settings,
        )

        monkeypatch.setenv("BALDUR_RATE_LIMIT_EMERGENCY_RATE_LIMIT", "100")
        reset_rate_limit_settings()

        assert get_rate_limit_settings().emergency_rate_limit == 100
