"""
Unit tests for RedisConnectionFactory (adapters/redis/connection_factory.py).

Contract tests verify URL scheme routing from 328_REDIS_CONNECTION_FACTORY.md §3.1-3.2.
Behavior tests verify auth injection, URL masking, None filtering, and singleton lifecycle.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.redis.connection_factory import (
    RedisConnectionFactory,
    get_redis_connection_factory,
    reset_redis_connection_factory,
)
from baldur.settings.redis import RedisSettings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def default_settings():
    """RedisSettings with no auth (defaults)."""
    return RedisSettings()


@pytest.fixture
def auth_settings():
    """RedisSettings with password and username configured."""
    return RedisSettings(
        password="master_pass",
        username="acl_user",
        sentinel_password="sentinel_pass",
    )


@pytest.fixture
def factory(default_settings):
    """RedisConnectionFactory with default settings."""
    return RedisConnectionFactory(settings=default_settings)


@pytest.fixture
def auth_factory(auth_settings):
    """RedisConnectionFactory with auth settings."""
    return RedisConnectionFactory(settings=auth_settings)


# ===========================================================================
# Contract: URL scheme routing (328 §3.1)
# ===========================================================================


class TestUrlSchemeRoutingContract:
    """URL scheme determines which Redis client type is created (328 §3.1)."""

    @patch("redis.from_url", autospec=True)
    def test_redis_scheme_calls_from_url(self, mock_from_url, factory):
        """redis:// URL routes to redis.from_url() (standalone)."""
        factory.create("redis://host:6379/0")
        mock_from_url.assert_called_once()

    @patch("redis.from_url", autospec=True)
    def test_rediss_scheme_calls_from_url(self, mock_from_url, factory):
        """rediss:// URL routes to redis.from_url() (TLS standalone)."""
        factory.create("rediss://host:6380/0")
        mock_from_url.assert_called_once()
        call_args = mock_from_url.call_args
        assert call_args[0][0] == "rediss://host:6380/0"

    @patch("redis.sentinel.Sentinel", autospec=True)
    def test_sentinel_scheme_creates_sentinel(self, mock_sentinel_cls, factory):
        """redis+sentinel:// URL routes to Sentinel.master_for()."""
        mock_sentinel = MagicMock()
        mock_sentinel_cls.return_value = mock_sentinel

        factory.create("redis+sentinel://mymaster@s1:26379,s2:26379/0")

        mock_sentinel_cls.assert_called_once()
        mock_sentinel.master_for.assert_called_once()

    @patch("redis.cluster.RedisCluster", autospec=True)
    @patch("redis.cluster.ClusterNode", autospec=True)
    def test_cluster_scheme_creates_cluster(
        self, mock_node_cls, mock_cluster_cls, factory
    ):
        """redis+cluster:// URL routes to RedisCluster."""
        factory.create("redis+cluster://n1:7000,n2:7001")
        mock_cluster_cls.assert_called_once()


# ===========================================================================
# Contract: Sentinel URL parsing (328 §3.2)
# ===========================================================================


class TestSentinelUrlParsingContract:
    """Sentinel URL format: redis+sentinel://master@host1:port,host2:port/db."""

    @patch("redis.sentinel.Sentinel", autospec=True)
    def test_sentinel_parses_master_name(self, mock_sentinel_cls, factory):
        """Master name is extracted from URL and passed to master_for()."""
        mock_sentinel = MagicMock()
        mock_sentinel_cls.return_value = mock_sentinel

        factory.create("redis+sentinel://mymaster@s1:26379/0")

        mock_sentinel.master_for.assert_called_once()
        call_kwargs = mock_sentinel.master_for.call_args
        assert call_kwargs[0][0] == "mymaster"

    @patch("redis.sentinel.Sentinel", autospec=True)
    def test_sentinel_parses_multiple_hosts(self, mock_sentinel_cls, factory):
        """Multiple sentinel hosts are parsed and passed as list of tuples."""
        mock_sentinel_cls.return_value = MagicMock()

        factory.create("redis+sentinel://mymaster@s1:26379,s2:26380,s3:26381/0")

        sentinels_arg = mock_sentinel_cls.call_args[0][0]
        assert sentinels_arg == [
            ("s1", 26379),
            ("s2", 26380),
            ("s3", 26381),
        ]

    @patch("redis.sentinel.Sentinel", autospec=True)
    def test_sentinel_parses_db_number(self, mock_sentinel_cls, factory):
        """DB number is extracted from URL and passed to master_for()."""
        mock_sentinel = MagicMock()
        mock_sentinel_cls.return_value = mock_sentinel

        factory.create("redis+sentinel://mymaster@s1:26379/3")

        call_kwargs = mock_sentinel.master_for.call_args[1]
        assert call_kwargs["db"] == 3

    @patch("redis.sentinel.Sentinel", autospec=True)
    def test_sentinel_default_db_zero_when_omitted(self, mock_sentinel_cls, factory):
        """DB defaults to 0 when not specified in URL."""
        mock_sentinel = MagicMock()
        mock_sentinel_cls.return_value = mock_sentinel

        factory.create("redis+sentinel://mymaster@s1:26379")

        call_kwargs = mock_sentinel.master_for.call_args[1]
        assert call_kwargs["db"] == 0

    def test_invalid_sentinel_url_raises_value_error(self, factory):
        """Invalid sentinel URL format raises ValueError."""
        with pytest.raises(ValueError, match="Invalid Sentinel URL format"):
            factory.create("redis+sentinel://missing-at-sign:26379/0")

    def test_sentinel_node_without_port_raises_value_error(self, factory):
        """Sentinel node without port raises clear ValueError."""
        with pytest.raises(ValueError, match="Invalid Sentinel node format"):
            factory.create("redis+sentinel://mymaster@host_no_port/0")

    @patch("redis.sentinel.Sentinel", autospec=True)
    def test_sentinel_kwargs_include_socket_timeouts(self, mock_sentinel_cls, factory):
        """Sentinel node connections receive socket timeouts via sentinel_kwargs."""
        mock_sentinel_cls.return_value = MagicMock()

        factory.create(
            "redis+sentinel://mymaster@s1:26379/0",
            socket_timeout=3.0,
            socket_connect_timeout=2.0,
        )

        call_kwargs = mock_sentinel_cls.call_args[1]
        assert call_kwargs["sentinel_kwargs"]["socket_timeout"] == 3.0
        assert call_kwargs["sentinel_kwargs"]["socket_connect_timeout"] == 2.0


# ===========================================================================
# Contract: Cluster URL parsing (328 §3.2)
# ===========================================================================


class TestClusterUrlParsingContract:
    """Cluster URL format: redis+cluster://host1:port,host2:port."""

    @patch("redis.cluster.RedisCluster", autospec=True)
    @patch("redis.cluster.ClusterNode", autospec=True)
    def test_cluster_parses_multiple_nodes(
        self, mock_node_cls, mock_cluster_cls, factory
    ):
        """Multiple cluster nodes are parsed into ClusterNode list."""
        factory.create("redis+cluster://n1:7000,n2:7001,n3:7002")

        assert mock_node_cls.call_count == 3
        mock_node_cls.assert_any_call("n1", 7000)
        mock_node_cls.assert_any_call("n2", 7001)
        mock_node_cls.assert_any_call("n3", 7002)

    def test_invalid_cluster_url_raises_value_error(self, factory):
        """Invalid cluster URL format raises ValueError."""
        with pytest.raises(ValueError, match="Invalid Cluster URL format"):
            factory.create("redis+cluster://")

    def test_cluster_node_without_port_raises_value_error(self, factory):
        """Cluster node without port raises clear ValueError."""
        with pytest.raises(ValueError, match="Invalid Cluster node format"):
            factory.create("redis+cluster://host_no_port")


# ===========================================================================
# Behavior: Auth injection (328 §3.7)
# ===========================================================================


class TestAuthInjectionBehavior:
    """Auth credentials are injected from settings, not from URL."""

    @patch("redis.from_url", autospec=True)
    def test_password_injected_from_settings(self, mock_from_url, auth_factory):
        """settings.password is injected into kwargs['password']."""
        auth_factory.create("redis://host:6379/0")

        call_kwargs = mock_from_url.call_args[1]
        assert call_kwargs["password"] == "master_pass"

    @patch("redis.from_url", autospec=True)
    def test_username_injected_from_settings(self, mock_from_url, auth_factory):
        """settings.username is injected into kwargs['username'] (Redis 6.0+ ACL)."""
        auth_factory.create("redis://host:6379/0")

        call_kwargs = mock_from_url.call_args[1]
        assert call_kwargs["username"] == "acl_user"

    @patch("redis.from_url", autospec=True)
    def test_no_auth_injected_when_settings_empty(self, mock_from_url, factory):
        """No auth kwargs are added when settings.password/username are None."""
        factory.create("redis://host:6379/0")

        call_kwargs = mock_from_url.call_args[1]
        assert "password" not in call_kwargs
        assert "username" not in call_kwargs

    @patch("redis.from_url", autospec=True)
    def test_explicit_password_kwarg_not_overwritten(self, mock_from_url, auth_factory):
        """Caller-provided password kwarg takes precedence over settings."""
        auth_factory.create("redis://host:6379/0", password="explicit_pass")

        call_kwargs = mock_from_url.call_args[1]
        assert call_kwargs["password"] == "explicit_pass"

    @patch("redis.sentinel.Sentinel", autospec=True)
    def test_sentinel_password_separated_from_master_password(
        self, mock_sentinel_cls, auth_factory
    ):
        """Sentinel node auth and master auth use separate passwords."""
        mock_sentinel = MagicMock()
        mock_sentinel_cls.return_value = mock_sentinel

        auth_factory.create("redis+sentinel://mymaster@s1:26379/0")

        # Sentinel node gets sentinel_password
        sentinel_call_kwargs = mock_sentinel_cls.call_args[1]
        assert sentinel_call_kwargs["sentinel_kwargs"]["password"] == "sentinel_pass"

        # Master gets master password
        master_call_kwargs = mock_sentinel.master_for.call_args[1]
        assert master_call_kwargs["password"] == "master_pass"


# ===========================================================================
# Behavior: None kwargs filtering
# ===========================================================================


class TestNoneKwargsFilteringBehavior:
    """None-valued kwargs are filtered out before passing to redis-py."""

    @patch("redis.from_url", autospec=True)
    def test_none_socket_timeout_defaults_from_settings(self, mock_from_url, factory):
        """socket_timeout=None resolves to the RedisSettings default (#581).

        A connected-but-hung Redis must not block the caller indefinitely, so an
        unset socket_timeout is backfilled from settings rather than dropped to
        redis-py's no-timeout default.
        """
        factory.create("redis://host:6379/0", socket_timeout=None)

        call_kwargs = mock_from_url.call_args[1]
        assert call_kwargs["socket_timeout"] == RedisSettings().socket_timeout

    @patch("redis.from_url", autospec=True)
    def test_none_max_connections_still_filtered(self, mock_from_url, factory):
        """Genuinely optional None kwargs (max_connections) are still dropped."""
        factory.create("redis://host:6379/0", max_connections=None)

        call_kwargs = mock_from_url.call_args[1]
        assert "max_connections" not in call_kwargs

    @patch("redis.from_url", autospec=True)
    def test_explicit_values_are_passed(self, mock_from_url, factory):
        """Explicitly set values are forwarded to redis.from_url()."""
        factory.create(
            "redis://host:6379/0",
            socket_timeout=3.0,
            decode_responses=True,
            max_connections=50,
        )

        call_kwargs = mock_from_url.call_args[1]
        assert call_kwargs["socket_timeout"] == 3.0
        assert call_kwargs["decode_responses"] is True
        assert call_kwargs["max_connections"] == 50

    @patch("redis.cluster.RedisCluster", autospec=True)
    @patch("redis.cluster.ClusterNode", autospec=True)
    def test_max_connections_stripped_for_cluster(
        self, mock_node_cls, mock_cluster_cls, factory
    ):
        """max_connections is removed for RedisCluster (not a valid arg)."""
        factory.create("redis+cluster://n1:7000", max_connections=100)

        call_kwargs = mock_cluster_cls.call_args[1]
        assert "max_connections" not in call_kwargs


# ===========================================================================
# Behavior: socket_timeout default safety (#581)
# ===========================================================================


class TestSocketTimeoutDefaultBehavior:
    """Every topology enforces a bounded read timeout from settings (#581).

    Regression guard for the hung-Redis blocking bug: an unset socket_timeout
    previously reached redis-py with no read timeout on the standalone/cluster
    paths (the sentinel path already defaulted), so a connected-but-hung Redis
    blocked the caller until the OS TCP timeout.
    """

    @patch("redis.from_url", autospec=True)
    def test_standalone_defaults_both_timeouts_from_settings(
        self, mock_from_url, factory
    ):
        """Standalone client without explicit timeouts inherits settings defaults."""
        factory.create("redis://host:6379/0")

        call_kwargs = mock_from_url.call_args[1]
        assert call_kwargs["socket_timeout"] == RedisSettings().socket_timeout
        assert (
            call_kwargs["socket_connect_timeout"]
            == RedisSettings().socket_connect_timeout
        )

    @patch("redis.cluster.RedisCluster", autospec=True)
    @patch("redis.cluster.ClusterNode", autospec=True)
    def test_cluster_defaults_socket_timeout_from_settings(
        self, mock_node_cls, mock_cluster_cls, factory
    ):
        """Cluster client without explicit timeouts inherits settings defaults."""
        factory.create("redis+cluster://n1:7000")

        call_kwargs = mock_cluster_cls.call_args[1]
        assert call_kwargs["socket_timeout"] == RedisSettings().socket_timeout
        assert (
            call_kwargs["socket_connect_timeout"]
            == RedisSettings().socket_connect_timeout
        )

    @patch("redis.sentinel.Sentinel", autospec=True)
    def test_sentinel_defaults_socket_timeout_from_settings(
        self, mock_sentinel_cls, factory
    ):
        """Sentinel node connections inherit settings defaults when unset."""
        mock_sentinel_cls.return_value = MagicMock()

        factory.create("redis+sentinel://mymaster@s1:26379/0")

        call_kwargs = mock_sentinel_cls.call_args[1]
        assert call_kwargs["socket_timeout"] == RedisSettings().socket_timeout
        assert (
            call_kwargs["sentinel_kwargs"]["socket_timeout"]
            == RedisSettings().socket_timeout
        )

    @patch("redis.from_url", autospec=True)
    def test_custom_settings_socket_timeout_propagates(self, mock_from_url):
        """A non-default RedisSettings.socket_timeout flows to the client."""
        custom = RedisConnectionFactory(settings=RedisSettings(socket_timeout=12.5))
        custom.create("redis://host:6379/0")

        assert mock_from_url.call_args[1]["socket_timeout"] == 12.5

    @patch("redis.from_url", autospec=True)
    def test_explicit_socket_timeout_overrides_settings_default(
        self, mock_from_url, factory
    ):
        """An explicit socket_timeout takes precedence over the settings default."""
        factory.create("redis://host:6379/0", socket_timeout=0.25)

        assert mock_from_url.call_args[1]["socket_timeout"] == 0.25


# ===========================================================================
# Behavior: retry_on_timeout default from settings
# ===========================================================================


class TestRetryOnTimeoutDefaultBehavior:
    """retry_on_timeout backfills from RedisSettings when unset.

    Without the backfill an unset retry_on_timeout was dropped by the None
    filter and silently reverted to redis-py's own default instead of the
    configured RedisSettings.retry_on_timeout (the sync-adapter parity gap).
    """

    @patch("redis.from_url", autospec=True)
    def test_none_retry_on_timeout_defaults_from_settings(self, mock_from_url, factory):
        """retry_on_timeout=None resolves to the RedisSettings default."""
        factory.create("redis://host:6379/0", retry_on_timeout=None)

        call_kwargs = mock_from_url.call_args[1]
        assert call_kwargs["retry_on_timeout"] == RedisSettings().retry_on_timeout

    @patch("redis.from_url", autospec=True)
    def test_unset_retry_on_timeout_defaults_from_settings(
        self, mock_from_url, factory
    ):
        """A client built without retry_on_timeout inherits the settings default."""
        factory.create("redis://host:6379/0")

        call_kwargs = mock_from_url.call_args[1]
        assert call_kwargs["retry_on_timeout"] == RedisSettings().retry_on_timeout

    @patch("redis.from_url", autospec=True)
    def test_custom_settings_retry_on_timeout_propagates(self, mock_from_url):
        """A non-default RedisSettings.retry_on_timeout flows to the client."""
        custom = RedisConnectionFactory(settings=RedisSettings(retry_on_timeout=False))
        custom.create("redis://host:6379/0")

        assert mock_from_url.call_args[1]["retry_on_timeout"] is False

    @patch("redis.from_url", autospec=True)
    def test_explicit_retry_on_timeout_overrides_settings(self, mock_from_url, factory):
        """An explicit retry_on_timeout=False overrides the settings default (True)."""
        factory.create("redis://host:6379/0", retry_on_timeout=False)

        assert mock_from_url.call_args[1]["retry_on_timeout"] is False


# ===========================================================================
# Behavior: Host:port parsing
# ===========================================================================


class TestHostPortParsingBehavior:
    """_parse_host_port_list validates host:port format."""

    def test_parses_single_host_port(self):
        """Single host:port returns one tuple."""
        result = RedisConnectionFactory._parse_host_port_list("h1:6379", "Test")
        assert result == [("h1", 6379)]

    def test_parses_multiple_host_ports(self):
        """Comma-separated host:port pairs are all parsed."""
        result = RedisConnectionFactory._parse_host_port_list(
            "h1:6379,h2:6380,h3:6381", "Test"
        )
        assert result == [("h1", 6379), ("h2", 6380), ("h3", 6381)]

    def test_missing_port_raises_value_error(self):
        """Host without port raises ValueError with context."""
        with pytest.raises(ValueError, match="Invalid Test node format: 'host_only'"):
            RedisConnectionFactory._parse_host_port_list("host_only", "Test")

    def test_non_numeric_port_raises_value_error(self):
        """Non-numeric port raises ValueError."""
        with pytest.raises(ValueError, match="Invalid Test node format"):
            RedisConnectionFactory._parse_host_port_list("host:abc", "Test")


# ===========================================================================
# Behavior: URL masking
# ===========================================================================


class TestUrlMaskingBehavior:
    """URL passwords are masked for safe logging."""

    def test_mask_url_with_password(self):
        """Password in URL is replaced with ***."""
        masked = RedisConnectionFactory._mask_url("redis://user:secret@host:6379/0")
        assert "secret" not in masked
        assert "***" in masked

    def test_mask_url_without_password(self):
        """URL without password is returned unchanged."""
        url = "redis://host:6379/0"
        assert RedisConnectionFactory._mask_url(url) == url

    def test_mask_url_preserves_structure(self):
        """Masked URL preserves host/port/db structure."""
        masked = RedisConnectionFactory._mask_url("redis://user:secret@host:6379/3")
        assert "host:6379" in masked
        assert "/3" in masked


# ===========================================================================
# Behavior: Singleton lifecycle
# ===========================================================================


class TestFactorySingletonBehavior:
    """get_redis_connection_factory()/reset_redis_connection_factory() lifecycle."""

    def setup_method(self):
        """Reset singleton before each test."""
        reset_redis_connection_factory()

    def teardown_method(self):
        """Reset singleton after each test."""
        reset_redis_connection_factory()

    def test_get_returns_same_instance(self):
        """get_redis_connection_factory() returns the same cached instance."""
        first = get_redis_connection_factory()
        second = get_redis_connection_factory()
        assert first is second

    def test_reset_clears_cached_instance(self):
        """reset clears cache, next get creates a new instance."""
        first = get_redis_connection_factory()
        reset_redis_connection_factory()
        second = get_redis_connection_factory()
        assert first is not second

    def test_get_returns_factory_type(self):
        """get_redis_connection_factory() returns RedisConnectionFactory instance."""
        factory = get_redis_connection_factory()
        assert isinstance(factory, RedisConnectionFactory)


# ===========================================================================
# Behavior: Factory init defaults to get_redis_settings()
# ===========================================================================


class TestFactoryInitBehavior:
    """Factory constructor loads settings automatically when not provided."""

    def test_init_without_settings_uses_default(self):
        """Factory() with no args uses get_redis_settings() internally."""
        factory = RedisConnectionFactory()
        assert factory._settings is not None
        assert isinstance(factory._settings, RedisSettings)

    def test_init_with_explicit_settings(self):
        """Factory(settings=...) uses the provided settings."""
        custom = RedisSettings(max_connections=42)
        factory = RedisConnectionFactory(settings=custom)
        assert factory._settings is custom
        assert factory._settings.max_connections == 42


# ===========================================================================
# Behavior: Logging side effects
# ===========================================================================


class TestLoggingSideEffectBehavior:
    """Factory emits structured log events on client creation."""

    @patch("redis.from_url", autospec=True)
    @patch("baldur.adapters.redis.connection_factory.logger")
    def test_standalone_creation_logs_debug(self, mock_logger, mock_from_url, factory):
        """Standalone creation logs redis_factory.standalone_created at DEBUG."""
        factory.create("redis://host:6379/0")
        mock_logger.debug.assert_called_once()
        assert mock_logger.debug.call_args[0][0] == "redis_factory.standalone_created"

    @patch("redis.sentinel.Sentinel", autospec=True)
    @patch("baldur.adapters.redis.connection_factory.logger")
    def test_sentinel_creation_logs_info(self, mock_logger, mock_sentinel_cls, factory):
        """Sentinel creation logs redis_factory.sentinel_created at INFO."""
        mock_sentinel_cls.return_value = MagicMock()
        factory.create("redis+sentinel://mymaster@s1:26379/0")
        mock_logger.info.assert_called_once()
        assert mock_logger.info.call_args[0][0] == "redis_factory.sentinel_created"

    @patch("redis.cluster.RedisCluster", autospec=True)
    @patch("redis.cluster.ClusterNode", autospec=True)
    @patch("baldur.adapters.redis.connection_factory.logger")
    def test_cluster_creation_logs_info(
        self, mock_logger, mock_node_cls, mock_cluster_cls, factory
    ):
        """Cluster creation logs redis_factory.cluster_created at INFO."""
        factory.create("redis+cluster://n1:7000")
        mock_logger.info.assert_called_once()
        assert mock_logger.info.call_args[0][0] == "redis_factory.cluster_created"

    @patch("redis.from_url", autospec=True, side_effect=ConnectionError("refused"))
    @patch("baldur.adapters.redis.connection_factory.logger")
    def test_connection_failure_logs_error(self, mock_logger, mock_from_url, factory):
        """Connection failure logs redis_factory.connection_failed at ERROR."""
        with pytest.raises(ConnectionError):
            factory.create("redis://host:6379/0")
        mock_logger.exception.assert_called_once()
        assert (
            mock_logger.exception.call_args[0][0] == "redis_factory.connection_failed"
        )

    @patch("redis.from_url", autospec=True)
    @patch("baldur.adapters.redis.connection_factory.logger")
    def test_auth_injected_logs_debug(self, mock_logger, mock_from_url, auth_factory):
        """Auth injection logs redis_factory.auth_injected at DEBUG."""
        auth_factory.create("redis://host:6379/0")
        debug_calls = [
            call
            for call in mock_logger.debug.call_args_list
            if call[0][0] == "redis_factory.auth_injected"
        ]
        assert len(debug_calls) == 1
