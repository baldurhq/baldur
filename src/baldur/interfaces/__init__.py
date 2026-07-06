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

# =============================================================================
# Admin Identity Resolver (537 OSS->PRO boundary)
# =============================================================================
from baldur.interfaces.admin_identity import (
    AdminIdentityResolver,
    AdminPrincipal,
)

# =============================================================================
# Alert Adapter Interface (Non-invasive alerting)
# =============================================================================
from baldur.interfaces.alert_adapter import (  # Enums; Data Classes; Interface
    Alert,
    AlertAdapter,
    AlertCategory,
    AlertSeverity,
)

# =============================================================================
# Audit Log Adapter Interface (Non-invasive audit logging)
# =============================================================================
from baldur.interfaces.audit_adapter import (  # Enums; Data Classes; Interface; NoOp defaults (528 Dormant boundary)
    AuditAction,
    AuditEntry,
    AuditLogAdapter,
    NoOpKafkaAuditAdapter,
    NoOpWormAdapter,
)

# =============================================================================
# Blast Radius Manager (519 PR 2 OSS->PRO boundary)
# =============================================================================
from baldur.interfaces.blast_radius import BlastRadiusManager

# =============================================================================
# Bulkhead (519 PR 2 / PR 3 OSS->PRO boundary)
# =============================================================================
from baldur.interfaces.bulkhead import Bulkhead, BulkheadRegistry

# =============================================================================
# Cache Provider Interface
# =============================================================================
from baldur.interfaces.cache_provider import (  # Lock interface; Exceptions; Interface; Utility
    AsyncCacheProviderInterface,
    CacheProviderInterface,
    DistributedLock,
    LockAcquisitionError,
    LockNotOwnedError,
    generate_lock_owner_id,
)

# =============================================================================
# Canary (519 PR 2 / PR 3 OSS->PRO boundary)
# =============================================================================
from baldur.interfaces.canary import CanaryRollout, CanaryRolloutService

# =============================================================================
# Canary Rollout Store Interface (Domain State Store)
# =============================================================================
from baldur.interfaces.canary_rollout_store import CanaryRolloutStore

# =============================================================================
# Chaos Singletons (519 PR 2 OSS->PRO boundary)
# =============================================================================
from baldur.interfaces.chaos import (
    ChaosScheduler,
    ReportGenerator,
    SafetyGuard,
)

# =============================================================================
# Chaos Experiment Store Interface (Domain State Store)
# =============================================================================
from baldur.interfaces.chaos_experiment_store import ChaosExperimentStore

# =============================================================================
# Configuration History Store Interface (Domain State Store)
# =============================================================================
from baldur.interfaces.config_history_store import ConfigHistoryStore

# =============================================================================
# Configuration Provider Interface
# =============================================================================
from baldur.interfaces.config_provider import (  # Interface; Default implementations
    ConfigProviderInterface,
    DictConfigProvider,
    EnvConfigProvider,
)

# =============================================================================
# Cross-Cluster Store Interface (Domain State Store)
# =============================================================================
from baldur.interfaces.cross_cluster_store import CrossClusterStore

# =============================================================================
# Database Health Provider Interface (368: Django Decoupling)
# =============================================================================
from baldur.interfaces.database_health import (
    DatabaseConnectionInfo,
    DatabaseHealthProvider,
)

# =============================================================================
# DLQ Singletons (519 PR 2 OSS->PRO boundary)
# =============================================================================
from baldur.interfaces.dlq import DLQRepository, DLQService

# =============================================================================
# Emergency Manager (519 PR 2 OSS->PRO boundary)
# =============================================================================
from baldur.interfaces.emergency import EmergencyManager

# =============================================================================
# Error Budget Singletons (519 PR 2 OSS->PRO boundary)
# =============================================================================
from baldur.interfaces.error_budget import ErrorBudgetGate, ErrorBudgetService

# =============================================================================
# Event Bus Protocol (Unified EventBus Contract)
# =============================================================================
from baldur.interfaces.event_bus import (
    ConsumedEventProtocol,
    EventBusProtocol,
    KafkaEventBusProtocol,
    NoOpKafkaEventBus,
)

# =============================================================================
# Event Journal Interface
# =============================================================================
from baldur.interfaces.event_journal import (
    EventJournalRepository,
    JournalEntry,
    JournalQueryFilter,
    JournalQueryResult,
)

# =============================================================================
# Governance Checker (516 OSS->PRO boundary)
# =============================================================================
from baldur.interfaces.governance import (
    GovernanceChecker,
    NoOpGovernanceChecker,
)

# =============================================================================
# Learning Service (599 D11 OSS->Dormant boundary)
# =============================================================================
from baldur.interfaces.learning import LearningServiceProtocol

# =============================================================================
# Selfhealer Watchdog (519 PR 2 OSS->PRO boundary)
# =============================================================================
from baldur.interfaces.meta_watchdog import SelfhealerWatchdog

# =============================================================================
# ML Strategy Interfaces (AI/ML extensibility foundation)
# =============================================================================
from baldur.interfaces.ml_strategy import (  # Protocols
    AnomalyDetectionStrategy,
    BatchClassifiable,
    BatchDetectable,
    ClassificationStrategy,
    ForecastStrategy,
    OptimizationStrategy,
    StrategyLifecycle,
)

# =============================================================================
# Notification Interface
# =============================================================================
from baldur.interfaces.notification import (
    NotificationAdapter,
    NotificationChannel,
    NotificationSeverity,
    UnifiedNotificationManager,
)

# =============================================================================
# PostgreSQL Admin Provider Interface (515)
# =============================================================================
from baldur.interfaces.pg_admin import (
    AdvisoryLockResult,
    ConnectionStats,
    PgAdminProvider,
)

# =============================================================================
# Pool Info Provider Interface (515)
# =============================================================================
from baldur.interfaces.pool_info import PoolInfoProvider

# =============================================================================
# Pool Monitor (516 OSS->PRO boundary; ConnectionPoolMonitor added 519 PR 3)
# =============================================================================
from baldur.interfaces.pool_monitor import (
    ConnectionInfo,
    ConnectionPoolMonitor,
    LeakReport,
    NoOpPoolStatsProvider,
    PoolHealthStatus,
    PoolStats,
    PoolStatsProvider,
)

# =============================================================================
# Quorum Witness Protocol (Multi-Region Split-brain Prevention)
# =============================================================================
from baldur.interfaces.quorum import QuorumLease, QuorumWitnessProtocol

# =============================================================================
# Rate Limit Storage Interface (Distributed Self-DDoS Prevention)
# =============================================================================
from baldur.interfaces.rate_limit_storage import (  # Enums; Data Classes; Interface; Exceptions
    RateLimitState,
    RateLimitStorageError,
    RateLimitStorageInterface,
    RateLimitStorageType,
    RateLimitStorageUnavailableError,
)

# =============================================================================
# Repository Interfaces
# =============================================================================
from baldur.interfaces.repositories import (  # Enums; Data Classes; Repository Interfaces
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

# =============================================================================
# Resilience Policy Interfaces (Policy Composition)
# =============================================================================
from baldur.interfaces.resilience_policy import (  # Enums; DTOs; Protocols
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

# =============================================================================
# Runtime Config Manager (519 PR 2 OSS->PRO boundary)
# =============================================================================
from baldur.interfaces.runtime_config import RuntimeConfigManager

# =============================================================================
# Session Invalidation Provider Interface (368: Django Decoupling)
# =============================================================================
from baldur.interfaces.session_provider import SessionInvalidationProvider

# =============================================================================
# Statistics Repository Interface (Hybrid Storage - v2.3.0)
# =============================================================================
from baldur.interfaces.statistics import (  # Data Classes; Audit Trail DTOs (The Master Trail - v2.4.0); Interface
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

# =============================================================================
# Task Queue Interface
# =============================================================================
from baldur.interfaces.task_queue import (  # Enums; DTOs; Exceptions; Interfaces
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

# =============================================================================
# Adaptive Throttle (519 PR 2 OSS->PRO boundary)
# =============================================================================
from baldur.interfaces.throttle import AdaptiveThrottle

# =============================================================================
# Traffic Routing Adapter Interface (Multi-Region Failover)
# =============================================================================
from baldur.interfaces.traffic_routing import (  # Data Classes; Interface
    RoutingChange,
    TrafficRoutingAdapter,
)

# =============================================================================
# Web Framework Interface
# =============================================================================
from baldur.interfaces.web_framework import (  # Enums; DTOs; Exceptions; Interface; Type alias
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
