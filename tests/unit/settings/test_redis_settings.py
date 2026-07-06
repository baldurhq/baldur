"""
Unit tests for RedisSettings (settings/redis.py).

Contract tests verify design document values from 328_REDIS_CONNECTION_FACTORY.md §4.4.
Behavior tests verify env var loading, singleton lifecycle, and boundary validation.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import BaseModel, ValidationError

from baldur.settings.redis import (
    RedisSettings,
    apply_redis_url_fallback,
    get_redis_settings,
    reset_redis_settings,
)


class TestRedisSettingsDefaultContract:
    """RedisSettings default values — contract from 328 §4.4."""

    def test_url_default(self):
        """Default URL: redis://localhost:6379/0."""
        settings = RedisSettings()
        assert settings.url == "redis://localhost:6379/0"

    def test_password_default_none(self):
        """Default password: None (no auth by default)."""
        settings = RedisSettings()
        assert settings.password is None

    def test_sentinel_password_default_none(self):
        """Default sentinel_password: None."""
        settings = RedisSettings()
        assert settings.sentinel_password is None

    def test_username_default_none(self):
        """Default username: None (no ACL by default)."""
        settings = RedisSettings()
        assert settings.username is None

    def test_socket_timeout_default(self):
        """Default socket_timeout: 5.0 seconds."""
        settings = RedisSettings()
        assert settings.socket_timeout == 5.0

    def test_socket_connect_timeout_default(self):
        """Default socket_connect_timeout: 5.0 seconds."""
        settings = RedisSettings()
        assert settings.socket_connect_timeout == 5.0

    def test_retry_on_timeout_default_true(self):
        """Default retry_on_timeout: True."""
        settings = RedisSettings()
        assert settings.retry_on_timeout is True

    def test_max_connections_default(self):
        """Default max_connections: 100."""
        settings = RedisSettings()
        assert settings.max_connections == 100

    def test_health_check_interval_default(self):
        """Default health_check_interval: 30 seconds."""
        settings = RedisSettings()
        assert settings.health_check_interval == 30

    def test_env_prefix_is_baldur_redis(self):
        """env_prefix contract: BALDUR_REDIS_."""
        assert RedisSettings.model_config["env_prefix"] == "BALDUR_REDIS_"

    def test_field_count(self):
        """RedisSettings has exactly 10 fields (328 §4.4 table + probe_connect_timeout)."""
        assert len(RedisSettings.model_fields) == 10


class TestRedisSettingsBoundaryContract:
    """RedisSettings field boundary constraints — contract from Field(ge=, le=)."""

    def test_socket_timeout_minimum_boundary(self):
        """socket_timeout ge=0.1: 0.09 fails, 0.1 passes."""
        with pytest.raises(ValidationError):
            RedisSettings(socket_timeout=0.09)
        settings = RedisSettings(socket_timeout=0.1)
        assert settings.socket_timeout == 0.1

    def test_socket_timeout_maximum_boundary(self):
        """socket_timeout le=60.0: 60.0 passes, 60.1 fails."""
        settings = RedisSettings(socket_timeout=60.0)
        assert settings.socket_timeout == 60.0
        with pytest.raises(ValidationError):
            RedisSettings(socket_timeout=60.1)

    def test_socket_connect_timeout_minimum_boundary(self):
        """socket_connect_timeout ge=0.1: 0.09 fails, 0.1 passes."""
        with pytest.raises(ValidationError):
            RedisSettings(socket_connect_timeout=0.09)
        settings = RedisSettings(socket_connect_timeout=0.1)
        assert settings.socket_connect_timeout == 0.1

    def test_socket_connect_timeout_maximum_boundary(self):
        """socket_connect_timeout le=60.0: 60.0 passes, 60.1 fails."""
        settings = RedisSettings(socket_connect_timeout=60.0)
        assert settings.socket_connect_timeout == 60.0
        with pytest.raises(ValidationError):
            RedisSettings(socket_connect_timeout=60.1)

    def test_max_connections_minimum_boundary(self):
        """max_connections ge=1: 0 fails, 1 passes."""
        with pytest.raises(ValidationError):
            RedisSettings(max_connections=0)
        settings = RedisSettings(max_connections=1)
        assert settings.max_connections == 1

    def test_max_connections_maximum_boundary(self):
        """max_connections le=10000: 10000 passes, 10001 fails."""
        settings = RedisSettings(max_connections=10000)
        assert settings.max_connections == 10000
        with pytest.raises(ValidationError):
            RedisSettings(max_connections=10001)

    def test_health_check_interval_minimum_boundary(self):
        """health_check_interval ge=5: 4 fails, 5 passes."""
        with pytest.raises(ValidationError):
            RedisSettings(health_check_interval=4)
        settings = RedisSettings(health_check_interval=5)
        assert settings.health_check_interval == 5

    def test_health_check_interval_maximum_boundary(self):
        """health_check_interval le=300: 300 passes, 301 fails."""
        settings = RedisSettings(health_check_interval=300)
        assert settings.health_check_interval == 300
        with pytest.raises(ValidationError):
            RedisSettings(health_check_interval=301)


class TestRedisProbeConnectTimeoutContract:
    """RedisSettings.probe_connect_timeout — 687 D2.

    Fast-fail connect timeout for lazy Redis liveness probes, deliberately
    shorter than the general ``socket_connect_timeout`` (5.0s).
    """

    def test_probe_connect_timeout_default_is_half_second(self):
        """probe_connect_timeout default: 0.5 seconds (687 D2 literal)."""
        settings = RedisSettings()
        assert settings.probe_connect_timeout == 0.5

    def test_probe_connect_timeout_field_exists(self):
        """probe_connect_timeout is declared on the model."""
        assert "probe_connect_timeout" in RedisSettings.model_fields

    def test_probe_connect_timeout_minimum_boundary(self):
        """probe_connect_timeout ge=0.1 (ShortDuration): 0.09 fails, 0.1 passes."""
        with pytest.raises(ValidationError):
            RedisSettings(probe_connect_timeout=0.09)
        settings = RedisSettings(probe_connect_timeout=0.1)
        assert settings.probe_connect_timeout == 0.1

    def test_probe_connect_timeout_maximum_boundary(self):
        """probe_connect_timeout le=60.0 (ShortDuration): 60.0 passes, 60.1 fails."""
        settings = RedisSettings(probe_connect_timeout=60.0)
        assert settings.probe_connect_timeout == 60.0
        with pytest.raises(ValidationError):
            RedisSettings(probe_connect_timeout=60.1)

    def test_probe_connect_timeout_env_override(self, monkeypatch):
        """BALDUR_REDIS_PROBE_CONNECT_TIMEOUT overrides the default."""
        monkeypatch.setenv("BALDUR_REDIS_PROBE_CONNECT_TIMEOUT", "1.5")
        settings = RedisSettings()
        assert settings.probe_connect_timeout == 1.5


class TestRedisSettingsEnvLoadingBehavior:
    """RedisSettings loads values from BALDUR_REDIS_* env vars."""

    def test_url_from_env(self, monkeypatch):
        """BALDUR_REDIS_URL env var overrides default."""
        monkeypatch.setenv("BALDUR_REDIS_URL", "redis+sentinel://mymaster@s1:26379/2")
        settings = RedisSettings()
        assert settings.url == "redis+sentinel://mymaster@s1:26379/2"

    def test_password_from_env(self, monkeypatch):
        """BALDUR_REDIS_PASSWORD env var sets password."""
        monkeypatch.setenv("BALDUR_REDIS_PASSWORD", "secret123")
        settings = RedisSettings()
        assert settings.password == "secret123"

    def test_sentinel_password_from_env(self, monkeypatch):
        """BALDUR_REDIS_SENTINEL_PASSWORD env var sets sentinel auth."""
        monkeypatch.setenv("BALDUR_REDIS_SENTINEL_PASSWORD", "sent_pass")
        settings = RedisSettings()
        assert settings.sentinel_password == "sent_pass"

    def test_username_from_env(self, monkeypatch):
        """BALDUR_REDIS_USERNAME env var sets ACL username."""
        monkeypatch.setenv("BALDUR_REDIS_USERNAME", "acl_user")
        settings = RedisSettings()
        assert settings.username == "acl_user"

    def test_max_connections_from_env(self, monkeypatch):
        """BALDUR_REDIS_MAX_CONNECTIONS env var overrides default."""
        monkeypatch.setenv("BALDUR_REDIS_MAX_CONNECTIONS", "200")
        settings = RedisSettings()
        assert settings.max_connections == 200


class TestRedisSettingsSingletonBehavior:
    """get_redis_settings()/reset_redis_settings() singleton lifecycle."""

    def setup_method(self):
        """Reset singleton before each test."""
        reset_redis_settings()

    def teardown_method(self):
        """Reset singleton after each test."""
        reset_redis_settings()

    def test_get_returns_same_instance(self):
        """get_redis_settings() returns the same cached instance."""
        first = get_redis_settings()
        second = get_redis_settings()
        assert first is second

    def test_reset_clears_cached_instance(self):
        """reset_redis_settings() clears cache, next get creates new instance."""
        first = get_redis_settings()
        reset_redis_settings()
        second = get_redis_settings()
        assert first is not second

    def test_get_returns_redis_settings_type(self):
        """get_redis_settings() returns a RedisSettings instance."""
        settings = get_redis_settings()
        assert isinstance(settings, RedisSettings)


class TestApplyRedisUrlFallback:
    """Direct unit tests for apply_redis_url_fallback (D3).

    The helper is the shared building block behind the per-class fallback
    validators. It is tested in isolation against a minimal model so the
    mechanism (model_fields_set short-circuit, object.__setattr__,
    fail-safe path, empty-resolved guard) is exercised independently of any
    consumer's own validators. Resolved values are asserted directly (not
    via the DEBUG log) to dodge the structlog capture_logs xdist flake.
    """

    DEFAULT = "redis://localhost:6379/0"

    @pytest.fixture(autouse=True)
    def _isolate_redis_env(self, monkeypatch):
        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        reset_redis_settings()
        yield
        reset_redis_settings()

    @staticmethod
    def _make_model(**kwargs):
        # Minimal model: the helper only touches model_fields_set and
        # object.__setattr__, both inherited from BaseModel, so no env
        # coupling is introduced by the probe model.
        class _FallbackProbeModel(BaseModel):
            redis_url: str = "redis://localhost:6379/0"

        return _FallbackProbeModel(**kwargs)

    def test_fallback_resolves_to_redis_settings_url_when_field_unset(self):
        # Given: field not in model_fields_set, resolver returns a URL
        model = self._make_model()
        resolved = SimpleNamespace(url="redis://resolved:6379/4")

        # When
        with patch("baldur.settings.redis.get_redis_settings", return_value=resolved):
            apply_redis_url_fallback(model, "redis_url")

        # Then: field takes the resolved canonical URL
        assert model.redis_url == "redis://resolved:6379/4"

    def test_fallback_noops_when_field_explicitly_set(self):
        # Given: field explicitly set (per-feature override present)
        model = self._make_model(redis_url="redis://explicit:6379/2")

        # When
        with patch("baldur.settings.redis.get_redis_settings") as mock_get:
            apply_redis_url_fallback(model, "redis_url")

        # Then: override wins and the resolver is never consulted
        assert model.redis_url == "redis://explicit:6379/2"
        mock_get.assert_not_called()

    def test_fallback_fail_safe_keeps_field_on_exception(self):
        # Given: resolver raises during fallback
        model = self._make_model()

        # When
        with (
            patch(
                "baldur.settings.redis.get_redis_settings",
                side_effect=RuntimeError("config assembly failed"),
            ),
            patch("baldur.settings.redis.logger") as mock_logger,
        ):
            apply_redis_url_fallback(model, "redis_url")

        # Then: field unchanged (no exception propagates) + WARNING emitted
        assert model.redis_url == self.DEFAULT
        mock_logger.warning.assert_called_once()
        assert (
            mock_logger.warning.call_args.args[0]
            == "settings.redis_url_fallback_failed"
        )

    def test_fallback_empty_resolved_url_keeps_default(self):
        # Given: BALDUR_REDIS_URL="" resolves to an empty string
        model = self._make_model()
        resolved = SimpleNamespace(url="")

        # When
        with patch("baldur.settings.redis.get_redis_settings", return_value=resolved):
            apply_redis_url_fallback(model, "redis_url")

        # Then: the empty string is NOT propagated past a consumer's
        # min_length=1 — the field keeps its default.
        assert model.redis_url == self.DEFAULT

    def test_fallback_is_idempotent_across_repeated_calls(self):
        # Given
        model = self._make_model()
        resolved = SimpleNamespace(url="redis://resolved:6379/4")

        # When: called twice
        with patch("baldur.settings.redis.get_redis_settings", return_value=resolved):
            apply_redis_url_fallback(model, "redis_url")
            apply_redis_url_fallback(model, "redis_url")

        # Then: same resolved value (no drift)
        assert model.redis_url == "redis://resolved:6379/4"
