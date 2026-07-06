"""
ApiRateLimitSettings unit tests.

Validates the Django API rate-limiting middleware's non-limit settings:
- default values
- environment-variable overrides
- field validation (min/max ranges)
- singleton pattern

The per-minute limit / window / emergency values live on the canonical
RateLimitSettings surface (BALDUR_RATE_LIMIT_*) and are covered by their own
tests; this class owns only the surrounding middleware configuration.
"""

import pytest
from pydantic import ValidationError


class TestApiRateLimitSettings:
    """ApiRateLimitSettings tests."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset the singleton before and after each test."""
        from baldur.settings.api_rate_limit import reset_api_rate_limit_settings

        reset_api_rate_limit_settings()
        yield
        reset_api_rate_limit_settings()

    def test_default_values(self):
        """Default values match the middleware's expected defaults."""
        from baldur.settings.api_rate_limit import ApiRateLimitSettings

        settings = ApiRateLimitSettings()

        # Control API Path
        assert settings.control_api_path_prefix == "/api/baldur/"

        # Redis Health Checker
        assert settings.redis_ping_interval == 5
        assert settings.redis_failure_threshold == 3
        assert settings.redis_recovery_jitter_max == 10
        assert settings.redis_ping_timeout_ms == 100

        # Local Memory Limiter
        assert settings.local_cleanup_interval == 60

    def test_env_override_redis_health_settings(self, monkeypatch):
        """Environment variables override the Redis health-checker settings."""
        from baldur.settings.api_rate_limit import ApiRateLimitSettings

        monkeypatch.setenv("BALDUR_API_RATE_LIMIT_REDIS_PING_INTERVAL", "10")
        monkeypatch.setenv("BALDUR_API_RATE_LIMIT_REDIS_FAILURE_THRESHOLD", "5")
        monkeypatch.setenv("BALDUR_API_RATE_LIMIT_REDIS_RECOVERY_JITTER_MAX", "20")

        settings = ApiRateLimitSettings()
        assert settings.redis_ping_interval == 10
        assert settings.redis_failure_threshold == 5
        assert settings.redis_recovery_jitter_max == 20

    def test_env_override_control_api_path(self, monkeypatch):
        """Environment variable overrides the Control API path prefix."""
        from baldur.settings.api_rate_limit import ApiRateLimitSettings

        monkeypatch.setenv(
            "BALDUR_API_RATE_LIMIT_CONTROL_API_PATH_PREFIX", "/custom/api/"
        )

        settings = ApiRateLimitSettings()
        assert settings.control_api_path_prefix == "/custom/api/"

    def test_validation_redis_ping_interval_range(self):
        """redis_ping_interval range (1-60)."""
        from baldur.settings.api_rate_limit import ApiRateLimitSettings

        # Too low
        with pytest.raises(ValidationError):
            ApiRateLimitSettings(redis_ping_interval=0)

        # Too high
        with pytest.raises(ValidationError):
            ApiRateLimitSettings(redis_ping_interval=61)

        # Valid edge case
        settings = ApiRateLimitSettings(redis_ping_interval=60)
        assert settings.redis_ping_interval == 60

    def test_validation_redis_failure_threshold_range(self):
        """redis_failure_threshold range (1-20)."""
        from baldur.settings.api_rate_limit import ApiRateLimitSettings

        # Too low
        with pytest.raises(ValidationError):
            ApiRateLimitSettings(redis_failure_threshold=0)

        # Too high
        with pytest.raises(ValidationError):
            ApiRateLimitSettings(redis_failure_threshold=21)

    def test_validation_recovery_jitter_max_range(self):
        """redis_recovery_jitter_max range (1-60)."""
        from baldur.settings.api_rate_limit import ApiRateLimitSettings

        # Too low
        with pytest.raises(ValidationError):
            ApiRateLimitSettings(redis_recovery_jitter_max=0)

        # Too high
        with pytest.raises(ValidationError):
            ApiRateLimitSettings(redis_recovery_jitter_max=61)

    def test_validation_local_cleanup_interval_range(self):
        """local_cleanup_interval range (10-300)."""
        from baldur.settings.api_rate_limit import ApiRateLimitSettings

        # Too low
        with pytest.raises(ValidationError):
            ApiRateLimitSettings(local_cleanup_interval=9)

        # Too high
        with pytest.raises(ValidationError):
            ApiRateLimitSettings(local_cleanup_interval=301)

    def test_singleton_pattern(self):
        """get_api_rate_limit_settings returns a cached singleton."""
        from baldur.settings.api_rate_limit import (
            get_api_rate_limit_settings,
            reset_api_rate_limit_settings,
        )

        # First call
        settings1 = get_api_rate_limit_settings()

        # Second call returns the same instance
        settings2 = get_api_rate_limit_settings()
        assert settings1 is settings2

        # After reset, a fresh instance
        reset_api_rate_limit_settings()
        settings3 = get_api_rate_limit_settings()
        assert settings1 is not settings3

    def test_singleton_env_reload(self, monkeypatch):
        """A reset after an env change reflects the new value."""
        from baldur.settings.api_rate_limit import (
            get_api_rate_limit_settings,
            reset_api_rate_limit_settings,
        )

        # Initial value
        settings1 = get_api_rate_limit_settings()
        assert settings1.redis_ping_interval == 5

        # Change env then reset
        monkeypatch.setenv("BALDUR_API_RATE_LIMIT_REDIS_PING_INTERVAL", "30")
        reset_api_rate_limit_settings()

        # New value reflected
        settings2 = get_api_rate_limit_settings()
        assert settings2.redis_ping_interval == 30


# =============================================================================
# The Django integration tests requiring Django settings live separately at
# tests/integration/test_api_rate_limit_integration.py (excluded from the
# pure unit suite).
# =============================================================================
