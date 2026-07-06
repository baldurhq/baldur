"""
Auto-discover callbacks for adapter-type registries.

Each function registers available adapter implementations when invoked.
These serve as auto_discover callbacks for GenericProviderRegistry instances
on ProviderRegistry (D3: DCL variant unification).
"""

from __future__ import annotations

from typing import Any

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
            """Factory for the file_hashchain adapter (D22 settings-aware)."""
            from baldur.settings.audit import get_audit_settings

            settings = get_audit_settings()
            redis_client: Any | None = None
            if settings.distributed_hash_chain:
                try:
                    # get_cache_adapter() is a duck-typed PRO extension; the
                    # OSS ProviderRegistry doesn't declare it. Falls open to
                    # in-process hash chain if no provider is registered.
                    get_cache = getattr(ProviderRegistry, "get_cache_adapter", None)
                    cache = get_cache() if callable(get_cache) else None
                    if cache is not None:
                        redis_client = getattr(cache, "_client", None) or getattr(
                            cache, "redis", None
                        )
                except Exception:
                    redis_client = None
            return HashChainFileAuditLogAdapter(
                log_dir=os.getenv("BALDUR_AUDIT_LOG_DIR", "logs/audit"),
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
            from baldur.settings.redis import get_redis_settings

            settings = get_redis_settings()
            factory = get_redis_connection_factory()
            client = factory.create(settings.url)
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
