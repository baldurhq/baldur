"""
API Rate Limit Settings integration tests.

Verifies api/django/rate_limit.py and ApiRateLimitSettings integration
in a Django environment.
"""

import pytest


@pytest.mark.django_db
class TestApiRateLimitSettingsIntegration:
    """api/django/rate_limit.py and ApiRateLimitSettings integration tests."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        """Reset all singletons before/after each test."""
        from baldur.settings.api_rate_limit import reset_api_rate_limit_settings
        from baldur.settings.rate_limit import reset_rate_limit_settings

        reset_api_rate_limit_settings()
        reset_rate_limit_settings()
        yield
        reset_api_rate_limit_settings()
        reset_rate_limit_settings()

    def test_get_rate_limit_config_sources_canonical_env_var(self, monkeypatch):
        """get_rate_limit_config() sources the limit from the canonical
        RateLimitSettings surface (BALDUR_RATE_LIMIT_*) on the OSS path."""
        from baldur.settings.rate_limit import reset_rate_limit_settings

        monkeypatch.setenv("BALDUR_RATE_LIMIT_CONTROL_API_RATE_LIMIT", "250")
        monkeypatch.setenv("BALDUR_RATE_LIMIT_EMERGENCY_RATE_LIMIT", "25")
        reset_rate_limit_settings()

        # Force the PRO-absent path so the canonical settings surface governs
        # deterministically, independent of any registered PRO manager.
        monkeypatch.setattr(
            "baldur.api.django.rate_limit.config._get_runtime_config_manager",
            lambda: None,
        )

        from baldur.api.django.rate_limit import get_rate_limit_config

        config = get_rate_limit_config()

        assert config["control_api_rate_limit"] == 250
        assert config["emergency_rate_limit"] == 25

    def test_get_setting_helper_reads_from_settings(self, monkeypatch):
        """_get_setting helper reads a staying ApiRateLimitSettings field."""
        from baldur.settings.api_rate_limit import reset_api_rate_limit_settings

        monkeypatch.setenv(
            "BALDUR_API_RATE_LIMIT_CONTROL_API_PATH_PREFIX", "/custom/admin/"
        )
        reset_api_rate_limit_settings()

        from baldur.api.django.rate_limit.config import (
            _FALLBACK_CONTROL_API_PATH_PREFIX,
            _get_setting,
        )

        prefix = _get_setting(
            "control_api_path_prefix", _FALLBACK_CONTROL_API_PATH_PREFIX
        )

        assert prefix == "/custom/admin/"

    def test_get_local_limiter_uses_cleanup_interval_setting(self, monkeypatch):
        """get_local_limiter() passes cleanup_interval from settings to SlidingWindowLimiter."""
        from baldur.api.django.rate_limit.middleware import (
            get_local_limiter,
        )
        from baldur.services.rate_limit import SlidingWindowLimiter
        from baldur.settings.api_rate_limit import reset_api_rate_limit_settings

        monkeypatch.setenv("BALDUR_API_RATE_LIMIT_LOCAL_CLEANUP_INTERVAL", "120")
        reset_api_rate_limit_settings()

        # Force singleton recreation
        import baldur.api.django.rate_limit.middleware as mod

        mod._local_limiter = None

        limiter = get_local_limiter()
        assert isinstance(limiter, SlidingWindowLimiter)
        assert limiter._cleanup_interval == 120

    def test_redis_health_checker_uses_settings(self, monkeypatch):
        """RedisHealthChecker uses settings values."""
        from baldur.api.django.rate_limit import RedisHealthChecker
        from baldur.settings.api_rate_limit import reset_api_rate_limit_settings

        monkeypatch.setenv("BALDUR_API_RATE_LIMIT_REDIS_PING_INTERVAL", "15")
        monkeypatch.setenv("BALDUR_API_RATE_LIMIT_REDIS_FAILURE_THRESHOLD", "7")
        monkeypatch.setenv("BALDUR_API_RATE_LIMIT_REDIS_RECOVERY_JITTER_MAX", "30")

        reset_api_rate_limit_settings()

        checker = RedisHealthChecker()

        assert checker.ping_interval == 15
        assert checker.failure_threshold == 7
        assert checker.recovery_jitter_max == 30

    def test_sliding_window_limiter_check_respects_per_call_params(self):
        """SlidingWindowLimiter.check() uses per-call max_requests/window_seconds."""
        from baldur.services.rate_limit import SlidingWindowLimiter

        limiter = SlidingWindowLimiter()

        state = limiter.check("test_key", max_requests=2, window_seconds=60)
        assert state.allowed is True
        assert state.remaining == 1

        state = limiter.check("test_key", max_requests=2, window_seconds=60)
        assert state.allowed is True
        assert state.remaining == 0

        state = limiter.check("test_key", max_requests=2, window_seconds=60)
        assert state.allowed is False
        assert state.remaining == 0
