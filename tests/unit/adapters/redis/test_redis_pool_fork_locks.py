"""The connection-pool locks of Baldur's Redis clients are registered for the
fork repair.

redis-py rebuilds a pool's connections in a fork child on its first command
but never renews the pool's own lock, so a parent thread inside a command at
the fork instant leaves every command in the child blocked. Every client
Baldur builds therefore has its pool's ``_lock`` and ``_fork_lock`` registered
with the process-wide fork repair — the standalone and Sentinel clients from
``RedisConnectionFactory.create()``, each Sentinel node's own client (a
child's first command asks the nodes for the master's address), and the
rate limiter's health-ping client, which is built outside the factory.

Building these clients opens no connection, so no server is needed.
Membership is POSIX-only: without ``fork()`` nothing is registered at all.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import redis

from baldur.adapters.redis.connection_factory import (
    RedisConnectionFactory,
    register_redis_pool_fork_locks,
)
from baldur.api.django.rate_limit.redis_health_checker import RedisHealthChecker
from baldur.core import process_utils
from baldur.settings.redis import RedisSettings

# Unroutable on purpose: constructing a client never dials, and nothing here
# issues a command.
_STANDALONE_URL = "redis://127.0.0.1:1/0"
_SENTINEL_URL = "redis+sentinel://mymaster@127.0.0.1:1,127.0.0.1:2/0"
_CLUSTER_URL = "redis+cluster://127.0.0.1:1,127.0.0.1:2"

posix_fork_repair = pytest.mark.skipif(
    not hasattr(threading.Lock(), "_at_fork_reinit"),
    reason="nothing is registered where fork() does not exist",
)


def _is_registered(lock: object) -> bool:
    return any(entry is lock for entry in list(process_utils._fork_safe_locks))


def _pool_locks(pool) -> list[object]:
    return [pool._lock, pool._fork_lock]


@pytest.fixture
def factory() -> RedisConnectionFactory:
    return RedisConnectionFactory(settings=RedisSettings())


class _RaisingClient:
    """A client whose pool cannot even be read."""

    def __init__(self) -> None:
        self.touched = False

    @property
    def connection_pool(self):
        self.touched = True
        raise RuntimeError("pool unavailable")


class TestRedisPoolForkLockRegistrationContract:
    """Which pool locks a Baldur-built Redis client registers, per client shape."""

    def test_pool_lock_attributes_exist_on_the_installed_redis_py(self):
        """Tripwire: the repair reads two private redis-py attributes by name.

        A release that renames them would silently drop the repair (the read
        degrades to ``None``), so the rename has to fail here instead.
        """
        pool = redis.Redis(host="127.0.0.1", port=1).connection_pool

        for attr in ("_lock", "_fork_lock"):
            lock = getattr(pool, attr, None)
            assert isinstance(
                lock, (type(threading.Lock()), type(threading.RLock()))
            ), attr

    @posix_fork_repair
    def test_standalone_client_from_create_registers_both_pool_locks(self, factory):
        client = factory.create(_STANDALONE_URL)

        for lock in _pool_locks(client.connection_pool):
            assert _is_registered(lock)

    @posix_fork_repair
    def test_sentinel_client_registers_its_pool_and_every_node_pool(self, factory):
        """A child's first command on a Sentinel client goes through every node
        client's pool before it reaches the master's.
        """
        # When
        client = factory.create(_SENTINEL_URL)

        # Then
        pool = client.connection_pool
        nodes = pool.sentinel_manager.sentinels
        assert len(nodes) == 2
        for lock in _pool_locks(pool):
            assert _is_registered(lock)
        for node in nodes:
            for lock in _pool_locks(node.connection_pool):
                assert _is_registered(lock)

    @posix_fork_repair
    def test_cluster_client_registers_no_pool_lock(self, factory):
        """A cluster client builds its per-node pools lazily, after ``create()``
        returns, so the cluster branch registers nothing — even for a client
        that happens to carry a pool.
        """
        stand_in = redis.Redis(host="127.0.0.1", port=1)

        with patch.object(
            RedisConnectionFactory,
            "_create_cluster",
            autospec=True,
            return_value=stand_in,
        ):
            client = factory.create(_CLUSTER_URL)

        assert client is stand_in
        for lock in _pool_locks(stand_in.connection_pool):
            assert not _is_registered(lock)

    @posix_fork_repair
    def test_health_ping_client_built_outside_the_factory_is_registered(self):
        """The rate limiter's low-timeout ping client copies the main client's
        connection kwargs into a client of its own.
        """
        # Given
        checker = RedisHealthChecker()
        checker._redis_client = redis.Redis(host="127.0.0.1", port=1)

        # When
        ping_client = checker._get_health_ping_client()

        # Then
        assert ping_client is not checker._redis_client
        for lock in _pool_locks(ping_client.connection_pool):
            assert _is_registered(lock)

    @pytest.mark.parametrize(
        "client",
        [
            object(),
            SimpleNamespace(connection_pool=None),
            SimpleNamespace(connection_pool=SimpleNamespace()),
            SimpleNamespace(
                connection_pool=SimpleNamespace(
                    _lock=None, _fork_lock=None, sentinel_manager=None
                )
            ),
        ],
        ids=[
            "no_pool_attribute",
            "pool_is_none",
            "pool_without_lock_attributes",
            "renamed_locks_read_as_none",
        ],
    )
    def test_client_of_another_shape_is_left_as_it_is(self, client):
        """Never raises: such a client keeps redis-py's own fork handling."""
        register_redis_pool_fork_locks(client)

    def test_client_whose_pool_cannot_be_read_does_not_raise(self):
        client = _RaisingClient()

        register_redis_pool_fork_locks(client)

        assert client.touched
