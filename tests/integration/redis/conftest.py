"""
Redis integration test fixtures for baldur modules.

Provides Redis connection fixtures for testing:
- RedisConnectionFactory (connection creation and routing)
- RedisCacheAdapter (cache operations with real Redis)
- Redis-backed adapters (cache, event bus, connection factory)

All tests require a running Redis instance.
"""

from __future__ import annotations

import os

import pytest
import redis

from baldur.adapters.redis.connection_factory import (
    RedisConnectionFactory,
    reset_redis_connection_factory,
)
from baldur.settings.redis import RedisSettings


def _detect_redis_url() -> str:
    """Detect available Redis URL (test port 16379 first, then 6379)."""
    env_url = os.environ.get("REDIS_URL")
    if env_url:
        return env_url

    for port in (16379, 6379):
        try:
            client = redis.Redis(host="localhost", port=port, db=1, socket_timeout=2)
            client.ping()
            client.close()
            return f"redis://localhost:{port}/1"
        except Exception:
            continue

    return "redis://localhost:6379/1"


@pytest.fixture(scope="session")
def redis_url() -> str:
    """Available Redis URL for integration tests."""
    return _detect_redis_url()


@pytest.fixture(scope="session")
def redis_test_client(redis_url):
    """Session-scoped Redis client for test assertions."""
    client = redis.from_url(redis_url, decode_responses=True)
    try:
        client.ping()
    except redis.ConnectionError:
        pytest.skip("Redis not available")
    yield client
    client.close()


@pytest.fixture(autouse=True)
def _cleanup_between_tests(redis_test_client):
    """Flush test DB and reset singletons between tests."""
    redis_test_client.flushdb()
    yield
    redis_test_client.flushdb()
    reset_redis_connection_factory()


@pytest.fixture
def redis_settings(redis_url) -> RedisSettings:
    """RedisSettings configured for test Redis."""
    return RedisSettings(url=redis_url)


@pytest.fixture
def connection_factory(redis_settings) -> RedisConnectionFactory:
    """RedisConnectionFactory with test settings."""
    return RedisConnectionFactory(settings=redis_settings)


# ---------------------------------------------------------------------------
# Sentinel topology fixtures (#422 / scenario 3.7 — requires_redis_sentinel)
# ---------------------------------------------------------------------------


_SENTINEL_HOSTS = [("localhost", 26379), ("localhost", 26380), ("localhost", 26381)]
_MASTER_NAME = "mymaster"


def _detect_sentinel_topology() -> bool:
    """Probe sentinels and verify master is reachable from this host.

    Returns True only when:
      1. At least one sentinel is reachable on localhost:26379-26381
      2. Sentinel can resolve the master via SENTINEL get-master-addr-by-name
      3. The reported master address resolves and accepts a ping from this host

    Used to skip Sentinel integration tests automatically when the topology
    is not running (or when host.docker.internal does not resolve, e.g. on
    Linux without Docker Desktop's auto-injection).
    """
    for host, port in _SENTINEL_HOSTS:
        try:
            sentinel_client = redis.Redis(
                host=host, port=port, socket_timeout=1.5, socket_connect_timeout=1.5
            )
            addr = sentinel_client.execute_command(
                "SENTINEL", "get-master-addr-by-name", _MASTER_NAME
            )
            sentinel_client.close()
            if not addr:
                continue
            master_host = addr[0].decode() if isinstance(addr[0], bytes) else addr[0]
            master_port = int(addr[1])
            master_client = redis.Redis(
                host=master_host,
                port=master_port,
                socket_timeout=1.5,
                socket_connect_timeout=1.5,
            )
            master_client.ping()
            master_client.close()
            return True
        except Exception:
            continue
    return False


@pytest.fixture(scope="session")
def sentinel_topology_reachable() -> bool:
    """Session-scoped probe; tests using this fixture skip when False."""
    return _detect_sentinel_topology()


@pytest.fixture(scope="session")
def sentinel_url() -> str:
    """redis+sentinel:// URL for the test topology."""
    hosts_str = ",".join(f"{h}:{p}" for h, p in _SENTINEL_HOSTS)
    return f"redis+sentinel://{_MASTER_NAME}@{hosts_str}/0"


@pytest.fixture(scope="session")
def sentinel_master_name() -> str:
    """Master name configured in sentinel.conf.template."""
    return _MASTER_NAME
