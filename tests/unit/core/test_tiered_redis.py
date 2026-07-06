"""
Tiered Redis Provider Unit Tests.

Tests TieredRedisProvider and related functions.

Reference: docs/baldur/middleware_system/70_MULTI_CLUSTER_ARCHITECTURE.md
"""

from unittest.mock import MagicMock, patch

import pytest

from baldur.settings.redis import reset_redis_settings
from baldur.settings.tiered_redis import TieredRedisSettings


def _make_settings(
    local_url: str = "redis://localhost:6379/0",
    local_password: str | None = None,
    global_url: str | None = None,
    global_password: str | None = None,
) -> TieredRedisSettings:
    """Create TieredRedisSettings for testing."""
    return TieredRedisSettings(
        local_url=local_url,
        local_password=local_password,
        global_url=global_url,
        global_password=global_password,
    )


class TestTieredRedisProviderInit:
    """TieredRedisProvider initialization tests."""

    def setup_method(self):
        from baldur.core.tiered_redis import reset_tiered_redis_provider
        from baldur.settings.tiered_redis import reset_tiered_redis_settings

        reset_tiered_redis_provider()
        reset_tiered_redis_settings()

    def teardown_method(self):
        from baldur.core.tiered_redis import reset_tiered_redis_provider
        from baldur.settings.tiered_redis import reset_tiered_redis_settings

        reset_tiered_redis_provider()
        reset_tiered_redis_settings()

    def test_default_urls(self, monkeypatch):
        """Default URL settings."""
        from baldur.core.tiered_redis import TieredRedisProvider

        # Remove env vars to use defaults
        monkeypatch.delenv("BALDUR_TIERED_REDIS_LOCAL_URL", raising=False)
        monkeypatch.delenv("BALDUR_TIERED_REDIS_GLOBAL_URL", raising=False)

        settings = _make_settings()
        provider = TieredRedisProvider(settings=settings)
        assert "localhost" in provider.local_url
        assert provider.global_url == provider.local_url

    def test_custom_urls(self):
        """Custom URL settings."""
        from baldur.core.tiered_redis import TieredRedisProvider

        settings = _make_settings(
            local_url="redis://local:6379/0",
            global_url="redis://global:6379/0",
        )
        provider = TieredRedisProvider(settings=settings)

        assert provider.local_url == "redis://local:6379/0"
        assert provider.global_url == "redis://global:6379/0"

    def test_env_var_urls(self, monkeypatch):
        """Load URLs from environment variables."""
        from baldur.core.tiered_redis import TieredRedisProvider
        from baldur.settings.tiered_redis import reset_tiered_redis_settings

        reset_tiered_redis_settings()

        monkeypatch.setenv("BALDUR_TIERED_REDIS_LOCAL_URL", "redis://env-local:6379/0")
        monkeypatch.setenv(
            "BALDUR_TIERED_REDIS_GLOBAL_URL", "redis://env-global:6379/0"
        )

        # Create settings from env vars
        settings = TieredRedisSettings()
        provider = TieredRedisProvider(settings=settings)

        assert provider.local_url == "redis://env-local:6379/0"
        assert provider.global_url == "redis://env-global:6379/0"

    def test_global_fallback_to_local(self):
        """Use local_url if global_url is not set."""
        from baldur.core.tiered_redis import TieredRedisProvider

        settings = _make_settings(local_url="redis://local:6379/0")
        provider = TieredRedisProvider(settings=settings)
        assert provider.local_url == provider.global_url


class TestTieredRedisLocalUrlFallback:
    """D5: ``local_url`` resolves to BALDUR_REDIS_URL when not explicitly set,
    and ``global_url`` transitively inherits the resolved ``local_url``. A
    per-class override (BALDUR_TIERED_REDIS_LOCAL_URL) wins.
    """

    DEFAULT = "redis://localhost:6379/0"
    GLOBAL = "redis://global-host:6379/1"
    OVERRIDE = "redis://tiered-local:6379/2"

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch):
        from baldur.settings.tiered_redis import reset_tiered_redis_settings

        monkeypatch.delenv("BALDUR_REDIS_URL", raising=False)
        monkeypatch.delenv("BALDUR_TIERED_REDIS_LOCAL_URL", raising=False)
        monkeypatch.delenv("BALDUR_TIERED_REDIS_GLOBAL_URL", raising=False)
        reset_redis_settings()
        reset_tiered_redis_settings()
        yield
        reset_redis_settings()
        reset_tiered_redis_settings()

    def test_local_url_per_class_override_only_wins(self, monkeypatch):
        monkeypatch.setenv("BALDUR_TIERED_REDIS_LOCAL_URL", self.OVERRIDE)
        assert TieredRedisSettings().local_url == self.OVERRIDE

    def test_local_url_falls_back_to_baldur_redis_url(self, monkeypatch):
        monkeypatch.setenv("BALDUR_REDIS_URL", self.GLOBAL)
        reset_redis_settings()
        assert TieredRedisSettings().local_url == self.GLOBAL

    def test_local_url_override_wins_when_both_set(self, monkeypatch):
        monkeypatch.setenv("BALDUR_REDIS_URL", self.GLOBAL)
        monkeypatch.setenv("BALDUR_TIERED_REDIS_LOCAL_URL", self.OVERRIDE)
        reset_redis_settings()
        assert TieredRedisSettings().local_url == self.OVERRIDE

    def test_local_url_default_when_neither_set(self):
        assert TieredRedisSettings().local_url == self.DEFAULT

    def test_global_url_inherits_resolved_local_url(self, monkeypatch):
        # D5: the helper runs BEFORE the ``global_url = local_url`` default
        # logic, so an unset global_url inherits the resolved local_url.
        monkeypatch.setenv("BALDUR_REDIS_URL", self.GLOBAL)
        reset_redis_settings()
        settings = TieredRedisSettings()
        assert settings.global_url == self.GLOBAL
        assert settings.global_url == settings.local_url

    def test_explicit_global_url_not_overwritten_by_fallback(self, monkeypatch):
        # An explicitly-set global_url is preserved; only local_url is resolved.
        monkeypatch.setenv("BALDUR_REDIS_URL", self.GLOBAL)
        reset_redis_settings()
        settings = TieredRedisSettings(global_url="redis://explicit-global:6379/5")
        assert settings.local_url == self.GLOBAL
        assert settings.global_url == "redis://explicit-global:6379/5"


class TestRedisScope:
    """RedisScope Enum tests."""

    def test_scope_values(self):
        """Verify RedisScope values."""
        from baldur.core.tiered_redis import RedisScope

        assert RedisScope.LOCAL.value == "local"
        assert RedisScope.GLOBAL.value == "global"


class TestTieredRedisProviderIsTiered:
    """is_tiered property tests."""

    def test_is_tiered_same_url(self):
        """is_tiered=False when URLs are identical."""
        from baldur.core.tiered_redis import TieredRedisProvider

        settings = _make_settings(
            local_url="redis://same:6379/0",
            global_url="redis://same:6379/0",
        )
        provider = TieredRedisProvider(settings=settings)

        assert provider.is_tiered is False

    def test_is_tiered_different_url(self):
        """is_tiered=True when URLs are different."""
        from baldur.core.tiered_redis import TieredRedisProvider

        settings = _make_settings(
            local_url="redis://local:6379/0",
            global_url="redis://global:6379/0",
        )
        provider = TieredRedisProvider(settings=settings)

        assert provider.is_tiered is True


class TestTieredRedisProviderSingleton:
    """TieredRedisProvider singleton tests."""

    def setup_method(self):
        from baldur.core.tiered_redis import reset_tiered_redis_provider

        reset_tiered_redis_provider()

    def teardown_method(self):
        from baldur.core.tiered_redis import reset_tiered_redis_provider

        reset_tiered_redis_provider()

    def test_singleton_returns_same_instance(self):
        """Singleton returns same instance."""
        from baldur.core.tiered_redis import (
            get_tiered_redis_provider,
            reset_tiered_redis_provider,
        )

        reset_tiered_redis_provider()
        p1 = get_tiered_redis_provider()
        p2 = get_tiered_redis_provider()
        assert p1 is p2

    def test_reset_clears_singleton(self):
        """New instance created after reset."""
        from baldur.core.tiered_redis import (
            get_tiered_redis_provider,
            reset_tiered_redis_provider,
        )

        p1 = get_tiered_redis_provider()
        reset_tiered_redis_provider()
        p2 = get_tiered_redis_provider()
        assert p1 is not p2


class TestTieredRedisProviderGetRedis:
    """get_redis method tests (mocked)."""

    def test_get_redis_local_scope(self):
        """Get client with LOCAL scope."""
        from baldur.core.tiered_redis import RedisScope, TieredRedisProvider

        settings = _make_settings(
            local_url="redis://local:6379/0",
            global_url="redis://global:6379/0",
        )
        provider = TieredRedisProvider(settings=settings)

        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory"
        ) as mock_get_factory:
            mock_factory = MagicMock()
            mock_client = MagicMock()
            mock_factory.create.return_value = mock_client
            mock_get_factory.return_value = mock_factory

            client = provider.get_redis(RedisScope.LOCAL)

            mock_factory.create.assert_called_with("redis://local:6379/0")
            assert client is mock_client

    def test_get_redis_global_scope(self):
        """Get client with GLOBAL scope."""
        from baldur.core.tiered_redis import RedisScope, TieredRedisProvider

        settings = _make_settings(
            local_url="redis://local:6379/0",
            global_url="redis://global:6379/0",
        )
        provider = TieredRedisProvider(settings=settings)

        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory"
        ) as mock_get_factory:
            mock_factory = MagicMock()
            mock_client = MagicMock()
            mock_factory.create.return_value = mock_client
            mock_get_factory.return_value = mock_factory

            client = provider.get_redis(RedisScope.GLOBAL)

            mock_factory.create.assert_called_with("redis://global:6379/0")
            assert client is mock_client

    def test_global_reuses_local_when_same_url(self):
        """GLOBAL reuses LOCAL client when URLs are identical."""
        from baldur.core.tiered_redis import RedisScope, TieredRedisProvider

        settings = _make_settings(
            local_url="redis://same:6379/0",
            global_url="redis://same:6379/0",
        )
        provider = TieredRedisProvider(settings=settings)

        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory"
        ) as mock_get_factory:
            mock_factory = MagicMock()
            mock_client = MagicMock()
            mock_factory.create.return_value = mock_client
            mock_get_factory.return_value = mock_factory

            local_client = provider.get_redis(RedisScope.LOCAL)
            global_client = provider.get_redis(RedisScope.GLOBAL)

            # factory.create should be called only once
            assert mock_factory.create.call_count == 1
            assert local_client is global_client

    def test_lazy_initialization(self):
        """Verify lazy initialization."""
        from baldur.core.tiered_redis import RedisScope, TieredRedisProvider

        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory"
        ) as mock_get_factory:
            mock_factory = MagicMock()
            mock_client = MagicMock()
            mock_factory.create.return_value = mock_client
            mock_get_factory.return_value = mock_factory

            settings = _make_settings(
                local_url="redis://local:6379/0",
                global_url="redis://global:6379/0",
            )
            provider = TieredRedisProvider(settings=settings)

            # Not called at creation time
            mock_factory.create.assert_not_called()

            # Initialized on get_redis call
            provider.get_redis(RedisScope.LOCAL)
            assert mock_factory.create.call_count == 1


class TestTieredRedisProviderHealthCheck:
    """health_check method tests."""

    def test_health_check_both_healthy(self):
        """Both healthy."""
        from baldur.core.tiered_redis import TieredRedisProvider

        settings = _make_settings(
            local_url="redis://local:6379/0",
            global_url="redis://global:6379/0",
        )
        provider = TieredRedisProvider(settings=settings)

        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory"
        ) as mock_get_factory:
            mock_factory = MagicMock()
            mock_client = MagicMock()
            mock_client.ping.return_value = True
            mock_factory.create.return_value = mock_client
            mock_get_factory.return_value = mock_factory

            result = provider.health_check()

            assert result["local"]["status"] == "healthy"
            assert result["global"]["status"] == "healthy"

    def test_health_check_local_only(self):
        """Check LOCAL only."""
        from baldur.core.tiered_redis import RedisScope, TieredRedisProvider

        settings = _make_settings()
        provider = TieredRedisProvider(settings=settings)

        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory"
        ) as mock_get_factory:
            mock_factory = MagicMock()
            mock_client = MagicMock()
            mock_client.ping.return_value = True
            mock_factory.create.return_value = mock_client
            mock_get_factory.return_value = mock_factory

            result = provider.health_check(scope=RedisScope.LOCAL)

            assert "local" in result
            assert "global" not in result

    def test_health_check_failure(self):
        """Connection failure."""
        from baldur.core.tiered_redis import TieredRedisProvider

        settings = _make_settings()
        provider = TieredRedisProvider(settings=settings)

        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory"
        ) as mock_get_factory:
            mock_factory = MagicMock()
            mock_client = MagicMock()
            mock_client.ping.side_effect = Exception("Connection refused")
            mock_factory.create.return_value = mock_client
            mock_get_factory.return_value = mock_factory

            result = provider.health_check()

            assert result["local"]["status"] == "unhealthy"
            assert "Connection refused" in result["local"]["error"]


class TestTieredRedisProviderClose:
    """close method tests."""

    def test_close_both_clients(self):
        """Close both clients."""
        from baldur.core.tiered_redis import RedisScope, TieredRedisProvider

        settings = _make_settings(
            local_url="redis://local:6379/0",
            global_url="redis://global:6379/0",
        )
        provider = TieredRedisProvider(settings=settings)

        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory"
        ) as mock_get_factory:
            mock_factory = MagicMock()
            mock_local = MagicMock()
            mock_global = MagicMock()
            mock_factory.create.side_effect = [mock_local, mock_global]
            mock_get_factory.return_value = mock_factory

            # Create clients
            provider.get_redis(RedisScope.LOCAL)
            provider.get_redis(RedisScope.GLOBAL)

            # Close
            provider.close()

            mock_local.close.assert_called_once()
            mock_global.close.assert_called_once()

    def test_close_handles_errors(self):
        """No exception raised even if error occurs during close."""
        from baldur.core.tiered_redis import RedisScope, TieredRedisProvider

        settings = _make_settings()
        provider = TieredRedisProvider(settings=settings)

        with patch(
            "baldur.adapters.redis.connection_factory.get_redis_connection_factory"
        ) as mock_get_factory:
            mock_factory = MagicMock()
            mock_client = MagicMock()
            mock_client.close.side_effect = Exception("Close error")
            mock_factory.create.return_value = mock_client
            mock_get_factory.return_value = mock_factory

            provider.get_redis(RedisScope.LOCAL)

            # Completes without exception
            provider.close()
