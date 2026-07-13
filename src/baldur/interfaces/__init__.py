"""
Baldur Interfaces Module

Abstract interfaces for the pluggable baldur architecture.
These interfaces decouple the baldur core logic from external
dependencies (Django, Redis, Celery, etc.), enabling:
- Framework migration (Django -> FastAPI, Flask)
- Cache backend switching (Redis -> Memcached, DynamoDB)
- Task queue switching (Celery -> RQ, Dramatiq)

Usage:
    from baldur.interfaces import (
        # Repository interfaces
        FailedOperationRepository,
        CircuitBreakerStateRepository,
        SecurityIncidentRepository,
        # Cache provider interface
        CacheProviderInterface,
        DistributedLock,
        # Task queue interface
        TaskQueueInterface,
        TaskResult,
        TaskOptions,
        # Web framework interface
        WebFrameworkInterface,
        RequestContext,
        ResponseContext,
    )

Status: Public
"""

# Lazy barrel — register names in `_LAZY_IMPORTS`; never add an eager
# top-level `from baldur.X import ...` here (defeats the lazy import path
# and is caught by the import-weight gate).

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from baldur.interfaces.admin_identity import (
        AdminIdentityResolver,
        AdminPrincipal,
    )
    from baldur.interfaces.alert_adapter import (
        Alert,
        AlertAdapter,
        AlertCategory,
        AlertSeverity,
    )
    from baldur.interfaces.audit_adapter import (
        AuditAction,
        AuditEntry,
        AuditLogAdapter,
        NoOpKafkaAuditAdapter,
        NoOpWormAdapter,
    )
    from baldur.interfaces.blast_radius import BlastRadiusManager
    from baldur.interfaces.bulkhead import (
        Bulkhead,
        BulkheadRegistry,
    )
    from baldur.interfaces.cache_provider import (
        AsyncCacheProviderInterface,
        CacheProviderInterface,
        DistributedLock,
        LockAcquisitionError,
        LockNotOwnedError,
        generate_lock_owner_id,
    )
    from baldur.interfaces.canary import (
        CanaryRollout,
        CanaryRolloutService,
    )
    from baldur.interfaces.canary_rollout_store import CanaryRolloutStore
    from baldur.interfaces.chaos import (
        ChaosScheduler,
        ReportGenerator,
        SafetyGuard,
    )
    from baldur.interfaces.chaos_experiment_store import ChaosExperimentStore
    from baldur.interfaces.config_history_store import ConfigHistoryStore
    from baldur.interfaces.config_provider import (
        ConfigProviderInterface,
        DictConfigProvider,
        EnvConfigProvider,
    )
    from baldur.interfaces.cross_cluster_store import CrossClusterStore
    from baldur.interfaces.database_health import (
        DatabaseConnectionInfo,
        DatabaseHealthProvider,
    )
    from baldur.interfaces.dlq import (
        DLQRepository,
        DLQService,
    )
    from baldur.interfaces.emergency import EmergencyManager
    from baldur.interfaces.error_budget import (
        ErrorBudgetGate,
        ErrorBudgetService,
    )
    from baldur.interfaces.event_bus import (
        ConsumedEventProtocol,
        EventBusProtocol,
        KafkaEventBusProtocol,
        NoOpKafkaEventBus,
    )
    from baldur.interfaces.event_journal import (
        EventJournalRepository,
        JournalEntry,
        JournalQueryFilter,
        JournalQueryResult,
    )
    from baldur.interfaces.governance import (
        GovernanceChecker,
        NoOpGovernanceChecker,
    )
    from baldur.interfaces.learning import LearningServiceProtocol
    from baldur.interfaces.meta_watchdog import SelfhealerWatchdog
    from baldur.interfaces.ml_strategy import (
        AnomalyDetectionStrategy,
        BatchClassifiable,
        BatchDetectable,
        ClassificationStrategy,
        ForecastStrategy,
        OptimizationStrategy,
        StrategyLifecycle,
    )
    from baldur.interfaces.notification import (
        NotificationAdapter,
        NotificationChannel,
        NotificationSeverity,
        UnifiedNotificationManager,
    )
    from baldur.interfaces.pg_admin import (
        AdvisoryLockResult,
        ConnectionStats,
        PgAdminProvider,
    )
    from baldur.interfaces.pool_info import PoolInfoProvider
    from baldur.interfaces.pool_monitor import (
        ConnectionInfo,
        ConnectionPoolMonitor,
        LeakReport,
        NoOpPoolStatsProvider,
        PoolHealthStatus,
        PoolStats,
        PoolStatsProvider,
    )
    from baldur.interfaces.quorum import (
        QuorumLease,
        QuorumWitnessProtocol,
    )
    from baldur.interfaces.rate_limit_storage import (
        RateLimitState,
        RateLimitStorageError,
        RateLimitStorageInterface,
        RateLimitStorageType,
        RateLimitStorageUnavailableError,
    )
    from baldur.interfaces.repositories import (
        CascadeEventArchiveRepository,
        CircuitBreakerStateData,
        CircuitBreakerStateEnum,
        CircuitBreakerStateRepository,
        DLQCompressedStatus,
        FailedOperationData,
        FailedOperationDomain,
        FailedOperationRepository,
        FailedOperationStatus,
        PostmortemData,
        PostmortemRepository,
        RecoverySessionArchiveRepository,
        SecurityIncidentData,
        SecurityIncidentRepository,
        SecurityIncidentStatus,
        SecurityIncidentType,
        SecuritySeverity,
    )
    from baldur.interfaces.resilience_policy import (
        AsyncFailureSink,
        AsyncPolicyGuard,
        AsyncPolicyHook,
        AsyncResiliencePolicy,
        FailureSink,
        GuardResult,
        PolicyContext,
        PolicyGuard,
        PolicyHook,
        PolicyOutcome,
        PolicyResult,
        ResiliencePolicy,
    )
    from baldur.interfaces.runtime_config import RuntimeConfigManager
    from baldur.interfaces.session_provider import SessionInvalidationProvider
    from baldur.interfaces.statistics import (
        AuditTrailEntry,
        CircuitBreakerInfo,
        CircuitBreakerSummary,
        CleanupStats,
        DomainDistribution,
        EntityAuditTrail,
        FailureTypeDistribution,
        PaginatedResult,
        RecentActivity,
        StatisticsRepositoryInterface,
        StatusCounts,
    )
    from baldur.interfaces.task_queue import (
        AsyncTaskQueueInterface,
        ScheduleInfo,
        TaskNotFoundError,
        TaskOptions,
        TaskPriority,
        TaskQueueError,
        TaskQueueInterface,
        TaskResult,
        TaskRevokedError,
        TaskStatus,
        TaskTimeoutError,
    )
    from baldur.interfaces.throttle import AdaptiveThrottle
    from baldur.interfaces.traffic_routing import (
        RoutingChange,
        TrafficRoutingAdapter,
    )
    from baldur.interfaces.web_framework import (
        AuthenticationError,
        ContentType,
        HandlerFunc,
        HttpMethod,
        PermissionDeniedError,
        PermissionLevel,
        RequestContext,
        ResponseContext,
        RouteNotFoundError,
        WebFrameworkError,
        WebFrameworkInterface,
    )

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "AdminIdentityResolver": (
        "baldur.interfaces.admin_identity",
        "AdminIdentityResolver",
    ),
    "AdminPrincipal": ("baldur.interfaces.admin_identity", "AdminPrincipal"),
    "Alert": ("baldur.interfaces.alert_adapter", "Alert"),
    "AlertAdapter": ("baldur.interfaces.alert_adapter", "AlertAdapter"),
    "AlertCategory": ("baldur.interfaces.alert_adapter", "AlertCategory"),
    "AlertSeverity": ("baldur.interfaces.alert_adapter", "AlertSeverity"),
    "AuditAction": ("baldur.interfaces.audit_adapter", "AuditAction"),
    "AuditEntry": ("baldur.interfaces.audit_adapter", "AuditEntry"),
    "AuditLogAdapter": ("baldur.interfaces.audit_adapter", "AuditLogAdapter"),
    "NoOpKafkaAuditAdapter": (
        "baldur.interfaces.audit_adapter",
        "NoOpKafkaAuditAdapter",
    ),
    "NoOpWormAdapter": ("baldur.interfaces.audit_adapter", "NoOpWormAdapter"),
    "BlastRadiusManager": ("baldur.interfaces.blast_radius", "BlastRadiusManager"),
    "Bulkhead": ("baldur.interfaces.bulkhead", "Bulkhead"),
    "BulkheadRegistry": ("baldur.interfaces.bulkhead", "BulkheadRegistry"),
    "AsyncCacheProviderInterface": (
        "baldur.interfaces.cache_provider",
        "AsyncCacheProviderInterface",
    ),
    "CacheProviderInterface": (
        "baldur.interfaces.cache_provider",
        "CacheProviderInterface",
    ),
    "DistributedLock": ("baldur.interfaces.cache_provider", "DistributedLock"),
    "LockAcquisitionError": (
        "baldur.interfaces.cache_provider",
        "LockAcquisitionError",
    ),
    "LockNotOwnedError": ("baldur.interfaces.cache_provider", "LockNotOwnedError"),
    "generate_lock_owner_id": (
        "baldur.interfaces.cache_provider",
        "generate_lock_owner_id",
    ),
    "CanaryRollout": ("baldur.interfaces.canary", "CanaryRollout"),
    "CanaryRolloutService": ("baldur.interfaces.canary", "CanaryRolloutService"),
    "CanaryRolloutStore": (
        "baldur.interfaces.canary_rollout_store",
        "CanaryRolloutStore",
    ),
    "ChaosScheduler": ("baldur.interfaces.chaos", "ChaosScheduler"),
    "ReportGenerator": ("baldur.interfaces.chaos", "ReportGenerator"),
    "SafetyGuard": ("baldur.interfaces.chaos", "SafetyGuard"),
    "ChaosExperimentStore": (
        "baldur.interfaces.chaos_experiment_store",
        "ChaosExperimentStore",
    ),
    "ConfigHistoryStore": (
        "baldur.interfaces.config_history_store",
        "ConfigHistoryStore",
    ),
    "ConfigProviderInterface": (
        "baldur.interfaces.config_provider",
        "ConfigProviderInterface",
    ),
    "DictConfigProvider": ("baldur.interfaces.config_provider", "DictConfigProvider"),
    "EnvConfigProvider": ("baldur.interfaces.config_provider", "EnvConfigProvider"),
    "CrossClusterStore": ("baldur.interfaces.cross_cluster_store", "CrossClusterStore"),
    "DatabaseConnectionInfo": (
        "baldur.interfaces.database_health",
        "DatabaseConnectionInfo",
    ),
    "DatabaseHealthProvider": (
        "baldur.interfaces.database_health",
        "DatabaseHealthProvider",
    ),
    "DLQRepository": ("baldur.interfaces.dlq", "DLQRepository"),
    "DLQService": ("baldur.interfaces.dlq", "DLQService"),
    "EmergencyManager": ("baldur.interfaces.emergency", "EmergencyManager"),
    "ErrorBudgetGate": ("baldur.interfaces.error_budget", "ErrorBudgetGate"),
    "ErrorBudgetService": ("baldur.interfaces.error_budget", "ErrorBudgetService"),
    "ConsumedEventProtocol": ("baldur.interfaces.event_bus", "ConsumedEventProtocol"),
    "EventBusProtocol": ("baldur.interfaces.event_bus", "EventBusProtocol"),
    "KafkaEventBusProtocol": ("baldur.interfaces.event_bus", "KafkaEventBusProtocol"),
    "NoOpKafkaEventBus": ("baldur.interfaces.event_bus", "NoOpKafkaEventBus"),
    "EventJournalRepository": (
        "baldur.interfaces.event_journal",
        "EventJournalRepository",
    ),
    "JournalEntry": ("baldur.interfaces.event_journal", "JournalEntry"),
    "JournalQueryFilter": ("baldur.interfaces.event_journal", "JournalQueryFilter"),
    "JournalQueryResult": ("baldur.interfaces.event_journal", "JournalQueryResult"),
    "GovernanceChecker": ("baldur.interfaces.governance", "GovernanceChecker"),
    "NoOpGovernanceChecker": ("baldur.interfaces.governance", "NoOpGovernanceChecker"),
    "LearningServiceProtocol": (
        "baldur.interfaces.learning",
        "LearningServiceProtocol",
    ),
    "SelfhealerWatchdog": ("baldur.interfaces.meta_watchdog", "SelfhealerWatchdog"),
    "AnomalyDetectionStrategy": (
        "baldur.interfaces.ml_strategy",
        "AnomalyDetectionStrategy",
    ),
    "BatchClassifiable": ("baldur.interfaces.ml_strategy", "BatchClassifiable"),
    "BatchDetectable": ("baldur.interfaces.ml_strategy", "BatchDetectable"),
    "ClassificationStrategy": (
        "baldur.interfaces.ml_strategy",
        "ClassificationStrategy",
    ),
    "ForecastStrategy": ("baldur.interfaces.ml_strategy", "ForecastStrategy"),
    "OptimizationStrategy": ("baldur.interfaces.ml_strategy", "OptimizationStrategy"),
    "StrategyLifecycle": ("baldur.interfaces.ml_strategy", "StrategyLifecycle"),
    "NotificationAdapter": ("baldur.interfaces.notification", "NotificationAdapter"),
    "NotificationChannel": ("baldur.interfaces.notification", "NotificationChannel"),
    "NotificationSeverity": ("baldur.interfaces.notification", "NotificationSeverity"),
    "UnifiedNotificationManager": (
        "baldur.interfaces.notification",
        "UnifiedNotificationManager",
    ),
    "AdvisoryLockResult": ("baldur.interfaces.pg_admin", "AdvisoryLockResult"),
    "ConnectionStats": ("baldur.interfaces.pg_admin", "ConnectionStats"),
    "PgAdminProvider": ("baldur.interfaces.pg_admin", "PgAdminProvider"),
    "PoolInfoProvider": ("baldur.interfaces.pool_info", "PoolInfoProvider"),
    "ConnectionInfo": ("baldur.interfaces.pool_monitor", "ConnectionInfo"),
    "ConnectionPoolMonitor": (
        "baldur.interfaces.pool_monitor",
        "ConnectionPoolMonitor",
    ),
    "LeakReport": ("baldur.interfaces.pool_monitor", "LeakReport"),
    "NoOpPoolStatsProvider": (
        "baldur.interfaces.pool_monitor",
        "NoOpPoolStatsProvider",
    ),
    "PoolHealthStatus": ("baldur.interfaces.pool_monitor", "PoolHealthStatus"),
    "PoolStats": ("baldur.interfaces.pool_monitor", "PoolStats"),
    "PoolStatsProvider": ("baldur.interfaces.pool_monitor", "PoolStatsProvider"),
    "QuorumLease": ("baldur.interfaces.quorum", "QuorumLease"),
    "QuorumWitnessProtocol": ("baldur.interfaces.quorum", "QuorumWitnessProtocol"),
    "RateLimitState": ("baldur.interfaces.rate_limit_storage", "RateLimitState"),
    "RateLimitStorageError": (
        "baldur.interfaces.rate_limit_storage",
        "RateLimitStorageError",
    ),
    "RateLimitStorageInterface": (
        "baldur.interfaces.rate_limit_storage",
        "RateLimitStorageInterface",
    ),
    "RateLimitStorageType": (
        "baldur.interfaces.rate_limit_storage",
        "RateLimitStorageType",
    ),
    "RateLimitStorageUnavailableError": (
        "baldur.interfaces.rate_limit_storage",
        "RateLimitStorageUnavailableError",
    ),
    "CascadeEventArchiveRepository": (
        "baldur.interfaces.repositories",
        "CascadeEventArchiveRepository",
    ),
    "CircuitBreakerStateData": (
        "baldur.interfaces.repositories",
        "CircuitBreakerStateData",
    ),
    "CircuitBreakerStateEnum": (
        "baldur.interfaces.repositories",
        "CircuitBreakerStateEnum",
    ),
    "CircuitBreakerStateRepository": (
        "baldur.interfaces.repositories",
        "CircuitBreakerStateRepository",
    ),
    "DLQCompressedStatus": ("baldur.interfaces.repositories", "DLQCompressedStatus"),
    "FailedOperationData": ("baldur.interfaces.repositories", "FailedOperationData"),
    "FailedOperationDomain": (
        "baldur.interfaces.repositories",
        "FailedOperationDomain",
    ),
    "FailedOperationRepository": (
        "baldur.interfaces.repositories",
        "FailedOperationRepository",
    ),
    "FailedOperationStatus": (
        "baldur.interfaces.repositories",
        "FailedOperationStatus",
    ),
    "PostmortemData": ("baldur.interfaces.repositories", "PostmortemData"),
    "PostmortemRepository": ("baldur.interfaces.repositories", "PostmortemRepository"),
    "RecoverySessionArchiveRepository": (
        "baldur.interfaces.repositories",
        "RecoverySessionArchiveRepository",
    ),
    "SecurityIncidentData": ("baldur.interfaces.repositories", "SecurityIncidentData"),
    "SecurityIncidentRepository": (
        "baldur.interfaces.repositories",
        "SecurityIncidentRepository",
    ),
    "SecurityIncidentStatus": (
        "baldur.interfaces.repositories",
        "SecurityIncidentStatus",
    ),
    "SecurityIncidentType": ("baldur.interfaces.repositories", "SecurityIncidentType"),
    "SecuritySeverity": ("baldur.interfaces.repositories", "SecuritySeverity"),
    "AsyncFailureSink": ("baldur.interfaces.resilience_policy", "AsyncFailureSink"),
    "AsyncPolicyGuard": ("baldur.interfaces.resilience_policy", "AsyncPolicyGuard"),
    "AsyncPolicyHook": ("baldur.interfaces.resilience_policy", "AsyncPolicyHook"),
    "AsyncResiliencePolicy": (
        "baldur.interfaces.resilience_policy",
        "AsyncResiliencePolicy",
    ),
    "FailureSink": ("baldur.interfaces.resilience_policy", "FailureSink"),
    "GuardResult": ("baldur.interfaces.resilience_policy", "GuardResult"),
    "PolicyContext": ("baldur.interfaces.resilience_policy", "PolicyContext"),
    "PolicyGuard": ("baldur.interfaces.resilience_policy", "PolicyGuard"),
    "PolicyHook": ("baldur.interfaces.resilience_policy", "PolicyHook"),
    "PolicyOutcome": ("baldur.interfaces.resilience_policy", "PolicyOutcome"),
    "PolicyResult": ("baldur.interfaces.resilience_policy", "PolicyResult"),
    "ResiliencePolicy": ("baldur.interfaces.resilience_policy", "ResiliencePolicy"),
    "RuntimeConfigManager": (
        "baldur.interfaces.runtime_config",
        "RuntimeConfigManager",
    ),
    "SessionInvalidationProvider": (
        "baldur.interfaces.session_provider",
        "SessionInvalidationProvider",
    ),
    "AuditTrailEntry": ("baldur.interfaces.statistics", "AuditTrailEntry"),
    "CircuitBreakerInfo": ("baldur.interfaces.statistics", "CircuitBreakerInfo"),
    "CircuitBreakerSummary": ("baldur.interfaces.statistics", "CircuitBreakerSummary"),
    "CleanupStats": ("baldur.interfaces.statistics", "CleanupStats"),
    "DomainDistribution": ("baldur.interfaces.statistics", "DomainDistribution"),
    "EntityAuditTrail": ("baldur.interfaces.statistics", "EntityAuditTrail"),
    "FailureTypeDistribution": (
        "baldur.interfaces.statistics",
        "FailureTypeDistribution",
    ),
    "PaginatedResult": ("baldur.interfaces.statistics", "PaginatedResult"),
    "RecentActivity": ("baldur.interfaces.statistics", "RecentActivity"),
    "StatisticsRepositoryInterface": (
        "baldur.interfaces.statistics",
        "StatisticsRepositoryInterface",
    ),
    "StatusCounts": ("baldur.interfaces.statistics", "StatusCounts"),
    "AsyncTaskQueueInterface": (
        "baldur.interfaces.task_queue",
        "AsyncTaskQueueInterface",
    ),
    "ScheduleInfo": ("baldur.interfaces.task_queue", "ScheduleInfo"),
    "TaskNotFoundError": ("baldur.interfaces.task_queue", "TaskNotFoundError"),
    "TaskOptions": ("baldur.interfaces.task_queue", "TaskOptions"),
    "TaskPriority": ("baldur.interfaces.task_queue", "TaskPriority"),
    "TaskQueueError": ("baldur.interfaces.task_queue", "TaskQueueError"),
    "TaskQueueInterface": ("baldur.interfaces.task_queue", "TaskQueueInterface"),
    "TaskResult": ("baldur.interfaces.task_queue", "TaskResult"),
    "TaskRevokedError": ("baldur.interfaces.task_queue", "TaskRevokedError"),
    "TaskStatus": ("baldur.interfaces.task_queue", "TaskStatus"),
    "TaskTimeoutError": ("baldur.interfaces.task_queue", "TaskTimeoutError"),
    "AdaptiveThrottle": ("baldur.interfaces.throttle", "AdaptiveThrottle"),
    "RoutingChange": ("baldur.interfaces.traffic_routing", "RoutingChange"),
    "TrafficRoutingAdapter": (
        "baldur.interfaces.traffic_routing",
        "TrafficRoutingAdapter",
    ),
    "AuthenticationError": ("baldur.interfaces.web_framework", "AuthenticationError"),
    "ContentType": ("baldur.interfaces.web_framework", "ContentType"),
    "HandlerFunc": ("baldur.interfaces.web_framework", "HandlerFunc"),
    "HttpMethod": ("baldur.interfaces.web_framework", "HttpMethod"),
    "PermissionDeniedError": (
        "baldur.interfaces.web_framework",
        "PermissionDeniedError",
    ),
    "PermissionLevel": ("baldur.interfaces.web_framework", "PermissionLevel"),
    "RequestContext": ("baldur.interfaces.web_framework", "RequestContext"),
    "ResponseContext": ("baldur.interfaces.web_framework", "ResponseContext"),
    "RouteNotFoundError": ("baldur.interfaces.web_framework", "RouteNotFoundError"),
    "WebFrameworkError": ("baldur.interfaces.web_framework", "WebFrameworkError"),
    "WebFrameworkInterface": (
        "baldur.interfaces.web_framework",
        "WebFrameworkInterface",
    ),
}


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        # Resolve live on each access (no globals() memoization) so the barrel
        # transparently reflects the current submodule attribute — a test that
        # patches `<this package>.<submodule>.<name>` must not be shadowed by a
        # value cached from an earlier patch. importlib already caches the module
        # import, so the cost is a dict lookup.
        return getattr(importlib.import_module(module_path), attr_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return list(__all__)


__all__ = [
    # =========================================================================
    # Repository Interfaces
    # =========================================================================
    # Enums
    "DLQCompressedStatus",
    "FailedOperationDomain",
    "FailedOperationStatus",
    "CircuitBreakerStateEnum",
    "SecurityIncidentType",
    "SecuritySeverity",
    "SecurityIncidentStatus",
    # Data Classes
    "FailedOperationData",
    "CircuitBreakerStateData",
    "SecurityIncidentData",
    "PostmortemData",
    # Interfaces
    "FailedOperationRepository",
    "CircuitBreakerStateRepository",
    "SecurityIncidentRepository",
    "PostmortemRepository",
    "CascadeEventArchiveRepository",
    "RecoverySessionArchiveRepository",
    # =========================================================================
    # Database Health Provider Interface (368)
    # =========================================================================
    "DatabaseConnectionInfo",
    "DatabaseHealthProvider",
    # =========================================================================
    # Session Invalidation Provider Interface (368)
    # =========================================================================
    "SessionInvalidationProvider",
    # =========================================================================
    # Cache Provider Interface
    # =========================================================================
    # Lock
    "DistributedLock",
    # Exceptions
    "LockAcquisitionError",
    "LockNotOwnedError",
    # Interface
    "CacheProviderInterface",
    "AsyncCacheProviderInterface",
    # Utility
    "generate_lock_owner_id",
    # =========================================================================
    # Canary Rollout Store Interface (Domain State Store)
    # =========================================================================
    "CanaryRolloutStore",
    # =========================================================================
    # Chaos Experiment Store Interface (Domain State Store)
    # =========================================================================
    "ChaosExperimentStore",
    # =========================================================================
    # Configuration History Store Interface (Domain State Store)
    # =========================================================================
    "ConfigHistoryStore",
    # =========================================================================
    # Cross-Cluster Store Interface (Domain State Store)
    # =========================================================================
    "CrossClusterStore",
    # =========================================================================
    # Task Queue Interface
    # =========================================================================
    # Enums
    "TaskStatus",
    "TaskPriority",
    # DTOs
    "TaskResult",
    "TaskOptions",
    "ScheduleInfo",
    # Exceptions
    "TaskQueueError",
    "TaskNotFoundError",
    "TaskTimeoutError",
    "TaskRevokedError",
    # Interfaces
    "TaskQueueInterface",
    "AsyncTaskQueueInterface",
    # =========================================================================
    # Web Framework Interface
    # =========================================================================
    # Enums
    "HttpMethod",
    "ContentType",
    "PermissionLevel",
    # DTOs
    "RequestContext",
    "ResponseContext",
    # Exceptions
    "WebFrameworkError",
    "RouteNotFoundError",
    "AuthenticationError",
    "PermissionDeniedError",
    # Interface
    "WebFrameworkInterface",
    # Type alias
    "HandlerFunc",
    # =========================================================================
    # Configuration Provider Interface
    # =========================================================================
    # Interface
    "ConfigProviderInterface",
    # Default implementations
    "DictConfigProvider",
    "EnvConfigProvider",
    # =========================================================================
    # Rate Limit Storage Interface (Distributed Self-DDoS Prevention)
    # =========================================================================
    # Enums
    "RateLimitStorageType",
    # Data Classes
    "RateLimitState",
    # Interface
    "RateLimitStorageInterface",
    # Exceptions
    "RateLimitStorageError",
    "RateLimitStorageUnavailableError",
    # =========================================================================
    # Audit Log Adapter Interface (Non-invasive audit logging)
    # =========================================================================
    # Enums
    "AuditAction",
    # Data Classes
    "AuditEntry",
    # Interface
    "AuditLogAdapter",
    # NoOp defaults (528 Dormant boundary)
    "NoOpKafkaAuditAdapter",
    "NoOpWormAdapter",
    # =========================================================================
    # Alert Adapter Interface (Non-invasive alerting)
    # =========================================================================
    # Enums
    "AlertSeverity",
    "AlertCategory",
    # Data Classes
    "Alert",
    # Interface
    "AlertAdapter",
    # =========================================================================
    # Traffic Routing Adapter Interface (Multi-Region Failover)
    # =========================================================================
    # Data Classes
    "RoutingChange",
    # Interface
    "TrafficRoutingAdapter",
    # =========================================================================
    # Statistics Repository Interface (Hybrid Storage - v2.3.0)
    # =========================================================================
    # Data Classes
    "StatusCounts",
    "DomainDistribution",
    "FailureTypeDistribution",
    "RecentActivity",
    "CleanupStats",
    "PaginatedResult",
    "CircuitBreakerSummary",
    "CircuitBreakerInfo",
    # Audit Trail DTOs (The Master Trail - v2.4.0)
    "AuditTrailEntry",
    "EntityAuditTrail",
    # Interface
    "StatisticsRepositoryInterface",
    # =========================================================================
    # Resilience Policy Interfaces (Policy Composition)
    # =========================================================================
    # Enums
    "PolicyOutcome",
    # DTOs
    "PolicyResult",
    "PolicyContext",
    "GuardResult",
    # Protocols
    "ResiliencePolicy",
    "AsyncResiliencePolicy",
    "PolicyGuard",
    "PolicyHook",
    "FailureSink",
    "AsyncPolicyGuard",
    "AsyncPolicyHook",
    "AsyncFailureSink",
    # =========================================================================
    # ML Strategy Interfaces (AI/ML extensibility foundation)
    # =========================================================================
    # Protocols
    "AnomalyDetectionStrategy",
    "ForecastStrategy",
    "ClassificationStrategy",
    "BatchDetectable",
    "BatchClassifiable",
    "OptimizationStrategy",
    "StrategyLifecycle",
    # =========================================================================
    # Event Journal Interface
    # =========================================================================
    "EventJournalRepository",
    "JournalEntry",
    "JournalQueryFilter",
    "JournalQueryResult",
    # =========================================================================
    # Notification Interface
    # =========================================================================
    "NotificationAdapter",
    "NotificationChannel",
    "NotificationSeverity",
    # =========================================================================
    # Quorum Witness Protocol (Multi-Region Split-brain Prevention)
    # =========================================================================
    "QuorumLease",
    "QuorumWitnessProtocol",
    # =========================================================================
    # PostgreSQL Admin Provider Interface (515)
    # =========================================================================
    "PgAdminProvider",
    "ConnectionStats",
    "AdvisoryLockResult",
    # =========================================================================
    # Pool Info Provider Interface (515)
    # =========================================================================
    "PoolInfoProvider",
    # =========================================================================
    # Event Bus Protocol (Unified EventBus Contract)
    # =========================================================================
    "EventBusProtocol",
    # Kafka Protocols (528 Dormant boundary — implementations in baldur_dormant)
    "ConsumedEventProtocol",
    "KafkaEventBusProtocol",
    "NoOpKafkaEventBus",
    # =========================================================================
    # Governance Checker (516 OSS->PRO boundary)
    # =========================================================================
    "GovernanceChecker",
    "NoOpGovernanceChecker",
    # =========================================================================
    # Learning Service (599 D11 OSS->Dormant boundary)
    # =========================================================================
    "LearningServiceProtocol",
    # =========================================================================
    # Admin Identity Resolver (537 OSS->PRO boundary)
    # =========================================================================
    "AdminIdentityResolver",
    "AdminPrincipal",
    # =========================================================================
    # Pool Monitor (516 OSS->PRO boundary)
    # =========================================================================
    "ConnectionInfo",
    "LeakReport",
    "NoOpPoolStatsProvider",
    "PoolHealthStatus",
    "PoolStats",
    "PoolStatsProvider",
    # =========================================================================
    # 519 PR 2 OSS->PRO singleton Protocols
    # =========================================================================
    "AdaptiveThrottle",
    "BlastRadiusManager",
    "BulkheadRegistry",
    "CanaryRolloutService",
    "ChaosScheduler",
    "DLQRepository",
    "DLQService",
    "EmergencyManager",
    "ErrorBudgetGate",
    "ErrorBudgetService",
    "ReportGenerator",
    "RuntimeConfigManager",
    "SafetyGuard",
    "SelfhealerWatchdog",
    # =========================================================================
    # 519 PR 3 OSS->PRO Protocol markers (TYPE_CHECKING-only consumers)
    # =========================================================================
    "Bulkhead",
    "CanaryRollout",
    "ConnectionPoolMonitor",
    "UnifiedNotificationManager",
]
