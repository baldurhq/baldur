"""
Unit tests for ResilientStorageSettings (#470 D8).

Replaces the legacy ResilientStorageConfig dataclass with a Pydantic
settings class so BALDUR_RESILIENT_STORAGE_* env vars flow through
automatically and operators can tune the recovery loop without code
changes.

Coverage:
- Contract: defaults, env-var prefix, per-field range validators,
  cross-field validator (jitter <= probe_interval).
- Behavior: env var override, singleton get/reset semantics,
  ResilienceGroup integration.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings.redis import reset_redis_settings
from baldur.settings.resilient_storage import (
    ResilientStorageSettings,
    get_resilient_storage_settings,
    reset_resilient_storage_settings,
)

# =============================================================================
# Contract: design-doc values + env var prefix
# =============================================================================


class TestResilientStorageSettingsContract:
    """Design-doc contract values and Pydantic config."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        reset_resilient_storage_settings()
        yield
        reset_resilient_storage_settings()

    def test_env_prefix(self):
        """env_prefix is BALDUR_RESILIENT_STORAGE_ per #470 D8."""
        assert (
            ResilientStorageSettings.model_config["env_prefix"]
            == "BALDUR_RESILIENT_STORAGE_"
        )

    def test_default_redis_url(self, monkeypatch):
        # D4: redis_url now carries a BALDUR_REDIS_URL fallback validator;
        # clear the canonical var so the localhost default is observable.
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        reset_redis_settings()
        settings = ResilientStorageSettings()
        assert settings.redis_url == "redis://localhost:6379/0"

    def test_default_wal_dir(self):
        settings = ResilientStorageSettings()
        assert settings.wal_dir == "/var/log/baldur/wal"

    def test_default_recovery_jitter_max(self):
        """Default 5.0s — matches D2/D4 thundering-herd dispersion."""
        settings = ResilientStorageSettings()
        assert settings.recovery_jitter_max == 5.0

    def test_default_recovery_probe_interval(self):
        """Default 5.0s — distinct from 30s first-init cooldown (D3)."""
        settings = ResilientStorageSettings()
        assert settings.recovery_probe_interval == 5.0

    def test_default_auto_recovery(self):
        """Default True — opt-out kill switch via env var."""
        settings = ResilientStorageSettings()
        assert settings.auto_recovery is True

    def test_default_key_prefix(self):
        settings = ResilientStorageSettings()
        assert settings.key_prefix == "baldur:"

    def test_default_allow_memory_only(self):
        settings = ResilientStorageSettings()
        assert settings.allow_memory_only is False

    def test_default_use_dynamic_prefix(self):
        settings = ResilientStorageSettings()
        assert settings.use_dynamic_prefix is True

    def test_default_degraded_blob_memory_max_bytes(self):
        """Default 128 MiB (#539 D2) — byte budget for the degraded blob store."""
        settings = ResilientStorageSettings()
        assert settings.degraded_blob_memory_max_bytes == 134217728


# =============================================================================
# Contract: per-field range validators (Pydantic Field constraints)
# =============================================================================


class TestResilientStorageSettingsBoundaries:
    """Boundary analysis — just below/above each ge=/le= constraint."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        reset_resilient_storage_settings()
        yield
        reset_resilient_storage_settings()

    # -- recovery_jitter_max: [0.0, 30.0] --

    def test_jitter_max_accepts_lower_bound(self):
        settings = ResilientStorageSettings(
            recovery_jitter_max=0.0, recovery_probe_interval=1.0
        )
        assert settings.recovery_jitter_max == 0.0

    def test_jitter_max_accepts_upper_bound(self):
        settings = ResilientStorageSettings(
            recovery_jitter_max=30.0, recovery_probe_interval=30.0
        )
        assert settings.recovery_jitter_max == 30.0

    def test_jitter_max_rejects_below_lower_bound(self):
        with pytest.raises(ValidationError):
            ResilientStorageSettings(recovery_jitter_max=-0.1)

    def test_jitter_max_rejects_above_upper_bound(self):
        with pytest.raises(ValidationError):
            ResilientStorageSettings(recovery_jitter_max=30.1)

    # -- recovery_probe_interval: [1.0, 60.0] --

    def test_probe_interval_accepts_lower_bound(self):
        settings = ResilientStorageSettings(
            recovery_jitter_max=0.0, recovery_probe_interval=1.0
        )
        assert settings.recovery_probe_interval == 1.0

    def test_probe_interval_accepts_upper_bound(self):
        settings = ResilientStorageSettings(recovery_probe_interval=60.0)
        assert settings.recovery_probe_interval == 60.0

    def test_probe_interval_rejects_below_lower_bound(self):
        """0.99s would let jitter sleep dominate the cooldown loop."""
        with pytest.raises(ValidationError):
            ResilientStorageSettings(recovery_probe_interval=0.99)

    def test_probe_interval_rejects_above_upper_bound(self):
        """>60s leaves diverged-write window too wide at PRO scale."""
        with pytest.raises(ValidationError):
            ResilientStorageSettings(recovery_probe_interval=60.1)

    # -- redis_url / wal_dir / key_prefix: min_length=1 --

    def test_redis_url_rejects_empty(self):
        with pytest.raises(ValidationError):
            ResilientStorageSettings(redis_url="")

    def test_wal_dir_rejects_empty(self):
        with pytest.raises(ValidationError):
            ResilientStorageSettings(wal_dir="")

    def test_key_prefix_rejects_empty(self):
        with pytest.raises(ValidationError):
            ResilientStorageSettings(key_prefix="")

    # -- degraded_blob_memory_max_bytes: [1048576, 2147483648] (#539 D2) --

    def test_blob_memory_max_bytes_accepts_lower_bound(self):
        settings = ResilientStorageSettings(degraded_blob_memory_max_bytes=1048576)
        assert settings.degraded_blob_memory_max_bytes == 1048576

    def test_blob_memory_max_bytes_accepts_upper_bound(self):
        settings = ResilientStorageSettings(degraded_blob_memory_max_bytes=2147483648)
        assert settings.degraded_blob_memory_max_bytes == 2147483648

    def test_blob_memory_max_bytes_rejects_below_lower_bound(self):
        """< 1 MiB floor — too small to hold even one DLQ blob batch."""
        with pytest.raises(ValidationError):
            ResilientStorageSettings(degraded_blob_memory_max_bytes=1048575)

    def test_blob_memory_max_bytes_rejects_above_upper_bound(self):
        """> 2 GiB ceiling — fail-safe bounded, no unbounded option."""
        with pytest.raises(ValidationError):
            ResilientStorageSettings(degraded_blob_memory_max_bytes=2147483649)


# =============================================================================
# Contract: cross-field validator — jitter <= probe_interval
# =============================================================================


class TestResilientStorageSettingsCrossFieldValidator:
    """#470 D8 model_validator: ``recovery_jitter_max`` must not exceed
    ``recovery_probe_interval`` (logical sanity check — per-field
    ranges allow jitter=5 + probe=2 which would leak past one cycle).
    """

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        reset_resilient_storage_settings()
        yield
        reset_resilient_storage_settings()

    def test_jitter_equal_to_probe_interval_is_accepted(self):
        """Boundary: jitter == probe_interval is the design max."""
        settings = ResilientStorageSettings(
            recovery_jitter_max=5.0, recovery_probe_interval=5.0
        )
        assert settings.recovery_jitter_max == settings.recovery_probe_interval

    def test_jitter_less_than_probe_interval_is_accepted(self):
        settings = ResilientStorageSettings(
            recovery_jitter_max=2.0, recovery_probe_interval=10.0
        )
        assert settings.recovery_jitter_max < settings.recovery_probe_interval

    def test_jitter_greater_than_probe_interval_is_rejected(self):
        """Misconfig: jitter sleep extends past next probe window."""
        with pytest.raises(ValidationError) as exc_info:
            ResilientStorageSettings(
                recovery_jitter_max=10.0, recovery_probe_interval=5.0
            )
        # Validator message names both fields so the operator can
        # diagnose without reading the source.
        assert "recovery_jitter_max" in str(exc_info.value)
        assert "recovery_probe_interval" in str(exc_info.value)


# =============================================================================
# Behavior: env var parsing
# =============================================================================


class TestResilientStorageSettingsEnvVars:
    """env var override flows through Pydantic when an instance is
    constructed after ``monkeypatch.setenv``.
    """

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        reset_resilient_storage_settings()
        yield
        reset_resilient_storage_settings()

    def test_env_var_overrides_redis_url(self, monkeypatch):
        monkeypatch.setenv(
            "BALDUR_RESILIENT_STORAGE_REDIS_URL", "redis://override:6379/9"
        )
        settings = ResilientStorageSettings()
        assert settings.redis_url == "redis://override:6379/9"

    def test_env_var_overrides_recovery_probe_interval(self, monkeypatch):
        monkeypatch.setenv("BALDUR_RESILIENT_STORAGE_RECOVERY_PROBE_INTERVAL", "12.5")
        settings = ResilientStorageSettings()
        assert settings.recovery_probe_interval == 12.5

    def test_env_var_overrides_recovery_jitter_max(self, monkeypatch):
        monkeypatch.setenv("BALDUR_RESILIENT_STORAGE_RECOVERY_JITTER_MAX", "3.0")
        settings = ResilientStorageSettings()
        assert settings.recovery_jitter_max == 3.0

    def test_env_var_overrides_auto_recovery_false(self, monkeypatch):
        """Operator kill switch — disables the lazy recovery loop."""
        monkeypatch.setenv("BALDUR_RESILIENT_STORAGE_AUTO_RECOVERY", "false")
        settings = ResilientStorageSettings()
        assert settings.auto_recovery is False

    def test_env_var_overrides_allow_memory_only(self, monkeypatch):
        monkeypatch.setenv("BALDUR_RESILIENT_STORAGE_ALLOW_MEMORY_ONLY", "true")
        settings = ResilientStorageSettings()
        assert settings.allow_memory_only is True

    def test_env_var_overrides_degraded_blob_memory_max_bytes(self, monkeypatch):
        monkeypatch.setenv(
            "BALDUR_RESILIENT_STORAGE_DEGRADED_BLOB_MEMORY_MAX_BYTES", "67108864"
        )
        settings = ResilientStorageSettings()
        assert settings.degraded_blob_memory_max_bytes == 67108864

    def test_env_var_violating_cross_field_validator_raises(self, monkeypatch):
        """Cross-field validator fires on env-driven config too."""
        monkeypatch.setenv("BALDUR_RESILIENT_STORAGE_RECOVERY_JITTER_MAX", "10.0")
        monkeypatch.setenv("BALDUR_RESILIENT_STORAGE_RECOVERY_PROBE_INTERVAL", "5.0")
        with pytest.raises(ValidationError):
            ResilientStorageSettings()


# =============================================================================
# Behavior: singleton get/reset
# =============================================================================


class TestResilientStorageSettingsSingleton:
    """``get_resilient_storage_settings()`` / ``reset_*()`` lifecycle."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        reset_resilient_storage_settings()
        yield
        reset_resilient_storage_settings()

    def test_singleton_returns_same_instance(self):
        s1 = get_resilient_storage_settings()
        s2 = get_resilient_storage_settings()
        assert s1 is s2

    def test_reset_creates_new_instance(self):
        s1 = get_resilient_storage_settings()
        reset_resilient_storage_settings()
        s2 = get_resilient_storage_settings()
        assert s1 is not s2

    def test_env_override_visible_after_reset(self, monkeypatch):
        """Reset semantics: env vars set after first ``get_*`` only
        take effect once ``reset_*`` clears the cache.
        """
        s1 = get_resilient_storage_settings()
        # Sanity: default values pre-override.
        assert s1.recovery_probe_interval == 5.0

        monkeypatch.setenv("BALDUR_RESILIENT_STORAGE_RECOVERY_PROBE_INTERVAL", "20.0")
        # Cache still serves stale instance.
        assert get_resilient_storage_settings() is s1

        reset_resilient_storage_settings()
        s2 = get_resilient_storage_settings()
        assert s2.recovery_probe_interval == 20.0

    def test_resilience_group_integration(self):
        """Settings are exposed via ``get_config().resilience.resilient_storage``
        — required wiring for any caller using the SSOT root.
        """
        from baldur.settings.root import get_config

        settings = get_config().resilience.resilient_storage
        assert isinstance(settings, ResilientStorageSettings)


# =============================================================================
# Behavior: D4 — redis_url BALDUR_REDIS_URL fallback validator
# =============================================================================


class TestResilientStorageRedisUrlFallback:
    """D4: ``redis_url`` resolves to BALDUR_REDIS_URL when not explicitly set;
    a per-class override (env var or kwarg) wins via the ``model_fields_set``
    convention. ``min_length=1`` is preserved (no empty-string sentinel).
    """

    DEFAULT = "redis://localhost:6379/0"
    GLOBAL = "redis://global-host:6379/1"
    OVERRIDE = "redis://override-host:6379/9"

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch):
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        monkeypatch.delenv("BALDUR_RESILIENT_STORAGE_REDIS_URL", raising=False)
        reset_redis_settings()
        reset_resilient_storage_settings()
        yield
        reset_redis_settings()
        reset_resilient_storage_settings()

    def test_per_class_env_override_only_wins(self, monkeypatch):
        monkeypatch.setenv("BALDUR_RESILIENT_STORAGE_REDIS_URL", self.OVERRIDE)
        assert ResilientStorageSettings().redis_url == self.OVERRIDE

    def test_baldur_redis_url_only_falls_back(self, monkeypatch):
        monkeypatch.setenv("BALDUR_REDIS_URL", self.GLOBAL)
        reset_redis_settings()
        assert ResilientStorageSettings().redis_url == self.GLOBAL

    def test_per_class_override_wins_when_both_set(self, monkeypatch):
        monkeypatch.setenv("BALDUR_REDIS_URL", self.GLOBAL)
        monkeypatch.setenv("BALDUR_RESILIENT_STORAGE_REDIS_URL", self.OVERRIDE)
        reset_redis_settings()
        assert ResilientStorageSettings().redis_url == self.OVERRIDE

    def test_localhost_default_when_neither_set(self):
        assert ResilientStorageSettings().redis_url == self.DEFAULT

    def test_explicit_kwarg_override_wins(self, monkeypatch):
        # bootstrap injects redis_url=... as an explicit kwarg → model_fields_set
        # → fallback no-ops and the injected value is honored.
        monkeypatch.setenv("BALDUR_REDIS_URL", self.GLOBAL)
        reset_redis_settings()
        settings = ResilientStorageSettings(redis_url=self.OVERRIDE)
        assert settings.redis_url == self.OVERRIDE

    def test_min_length_still_rejects_empty_kwarg(self):
        # D4 keeps min_length=1: an empty kwarg is still a hard validation
        # error (the fallback relies on model_fields_set, not an empty sentinel).
        with pytest.raises(ValidationError):
            ResilientStorageSettings(redis_url="")
