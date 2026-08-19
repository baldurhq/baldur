"""
Auto-discover callbacks for adapter-type registries.

Each function registers available adapter implementations when invoked.
These serve as auto_discover callbacks for GenericProviderRegistry instances
on ProviderRegistry (D3: DCL variant unification).
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

# One announcement per outage, not one per resolution attempt.
# ``get_rate_limit_storage()`` is re-entered by every component that resolves
# storage, and each entry re-attempts the provider (the auto-detect loop
# invalidates the cached instance on failure), so an unlatched line is one
# WARNING per resolution rather than one per outage. Mirrors
# ``RedisRateLimitStorage.is_available()``'s own ``if not self._fallback_mode:``
# guard, which the admission probe now pre-empts.
_rate_limit_redis_probe_failure_reported = False

# Operator override for the hash-chain audit directory. Named here so the
# factory and the writable-directory resolver report the same remedy.
_AUDIT_LOG_DIR_ENV = "BALDUR_AUDIT_LOG_DIR"

__all__ = [
    "discover_cache_adapters",
    "discover_queue_adapters",
    "discover_async_queue_adapters",
    "discover_audit_adapters",
    "discover_traffic_routing_adapters",
    "discover_notification_adapters",
    "discover_alert_adapters",
    "discover_database_health_adapters",
    "discover_pg_admin_adapters",
    "discover_pool_info_adapters",
    "discover_session_adapters",
    "discover_web_framework_adapters",
    "discover_rate_limit_storage_adapters",
]


def discover_cache_adapters() -> None:
    """Auto-register available cache adapters."""
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.cache

    try:
        from baldur.adapters.cache.redis_adapter import RedisCacheAdapter

        if not reg.has_provider("redis"):
            reg.register("redis", RedisCacheAdapter)
    except ImportError:
        pass

    try:
        from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter

        if not reg.has_provider("memory"):
            reg.register("memory", InMemoryCacheAdapter)
    except ImportError:
        pass

    try:
        from baldur.adapters.cache.memcached_adapter import MemcachedCacheAdapter

        if not reg.has_provider("memcached"):
            reg.register("memcached", MemcachedCacheAdapter)
    except ImportError:
        pass


def discover_queue_adapters() -> None:
    """Auto-register available task queue adapters."""
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.queue

    try:
        from baldur.adapters.queues.celery_adapter import CeleryTaskAdapter

        if not reg.has_provider("celery"):
            reg.register("celery", CeleryTaskAdapter)
    except ImportError:
        pass

    try:
        from baldur.adapters.queues.sync_adapter import SyncTaskAdapter

        if not reg.has_provider("sync"):
            reg.register("sync", SyncTaskAdapter)
    except ImportError:
        pass

    try:
        from baldur.adapters.queues.rq_adapter import RQTaskAdapter

        if not reg.has_provider("rq"):
            reg.register("rq", RQTaskAdapter)
    except ImportError:
        pass


def discover_async_queue_adapters() -> None:
    """Auto-register available async task queue adapters."""
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.async_queue

    try:
        from baldur.adapters.queues.arq_adapter import ArqTaskAdapter

        if not reg.has_provider("arq"):
            reg.register("arq", ArqTaskAdapter)
    except ImportError:
        pass


def discover_audit_adapters() -> None:  # noqa: C901
    """Auto-register default audit adapters.

    Provides four named providers:

    - ``"file"``       — plain ``FileAuditLogAdapter`` (H1 entry schema).
    - ``"file_hashchain"`` — ``HashChainFileAuditLogAdapter`` (D6, D22, D23
      compliance-grade with hash chain integrity, partition-aware,
      cross-process file lock + optional Redis distributed mode).
    - ``"stdout"``     — ``StdoutAuditLogAdapter``.
    - ``"null"``       — ``NullAuditLogAdapter`` (the OSS-safe default,
      see ``factory/registry.py:995`` D11).
    """
    import os

    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.audit

    try:
        from baldur.adapters.audit.file_adapter import FileAuditLogAdapter
        from baldur.adapters.audit.null_adapter import NullAuditLogAdapter
        from baldur.adapters.audit.stdout_adapter import StdoutAuditLogAdapter

        if not reg.has_provider("file"):
            reg.register(
                "file",
                lambda: FileAuditLogAdapter(
                    os.getenv("AUDIT_LOG_PATH", "logs/audit.jsonl")
                ),
            )
        if not reg.has_provider("stdout"):
            reg.register("stdout", StdoutAuditLogAdapter)
        if not reg.has_provider("null"):
            reg.register("null", NullAuditLogAdapter)
    except ImportError:
        pass

    try:
        from baldur.adapters.audit.hashchain_adapter import (
            HashChainFileAuditLogAdapter,
        )

        def _create_hashchain_adapter() -> HashChainFileAuditLogAdapter:
            """Factory for the settings-aware ``file_hashchain`` adapter.

            The directory goes through the canonical writable-directory
            resolver — the same contract the WAL, checkpoint storage and
            disk buffer already use — so the zero-config relative default
            falls back instead of raising on a read-only root filesystem,
            while an operator-chosen directory still fails loudly.
            """
            from baldur.audit.config import create_hash_chain_redis_client
            from baldur.settings.audit import get_audit_settings
            from baldur.utils.fs import resolve_writable_dir

            settings = get_audit_settings()
            redis_client: Any | None = None
            if settings.distributed_hash_chain:
                redis_client = create_hash_chain_redis_client()

            operator_log_dir = os.getenv(_AUDIT_LOG_DIR_ENV)
            resolved_dir = resolve_writable_dir(
                operator_log_dir or HashChainFileAuditLogAdapter.DEFAULT_LOG_DIR,
                purpose="audit_hashchain",
                operator_set=bool(operator_log_dir),
                env_override_name=_AUDIT_LOG_DIR_ENV,
            )

            return HashChainFileAuditLogAdapter(
                log_dir=str(resolved_dir.path),
                distributed_hash_chain=settings.distributed_hash_chain,
                redis_client=redis_client,
                use_file_lock=settings.use_file_lock,
                partition=settings.partition,
            )

        if not reg.has_provider("file_hashchain"):
            reg.register("file_hashchain", _create_hashchain_adapter)
    except ImportError:
        pass


def discover_traffic_routing_adapters() -> None:
    """Auto-register default traffic routing adapters.

    K8sIngressTrafficRoutingAdapter moved to ``baldur_dormant.adapters.
    traffic_routing.k8s_ingress_adapter`` per doc 528 D10-v2; it self-
    registers via ``baldur_dormant.register_dormant_services()`` when the
    wheel is installed. OSS keeps only the logging-based adapter.
    """
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.traffic_routing

    try:
        from baldur.adapters.traffic_routing.logging_adapter import (
            LoggingTrafficRoutingAdapter,
        )

        if not reg.has_provider("logging"):
            reg.register("logging", LoggingTrafficRoutingAdapter)
    except ImportError:
        pass


def discover_notification_adapters() -> None:
    """Auto-register default notification adapters."""
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.notification

    try:
        from baldur.interfaces.notification import (
            LoggingNotificationAdapter,
            StdoutNotificationAdapter,
        )

        if not reg.has_provider("logging"):
            reg.register("logging", LoggingNotificationAdapter)
        if not reg.has_provider("stdout"):
            reg.register("stdout", StdoutNotificationAdapter)
    except ImportError:
        pass


def discover_alert_adapters() -> None:
    """Auto-register default alert adapters."""
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.alert

    try:
        from baldur.adapters.alert import NullAlertAdapter, StdoutAlertAdapter

        if not reg.has_provider("stdout"):
            reg.register("stdout", StdoutAlertAdapter)
        if not reg.has_provider("null"):
            reg.register("null", NullAlertAdapter)
    except ImportError:
        pass


def discover_database_health_adapters() -> None:
    """Auto-register available database health adapters."""
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.database_health

    try:
        from baldur.adapters.database.django_health import (
            DjangoDatabaseHealthAdapter,
        )

        if not reg.has_provider("django"):
            reg.register("django", DjangoDatabaseHealthAdapter)
    except ImportError:
        pass

    try:
        from baldur.adapters.database.sql_health import SQLDatabaseHealthAdapter

        def _create_sql_database_health_adapter() -> SQLDatabaseHealthAdapter:
            """Build SQLDatabaseHealthAdapter from BALDUR_SQL_DSN / BALDUR_POSTGRES_* env."""
            from baldur.adapters.sql.connection import build_connection_factory
            from baldur.settings.sql import get_sql_settings

            settings = get_sql_settings()
            return SQLDatabaseHealthAdapter(
                get_connection=build_connection_factory(),
                dialect=settings.resolved_dialect(),
            )

        if not reg.has_provider("sql"):
            reg.register("sql", _create_sql_database_health_adapter)
    except ImportError:
        pass

    try:
        from baldur.adapters.database.noop_health import NoopDatabaseHealthAdapter

        if not reg.has_provider("noop"):
            reg.register("noop", NoopDatabaseHealthAdapter)
    except ImportError:
        pass


def _django_default_alias_is_postgres() -> bool:
    """Whether Django's default alias is wired to a PostgreSQL.

    Reads the vendor string without opening a connection, so it is safe to call
    on every availability check.

    The import is guarded rather than left to raise into the caller's
    catch-all: django ships in an extra, and "django is not installed here"
    answers this probe's question exactly the way a sqlite alias does — the
    instance is not wired to a PostgreSQL.
    """
    try:
        from django.db import connections
    except ImportError:
        return False

    return connections["default"].vendor == "postgresql"


def _configured_sql_dialect_is_postgres() -> bool:
    """Whether the resolved SQL DSN / dialect override names a PostgreSQL."""
    from baldur.settings.sql import SQLDialect, get_sql_settings

    return get_sql_settings().resolved_dialect() == SQLDialect.POSTGRESQL


def discover_pg_admin_adapters() -> None:
    """Auto-register available PostgreSQL admin SQL providers (515)."""
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.pg_admin

    try:
        from baldur.adapters.postgres.admin import PgAdmin
        from baldur.adapters.postgres.sessions import (
            django_connection_factory,
            django_session_factory,
        )

        def _create_django_pg_admin() -> PgAdmin:
            return PgAdmin(
                get_session=django_session_factory("default"),
                get_connection=django_connection_factory("default"),
                label="django:default",
                availability_probe=_django_default_alias_is_postgres,
            )

        if not reg.has_provider("django"):
            reg.register("django", _create_django_pg_admin)
    except ImportError:
        pass

    try:
        from baldur.adapters.postgres.admin import PgAdmin
        from baldur.adapters.postgres.sessions import dbapi_session_factory

        def _create_sql_pg_admin() -> PgAdmin:
            from baldur.adapters.sql.connection import build_connection_factory

            factory = build_connection_factory()
            return PgAdmin(
                get_session=dbapi_session_factory(factory),
                get_connection=factory,
                label="sql:default",
                availability_probe=_configured_sql_dialect_is_postgres,
            )

        if not reg.has_provider("sql"):
            reg.register("sql", _create_sql_pg_admin)
    except ImportError:
        pass

    try:
        from baldur.adapters.postgres.noop_admin import NoopPgAdmin

        if not reg.has_provider("noop"):
            reg.register("noop", NoopPgAdmin)
    except ImportError:
        pass


def discover_pool_info_adapters() -> None:
    """Auto-register available connection-pool info providers (515)."""
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.pool_info

    try:
        from baldur.adapters.pool.django_info import DjangoPoolInfoProvider

        if not reg.has_provider("django"):
            reg.register("django", DjangoPoolInfoProvider)
    except ImportError:
        pass

    try:
        from baldur.adapters.pool.noop_info import NoopPoolInfoProvider

        if not reg.has_provider("noop"):
            reg.register("noop", NoopPoolInfoProvider)
    except ImportError:
        pass


def discover_session_adapters() -> None:
    """Auto-register available session invalidation adapters."""
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.session_invalidation

    try:
        from baldur.adapters.django.session_adapter import DjangoSessionAdapter

        if not reg.has_provider("django"):
            reg.register("django", DjangoSessionAdapter)
    except ImportError:
        pass

    try:
        from baldur.adapters.session.noop_adapter import NoopSessionAdapter

        if not reg.has_provider("noop"):
            reg.register("noop", NoopSessionAdapter)
    except ImportError:
        pass


def discover_web_framework_adapters() -> None:
    """Auto-register available web framework adapters."""
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.web_framework

    try:
        from baldur.api.django.adapter import DjangoFrameworkAdapter

        if not reg.has_provider("django"):
            reg.register("django", DjangoFrameworkAdapter)
    except ImportError:
        pass

    # Set default to django if available and no default set
    if not reg.get_default_name() and reg.has_provider("django"):
        reg.set_default("django")


def _probe_rate_limit_redis(factory: Any, url: str) -> None:
    """Run the rate-limit admission probe, preserving the fallback signal.

    ``RedisRateLimitStorage.is_available()`` is the only writer of the
    fallback gauge and the unavailable counter in the tree, and the
    auto-detect loop is its only caller — so a probe that refuses admission
    before the adapter exists would leave the shipped "rate limiting fell
    back to per-process" gauge reading 0 for the whole outage. This writes
    what that method would have written, and logs at the level it would have
    used, before letting the failure out.

    Raises:
        Exception: whatever the probe raised — the provider factory must
            still fail so auto-detect moves on to the next backend.
    """
    global _rate_limit_redis_probe_failure_reported

    try:
        factory.probe(url)
    except Exception as e:
        _report_rate_limit_redis_probe_failure(e)
        raise

    _rate_limit_redis_probe_failure_reported = False


def _report_rate_limit_redis_probe_failure(error: Exception) -> None:
    """Record the metrics and announce a failed rate-limit admission probe."""
    global _rate_limit_redis_probe_failure_reported

    try:
        from baldur.metrics.drift_metrics import (
            record_ratelimit_redis_unavailable,
            set_ratelimit_fallback_mode,
        )
    except ImportError:
        pass
    else:
        record_ratelimit_redis_unavailable()
        set_ratelimit_fallback_mode(True)

    if _rate_limit_redis_probe_failure_reported:
        return
    _rate_limit_redis_probe_failure_reported = True

    from baldur.settings.redis import redis_absence_is_expected

    if redis_absence_is_expected():
        # Nobody named a Redis outside production: the framework found its
        # own default address unreachable and the memory store serves the
        # lane. Fail-open with the main logic unaffected — LOGGING_STANDARDS
        # §3.1 — and the same split is_available() already applies.
        logger.debug(
            "rate_limit_storage.redis_probe_failed",
            error=str(error),
        )
    else:
        logger.warning(
            "rate_limit_storage.redis_probe_failed",
            error=str(error),
            # Named here so an operator whose Redis needs a longer connect
            # than the probe budget does not have to find the knob.
            escape_hatch="BALDUR_REDIS_PROBE_CONNECT_TIMEOUT",
        )


def discover_rate_limit_storage_adapters() -> None:
    """Auto-register available rate limit storage adapters.

    Priority order (first registered becomes default):
        1. Redis — fastest, requires redis + connection
        2. Database — universal fallback via Django ORM
        3. Memory — single-process only, always available
    """
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.rate_limit_storage

    # 1. Redis — register a factory function (needs redis_client arg)
    try:
        from baldur.adapters.rate_limit.redis_adapter import (
            RedisRateLimitStorage,
        )

        def _create_redis_rate_limit_storage() -> RedisRateLimitStorage:
            """Create RedisRateLimitStorage with auto-detected client."""
            from baldur.adapters.redis.connection_factory import (
                get_redis_connection_factory,
            )
            from baldur.settings.redis import (
                get_redis_settings,
                redis_absence_is_expected,
            )

            settings = get_redis_settings()
            factory = get_redis_connection_factory()
            # Admission first, on the bounded probe budget. Without it the
            # auto-detect loop's is_available() ping is the first connect,
            # and it runs on the data-path connect timeout inside the
            # caller's own timed section — a first protected call in the
            # "redis-py installed, no server" posture blocked for seconds and
            # misfired the caller's fallback. After a successful probe that
            # same ping costs an RTT, so nothing stalls twice.
            _probe_rate_limit_redis(factory, settings.url)
            # A zero-config run reaching the client build has a reachable
            # Redis at the default address, but keep the demotion: a
            # malformed default URL is still not an outage anyone caused.
            client = factory.create(
                settings.url,
                unconfigured_probe=redis_absence_is_expected(),
            )
            return RedisRateLimitStorage(client)

        if not reg.has_provider("redis"):
            reg.register("redis", _create_redis_rate_limit_storage)
    except ImportError:
        pass

    # 2. Database — Django ORM-backed storage
    try:
        from baldur.adapters.rate_limit.database_adapter import (
            DatabaseRateLimitStorage,
        )

        if not reg.has_provider("database"):
            reg.register("database", DatabaseRateLimitStorage)
    except ImportError:
        pass

    # 3. Memory — always available fallback
    try:
        from baldur.adapters.rate_limit.memory_adapter import (
            InMemoryRateLimitStorage,
        )

        if not reg.has_provider("memory"):
            reg.register("memory", InMemoryRateLimitStorage.get_instance)
    except ImportError:
        pass
