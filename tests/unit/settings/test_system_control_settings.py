"""
SystemControlSettings unit tests.

Test classification (UNIT_TEST_GUIDELINES §0):
- Contract: defaults and constraints the design (339) states, and the backend
  derivation from a named Redis URL (802 D13) — hardcoded
- Behavior: backend Literal validation, Django fallback, the Redis URL 3-tier
  fallback, environment overrides, the singleton pair

Source under test: settings/system_control.py (SystemControlSettings).
"""

from __future__ import annotations

import pytest
from django.test import override_settings
from pydantic import ValidationError

from baldur.settings.system_control import (
    SystemControlSettings,
    get_system_control_settings,
    reset_system_control_settings,
)


@pytest.fixture(autouse=True)
def _reset_settings():
    """Reset the singleton before and after each test."""
    reset_system_control_settings()
    yield
    reset_system_control_settings()


# =============================================================================
# Contract Tests — design contract values (339 §7.1)
# =============================================================================


class TestSystemControlSettingsDefaultContract:
    """SystemControlSettings default-value design contract."""

    @pytest.fixture(autouse=True)
    def _clean_state_env(self, monkeypatch):
        """Contract tests verify pure defaults — remove test-environment overrides.

        The backend is derived from a named Redis URL, so the URL variables
        this resolver reads go too.
        """
        monkeypatch.delenv("BALDUR_SYSTEM_CONTROL_BACKEND", raising=False)
        monkeypatch.delenv("BALDUR_SYSTEM_CONTROL_REDIS_URL", raising=False)
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)

    def test_backend_default_file(self):
        """With no backend set and no Redis URL named, the store is 'file'."""
        assert SystemControlSettings().backend == "file"

    def test_backend_field_has_no_fixed_default(self):
        """The backend is derived when not set — its declared default is None (D13)."""
        assert SystemControlSettings.model_fields["backend"].default is None

    def test_state_dir_default(self):
        """State directory default: 'logs/baldur_state'."""
        s = SystemControlSettings()
        assert s.state_dir == "logs/baldur_state"
        assert s.dir == "logs/baldur_state"

    def test_redis_url_default_empty(self):
        """Redis URL default: empty string (triggers the fallback chain)."""
        # The fallback runs, so the value may be filled — without env/Django it
        # is the RedisSettings.url default.
        s = SystemControlSettings(redis_url="")
        assert isinstance(s.redis_url, str)

    def test_redis_key_prefix_default(self):
        """Redis key prefix default: 'baldur:state:'."""
        assert SystemControlSettings().redis_key_prefix == "baldur:state:"

    def test_redis_scan_batch_size_default(self):
        """Redis SCAN batch size default: 100."""
        assert SystemControlSettings().redis_scan_batch_size == 100

    def test_redis_max_scan_keys_default(self):
        """Redis max scan keys default: 10000."""
        assert SystemControlSettings().redis_max_scan_keys == 10000

    def test_field_count(self):
        """SystemControlSettings has exactly 6 fields."""
        assert len(SystemControlSettings.model_fields) == 6

    def test_env_prefix(self):
        """Environment variable prefix: BALDUR_SYSTEM_CONTROL_."""
        assert (
            SystemControlSettings.model_config.get("env_prefix")
            == "BALDUR_SYSTEM_CONTROL_"
        )


class TestSystemControlSettingsDerivationContract:
    """Where the switch state lives when no backend is set (D13).

    An explicit backend (env, Django setting or argument) wins; otherwise
    ``redis`` when a Redis URL this resolver dials is named
    (``BALDUR_SYSTEM_CONTROL_REDIS_URL``, ``BALDUR_REDIS_URL`` env or Django
    setting); otherwise ``file``. A Redis named only for another channel does
    not count.
    """

    @pytest.fixture(autouse=True)
    def _clean_redis_env(self, monkeypatch):
        for name in (
            "BALDUR_SYSTEM_CONTROL_BACKEND",
            "BALDUR_SYSTEM_CONTROL_REDIS_URL",
            "BALDUR_REDIS_URL",
            "BALDUR_RESILIENT_STORAGE_REDIS_URL",
        ):
            monkeypatch.delenv(name, raising=False)

    @pytest.mark.parametrize(
        ("env", "expected_backend", "expected_derived"),
        [
            ({}, "file", True),
            ({"BALDUR_SYSTEM_CONTROL_REDIS_URL": "redis://sc:6379/1"}, "redis", True),
            ({"BALDUR_REDIS_URL": "redis://shared:6379/0"}, "redis", True),
            (
                {"BALDUR_RESILIENT_STORAGE_REDIS_URL": "redis://other:6379/0"},
                "file",
                True,
            ),
            (
                {
                    "BALDUR_SYSTEM_CONTROL_BACKEND": "file",
                    "BALDUR_REDIS_URL": "redis://shared:6379/0",
                },
                "file",
                False,
            ),
            ({"BALDUR_SYSTEM_CONTROL_BACKEND": "memory"}, "memory", False),
        ],
        ids=[
            "nothing_named_file",
            "own_url_redis",
            "shared_url_redis",
            "other_channel_only_file",
            "explicit_file_wins",
            "explicit_memory",
        ],
    )
    def test_backend_derivation_from_env(
        self, monkeypatch, env, expected_backend, expected_derived
    ):
        """Each channel's effect on the store the switch state lives in."""
        for name, value in env.items():
            monkeypatch.setenv(name, value)

        settings = SystemControlSettings()

        assert settings.backend == expected_backend
        assert settings.backend_was_derived is expected_derived

    def test_backend_derivation_from_django_redis_url_setting(self):
        """A Redis URL named in Django settings derives 'redis'."""
        with override_settings(BALDUR_REDIS_URL="redis://django:6379/3"):
            settings = SystemControlSettings()

        assert settings.backend == "redis"
        assert settings.backend_was_derived is True

    def test_explicit_django_backend_setting_wins_over_a_named_url(self, monkeypatch):
        """An explicit Django backend is not overridden by the derivation."""
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://shared:6379/0")

        with override_settings(BALDUR_SYSTEM_CONTROL_BACKEND="file"):
            settings = SystemControlSettings()

        assert settings.backend == "file"
        assert settings.backend_was_derived is False

    def test_explicit_argument_is_not_reported_as_derived(self):
        """A backend passed in is set, not derived."""
        settings = SystemControlSettings(backend="redis")

        assert settings.backend == "redis"
        assert settings.backend_was_derived is False


# =============================================================================
# Boundary Tests — field boundary values (§8.1)
# =============================================================================


class TestSystemControlSettingsBoundaryContract:
    """SystemControlSettings field boundary contract."""

    def test_redis_scan_batch_size_below_minimum_rejected(self):
        """redis_scan_batch_size below ge=50 → ValidationError."""
        with pytest.raises(ValidationError):
            SystemControlSettings(redis_scan_batch_size=49)

    def test_redis_scan_batch_size_at_minimum_accepted(self):
        """redis_scan_batch_size at the ge=50 boundary → accepted."""
        s = SystemControlSettings(redis_scan_batch_size=50)
        assert s.redis_scan_batch_size == 50

    def test_redis_scan_batch_size_above_maximum_rejected(self):
        """redis_scan_batch_size above le=1000 → ValidationError."""
        with pytest.raises(ValidationError):
            SystemControlSettings(redis_scan_batch_size=1001)

    def test_redis_max_scan_keys_below_minimum_rejected(self):
        """redis_max_scan_keys below ge=100 → ValidationError."""
        with pytest.raises(ValidationError):
            SystemControlSettings(redis_max_scan_keys=99)

    def test_redis_max_scan_keys_above_maximum_rejected(self):
        """redis_max_scan_keys above le=1_000_000 → ValidationError."""
        with pytest.raises(ValidationError):
            SystemControlSettings(redis_max_scan_keys=1_000_001)


# =============================================================================
# Behavior Tests — backend validation, fallback, environment, singleton
# =============================================================================


class TestSystemControlSettingsBackendValidationBehavior:
    """backend Literal validation."""

    def test_backend_file_accepted(self):
        """'file' backend accepted."""
        s = SystemControlSettings(backend="file")
        assert s.backend == "file"

    def test_backend_redis_accepted(self):
        """'redis' backend accepted."""
        s = SystemControlSettings(backend="redis")
        assert s.backend == "redis"

    def test_backend_memory_accepted(self):
        """'memory' backend accepted."""
        s = SystemControlSettings(backend="memory")
        assert s.backend == "memory"

    def test_backend_invalid_value_rejected(self):
        """An unknown backend value → ValidationError."""
        with pytest.raises(ValidationError):
            SystemControlSettings(backend="dynamodb")

    def test_backend_case_insensitive_normalization(self):
        """The backend value is normalized with .lower()."""
        s = SystemControlSettings(backend="Redis")
        assert s.backend == "redis"

    def test_backend_uppercase_normalization(self):
        """An upper-case backend value is normalized too."""
        s = SystemControlSettings(backend="FILE")
        assert s.backend == "file"


class TestSystemControlSettingsEnvOverrideBehavior:
    """Environment variable overrides."""

    def test_env_override_backend(self, monkeypatch):
        """BALDUR_SYSTEM_CONTROL_BACKEND overrides the backend."""
        monkeypatch.setenv("BALDUR_SYSTEM_CONTROL_BACKEND", "memory")
        s = SystemControlSettings()
        assert s.backend == "memory"

    def test_env_override_state_dir(self, monkeypatch):
        """BALDUR_SYSTEM_CONTROL_DIR overrides dir."""
        monkeypatch.setenv("BALDUR_SYSTEM_CONTROL_DIR", "/custom/path")
        s = SystemControlSettings()
        assert s.dir == "/custom/path"
        assert s.state_dir == "/custom/path"

    def test_env_override_redis_url(self, monkeypatch):
        """BALDUR_SYSTEM_CONTROL_REDIS_URL overrides redis_url."""
        monkeypatch.setenv("BALDUR_SYSTEM_CONTROL_REDIS_URL", "redis://prod:6379/1")
        s = SystemControlSettings()
        assert s.redis_url == "redis://prod:6379/1"


class TestSystemControlSettingsRedisUrlFallbackBehavior:
    """Redis URL 3-tier fallback chain."""

    def test_legacy_env_var_fallback(self, monkeypatch):
        """Tier 1: BALDUR_REDIS_URL (legacy env) fallback."""
        # STATE_REDIS_URL unset, only BALDUR_REDIS_URL set
        monkeypatch.delenv("BALDUR_SYSTEM_CONTROL_REDIS_URL", raising=False)
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://legacy:6379/2")
        s = SystemControlSettings(redis_url="")
        assert s.redis_url == "redis://legacy:6379/2"

    def test_explicit_redis_url_takes_precedence(self, monkeypatch):
        """An explicit STATE_REDIS_URL wins over the fallback."""
        monkeypatch.setenv("BALDUR_SYSTEM_CONTROL_REDIS_URL", "redis://explicit:6379/0")
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis://legacy:6379/2")
        s = SystemControlSettings()
        assert s.redis_url == "redis://explicit:6379/0"


class TestSystemControlSettingsSingletonBehavior:
    """SystemControlSettings singleton pair."""

    def test_get_returns_same_instance(self):
        """get_system_control_settings() returns the same instance."""
        s1 = get_system_control_settings()
        s2 = get_system_control_settings()
        assert s1 is s2

    def test_reset_clears_cached_instance(self):
        """A new instance is created after reset."""
        s1 = get_system_control_settings()
        reset_system_control_settings()
        s2 = get_system_control_settings()
        assert s1 is not s2
