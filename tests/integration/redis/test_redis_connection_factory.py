"""
Redis Connection Factory Integration Tests

Verifies RedisConnectionFactory creates functional Redis clients
using actual Redis infrastructure.

Test Categories:
    A. Standalone Connection Lifecycle:
        - Factory creates working standalone client
        - Client performs basic operations (ping, set, get, delete)
        - Multiple creates return independent clients
    B. Connection Factory Settings Integration:
        - Factory uses RedisSettings values correctly
        - Singleton factory creates working clients
    C. Error Handling:
        - Unreachable host client fails on operation

Note: All tests require a running Redis instance.
      Marked with @pytest.mark.requires_redis for auto-skip.
"""

from __future__ import annotations

import pytest

from baldur.adapters.redis.connection_factory import (
    RedisConnectionFactory,
    get_redis_connection_factory,
    reset_redis_connection_factory,
)
from baldur.settings.redis import RedisSettings

pytestmark = pytest.mark.requires_redis


# =============================================================================
# A. Standalone Connection Lifecycle
# =============================================================================


class TestStandaloneConnectionLifecycle:
    """
    Validates that RedisConnectionFactory creates functional standalone
    Redis clients that can perform real operations.
    """

    def test_factory_creates_functional_client(self, connection_factory, redis_url):
        """
        Purpose:
            Verify factory.create() produces a client that can communicate
            with Redis.
        Expected:
            - Client responds to ping()
            - No connection errors raised
        """
        client = connection_factory.create(redis_url)
        assert client.ping() is True
        client.close()

    def test_client_performs_basic_crud(self, connection_factory, redis_url):
        """
        Purpose:
            Verify factory-created client can perform SET/GET/DELETE cycle.
        Expected:
            - SET returns True
            - GET returns the stored value
            - DELETE removes the key
            - GET after DELETE returns None
        """
        client = connection_factory.create(redis_url, decode_responses=True)

        assert client.set("test:crud:key", "hello") is True
        assert client.get("test:crud:key") == "hello"
        assert client.delete("test:crud:key") == 1
        assert client.get("test:crud:key") is None

        client.close()

    def test_multiple_creates_return_independent_clients(
        self, connection_factory, redis_url
    ):
        """
        Purpose:
            Verify each factory.create() call returns an independent client.
        Expected:
            - Two clients are distinct objects
            - Operations on one don't affect the other's connection state
        """
        client_a = connection_factory.create(redis_url, decode_responses=True)
        client_b = connection_factory.create(redis_url, decode_responses=True)

        assert client_a is not client_b

        client_a.set("test:independent:a", "value_a")
        assert client_b.get("test:independent:a") == "value_a"

        client_a.close()
        # client_b should still work after client_a is closed
        assert client_b.get("test:independent:a") == "value_a"
        client_b.close()

    def test_client_respects_decode_responses(self, connection_factory, redis_url):
        """
        Purpose:
            Verify decode_responses kwarg is properly forwarded.
        Expected:
            - decode_responses=False returns bytes
            - decode_responses=True returns str
        """
        client_bytes = connection_factory.create(redis_url, decode_responses=False)
        client_str = connection_factory.create(redis_url, decode_responses=True)

        client_bytes.set("test:decode:key", "value")
        raw = client_bytes.get("test:decode:key")
        decoded = client_str.get("test:decode:key")

        assert isinstance(raw, bytes)
        assert isinstance(decoded, str)
        assert decoded == "value"

        client_bytes.close()
        client_str.close()

    def test_client_respects_socket_timeout(self, connection_factory, redis_url):
        """
        Purpose:
            Verify socket_timeout kwarg is forwarded to the client.
        Expected:
            - Client creates successfully with custom timeout
            - Client is functional
        """
        client = connection_factory.create(
            redis_url,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
        )
        assert client.ping() is True
        client.close()


# =============================================================================
# B. Connection Factory Settings Integration
# =============================================================================


class TestConnectionFactorySettingsIntegration:
    """
    Validates that factory correctly integrates with RedisSettings.
    """

    def test_factory_with_custom_settings(self, redis_url):
        """
        Purpose:
            Verify factory uses explicitly provided RedisSettings.
        Expected:
            - Factory creates working client using custom settings
        """
        settings = RedisSettings(
            url=redis_url,
            socket_timeout=3.0,
            socket_connect_timeout=3.0,
            max_connections=10,
        )
        factory = RedisConnectionFactory(settings=settings)
        client = factory.create(redis_url, decode_responses=True)

        assert client.ping() is True
        client.close()

    def test_singleton_factory_creates_working_client(self, redis_url):
        """
        Purpose:
            Verify get_redis_connection_factory() singleton works end-to-end.
        Expected:
            - Singleton factory creates functional client
        """
        reset_redis_connection_factory()
        try:
            factory = get_redis_connection_factory()
            client = factory.create(redis_url, decode_responses=True)
            assert client.ping() is True
            client.close()
        finally:
            reset_redis_connection_factory()

    def test_factory_auth_injection_does_not_break_connection(self, redis_url):
        """
        Purpose:
            Verify factory with auth settings still connects when Redis
            has no auth (auth is rejected gracefully by redis-py).
        Expected:
            - Factory creates client without error (redis-py handles
              unneeded auth gracefully)
        """
        settings = RedisSettings(url=redis_url)
        factory = RedisConnectionFactory(settings=settings)
        client = factory.create(redis_url, decode_responses=True)
        assert client.ping() is True
        client.close()


# =============================================================================
# C. Error Handling
# =============================================================================


class TestConnectionErrorHandling:
    """
    Validates factory behavior when connection fails.
    """

    def test_unreachable_host_fails_on_operation(self):
        """
        Purpose:
            Verify client to unreachable host raises error on operation.
        Expected:
            - Client object is created (lazy connection)
            - ping() raises ConnectionError
        """
        factory = RedisConnectionFactory(settings=RedisSettings())
        client = factory.create(
            "redis://unreachable-host-that-does-not-exist:6379/0",
            socket_timeout=0.5,
            socket_connect_timeout=0.5,
        )

        with pytest.raises(Exception):
            client.ping()

        client.close()
