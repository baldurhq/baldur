"""
Redis Connection Factory — route to Standalone/Sentinel/Cluster by URL scheme.

Replaces direct redis.from_url() calls with transparent HA connections.

Ownership contract:
    Factory only creates clients. The caller owns close().
    Callers must call client.close() in their own close()/shutdown methods.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import structlog

if TYPE_CHECKING:
    from baldur.settings.redis import RedisSettings

logger = structlog.get_logger()

# A connect that timed out is retried exactly once by ``probe()``. One lost SYN
# is the dominant transient on an otherwise reachable host; a second timeout is
# evidence, not noise. Not an operator tuning axis — widening it would rebuild
# the multi-second stall the bounded probe exists to remove.
_PROBE_CONNECT_ATTEMPTS = 2


__all__ = [
    "RedisConnectionFactory",
    "get_redis_connection_factory",
    "configure_redis_connection_factory",
    "reset_redis_connection_factory",
]


class RedisConnectionFactory:
    """
    Create the appropriate Redis client based on URL scheme.

    - redis:// / rediss:// → redis.Redis (preserves existing behavior)
    - redis+sentinel://master@host1:port,host2:port/db → Sentinel
    - redis+cluster://host1:port,host2:port → RedisCluster

    Ownership: Factory only creates. Caller is responsible for close().
    """

    _SENTINEL_PATTERN = re.compile(
        r"^redis\+sentinel://(?P<master>[^@]+)@(?P<hosts>[^/]+)(?:/(?P<db>\d+))?$"
    )
    _CLUSTER_PATTERN = re.compile(r"^redis\+cluster://(?P<hosts>[^/]+)$")

    def __init__(self, settings: RedisSettings | None = None) -> None:
        if settings is None:
            from baldur.settings.redis import get_redis_settings

            settings = get_redis_settings()
        self._settings = settings

    def create(
        self,
        url: str,
        *,
        decode_responses: bool = False,
        socket_timeout: float | None = None,
        socket_connect_timeout: float | None = None,
        retry_on_timeout: bool | None = None,
        max_connections: int | None = None,
        unconfigured_probe: bool = False,
        inject_settings_auth: bool = True,
        **kwargs: Any,
    ) -> Any:
        """
        Create a Redis client based on URL scheme.

        Args:
            url: Redis connection URL (routing info only, no password)
            decode_responses: Decode responses to str
            socket_timeout: Read timeout in seconds. When None, defaults from
                RedisSettings.socket_timeout so a connected-but-hung Redis
                cannot block the caller until the OS TCP timeout. Pass an
                explicit value to override.
            socket_connect_timeout: Connection timeout in seconds. When None,
                defaults from RedisSettings.socket_connect_timeout.
            retry_on_timeout: Retry on timeout errors. When None, defaults from
                RedisSettings.retry_on_timeout.
            max_connections: Connection pool max connections
            unconfigured_probe: The caller knows this URL is a default nobody
                configured, so a failure here is the expected posture of a
                zero-config run rather than an outage. Only the failure log
                level changes — DEBUG without a traceback instead of ERROR
                with one; the exception is re-raised either way. Defaults to
                False, so any caller that does not know stays loud, including
                every caller of a feature-local URL field that falls back to
                the same default address.
            inject_settings_auth: Apply ``RedisSettings.password`` /
                ``username`` when the kwargs carry none. True by default
                because most URLs handed here name the framework's own Redis.
                Pass False for a URL that comes from a channel naming a
                *different* server (a task broker, a second instance): sending
                AUTH to a server with no password set fails every command with
                ``ResponseError``, so a credential that belongs to one
                instance must not travel to another.
            **kwargs: Additional redis-py options

        Returns:
            redis.Redis | redis.sentinel.Sentinel.master_for | redis.cluster.RedisCluster

        Note:
            Returned client's close() is the caller's responsibility.
            Blocking consumers (pub/sub ``listen()``) that need no read timeout
            must build their own client directly rather than via this factory.
        """
        # Default the read/connect timeouts and retry flag from settings when
        # unset so every topology (standalone/cluster here, sentinel below)
        # enforces a bounded read timeout. Without this, an unset socket_timeout
        # fell through to redis-py's no-timeout default and a connected-but-hung
        # Redis blocked the caller until the OS TCP timeout; and an unset
        # retry_on_timeout was dropped by the None-filter below, silently
        # reverting to redis-py's own default instead of RedisSettings.
        if socket_timeout is None:
            socket_timeout = self._settings.socket_timeout
        if socket_connect_timeout is None:
            socket_connect_timeout = self._settings.socket_connect_timeout
        if retry_on_timeout is None:
            retry_on_timeout = self._settings.retry_on_timeout

        common_kwargs: dict[str, Any] = {
            "decode_responses": decode_responses,
            "socket_timeout": socket_timeout,
            "socket_connect_timeout": socket_connect_timeout,
            "retry_on_timeout": retry_on_timeout,
            "max_connections": max_connections,
            **kwargs,
        }
        # Filter out None values (redis-py may not treat None as default)
        common_kwargs = {k: v for k, v in common_kwargs.items() if v is not None}

        # Inject auth from settings (not from URL, for security)
        if inject_settings_auth:
            self._inject_auth(url, common_kwargs)

        try:
            if url.startswith("redis+sentinel://"):
                return self._create_sentinel(url, common_kwargs)
            if url.startswith("redis+cluster://"):
                return self._create_cluster(url, common_kwargs)
            return self._create_standalone(url, common_kwargs)
        except Exception as e:
            if unconfigured_probe:
                logger.debug(
                    "redis_factory.connection_failed",
                    url=self._mask_url(url),
                    error_type=type(e).__name__,
                    error=str(e),
                )
            else:
                logger.exception(
                    "redis_factory.connection_failed",
                    url=self._mask_url(url),
                    error_type=type(e).__name__,
                )
            raise

    def probe(self, url: str, **kwargs: Any) -> None:
        """Decide whether ``url`` is reachable, on a bounded connect budget.

        Admission check for a call site that is about to build a
        process-lifetime client. Returns None when the server answered PING;
        re-raises the underlying exception otherwise.

        The probe client is ephemeral and is closed before this returns, so
        the verdict costs a connect budget and nothing else — the data client
        the caller builds afterwards keeps its own untouched
        ``RedisSettings`` timeouts. Bounding a *shared* client instead would
        rewrite the data path: every later pool reconnect (Sentinel failover,
        a BGSAVE fork stall) would fail at the probe budget rather than the
        read budget.

        Only the CONNECT phase is narrowed, to
        ``RedisSettings.probe_connect_timeout``. ``socket_timeout`` keeps its
        data-path value on purpose: a server that accepts the connection is
        reachable, which is the question being asked, and a healthy Redis
        that answers a PING slowly (fork/COW pause, a slow command on the
        single thread) must not be judged unreachable and demoted for the
        rest of the process.

        ``retry_on_timeout`` is pinned False so an accepting-but-hung host
        costs one read budget instead of two; the flag only acts after a
        timeout has already elapsed, so a healthy-but-slow server never
        reaches it.

        One connect-phase retry, selected by elapsed time rather than
        exception type — redis-py raises the same ``TimeoutError`` for both
        phases. A failure that took at least the connect budget but less than
        the read budget was a connect timeout (a refusal returns immediately;
        a hung read costs at least the read budget), and one lost SYN should
        not demote an otherwise reachable host. A refused connect is
        definitive and is never retried.

        Logs nothing: the caller's ``except`` owns the level and the metrics,
        which is what keeps each site's existing log vocabulary intact.

        Args:
            url: Redis connection URL (same routing rules as :meth:`create`)
            **kwargs: Additional redis-py options forwarded to :meth:`create`

        Raises:
            Exception: whatever the connect or PING raised on the last attempt

        Note:
            Not bounded here: DNS resolution runs before any redis-py timeout
            applies, so an unresponsive resolver blocks for the OS resolver
            budget regardless.
        """
        connect_budget = self._settings.probe_connect_timeout
        read_budget = kwargs.get("socket_timeout")
        if read_budget is None:
            read_budget = self._settings.socket_timeout

        for attempt in range(1, _PROBE_CONNECT_ATTEMPTS + 1):
            started = time.monotonic()
            client = None
            try:
                client = self.create(
                    url,
                    socket_connect_timeout=connect_budget,
                    retry_on_timeout=False,
                    **kwargs,
                )
                client.ping()
                return
            except Exception:
                elapsed = time.monotonic() - started
                was_connect_timeout = connect_budget <= elapsed < read_budget
                if attempt == _PROBE_CONNECT_ATTEMPTS or not was_connect_timeout:
                    raise
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        # A client that cannot be closed has nothing left to
                        # leak that the caller could act on, and the probe's
                        # verdict is already decided.
                        pass

    def _inject_auth(self, url: str, kwargs: dict[str, Any]) -> None:
        """Inject auth credentials from settings into kwargs.

        Design principle — passwords are never embedded in URLs:
        - Security: prevents plaintext exposure in logs/stacktraces/APM
        - Sentinel: sentinel node password and master password are separate
        - Redis 6.0+ ACL: supports username + password combination
        """
        injected = False
        if "password" not in kwargs and self._settings.password:
            kwargs["password"] = self._settings.password
            injected = True
        if "username" not in kwargs and self._settings.username:
            kwargs["username"] = self._settings.username
            injected = True
        if injected:
            logger.debug(
                "redis_factory.auth_injected",
                has_username=bool(self._settings.username),
            )

    def _create_standalone(self, url: str, kwargs: dict[str, Any]) -> Any:
        """Standalone Redis connection (existing behavior)."""
        import redis

        client = redis.from_url(url, **kwargs)
        logger.debug("redis_factory.standalone_created", url=self._mask_url(url))
        return client

    def _create_sentinel(self, url: str, kwargs: dict[str, Any]) -> Any:
        """Redis Sentinel connection."""
        import redis.sentinel

        match = self._SENTINEL_PATTERN.match(url)
        if not match:
            raise ValueError(
                f"Invalid Sentinel URL format: {self._mask_url(url)}. "
                "Expected: redis+sentinel://master_name@host1:port,host2:port/db"
            )

        master_name = match.group("master")
        hosts_str = match.group("hosts")
        db = int(match.group("db") or 0)

        sentinels = self._parse_host_port_list(hosts_str, "Sentinel")

        # Sentinel node auth (separate from master auth)
        sentinel_kwargs: dict[str, Any] = {}
        if self._settings.sentinel_password:
            sentinel_kwargs["password"] = self._settings.sentinel_password
        # Propagate socket timeouts to sentinel node connections. create()
        # always populates these from settings when unset, so the fallbacks
        # only guard a hypothetical direct call to this method.
        sentinel_timeout = kwargs.pop("socket_timeout", self._settings.socket_timeout)
        sentinel_connect_timeout = kwargs.pop(
            "socket_connect_timeout", self._settings.socket_connect_timeout
        )
        sentinel_kwargs.setdefault("socket_timeout", sentinel_timeout)
        sentinel_kwargs.setdefault("socket_connect_timeout", sentinel_connect_timeout)

        sentinel = redis.sentinel.Sentinel(
            sentinels,
            socket_timeout=sentinel_timeout,
            socket_connect_timeout=sentinel_connect_timeout,
            sentinel_kwargs=sentinel_kwargs,
        )

        client = sentinel.master_for(
            master_name,
            db=db,
            **kwargs,
        )
        logger.info(
            "redis_factory.sentinel_created",
            master=master_name,
            sentinels_count=len(sentinels),
            db=db,
        )
        return client

    def _create_cluster(self, url: str, kwargs: dict[str, Any]) -> Any:
        """Redis Cluster connection."""
        import redis.cluster

        match = self._CLUSTER_PATTERN.match(url)
        if not match:
            raise ValueError(
                f"Invalid Cluster URL format: {self._mask_url(url)}. "
                "Expected: redis+cluster://host1:port,host2:port"
            )

        hosts_str = match.group("hosts")
        parsed_hosts = self._parse_host_port_list(hosts_str, "Cluster")
        startup_nodes = [
            redis.cluster.ClusterNode(host, port) for host, port in parsed_hosts
        ]

        # max_connections is not a valid arg for RedisCluster constructor
        kwargs.pop("max_connections", None)

        client = redis.cluster.RedisCluster(
            startup_nodes=startup_nodes,
            **kwargs,
        )
        logger.info(
            "redis_factory.cluster_created",
            nodes_count=len(startup_nodes),
        )
        return client

    @staticmethod
    def _parse_host_port_list(hosts_str: str, context: str) -> list[tuple[str, int]]:
        """Parse comma-separated host:port string into list of (host, port) tuples."""
        result: list[tuple[str, int]] = []
        for entry in hosts_str.split(","):
            host, sep, port_str = entry.rpartition(":")
            if not sep or not port_str.isdigit():
                raise ValueError(
                    f"Invalid {context} node format: {entry!r}. Expected: host:port"
                )
            result.append((host, int(port_str)))
        return result

    @staticmethod
    def _mask_url(url: str) -> str:
        """Mask password in URL for safe logging."""
        parsed = urlparse(url)
        if parsed.password:
            return url.replace(parsed.password, "***")
        return url


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

from baldur.utils.singleton import make_singleton_factory

(
    get_redis_connection_factory,
    configure_redis_connection_factory,
    reset_redis_connection_factory,
) = make_singleton_factory("redis_connection_factory", RedisConnectionFactory)
