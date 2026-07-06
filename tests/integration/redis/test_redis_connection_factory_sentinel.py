"""
Redis Connection Factory — Sentinel topology integration tests.

Verifies #422 RedisConnectionFactory under a real Redis Sentinel topology:

    A. Live Sentinel routing
       - Factory.create("redis+sentinel://...") returns a working client
       - Client performs basic operations (ping, set, get) against the master
       - Client uses Sentinel master discovery (not direct master URL)

    B. Master failover
       - Live writes succeed against the original master
       - DEBUG SLEEP on master triggers Sentinel down-after detection
       - Sentinel quorum promotes replica to master within failover window
       - The SAME client (created via Factory) auto-rediscovers and routes
         writes to the new master without reconfiguration

The Sentinel topology lives in docker-compose.test.yml:
    docker compose -f docker-compose.test.yml up -d \\
        redis-master redis-replica sentinel-1 sentinel-2 sentinel-3

These tests skip automatically when the topology is unreachable from the
host (sentinel.conf uses host.docker.internal, which resolves on Docker
Desktop natively but requires extra_hosts on plain Linux).

This is the integration leg of scenario 3.7 (Redis Factory Sentinel routing).
The unit-level callsite verification lives in
tests/unit/adapters/redis/test_factory_routing_callsites.py.
"""

from __future__ import annotations

import threading
import time

import pytest
import redis

from baldur.adapters.redis.connection_factory import RedisConnectionFactory
from baldur.settings.redis import RedisSettings

pytestmark = pytest.mark.requires_redis_sentinel


@pytest.fixture(autouse=True)
def _skip_when_topology_unreachable(sentinel_topology_reachable):
    """Skip Sentinel tests when the topology is not running on this host."""
    if not sentinel_topology_reachable:
        pytest.skip(
            "Redis Sentinel topology unreachable. Start it with: "
            "docker compose -f docker-compose.test.yml up -d "
            "redis-master redis-replica sentinel-1 sentinel-2 sentinel-3"
        )


@pytest.fixture
def factory() -> RedisConnectionFactory:
    """Fresh Factory instance with default settings (no auth)."""
    return RedisConnectionFactory(settings=RedisSettings())


# =============================================================================
# A. Live Sentinel routing
# =============================================================================


class TestSentinelLiveRouting:
    """Factory.create("redis+sentinel://...") must produce a working client."""

    def test_factory_returns_client_that_pings(self, factory, sentinel_url):
        """
        Purpose:
            Verify the Sentinel-routed client connects through Sentinel
            and successfully pings the discovered master.
        Expected:
            - factory.create(sentinel_url) returns a client object
            - client.ping() returns True (connection works end-to-end)
        """
        client = factory.create(sentinel_url, decode_responses=True)
        try:
            assert client.ping() is True
        finally:
            client.close()

    def test_factory_client_set_get_via_master(self, factory, sentinel_url):
        """
        Purpose:
            Verify SET/GET round-trip through the Sentinel-routed client.
        Expected:
            - SET returns True
            - GET returns the stored value
            - Cleanup DELETE removes the key
        """
        client = factory.create(sentinel_url, decode_responses=True)
        try:
            assert client.set("test:sentinel:routing", "alive") is True
            assert client.get("test:sentinel:routing") == "alive"
        finally:
            client.delete("test:sentinel:routing")
            client.close()

    def test_factory_client_routes_via_sentinel_not_direct(self, factory, sentinel_url):
        """
        Purpose:
            Confirm the underlying connection uses Sentinel master discovery
            (redis.sentinel.Sentinel.master_for) rather than a direct
            standalone connection. Distinguishable by inspecting the client's
            ConnectionPool class.
        Expected:
            - The pool class name reflects Sentinel managed pool
              (SentinelConnectionPool in redis-py)
        """
        client = factory.create(sentinel_url, decode_responses=True)
        try:
            pool_cls_name = type(client.connection_pool).__name__
            assert "Sentinel" in pool_cls_name, (
                f"Expected SentinelConnectionPool, got {pool_cls_name}"
            )
        finally:
            client.close()


# =============================================================================
# B. Master failover (#422 PRO ha_pipeline default topology proof)
# =============================================================================


class TestSentinelFailover:
    """The Factory-created client must survive master failure via Sentinel.

    Failover is induced via DEBUG SLEEP issued from a separate thread
    against the current master. With ``down-after-milliseconds=2000`` and
    ``failover-timeout=5000`` (sentinel.conf.template), quorum should
    detect the master as down and promote the replica within ~3-7 seconds.

    Non-destructive: the slept master rejoins as a replica when SLEEP
    expires, so the topology recovers without docker compose intervention.
    """

    FAILOVER_BUDGET_SEC = 30.0
    SLEEP_DURATION_SEC = 12  # > down-after (2s) + failover-timeout (5s) margin

    def _force_master_unresponsive(self, sentinel_master_name: str) -> threading.Thread:
        """Issue DEBUG SLEEP against the current master from a separate thread.

        Returns the started thread so the caller can join it after the test.
        """
        # Discover current master via Sentinel
        s = redis.Redis(host="localhost", port=26379, socket_timeout=1.5)
        addr = s.execute_command(
            "SENTINEL", "get-master-addr-by-name", sentinel_master_name
        )
        s.close()
        master_host = addr[0].decode() if isinstance(addr[0], bytes) else addr[0]
        master_port = int(addr[1])

        def _sleep_master() -> None:
            try:
                direct = redis.Redis(
                    host=master_host,
                    port=master_port,
                    socket_timeout=self.SLEEP_DURATION_SEC + 5,
                )
                direct.execute_command("DEBUG", "SLEEP", str(self.SLEEP_DURATION_SEC))
                direct.close()
            except Exception:
                # Connection drop / timeout / role change is expected during failover.
                pass

        t = threading.Thread(target=_sleep_master, daemon=True)
        t.start()
        # Give SLEEP a moment to land before sentinels start probing
        time.sleep(0.3)
        return t

    def test_client_survives_master_failover(
        self, factory, sentinel_url, sentinel_master_name
    ):
        """
        Purpose:
            Prove that a Factory-created Sentinel client transparently
            re-routes to the promoted replica after master failure, without
            client-side reconfiguration.
        Steps:
            1. Pre-failure SET via Factory client succeeds.
            2. Force current master into DEBUG SLEEP for 12 s.
            3. Within FAILOVER_BUDGET_SEC, the same Factory client must
               eventually accept a new SET (Sentinel re-discovery + new master).
        Expected:
            - Pre-failure SET returns True.
            - Post-failure SET succeeds within the failover budget.
            - The post-failure value is readable.
        """
        client = factory.create(sentinel_url, decode_responses=True, socket_timeout=2.0)
        try:
            # --- 1. Pre-failure write ---
            assert client.set("test:failover:key", "before") is True
            assert client.get("test:failover:key") == "before"

            # --- 2. Force master unresponsive ---
            sleeper = self._force_master_unresponsive(sentinel_master_name)

            # --- 3. Poll until failover completes ---
            deadline = time.monotonic() + self.FAILOVER_BUDGET_SEC
            last_error: Exception | None = None
            wrote_after = False
            while time.monotonic() < deadline:
                try:
                    if client.set("test:failover:key", "after") is True:
                        wrote_after = True
                        break
                except (
                    redis.ConnectionError,
                    redis.TimeoutError,
                    redis.ReadOnlyError,
                    redis.RedisError,
                ) as e:
                    last_error = e
                    time.sleep(0.5)

            assert wrote_after, (
                f"Sentinel did not promote replica within "
                f"{self.FAILOVER_BUDGET_SEC} s; last error: {last_error!r}"
            )
            assert client.get("test:failover:key") == "after"

            # Drain the sleeper thread so the next test sees a clean topology
            sleeper.join(timeout=self.SLEEP_DURATION_SEC + 5)
        finally:
            try:
                client.delete("test:failover:key")
            except Exception:
                pass
            client.close()
