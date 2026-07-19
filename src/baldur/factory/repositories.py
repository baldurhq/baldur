"""
Auto-discover callbacks for repository-type registries.

Each function registers available repository implementations when invoked.
These serve as auto_discover callbacks for GenericProviderRegistry instances
on ProviderRegistry (D3: DCL variant unification).
"""

from __future__ import annotations

__all__ = [
    "discover_failed_op_repos",
    "discover_circuit_breaker_repos",
    "discover_security_repos",
    "discover_event_journal_repos",
    "discover_postmortem_repos",
    "discover_recovery_session_repos",
    "discover_config_history_stores",
    "discover_canary_rollout_stores",
    "discover_chaos_experiment_stores",
    "discover_cross_cluster_stores",
]


def discover_failed_op_repos() -> None:
    """Auto-register available failed operation repository implementations."""
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.failed_op_repo

    # In-memory (for testing, standalone)
    try:
        from baldur.adapters.memory import InMemoryFailedOperationRepository

        if not reg.has_provider("memory"):
            reg.register("memory", InMemoryFailedOperationRepository)
    except ImportError:
        pass

    # Redis-based (using ResilientStorageBackend)
    try:
        from baldur.adapters.redis import RedisDLQRepository
        from baldur.adapters.resilient.backend import get_storage_backend

        def _create_redis_dlq_repo():
            return RedisDLQRepository(get_storage_backend())

        if not reg.has_provider("redis"):
            reg.register("redis", _create_redis_dlq_repo)
    except ImportError:
        pass

    # SQL-based (DB-API 2.0 — PostgreSQL / MySQL / SQLite)
    try:
        from baldur.adapters.sql import SQLFailedOperationRepository
        from baldur.adapters.sql.connection import build_connection_factory

        def _create_sql_dlq_repo():
            return SQLFailedOperationRepository(build_connection_factory())

        if not reg.has_provider("sql"):
            reg.register("sql", _create_sql_dlq_repo)
    except ImportError:
        pass


def discover_circuit_breaker_repos() -> None:  # noqa: C901
    """Auto-register available circuit breaker repository implementations."""
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.circuit_breaker_repo

    # In-memory
    try:
        from baldur.adapters.memory import InMemoryCircuitBreakerStateRepository

        if not reg.has_provider("memory"):
            reg.register("memory", InMemoryCircuitBreakerStateRepository)
    except ImportError:
        pass

    # Redis-based
    try:
        from baldur.adapters.redis import RedisCircuitBreakerStateRepository
        from baldur.adapters.resilient.backend import get_storage_backend

        def _create_redis_cb_repo():
            return RedisCircuitBreakerStateRepository(get_storage_backend())

        if not reg.has_provider("redis"):
            reg.register("redis", _create_redis_cb_repo)
    except ImportError:
        pass

    # Layered (L1=Memory + L2=Redis)
    try:
        from baldur.adapters.memory.layered_repository import (
            LayeredCircuitBreakerStateRepository,
        )
        from baldur.adapters.redis import (
            RedisCircuitBreakerStateRepository as _RedisCBRepo,
        )
        from baldur.adapters.resilient.backend import (
            get_storage_backend as _get_backend,
        )

        def _create_layered_cb_repo():
            l2_repo = _RedisCBRepo(_get_backend())
            return LayeredCircuitBreakerStateRepository(
                l2_repo=l2_repo,
                sync_interval_seconds=5.0,
                adapter_type="redis",
            )

        if not reg.has_provider("layered"):
            reg.register("layered", _create_layered_cb_repo)
    except ImportError:
        pass

    # SQL-based (DB-API 2.0 — PostgreSQL / MySQL / SQLite)
    try:
        from baldur.adapters.sql import SQLCircuitBreakerStateRepository
        from baldur.adapters.sql.connection import build_connection_factory

        def _create_sql_cb_repo():
            return SQLCircuitBreakerStateRepository(build_connection_factory())

        if not reg.has_provider("sql"):
            reg.register("sql", _create_sql_cb_repo)
    except ImportError:
        pass


def discover_security_repos() -> None:
    """Auto-register available security incident repository implementations."""
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.security_repo

    # In-memory
    try:
        from baldur.adapters.memory import InMemorySecurityIncidentRepository

        if not reg.has_provider("memory"):
            reg.register("memory", InMemorySecurityIncidentRepository)
    except ImportError:
        pass

    # Django-based
    try:
        from baldur.adapters.django.security_incident import (
            DjangoSecurityIncidentRepository,
        )

        if not reg.has_provider("django"):
            reg.register("django", DjangoSecurityIncidentRepository)
    except ImportError:
        pass

    # SQL-based (DB-API 2.0 — PostgreSQL / MySQL / SQLite)
    try:
        from baldur.adapters.sql import SQLSecurityIncidentRepository
        from baldur.adapters.sql.connection import build_connection_factory

        def _create_sql_security_repo():
            return SQLSecurityIncidentRepository(build_connection_factory())

        if not reg.has_provider("sql"):
            reg.register("sql", _create_sql_security_repo)
    except ImportError:
        pass


def discover_event_journal_repos() -> None:  # noqa: C901
    """Auto-register available event journal repository implementations."""
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.event_journal_repo

    # In-memory
    try:
        from baldur.adapters.memory import InMemoryEventJournalRepository

        def _create_memory_journal_repo():
            from baldur.settings.event_journal import get_event_journal_settings

            settings = get_event_journal_settings()
            return InMemoryEventJournalRepository(
                max_entries=settings.max_entries_memory,
                max_query_limit=settings.max_query_limit,
            )

        if not reg.has_provider("memory"):
            reg.register("memory", _create_memory_journal_repo)
    except ImportError:
        pass

    # Redis-based
    try:
        from baldur.adapters.redis import get_redis_client as _get_journal_redis
        from baldur.adapters.redis.event_journal import RedisEventJournalRepository

        def _create_redis_journal_repo():
            from baldur.settings.event_journal import get_event_journal_settings

            settings = get_event_journal_settings()
            client = _get_journal_redis()
            if client is None:
                from baldur.core.exceptions import AdapterInitializationError

                raise AdapterInitializationError(
                    "Redis client not available for EventJournal"
                )
            return RedisEventJournalRepository(
                redis_client=client,
                ttl_seconds=settings.ttl_days * 86400,
                max_query_limit=settings.max_query_limit,
            )

        if not reg.has_provider("redis"):
            reg.register("redis", _create_redis_journal_repo)
    except ImportError:
        pass

    # SQL-based (DB-API 2.0 — PostgreSQL / MySQL / SQLite)
    try:
        from baldur.adapters.sql import SQLEventJournalRepository
        from baldur.adapters.sql.connection import build_connection_factory

        def _create_sql_journal_repo():
            from baldur.settings.event_journal import get_event_journal_settings

            settings = get_event_journal_settings()
            return SQLEventJournalRepository(
                build_connection_factory(),
                max_query_limit=settings.max_query_limit,
            )

        if not reg.has_provider("sql"):
            reg.register("sql", _create_sql_journal_repo)
    except ImportError:
        pass


# discover_mesh_override_stores removed (599 D7): the store implementation
# moved to baldur_pro.services.circuit_mesh.store with the feature, and the
# relocated service constructs TwoTierMeshOverrideStore directly via cache
# injection. The vestigial ``mesh_override_store`` registry slot itself stays
# for API compatibility (464 D8) — it is simply empty on OSS installs.


def discover_postmortem_repos() -> None:
    """Auto-register available postmortem repository implementations."""
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.postmortem_repo

    # In-memory (for testing, standalone, non-Django)
    try:
        from baldur.adapters.memory.postmortem import (
            InMemoryPostmortemRepository,
        )

        if not reg.has_provider("memory"):
            reg.register("memory", InMemoryPostmortemRepository)
    except ImportError:
        pass

    # Django-based
    try:
        from baldur.adapters.django.repositories.postmortem import (
            DjangoPostmortemRepository,
        )

        if not reg.has_provider("django"):
            reg.register("django", DjangoPostmortemRepository)
    except ImportError:
        pass

    # SQL-based (DB-API 2.0 — PostgreSQL / MySQL / SQLite)
    try:
        from baldur.adapters.sql import SQLPostmortemRepository
        from baldur.adapters.sql.connection import build_connection_factory

        def _create_sql_postmortem_repo():
            return SQLPostmortemRepository(build_connection_factory())

        if not reg.has_provider("sql"):
            reg.register("sql", _create_sql_postmortem_repo)
    except ImportError:
        pass


def discover_recovery_session_repos() -> None:
    """Auto-register available recovery session archive repository implementations."""
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.recovery_session_repo

    # In-memory (for testing, standalone, non-Django)
    try:
        from baldur.adapters.memory.recovery_session import (
            InMemoryRecoverySessionArchiveRepository,
        )

        if not reg.has_provider("memory"):
            reg.register("memory", InMemoryRecoverySessionArchiveRepository)
    except ImportError:
        pass

    # Django-based
    try:
        from baldur.adapters.django.repositories.recovery_session import (
            DjangoRecoverySessionArchiveRepository,
        )

        if not reg.has_provider("django"):
            reg.register("django", DjangoRecoverySessionArchiveRepository)
    except ImportError:
        pass

    # SQL-based (DB-API 2.0 — PostgreSQL / MySQL / SQLite)
    try:
        from baldur.adapters.sql import SQLRecoverySessionArchiveRepository
        from baldur.adapters.sql.connection import build_connection_factory

        def _create_sql_recovery_session_repo():
            return SQLRecoverySessionArchiveRepository(build_connection_factory())

        if not reg.has_provider("sql"):
            reg.register("sql", _create_sql_recovery_session_repo)
    except ImportError:
        pass


def discover_config_history_stores() -> None:
    """Auto-register available config history store implementations."""
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.config_history_store

    # In-memory
    try:
        from baldur.adapters.memory.config_history import (
            InMemoryConfigHistoryStore,
        )

        if not reg.has_provider("memory"):
            reg.register("memory", InMemoryConfigHistoryStore)
    except ImportError:
        pass

    # Redis-based
    try:
        from baldur.adapters.redis.config_history import RedisConfigHistoryStore

        def _create_redis_config_history_store():
            from baldur.adapters.redis import get_redis_client

            client = get_redis_client()
            if client is None:
                from baldur.core.exceptions import AdapterInitializationError

                raise AdapterInitializationError(
                    "Redis client not available for ConfigHistoryStore"
                )
            return RedisConfigHistoryStore(client)

        if not reg.has_provider("redis"):
            reg.register("redis", _create_redis_config_history_store)
    except ImportError:
        pass


def discover_canary_rollout_stores() -> None:
    """Auto-register available canary rollout store implementations."""
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.canary_rollout_store

    # In-memory
    try:
        from baldur.adapters.memory.canary_rollout import (
            InMemoryCanaryRolloutStore,
        )

        if not reg.has_provider("memory"):
            reg.register("memory", InMemoryCanaryRolloutStore)
    except ImportError:
        pass

    # Redis-based
    try:
        from baldur.adapters.redis.canary_rollout import RedisCanaryRolloutStore

        def _create_redis_canary_rollout_store():
            from baldur.adapters.redis import get_redis_client

            client = get_redis_client()
            if client is None:
                from baldur.core.exceptions import AdapterInitializationError

                raise AdapterInitializationError(
                    "Redis client not available for CanaryRolloutStore"
                )
            return RedisCanaryRolloutStore(client)

        if not reg.has_provider("redis"):
            reg.register("redis", _create_redis_canary_rollout_store)
    except ImportError:
        pass


def discover_chaos_experiment_stores() -> None:
    """Auto-register available chaos experiment store implementations."""
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.chaos_experiment_store

    # In-memory
    try:
        from baldur.adapters.memory.chaos_experiment import (
            InMemoryChaosExperimentStore,
        )

        if not reg.has_provider("memory"):
            reg.register("memory", InMemoryChaosExperimentStore)
    except ImportError:
        pass

    # Redis-based
    try:
        from baldur.adapters.redis.chaos_experiment import (
            RedisChaosExperimentStore,
        )

        def _create_redis_chaos_experiment_store():
            from baldur.adapters.redis import get_redis_client

            client = get_redis_client()
            if client is None:
                from baldur.core.exceptions import AdapterInitializationError

                raise AdapterInitializationError(
                    "Redis client not available for ChaosExperimentStore"
                )
            return RedisChaosExperimentStore(client)

        if not reg.has_provider("redis"):
            reg.register("redis", _create_redis_chaos_experiment_store)
    except ImportError:
        pass


def discover_cross_cluster_stores() -> None:
    """Auto-register available cross-cluster store implementations."""
    from baldur.factory.registry import ProviderRegistry

    reg = ProviderRegistry.cross_cluster_store

    # In-memory
    try:
        from baldur.adapters.memory.cross_cluster import InMemoryCrossClusterStore

        if not reg.has_provider("memory"):
            reg.register("memory", InMemoryCrossClusterStore)
    except ImportError:
        pass

    # Redis-based
    try:
        from baldur.adapters.redis.cross_cluster import RedisCrossClusterStore

        def _create_redis_cross_cluster_store():
            from baldur.adapters.redis import get_redis_client

            client = get_redis_client()
            if client is None:
                from baldur.core.exceptions import AdapterInitializationError

                raise AdapterInitializationError(
                    "Redis client not available for CrossClusterStore"
                )
            return RedisCrossClusterStore(client)

        if not reg.has_provider("redis"):
            reg.register("redis", _create_redis_cross_cluster_store)
    except ImportError:
        pass
