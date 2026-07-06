"""
Provider Registry — Thin Facade over GenericProviderRegistry instances.

ProviderRegistry composes sub-registries (one per adapter/repository/strategy
type) as class-level attributes, delegating all storage and lifecycle logic
to GenericProviderRegistry[T].

Usage:
    from baldur.factory import ProviderRegistry

    # Register a provider
    ProviderRegistry.cache.register("redis", RedisCacheAdapter)

    # Get default provider
    cache = ProviderRegistry.cache.get()

    # Get specific provider
    cache = ProviderRegistry.cache.get("redis")

    # Test isolation
    with ProviderRegistry.cache.override(mock_cache):
        ...
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from baldur.factory.base import GenericProviderRegistry

if TYPE_CHECKING:
    from baldur.coordination.base import LeaderElector
    from baldur.core.shutdown_coordinator import ShutdownHandler
    from baldur.interfaces.admin_identity import AdminIdentityResolver
    from baldur.interfaces.alert_adapter import AlertAdapter
    from baldur.interfaces.audit_adapter import AuditLogAdapter
    from baldur.interfaces.blast_radius import BlastRadiusManager
    from baldur.interfaces.bulkhead import BulkheadRegistry
    from baldur.interfaces.cache_provider import CacheProviderInterface
    from baldur.interfaces.canary import CanaryRolloutService
    from baldur.interfaces.canary_rollout_store import CanaryRolloutStore
    from baldur.interfaces.chaos import ChaosScheduler, ReportGenerator, SafetyGuard
    from baldur.interfaces.chaos_experiment_store import ChaosExperimentStore
    from baldur.interfaces.config_history_store import ConfigHistoryStore
    from baldur.interfaces.cross_cluster_store import CrossClusterStore
    from baldur.interfaces.database_health import DatabaseHealthProvider
    from baldur.interfaces.dlq import DLQRepository, DLQService
    from baldur.interfaces.emergency import EmergencyManager
    from baldur.interfaces.error_budget import ErrorBudgetGate, ErrorBudgetService
    from baldur.interfaces.event_journal import EventJournalRepository
    from baldur.interfaces.governance import GovernanceChecker
    from baldur.interfaces.meta_watchdog import SelfhealerWatchdog
    from baldur.interfaces.notification import NotificationAdapter
    from baldur.interfaces.pg_admin import PgAdminProvider
    from baldur.interfaces.pool_info import PoolInfoProvider
    from baldur.interfaces.pool_monitor import PoolStatsProvider
    from baldur.interfaces.quorum import QuorumWitnessProtocol
    from baldur.interfaces.rate_limit_storage import RateLimitStorageInterface
    from baldur.interfaces.repositories import (
        CascadeEventArchiveRepository,
        CircuitBreakerStateRepository,
        FailedOperationRepository,
        PostmortemRepository,
        RecoverySessionArchiveRepository,
        SecurityIncidentRepository,
    )
    from baldur.interfaces.runtime_config import RuntimeConfigManager
    from baldur.interfaces.session_provider import SessionInvalidationProvider
    from baldur.interfaces.statistics import StatisticsRepositoryInterface
    from baldur.interfaces.task_queue import (
        AsyncTaskQueueInterface,
        TaskQueueInterface,
    )
    from baldur.interfaces.throttle import AdaptiveThrottle
    from baldur.interfaces.traffic_routing import TrafficRoutingAdapter
    from baldur.interfaces.web_framework import WebFrameworkInterface
    from baldur.meta.recovery_adapter import RecoveryInfrastructureAdapter

logger = structlog.get_logger()

__all__ = [
    "ProviderRegistry",
    "get_storage_backend",
    "get_circuit_breaker_repo",
    "get_dlq_repo",
]


# =============================================================================
# ProviderRegistry Facade
# =============================================================================


class ProviderRegistry:
    """
    Central registry for all pluggable components.

    Each adapter/repository/strategy type is a GenericProviderRegistry
    class attribute. Thread-safe singleton creation is handled by
    GenericProviderRegistry's DCL pattern.

    Sub-registries:
        cache, queue, async_queue          — adapters
        audit, traffic_routing             — adapters
        notification, alert                — adapters
        rate_limit_storage                 — adapters
        failed_op_repo, circuit_breaker_repo, security_repo — repositories
        event_journal_repo, mesh_override_store             — repositories
        correlation_strategy, root_cause_strategy           — strategies
        graph_build_strategy                                — strategies
        anomaly_detection, forecast, classification         — ML strategies
        optimization                                        — ML strategies
        finops_service, learning_service                    — feature services
        compliance_engine, predictive_forecaster_service    — feature services
        worker_background_starts                            — start callables
        shutdown_integrations, startup_integrations         — lifecycle callables

    Special (non-generic) registries:
        statistics  — singleton adapter, not name-based
    """

    # -- Adapter registries ---------------------------------------------------

    cache: GenericProviderRegistry[CacheProviderInterface] = GenericProviderRegistry(
        adapter_type="cache",
        auto_discover=lambda: __import__(
            "baldur.factory.adapters", fromlist=["discover_cache_adapters"]
        ).discover_cache_adapters(),
    )

    queue: GenericProviderRegistry[TaskQueueInterface] = GenericProviderRegistry(
        adapter_type="queue",
        auto_discover=lambda: __import__(
            "baldur.factory.adapters", fromlist=["discover_queue_adapters"]
        ).discover_queue_adapters(),
    )

    async_queue: GenericProviderRegistry[AsyncTaskQueueInterface] = (
        GenericProviderRegistry(
            adapter_type="async_queue",
            auto_discover=lambda: __import__(
                "baldur.factory.adapters",
                fromlist=["discover_async_queue_adapters"],
            ).discover_async_queue_adapters(),
        )
    )

    audit: GenericProviderRegistry[AuditLogAdapter] = GenericProviderRegistry(
        adapter_type="audit",
        auto_discover=lambda: __import__(
            "baldur.factory.adapters", fromlist=["discover_audit_adapters"]
        ).discover_audit_adapters(),
    )

    traffic_routing: GenericProviderRegistry[TrafficRoutingAdapter] = (
        GenericProviderRegistry(
            adapter_type="traffic_routing",
            auto_discover=lambda: __import__(
                "baldur.factory.adapters",
                fromlist=["discover_traffic_routing_adapters"],
            ).discover_traffic_routing_adapters(),
        )
    )

    notification: GenericProviderRegistry[NotificationAdapter] = (
        GenericProviderRegistry(
            adapter_type="notification",
            auto_discover=lambda: __import__(
                "baldur.factory.adapters",
                fromlist=["discover_notification_adapters"],
            ).discover_notification_adapters(),
        )
    )

    alert: GenericProviderRegistry[AlertAdapter] = GenericProviderRegistry(
        adapter_type="alert",
        auto_discover=lambda: __import__(
            "baldur.factory.adapters", fromlist=["discover_alert_adapters"]
        ).discover_alert_adapters(),
    )

    database_health: GenericProviderRegistry[DatabaseHealthProvider] = (
        GenericProviderRegistry(
            adapter_type="database_health",
            auto_discover=lambda: __import__(
                "baldur.factory.adapters",
                fromlist=["discover_database_health_adapters"],
            ).discover_database_health_adapters(),
        )
    )

    pg_admin: GenericProviderRegistry[PgAdminProvider] = GenericProviderRegistry(
        adapter_type="pg_admin",
        auto_discover=lambda: __import__(
            "baldur.factory.adapters",
            fromlist=["discover_pg_admin_adapters"],
        ).discover_pg_admin_adapters(),
    )

    pool_info: GenericProviderRegistry[PoolInfoProvider] = GenericProviderRegistry(
        adapter_type="pool_info",
        auto_discover=lambda: __import__(
            "baldur.factory.adapters",
            fromlist=["discover_pool_info_adapters"],
        ).discover_pool_info_adapters(),
    )

    session_invalidation: GenericProviderRegistry[SessionInvalidationProvider] = (
        GenericProviderRegistry(
            adapter_type="session_invalidation",
            auto_discover=lambda: __import__(
                "baldur.factory.adapters",
                fromlist=["discover_session_adapters"],
            ).discover_session_adapters(),
        )
    )

    web_framework: GenericProviderRegistry[WebFrameworkInterface] = (
        GenericProviderRegistry(
            adapter_type="web_framework",
            auto_discover=lambda: __import__(
                "baldur.factory.adapters",
                fromlist=["discover_web_framework_adapters"],
            ).discover_web_framework_adapters(),
        )
    )

    rate_limit_storage: GenericProviderRegistry[RateLimitStorageInterface] = (
        GenericProviderRegistry(
            adapter_type="rate_limit_storage",
            auto_discover=lambda: __import__(
                "baldur.factory.adapters",
                fromlist=["discover_rate_limit_storage_adapters"],
            ).discover_rate_limit_storage_adapters(),
        )
    )

    # -- 516 OSS->PRO boundary registries (D2) --------------------------------
    # OSS NoOp defaults are pre-registered below; PRO concrete adapters
    # override via ProviderRegistry.X.register("pro", ...) at import time.

    pool_monitor: GenericProviderRegistry[PoolStatsProvider] = GenericProviderRegistry(
        adapter_type="pool_monitor"
    )

    governance: GenericProviderRegistry[GovernanceChecker] = GenericProviderRegistry(
        adapter_type="governance"
    )

    shutdown_integrations: GenericProviderRegistry[ShutdownHandler] = (
        GenericProviderRegistry(adapter_type="shutdown_integrations")
    )

    # 615 D1 — startup mirror of ``shutdown_integrations``. Holds zero-arg,
    # self-gating, master-skipping, fail-soft start callables registered by
    # the PRO package (``baldur_pro.startup.register_startup_integrations``).
    # ``bootstrap.start_background_workers()`` iterates this slot after the OSS
    # ``_BACKGROUND_WORKER_STARTERS`` tuple. Consumed by name via
    # ``get_provider(name)()`` (not ``get()``, which would invoke-and-cache the
    # starter as a factory) — the ``worker_background_starts`` precedent. An
    # empty slot (OSS-only / unentitled install) iterates to a no-op.
    startup_integrations: GenericProviderRegistry = GenericProviderRegistry(
        adapter_type="startup_integrations"
    )

    # -- 519 PR 2 (c) singleton service registries ----------------------------
    # OSS leaves these slots empty; PRO modules register concrete singletons
    # at import time via ``ProviderRegistry.<slot>.register("pro", instance)
    # + set_default("pro")``. OSS callers use ``safe_get()`` which returns
    # ``None`` when no provider is registered (519 D-c2 replacement shape).

    emergency_manager: GenericProviderRegistry[EmergencyManager] = (
        GenericProviderRegistry(adapter_type="emergency_manager")
    )

    adaptive_throttle: GenericProviderRegistry[AdaptiveThrottle] = (
        GenericProviderRegistry(adapter_type="adaptive_throttle")
    )

    bulkhead_registry: GenericProviderRegistry[BulkheadRegistry] = (
        GenericProviderRegistry(adapter_type="bulkhead_registry")
    )

    runtime_config_manager: GenericProviderRegistry[RuntimeConfigManager] = (
        GenericProviderRegistry(adapter_type="runtime_config_manager")
    )

    chaos_scheduler: GenericProviderRegistry[ChaosScheduler] = GenericProviderRegistry(
        adapter_type="chaos_scheduler"
    )

    report_generator: GenericProviderRegistry[ReportGenerator] = (
        GenericProviderRegistry(adapter_type="report_generator")
    )

    safety_guard: GenericProviderRegistry[SafetyGuard] = GenericProviderRegistry(
        adapter_type="safety_guard"
    )

    dlq_service: GenericProviderRegistry[DLQService] = GenericProviderRegistry(
        adapter_type="dlq_service"
    )

    dlq_repository: GenericProviderRegistry[DLQRepository] = GenericProviderRegistry(
        adapter_type="dlq_repository"
    )

    selfhealer_watchdog: GenericProviderRegistry[SelfhealerWatchdog] = (
        GenericProviderRegistry(adapter_type="selfhealer_watchdog")
    )

    error_budget_service: GenericProviderRegistry[ErrorBudgetService] = (
        GenericProviderRegistry(adapter_type="error_budget_service")
    )

    error_budget_gate: GenericProviderRegistry[ErrorBudgetGate] = (
        GenericProviderRegistry(adapter_type="error_budget_gate")
    )

    canary_rollout_service: GenericProviderRegistry[CanaryRolloutService] = (
        GenericProviderRegistry(adapter_type="canary_rollout_service")
    )

    blast_radius_manager: GenericProviderRegistry[BlastRadiusManager] = (
        GenericProviderRegistry(adapter_type="blast_radius_manager")
    )

    # -- 537 admin identity seam (OSS->PRO boundary) --------------------------
    # No OSS default — empty slot (519 D-c2 shape). ``safe_get()`` returns None
    # for OSS so the admin dispatch seam is a no-op (ctx.user stays None ->
    # resolve_actor = "anonymous"). PRO registers the concrete IdentityResolver.
    admin_identity_resolver: GenericProviderRegistry[AdminIdentityResolver] = (
        GenericProviderRegistry(adapter_type="admin_identity_resolver")
    )

    # -- 528 D10-v2 Dormant boundary registries -------------------------------
    # OSS leaves these slots empty at PR 1 land time. PR 2 (Stage 2b) relocates
    # the concrete K8s/Kafka/WORM/S3/DynamoDB adapters into ``src/baldur_dormant/``
    # and registers them here via ``baldur_dormant.register_dormant_services()``.
    # Until Stage 2b lands, OSS callers continue to resolve through the legacy
    # in-function imports inside ``coordination/factory.py`` / ``multiregion/
    # quorum_factory.py`` / etc. — the slots exist now so the Stage 2b refactor
    # is purely a callsite-routing change with no registry-shape change.

    leader_elector: GenericProviderRegistry[LeaderElector] = GenericProviderRegistry(
        adapter_type="leader_elector"
    )

    audit_kafka_adapter: GenericProviderRegistry[AuditLogAdapter] = (
        GenericProviderRegistry(adapter_type="audit_kafka_adapter")
    )

    audit_worm_adapter: GenericProviderRegistry[AuditLogAdapter] = (
        GenericProviderRegistry(adapter_type="audit_worm_adapter")
    )

    audit_s3_exporter: GenericProviderRegistry = GenericProviderRegistry(
        adapter_type="audit_s3_exporter"
    )

    kafka_eventbus: GenericProviderRegistry = GenericProviderRegistry(
        adapter_type="kafka_eventbus"
    )

    quorum_witness: GenericProviderRegistry[QuorumWitnessProtocol] = (
        GenericProviderRegistry(adapter_type="quorum_witness")
    )

    recovery_adapter: GenericProviderRegistry[RecoveryInfrastructureAdapter] = (
        GenericProviderRegistry(adapter_type="recovery_adapter")
    )

    # -- Repository registries ------------------------------------------------

    failed_op_repo: GenericProviderRegistry[FailedOperationRepository] = (
        GenericProviderRegistry(
            adapter_type="failed_op_repo",
            auto_discover=lambda: __import__(
                "baldur.factory.repositories",
                fromlist=["discover_failed_op_repos"],
            ).discover_failed_op_repos(),
        )
    )

    circuit_breaker_repo: GenericProviderRegistry[CircuitBreakerStateRepository] = (
        GenericProviderRegistry(
            adapter_type="circuit_breaker_repo",
            auto_discover=lambda: __import__(
                "baldur.factory.repositories",
                fromlist=["discover_circuit_breaker_repos"],
            ).discover_circuit_breaker_repos(),
        )
    )

    security_repo: GenericProviderRegistry[SecurityIncidentRepository] = (
        GenericProviderRegistry(
            adapter_type="security_repo",
            auto_discover=lambda: __import__(
                "baldur.factory.repositories", fromlist=["discover_security_repos"]
            ).discover_security_repos(),
        )
    )

    event_journal_repo: GenericProviderRegistry[EventJournalRepository] = (
        GenericProviderRegistry(
            adapter_type="event_journal_repo",
            auto_discover=lambda: __import__(
                "baldur.factory.repositories",
                fromlist=["discover_event_journal_repos"],
            ).discover_event_journal_repos(),
        )
    )

    # 464 D8 / 599 D7 — vestigial slot kept for API compatibility with
    # register_mesh_override_store / get_mesh_override_store. The store
    # implementation moved to baldur_pro with the circuit_mesh feature; the
    # slot is empty on OSS installs (no auto_discover, no default).
    mesh_override_store: GenericProviderRegistry = GenericProviderRegistry(
        adapter_type="mesh_override_store"
    )

    postmortem_repo: GenericProviderRegistry[PostmortemRepository] = (
        GenericProviderRegistry(
            adapter_type="postmortem_repo",
            auto_discover=lambda: __import__(
                "baldur.factory.repositories",
                fromlist=["discover_postmortem_repos"],
            ).discover_postmortem_repos(),
        )
    )

    cascade_event_repo: GenericProviderRegistry[CascadeEventArchiveRepository] = (
        GenericProviderRegistry(
            adapter_type="cascade_event_repo",
            auto_discover=lambda: __import__(
                "baldur.factory.repositories",
                fromlist=["discover_cascade_event_repos"],
            ).discover_cascade_event_repos(),
        )
    )

    recovery_session_repo: GenericProviderRegistry[RecoverySessionArchiveRepository] = (
        GenericProviderRegistry(
            adapter_type="recovery_session_repo",
            auto_discover=lambda: __import__(
                "baldur.factory.repositories",
                fromlist=["discover_recovery_session_repos"],
            ).discover_recovery_session_repos(),
        )
    )

    # -- Domain state store registries ----------------------------------------

    config_history_store: GenericProviderRegistry[ConfigHistoryStore] = (
        GenericProviderRegistry(
            adapter_type="config_history_store",
            auto_discover=lambda: __import__(
                "baldur.factory.repositories",
                fromlist=["discover_config_history_stores"],
            ).discover_config_history_stores(),
        )
    )

    canary_rollout_store: GenericProviderRegistry[CanaryRolloutStore] = (
        GenericProviderRegistry(
            adapter_type="canary_rollout_store",
            auto_discover=lambda: __import__(
                "baldur.factory.repositories",
                fromlist=["discover_canary_rollout_stores"],
            ).discover_canary_rollout_stores(),
        )
    )

    chaos_experiment_store: GenericProviderRegistry[ChaosExperimentStore] = (
        GenericProviderRegistry(
            adapter_type="chaos_experiment_store",
            auto_discover=lambda: __import__(
                "baldur.factory.repositories",
                fromlist=["discover_chaos_experiment_stores"],
            ).discover_chaos_experiment_stores(),
        )
    )

    cross_cluster_store: GenericProviderRegistry[CrossClusterStore] = (
        GenericProviderRegistry(
            adapter_type="cross_cluster_store",
            auto_discover=lambda: __import__(
                "baldur.factory.repositories",
                fromlist=["discover_cross_cluster_stores"],
            ).discover_cross_cluster_stores(),
        )
    )

    # -- Strategy registries --------------------------------------------------

    correlation_strategy: GenericProviderRegistry = GenericProviderRegistry(
        adapter_type="correlation_strategy",
        auto_discover=lambda: __import__(
            "baldur.factory.strategies",
            fromlist=["discover_correlation_strategies"],
        ).discover_correlation_strategies(),
    )

    root_cause_strategy: GenericProviderRegistry = GenericProviderRegistry(
        adapter_type="root_cause_strategy",
        auto_discover=lambda: __import__(
            "baldur.factory.strategies",
            fromlist=["discover_root_cause_strategies"],
        ).discover_root_cause_strategies(),
    )

    graph_build_strategy: GenericProviderRegistry = GenericProviderRegistry(
        adapter_type="graph_build_strategy",
        auto_discover=lambda: __import__(
            "baldur.factory.strategies",
            fromlist=["discover_graph_build_strategies"],
        ).discover_graph_build_strategies(),
    )

    # -- ML Strategy registries (instance-based, DCL caching) -----------------
    # 599 D9 — empty slots at module load (OSS chassis). The private
    # bootstrap hook (baldur_dormant.register_dormant_services) registers
    # the statistical defaults, sets the slot defaults, and runs the
    # flag-gated ML registration. OSS-only installs leave these empty:
    # baldur.factory.strategies.get_best_* raises AdapterNotFoundError /
    # returns None into the consumers' fail-open seams.

    anomaly_detection: GenericProviderRegistry = GenericProviderRegistry(
        adapter_type="anomaly_detection"
    )
    forecast: GenericProviderRegistry = GenericProviderRegistry(adapter_type="forecast")
    classification: GenericProviderRegistry = GenericProviderRegistry(
        adapter_type="classification"
    )
    optimization: GenericProviderRegistry = GenericProviderRegistry(
        adapter_type="optimization"
    )

    # -- 599 D7 — feature-service slots for relocated private implementations -
    # OSS leaves these slots empty; the owning private package registers a
    # concrete singleton via its bootstrap hook (register_pro_services /
    # register_dormant_services). OSS handlers resolve via ``safe_get()``
    # and degrade to service-unavailable when None (canary/chaos pattern).

    finops_service: GenericProviderRegistry = GenericProviderRegistry(
        adapter_type="finops_service"
    )

    learning_service: GenericProviderRegistry = GenericProviderRegistry(
        adapter_type="learning_service"
    )

    compliance_engine: GenericProviderRegistry = GenericProviderRegistry(
        adapter_type="compliance_engine"
    )

    predictive_forecaster_service: GenericProviderRegistry = GenericProviderRegistry(
        adapter_type="predictive_forecaster_service"
    )

    # -- 599 D12 — per-worker background start callables -----------------------
    # Django apps.py resolves these by name on worker boot/restart
    # (post_worker_init re-runs the start path after fork). The owning
    # private package registers a zero-arg callable per start path; an
    # empty slot means the feature is absent (OSS-only install) and the
    # start path degrades to a debug-log no-op.
    worker_background_starts: GenericProviderRegistry = GenericProviderRegistry(
        adapter_type="worker_background_starts"
    )

    # -- Special registries (not name-based) ----------------------------------

    _statistics_adapter: StatisticsRepositoryInterface | None = None

    # =========================================================================
    # Cache adapter — special wrapping with MetricsAwareCacheAdapter
    # =========================================================================

    @classmethod
    def get_cache(
        cls,
        name: str | None = None,
        singleton: bool = True,
    ) -> CacheProviderInterface:
        """Get cache provider instance, wrapped with metrics decorator.

        Args:
            name: Provider name (e.g., 'redis', 'memory')
            singleton: If True, return cached instance

        Returns:
            CacheProviderInterface instance (metrics-wrapped)
        """
        _warn_if_init_not_called_cache()
        if not singleton:
            return cls._wrap_cache_with_metrics(cls.cache.create_new(name))

        # Singleton path: get from cache, wrap, and store wrapped version back
        instance = cls.cache.get(name)
        wrapped = cls._wrap_cache_with_metrics(instance)
        if wrapped is not instance:
            # Store the wrapped version so subsequent calls return the same object
            resolved_name = name or cls.cache.get_default_name()
            if resolved_name is not None:
                cls.cache.set_instance(resolved_name, wrapped)
        return wrapped

    @staticmethod
    def _wrap_cache_with_metrics(
        cache: CacheProviderInterface,
    ) -> CacheProviderInterface:
        """Wrap cache adapter with MetricsAwareCacheAdapter for uniform metrics."""
        from baldur.adapters.cache.metrics_decorator import (
            MetricsAwareCacheAdapter,
        )

        if isinstance(cache, MetricsAwareCacheAdapter):
            return cache
        return MetricsAwareCacheAdapter(cache)

    # =========================================================================
    # Audit adapter — special file-path handling
    # =========================================================================

    @classmethod
    def get_audit_adapter(
        cls,
        name: str | None = None,
        singleton: bool = True,
    ) -> AuditLogAdapter:
        """Get audit adapter instance.

        Args:
            name: Adapter name (e.g., 'file', 'stdout', 'null')
            singleton: If True, return cached instance

        Returns:
            AuditLogAdapter instance
        """
        name = name or cls.audit.get_default_name() or "null"
        if not singleton:
            return cls.audit.create_new(name)
        return cls.audit.get(name)

    # =========================================================================
    # Event journal — registry-default name (wired by init() per 570 D1)
    # =========================================================================

    @classmethod
    def get_event_journal_repo(
        cls,
        name: str | None = None,
        singleton: bool = True,
    ) -> EventJournalRepository:
        """Get event journal repository instance.

        When ``name`` is None the registry default is used. The default is
        "memory" at module load and is rewired by ``init()`` to
        "redis"/"sql"/"memory" via the ``event_journal_repo``
        PRIORITY_CHAIN row, honoring ``BALDUR_REDIS_URL`` and the
        ``BALDUR_EVENT_JOURNAL_BACKEND`` operator override. Passing an
        explicit ``name`` bypasses that wired default.

        Args:
            name: Repository name (e.g., 'memory', 'redis', 'sql')
            singleton: If True, return cached instance

        Returns:
            EventJournalRepository instance
        """
        if not singleton:
            return cls.event_journal_repo.create_new(name)

        return cls.event_journal_repo.get(name)

    # =========================================================================
    # Statistics adapter (singleton, not name-based)
    # =========================================================================

    @classmethod
    def register_statistics_adapter(
        cls,
        adapter: StatisticsRepositoryInterface,
    ) -> None:
        """Register a statistics adapter.

        Should be called during app initialization (e.g., Django's AppConfig.ready()).
        Only one statistics adapter can be registered at a time.

        Args:
            adapter: StatisticsRepositoryInterface implementation
        """
        cls._statistics_adapter = adapter
        logger.info(
            "registry.statistics_adapter_registered",
            adapter_type=type(adapter).__name__,
        )

    @classmethod
    def get_statistics_repo(cls) -> StatisticsRepositoryInterface:
        """Get statistics repository instance.

        Returns the registered statistics adapter, or NullStatisticsRepository
        if no adapter is registered.

        Returns:
            StatisticsRepositoryInterface instance
        """
        if cls._statistics_adapter is None:
            from baldur.adapters.statistics.null import NullStatisticsRepository

            return NullStatisticsRepository()
        return cls._statistics_adapter

    @classmethod
    def has_statistics_adapter(cls) -> bool:
        """Check if a statistics adapter is registered."""
        return cls._statistics_adapter is not None

    # =========================================================================
    # Postmortem repository
    # =========================================================================

    @classmethod
    def register_postmortem_repo(cls, name: str, repo_class: type) -> None:
        """Register a postmortem repository."""
        cls.postmortem_repo.register(name, repo_class)

    @classmethod
    def get_postmortem_repo(
        cls,
        name: str | None = None,
        singleton: bool = True,
    ) -> PostmortemRepository:
        """Get postmortem repository instance."""
        if not singleton:
            return cls.postmortem_repo.create_new(name)
        return cls.postmortem_repo.get(name)

    @classmethod
    def has_postmortem_repo(cls) -> bool:
        """Check if any postmortem repository is registered."""
        return cls.postmortem_repo.has_any_providers()

    # =========================================================================
    # Cascade Event repository
    # =========================================================================

    @classmethod
    def register_cascade_event_repo(cls, name: str, repo_class: type) -> None:
        """Register a cascade event archive repository."""
        cls.cascade_event_repo.register(name, repo_class)

    @classmethod
    def get_cascade_event_repo(
        cls,
        name: str | None = None,
        singleton: bool = True,
    ) -> CascadeEventArchiveRepository:
        """Get cascade event archive repository instance."""
        if not singleton:
            return cls.cascade_event_repo.create_new(name)
        return cls.cascade_event_repo.get(name)

    # =========================================================================
    # Recovery Session repository
    # =========================================================================

    @classmethod
    def register_recovery_session_repo(cls, name: str, repo_class: type) -> None:
        """Register a recovery session archive repository."""
        cls.recovery_session_repo.register(name, repo_class)

    @classmethod
    def get_recovery_session_repo(
        cls,
        name: str | None = None,
        singleton: bool = True,
    ) -> RecoverySessionArchiveRepository:
        """Get recovery session archive repository instance."""
        if not singleton:
            return cls.recovery_session_repo.create_new(name)
        return cls.recovery_session_repo.get(name)

    # =========================================================================
    # Backward-compatible convenience methods (delegate to sub-registries)
    # =========================================================================

    @classmethod
    def register_cache(cls, name: str, provider_class: type) -> None:
        """Register a cache provider adapter."""
        cls.cache.register(name, provider_class)

    @classmethod
    def register_queue(cls, name: str, provider_class: type) -> None:
        """Register a task queue adapter."""
        cls.queue.register(name, provider_class)

    @classmethod
    def register_async_queue(
        cls,
        name: str,
        adapter_class: type,
    ) -> None:
        """Register an async task queue adapter."""
        cls.async_queue.register(name, adapter_class)

    @classmethod
    def register_failed_operation_repo(cls, name: str, repo_class: type) -> None:
        """Register a failed operation repository."""
        cls.failed_op_repo.register(name, repo_class)

    @classmethod
    def register_circuit_breaker_repo(cls, name: str, repo_class: type) -> None:
        """Register a circuit breaker state repository."""
        cls.circuit_breaker_repo.register(name, repo_class)

    @classmethod
    def register_security_repo(cls, name: str, repo_class: type) -> None:
        """Register a security incident repository."""
        cls.security_repo.register(name, repo_class)

    @classmethod
    def register_event_journal_repo(cls, name: str, repo_class: type) -> None:
        """Register an event journal repository."""
        cls.event_journal_repo.register(name, repo_class)

    @classmethod
    def register_audit_adapter(cls, name: str, adapter_class: type) -> None:
        """Register an audit log adapter."""
        cls.audit.register(name, adapter_class)

    @classmethod
    def register_traffic_routing(cls, name: str, adapter_class: type) -> None:
        """Register a traffic routing adapter."""
        cls.traffic_routing.register(name, adapter_class)

    @classmethod
    def register_notification(
        cls, name: str, adapter_class: type | Callable[..., Any]
    ) -> None:
        """Register a notification adapter (class or zero-arg factory)."""
        cls.notification.register(name, adapter_class)

    @classmethod
    def register_alert(cls, name: str, factory: type | Callable[..., Any]) -> None:
        """Register an alert adapter (class or zero-arg factory)."""
        cls.alert.register(name, factory)

    @classmethod
    def register_correlation_strategy(cls, name: str, strategy_class: type) -> None:
        """Register a correlation strategy."""
        cls.correlation_strategy.register(name, strategy_class)

    @classmethod
    def register_root_cause_strategy(cls, name: str, strategy_class: type) -> None:
        """Register a root cause strategy."""
        cls.root_cause_strategy.register(name, strategy_class)

    @classmethod
    def register_graph_build_strategy(cls, name: str, strategy_class: type) -> None:
        """Register a graph build strategy."""
        cls.graph_build_strategy.register(name, strategy_class)

    @classmethod
    def register_mesh_override_store(cls, name: str, store_class: type) -> None:
        """Register a mesh override store implementation."""
        cls.mesh_override_store.register(name, store_class)

    # -- Getter convenience methods -------------------------------------------

    @classmethod
    def get_queue(
        cls,
        name: str | None = None,
        singleton: bool = True,
    ) -> TaskQueueInterface:
        """Get task queue instance."""
        if not singleton:
            return cls.queue.create_new(name)
        return cls.queue.get(name)

    @classmethod
    def get_async_queue(
        cls,
        name: str | None = None,
        singleton: bool = True,
    ) -> AsyncTaskQueueInterface:
        """Get async task queue instance."""
        if not singleton:
            return cls.async_queue.create_new(name)
        return cls.async_queue.get(name)

    @classmethod
    def get_failed_operation_repo(
        cls,
        name: str | None = None,
        singleton: bool = True,
    ) -> FailedOperationRepository:
        """Get failed operation repository instance."""
        if not singleton:
            return cls.failed_op_repo.create_new(name)
        return cls.failed_op_repo.get(name)

    @classmethod
    def get_circuit_breaker_repo(
        cls,
        name: str | None = None,
        singleton: bool = True,
    ) -> CircuitBreakerStateRepository:
        """Get circuit breaker state repository instance."""
        if not singleton:
            return cls.circuit_breaker_repo.create_new(name)
        return cls.circuit_breaker_repo.get(name)

    @classmethod
    def get_security_repo(
        cls,
        name: str | None = None,
        singleton: bool = True,
    ) -> SecurityIncidentRepository:
        """Get security incident repository instance."""
        if not singleton:
            return cls.security_repo.create_new(name)
        return cls.security_repo.get(name)

    @classmethod
    def get_traffic_routing(
        cls,
        name: str | None = None,
        singleton: bool = True,
    ) -> TrafficRoutingAdapter:
        """Get traffic routing adapter instance."""
        if not singleton:
            return cls.traffic_routing.create_new(name)
        return cls.traffic_routing.get(name)

    @classmethod
    def get_notification(
        cls,
        name: str | None = None,
    ) -> NotificationAdapter:
        """Get notification adapter instance."""
        return cls.notification.get(name)

    @classmethod
    def get_alert(
        cls,
        name: str | None = None,
    ) -> AlertAdapter:
        """Get alert adapter instance."""
        return cls.alert.get(name)

    @classmethod
    def get_mesh_override_store(
        cls,
        name: str | None = None,
        singleton: bool = True,
    ) -> Any:
        # Vestigial API-compat accessor (464 D8 / 599 D7) — kept while the
        # registry-deletion sweep is pending.
        """Get a mesh override store instance.

        Vestigial API-compat accessor: the store implementation moved to
        the private distribution with the circuit_mesh feature, so the
        slot is empty on OSS installs and this raises AdapterNotFoundError
        unless a provider was registered via ``register_mesh_override_store``.
        """
        name = name or "memory"
        if not singleton:
            return cls.mesh_override_store.create_new(name)
        return cls.mesh_override_store.get(name)

    @classmethod
    def get_correlation_strategy(cls, name: str) -> type | Callable[..., Any]:
        """Get registered correlation strategy class (not instance).

        Strategy registries store class references that callers instantiate
        themselves, so we return the provider class directly rather than
        calling GenericProviderRegistry.get() which would instantiate it.
        """
        return cls.correlation_strategy.get_provider(name)

    @classmethod
    def get_root_cause_strategy(cls, name: str) -> type | Callable[..., Any]:
        """Get registered root cause strategy class (not instance)."""
        return cls.root_cause_strategy.get_provider(name)

    @classmethod
    def get_graph_build_strategy(cls, name: str) -> type | Callable[..., Any]:
        """Get registered graph build strategy class (not instance)."""
        return cls.graph_build_strategy.get_provider(name)

    # =========================================================================
    # Configuration
    # =========================================================================

    @classmethod
    def set_defaults(
        cls,
        cache: str | None = None,
        queue: str | None = None,
        repo: str | None = None,
    ) -> None:
        """Set default providers.

        Args:
            cache: Default cache provider name
            queue: Default task queue name
            repo: Default repository name (applied to all repo registries)
        """
        if cache:
            cls.cache.set_default(cache)
        if queue:
            cls.queue.set_default(queue)
        if repo:
            cls.failed_op_repo.set_default(repo)
            cls.circuit_breaker_repo.set_default(repo)
            cls.security_repo.set_default(repo)

        logger.info(
            "registry.defaults_updated",
            cache=cls.cache.get_default_name(),
            queue=cls.queue.get_default_name(),
            repo=cls.failed_op_repo.get_default_name(),
        )

    @classmethod
    def get_defaults(cls) -> dict[str, str | None]:
        """Get current default provider names."""
        return {
            "cache": cls.cache.get_default_name(),
            "queue": cls.queue.get_default_name(),
            "repo": cls.failed_op_repo.get_default_name(),
        }

    @classmethod
    def list_providers(cls) -> dict[str, Any]:
        """List all registered providers."""
        return {
            "cache": cls.cache.list_providers(),
            "queue": cls.queue.list_providers(),
            "failed_operation_repo": cls.failed_op_repo.list_providers(),
            "circuit_breaker_repo": cls.circuit_breaker_repo.list_providers(),
            "security_repo": cls.security_repo.list_providers(),
            "audit_adapter": cls.audit.list_providers(),
            "traffic_routing": cls.traffic_routing.list_providers(),
            "event_journal_repo": cls.event_journal_repo.list_providers(),
            "notification": cls.notification.list_providers(),
            "alert": cls.alert.list_providers(),
            "async_queue": cls.async_queue.list_providers(),
            "postmortem_repo": cls.postmortem_repo.list_providers(),
            "cascade_event_repo": cls.cascade_event_repo.list_providers(),
            "recovery_session_repo": cls.recovery_session_repo.list_providers(),
            "rate_limit_storage": cls.rate_limit_storage.list_providers(),
            "statistics_adapter": (
                type(cls._statistics_adapter).__name__
                if cls._statistics_adapter
                else None
            ),
        }

    # =========================================================================
    # Lifecycle
    # =========================================================================

    @classmethod
    def clear_instances(cls) -> None:
        """Clear all cached instances (providers remain registered)."""
        for attr in _iter_sub_registries(cls):
            attr.clear_instances()
        logger.debug("registry.instances_cleared", scope="instances")

    @classmethod
    def reset(cls) -> None:
        """Reset registry to initial state (all providers and instances cleared)."""
        for attr in _iter_sub_registries(cls):
            attr.reset()
        cls._statistics_adapter = None
        logger.debug("registry.instances_cleared", scope="all")

    # =========================================================================
    # Health Check
    # =========================================================================

    @classmethod
    def health_check_all(cls) -> dict[str, bool]:
        """Run health checks on all default providers.

        Returns:
            Dict mapping provider type to health status
        """
        results: dict[str, bool] = {}

        try:
            cache_instance = cls.get_cache()
            results["cache"] = cache_instance.health_check()
        except Exception as e:
            logger.exception(
                "registry.cache_health_check_failed",
                error=e,
            )
            results["cache"] = False

        try:
            queue_instance = cls.get_queue()
            results["queue"] = queue_instance.health_check()
        except Exception as e:
            logger.exception(
                "registry.queue_health_check_failed",
                error=e,
            )
            results["queue"] = False

        return results


# =============================================================================
# Internal Helpers
# =============================================================================


def _iter_sub_registries(cls: type) -> list[GenericProviderRegistry]:
    """Return all GenericProviderRegistry class attributes."""
    return [v for v in vars(cls).values() if isinstance(v, GenericProviderRegistry)]


# =============================================================================
# Auto-registration on import
# =============================================================================


def _auto_register_adapters() -> None:
    """Auto-register available adapters based on installed packages."""
    from baldur.factory.adapters import (
        discover_alert_adapters,
        discover_async_queue_adapters,
        discover_audit_adapters,
        discover_cache_adapters,
        discover_database_health_adapters,
        discover_notification_adapters,
        discover_pg_admin_adapters,
        discover_pool_info_adapters,
        discover_queue_adapters,
        discover_rate_limit_storage_adapters,
        discover_session_adapters,
        discover_traffic_routing_adapters,
    )
    from baldur.factory.repositories import (
        discover_canary_rollout_stores,
        discover_cascade_event_repos,
        discover_chaos_experiment_stores,
        discover_circuit_breaker_repos,
        discover_config_history_stores,
        discover_cross_cluster_stores,
        discover_event_journal_repos,
        discover_failed_op_repos,
        discover_postmortem_repos,
        discover_recovery_session_repos,
        discover_security_repos,
    )

    discover_cache_adapters()
    discover_queue_adapters()
    discover_async_queue_adapters()
    discover_failed_op_repos()
    discover_circuit_breaker_repos()
    discover_security_repos()
    discover_event_journal_repos()
    discover_postmortem_repos()
    discover_cascade_event_repos()
    discover_recovery_session_repos()
    discover_config_history_stores()
    discover_canary_rollout_stores()
    discover_chaos_experiment_stores()
    discover_cross_cluster_stores()
    discover_audit_adapters()
    discover_traffic_routing_adapters()
    discover_notification_adapters()
    discover_alert_adapters()
    discover_database_health_adapters()
    discover_pg_admin_adapters()
    discover_pool_info_adapters()
    discover_session_adapters()
    discover_rate_limit_storage_adapters()

    # ML strategy slots are NOT populated here (599 D9) — statistical
    # defaults are registered by register_dormant_services() at init().


# Set default names that match the old registry behavior.
#
# 463 D5 / 464 — the module-load "memory" default is the "init() not called"
# fallback. baldur.bootstrap._wire_registry_defaults() re-asserts the
# environment-aware default ("redis" / "sql" / "django" / "memory") on top
# during init() for the registries listed in _REGISTRIES_TO_WIRE.
# Production deploys that follow the framework-adapter invariant always go
# through that override; this default only matters for ad-hoc CLI / REPL /
# utility scripts, where the D12 WARNING in get_cache() flags the situation.
ProviderRegistry.cache.set_default("memory")
ProviderRegistry.queue.set_default("sync")
ProviderRegistry.failed_op_repo.set_default("redis")
ProviderRegistry.circuit_breaker_repo.set_default("redis")
# 464 D7 — was "redis" before the framework-agnostic registry redesign reclassified
# security_repo to SQL/Django. No Redis adapter ever existed
# (discover_security_repos registers only memory/django/sql), so the
# "redis" default raised AdapterNotFoundError on first call. Treated as a
# Group B row by _wire_registry_defaults() at init() time.
ProviderRegistry.security_repo.set_default("memory")
ProviderRegistry.audit.set_default("null")  # D11: OSS-safe default at module load
ProviderRegistry.traffic_routing.set_default("logging")
ProviderRegistry.notification.set_default("logging")
ProviderRegistry.alert.set_default("stdout")
ProviderRegistry.cascade_event_repo.set_default("memory")
ProviderRegistry.recovery_session_repo.set_default("memory")
# 570 D6 — explicit module-load baseline for the two registries 464 D12
# deferred. Both previously relied on auto-discover first-registered-wins
# (which already yields "memory"), so this is behavior-preserving at
# module load; it makes the baseline explicit so it matches the wiring
# row's reset_baseline ("memory") — module-load default and reset target
# stay symmetric. init() rewires both: postmortem_repo via its Group B
# SQL_DJANGO row, event_journal_repo via its PRIORITY_CHAIN row.
ProviderRegistry.postmortem_repo.set_default("memory")
ProviderRegistry.event_journal_repo.set_default("memory")
ProviderRegistry.config_history_store.set_default("memory")
ProviderRegistry.canary_rollout_store.set_default("memory")
ProviderRegistry.chaos_experiment_store.set_default("memory")
ProviderRegistry.cross_cluster_store.set_default("memory")
ProviderRegistry.database_health.set_default("noop")
ProviderRegistry.pg_admin.set_default("noop")
ProviderRegistry.pool_info.set_default("noop")
ProviderRegistry.session_invalidation.set_default("noop")
ProviderRegistry.rate_limit_storage.set_default("memory")

# ML Strategy slots stay empty at module load (599 D9) — statistical
# defaults + slot defaults are registered by register_dormant_services().

# Run auto-registration on module import
_auto_register_adapters()

# 516 D2 — OSS NoOp defaults for the OSS->PRO boundary registries. Imported
# AFTER ``_auto_register_adapters()`` so the chained interfaces-package init
# runs in the same order as it did pre-516 (eager imports here would relocate
# ``baldur.interfaces.__init__`` ahead of the discover_* pass and cause
# partial re-entry into in-progress ``baldur.adapters.django`` initialization
# — see the apps_ready_lifecycle regression that surfaced from the original
# pre-discover placement). PRO overrides via ``ProviderRegistry.X.register(
# "pro", ...) + set_default("pro")`` at import time when baldur_pro is
# installed.
from baldur.interfaces.governance import (  # noqa: E402
    NoOpGovernanceChecker,
)
from baldur.interfaces.pool_monitor import (  # noqa: E402
    NoOpPoolStatsProvider,
)

ProviderRegistry.pool_monitor.register("oss-noop", NoOpPoolStatsProvider)
ProviderRegistry.pool_monitor.set_default("oss-noop")
ProviderRegistry.governance.register("oss-noop", NoOpGovernanceChecker)
ProviderRegistry.governance.set_default("oss-noop")
# shutdown_integrations has no NoOp default — empty registry means "no
# extra handlers to register", which is the correct OSS behavior.
# startup_integrations is the same shape (615 D1) — empty registry means "no
# extra starters to run"; the PRO package populates it under ACTIVE entitlement.

# 528 D10-v2 — OSS NoOp defaults for the Dormant boundary slots. Registered
# at the same load-time phase as the 516 boundary NoOps. baldur_dormant.
# register_dormant_services() overwrites these defaults with the concrete
# K8s/Kafka/WORM/DynamoDB adapters when the wheel is installed.
from baldur.audit.export import NoOpS3Exporter  # noqa: E402
from baldur.coordination.noop_elector import NoOpLeaderElector  # noqa: E402
from baldur.interfaces.audit_adapter import (  # noqa: E402
    NoOpKafkaAuditAdapter,
    NoOpWormAdapter,
)
from baldur.interfaces.event_bus import NoOpKafkaEventBus  # noqa: E402
from baldur.meta.recovery_adapter import NoOpRecoveryAdapter  # noqa: E402

ProviderRegistry.leader_elector.register("oss-noop", NoOpLeaderElector)
ProviderRegistry.leader_elector.set_default("oss-noop")
ProviderRegistry.audit_kafka_adapter.register("oss-noop", NoOpKafkaAuditAdapter)
ProviderRegistry.audit_kafka_adapter.set_default("oss-noop")
ProviderRegistry.audit_worm_adapter.register("oss-noop", NoOpWormAdapter)
ProviderRegistry.audit_worm_adapter.set_default("oss-noop")
ProviderRegistry.audit_s3_exporter.register("oss-noop", NoOpS3Exporter)
ProviderRegistry.audit_s3_exporter.set_default("oss-noop")
ProviderRegistry.kafka_eventbus.register("oss-noop", NoOpKafkaEventBus)
ProviderRegistry.kafka_eventbus.set_default("oss-noop")
# quorum_witness has no OSS default — the whole multiregion package
# (including the in-memory witness) relocated to baldur_dormant (599 D5);
# register_dormant_services() registers the "memory" default, and all slot
# consumers are multiregion-internal, so an empty slot on a clean OSS
# install is unreachable.
ProviderRegistry.recovery_adapter.register("oss-noop", NoOpRecoveryAdapter)
ProviderRegistry.recovery_adapter.set_default("oss-noop")


# =============================================================================
# Resilient Storage Convenience Functions
# =============================================================================


# =============================================================================
# init() not-called WARNINGs (463 D12)
# =============================================================================


def _warn_if_init_not_called_cache() -> None:
    """Emit a one-time WARNING when ``get_cache()`` runs before ``init()``.

    Production deploys never trip this — ``baldur.init()`` is invariant
    on the framework-adapter startup paths. Ad-hoc utility / CLI / REPL
    scripts that import baldur for sub-features may legitimately skip
    init(); the warning makes the silent memory fallback visible exactly
    once per process.
    """
    # warn once when get_cache() precedes init().
    import baldur.bootstrap as _bootstrap

    if _bootstrap._init_done or _bootstrap._init_not_called_cache_warned:
        return
    _bootstrap._init_not_called_cache_warned = True
    logger.warning(
        "baldur.init_not_called_get_cache",
        hint=(
            "ProviderRegistry.get_cache() invoked before baldur.init(). "
            "Falling back to in-memory cache. Call baldur.init() at "
            "process startup for environment-aware wiring."
        ),
    )


def _warn_if_init_not_called_storage() -> None:
    """Emit a one-time WARNING when ``get_storage_backend()`` runs before ``init()``.

    Companion to :func:`_warn_if_init_not_called_cache`. Maximum two
    WARNINGs per ad-hoc process (one per registry).
    """
    import baldur.bootstrap as _bootstrap

    if _bootstrap._init_done or _bootstrap._init_not_called_storage_warned:
        return
    _bootstrap._init_not_called_storage_warned = True
    logger.warning(
        "baldur.init_not_called_get_storage_backend",
        hint=(
            "get_storage_backend() invoked before baldur.init(). The "
            "backend will lazy-construct with default Redis URL "
            "(redis://localhost:6379/0). Call baldur.init() at process "
            "startup for environment-aware wiring."
        ),
    )


def get_storage_backend():  # verified-by: test_wal_survives_memory_clear
    """
    Get ResilientStorageBackend instance.

    Provides unified storage with:
    - Redis-First architecture
    - Graceful degradation to Memory + WAL
    - Zero data loss guarantee

    Returns:
        ResilientStorageBackend singleton instance
    """
    _warn_if_init_not_called_storage()
    from baldur.adapters.resilient.backend import (
        get_storage_backend as _get_backend,
    )

    return _get_backend()


def get_circuit_breaker_repo():  # verified-by: test_wal_survives_memory_clear
    """
    Get Redis-based Circuit Breaker Repository.

    Uses ResilientStorageBackend for zero data loss.
    Falls back to memory on Redis failure.

    Returns:
        RedisCircuitBreakerStateRepository instance
    """
    from baldur.adapters.redis.circuit_breaker import (
        get_redis_circuit_breaker_repo,
    )

    return get_redis_circuit_breaker_repo()


def get_dlq_repo():  # verified-by: test_wal_survives_memory_clear
    """
    Get Redis-based DLQ Repository.

    Uses ResilientStorageBackend for zero data loss.
    Falls back to memory on Redis failure.

    Returns:
        RedisDLQRepository instance
    """
    from baldur.adapters.redis.dlq import get_redis_dlq_repo

    return get_redis_dlq_repo()
