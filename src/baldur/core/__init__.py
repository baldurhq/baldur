"""
Core module - Framework-agnostic business logic

This module contains pure Python implementations without any framework dependencies.

Backoff API:
    - ExponentialBackoff, LinearBackoff, etc.: Strategy pattern implementations
      Usage: strategy = ExponentialBackoff(base=2); strategy.calculate(attempt)

Status: Internal
"""

from baldur.core.action_executor import (
    Action,
    ActionExecutor,
    ActionResult,
    execute_action,
    get_action_executor,
)
from baldur.core.adaptive_jitter import AdaptiveJitter
from baldur.core.backoff import (
    BackoffStrategy,
    ConstantBackoff,
    DecorrelatedJitterBackoff,
    ExponentialBackoff,
    LinearBackoff,
    get_backoff_calculator,
)
from baldur.core.cert_monitor import (
    CertificateAlertManager,
    CertificateExpiryMonitor,
    CertificateInfo,
    CertificateStatus,
)
from baldur.core.connection_health import (
    ConnectionHealth,
    ConnectionHealthMonitor,
    ConnectionStatus,
    ConnectionType,
    DefaultConnectionHealthMonitor,
    PartitionState,
)
from baldur.core.constraint_engine import (
    ConstraintEngine,
    ConstraintResult,
    ConstraintViolation,
    get_constraint_engine,
    reset_constraint_engine,
)
from baldur.core.decision_logger import (
    DecisionBoundaryEventType,
    DecisionLogger,
    ReasonCode,
    log_enter_pre_decision_zone,
    log_exit_pre_decision_zone,
    log_intervention_evaluated,
)
from baldur.core.degraded_mode_handler import DegradedModeHandler
from baldur.core.degraded_mode_protocol import DegradedModeProtocol
from baldur.core.entitlement import (
    EntitlementClaims,
    EntitlementError,
    EntitlementResult,
    EntitlementStatus,
    get_entitlement_status,
    reset_entitlement_status,
)
from baldur.core.exceptions import (
    CompensationError,
    ConcurrencyConflictError,
    StepExecutionError,
    StepTimeoutError,
)
from baldur.core.execution_mode import (
    ExecutionMode,
    ExecutionModeType,
    clear_execution_mode_override,
    get_execution_mode,
    set_execution_mode,
)
from baldur.core.execution_protocol import ExecutionOutcome
from baldur.core.fallback_strategy import (
    CacheFirstFallback,
    FallbackMode,
    FallbackResult,
    FallbackStrategy,
    PartitionAwareFallback,
    SimpleFallback,
)
from baldur.core.idempotency_gate import (
    IdempotencyCheckResult,
    IdempotencyDecision,
    IdempotencyGate,
    get_idempotency_gate,
    reset_idempotency_gate,
)
from baldur.core.pool_watchdog import (
    PoolRecoveryAction,
    PoolRecoveryHandler,
    PoolRecoveryResult,
    PoolWatchdog,
)
from baldur.core.request_context import (
    RequestLifecycleContext,
    track_request,
)
from baldur.core.serializable import SerializableMixin
from baldur.core.settings_dependency import (
    CycleDetectedError,
    DependencyType,
    SettingsDependency,
    SettingsDependencyGraph,
    SettingsInvariant,
    get_dependency_graph,
    reset_dependency_graph,
)
from baldur.core.shutdown_coordinator import (
    GracefulShutdownCoordinator,
    RequestState,
    RequestTracker,
    ShutdownHandler,
    ShutdownPhase,
    ShutdownStats,
    TrackedRequest,
)
from baldur.core.singleflight import Singleflight
from baldur.core.state_cache import CBStateCache
from baldur.core.step_execution_engine import (
    CompensationFailure,
    CompensationResult,
    FailureAction,
    ForwardResult,
    LockConfig,
    SkipDecision,
    StepExecutionEngine,
)
from baldur.core.test_mode_context import (
    TestModeContext,
    get_synthetic_session_id,
    is_synthetic_context,
    synthetic_context,
)
from baldur.core.time_provider import (
    FrozenTime,
    MockTimeProvider,
    SystemTimeProvider,
    TimeProvider,
    get_time_provider,
    is_within_clock_skew,
    reset_time_provider,
)
from baldur.core.time_provider import set_time_provider as set_global_time_provider
from baldur.core.time_series import (
    EWMAForecaster,
    ForecastDataPoint,
    HoltLinearForecaster,
    HoltWintersForecaster,
)
from baldur.core.timeout_executor import LockExtendable, TimeoutExecutor
from baldur.core.tls import (
    TLSConfig,
    get_tls_config,
    reset_tls_config,
)
from baldur.core.tls_handler import (
    TLSErrorClassifier,
    TLSErrorInfo,
    TLSErrorSeverity,
    TLSErrorType,
)
from baldur.core.ttl_cache import CacheStats, TTLCacheBase

# ForensicContext, ForensicContextBuilder, etc. removed - forensic.py deleted
from baldur.interfaces.pool_monitor import (
    ConnectionInfo,
    ConnectionPoolMonitor,
    LeakReport,
    PoolHealthStatus,
    PoolStats,
    PoolStatsProvider,
)
from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    FailedOperationData,
)
from baldur.interfaces.repositories import (
    CircuitBreakerStateEnum as CircuitState,
)

__all__ = [
    # Entitlement (427)
    "EntitlementStatus",
    "EntitlementClaims",
    "EntitlementError",
    "EntitlementResult",
    "get_entitlement_status",
    "reset_entitlement_status",
    # Types
    "CircuitState",
    "FailedOperationData",
    "CircuitBreakerStateData",
    # Backoff - Strategy implementations
    "BackoffStrategy",  # ABC for all backoff strategies
    "ExponentialBackoff",
    "LinearBackoff",
    "ConstantBackoff",
    "DecorrelatedJitterBackoff",
    "get_backoff_calculator",
    # Backoff - Simple config-based interface
    # Pool Monitor (Stage 26)
    "PoolHealthStatus",
    "PoolStats",
    "ConnectionInfo",
    "LeakReport",
    "PoolStatsProvider",
    "ConnectionPoolMonitor",
    # Pool Watchdog (Stage 26)
    "PoolRecoveryAction",
    "PoolRecoveryResult",
    "PoolRecoveryHandler",
    "PoolWatchdog",
    # Shutdown Coordinator (Stage 27)
    "ShutdownPhase",
    "RequestState",
    "TrackedRequest",
    "ShutdownStats",
    "ShutdownHandler",
    "RequestTracker",
    "GracefulShutdownCoordinator",
    # Request Context (Stage 27)
    "RequestLifecycleContext",
    "track_request",
    # Time Provider (Stage 23 - Clock Skew)
    "TimeProvider",
    "SystemTimeProvider",
    "MockTimeProvider",
    "FrozenTime",
    "get_time_provider",
    "set_global_time_provider",
    "reset_time_provider",
    "is_within_clock_skew",
    # Connection Health (Stage 24 - Partial Partition)
    "ConnectionType",
    "ConnectionStatus",
    "ConnectionHealth",
    "PartitionState",
    "ConnectionHealthMonitor",
    "DefaultConnectionHealthMonitor",
    # Fallback Strategy (Stage 24 - Partial Partition)
    "FallbackMode",
    "FallbackResult",
    "FallbackStrategy",
    "SimpleFallback",
    "PartitionAwareFallback",
    "CacheFirstFallback",
    # TLS Config (Stage 25 - TLS Configuration)
    "TLSConfig",
    "get_tls_config",
    "reset_tls_config",
    # TLS Handler (Stage 25 - TLS Failure)
    "TLSErrorType",
    "TLSErrorSeverity",
    "TLSErrorInfo",
    "TLSErrorClassifier",
    # Certificate Monitor (Stage 25 - TLS Failure)
    "CertificateStatus",
    "CertificateInfo",
    "CertificateExpiryMonitor",
    "CertificateAlertManager",
    # Decision Logger (Skeleton - Observability)
    "ReasonCode",
    "DecisionBoundaryEventType",
    "DecisionLogger",
    "log_enter_pre_decision_zone",
    "log_intervention_evaluated",
    "log_exit_pre_decision_zone",
    # Execution Mode (Shadow/Evaluation Mode Support)
    "ExecutionModeType",
    "ExecutionMode",
    "get_execution_mode",
    "set_execution_mode",
    "clear_execution_mode_override",
    # Action Executor (Central Execution Point)
    "Action",
    "ActionResult",
    "ActionExecutor",
    "get_action_executor",
    "execute_action",
    # Platinum SLA Optimization
    "CBStateCache",
    "DegradedModeHandler",
    "AdaptiveJitter",
    # Test Mode Context (X-Test-Mode, Chaos)
    "TestModeContext",
    "is_synthetic_context",
    "get_synthetic_session_id",
    "synthetic_context",
    # Timeout Executor (#357)
    "TimeoutExecutor",
    "LockExtendable",
    # Idempotency Gate (#357)
    "IdempotencyGate",
    "IdempotencyDecision",
    "IdempotencyCheckResult",
    "get_idempotency_gate",
    "reset_idempotency_gate",
    # Step Execution Engine (#357)
    "StepExecutionEngine",
    "SkipDecision",
    "FailureAction",
    "ForwardResult",
    "CompensationResult",
    "CompensationFailure",
    "LockConfig",
    # Step Execution Exceptions (#357)
    "StepExecutionError",
    "StepTimeoutError",
    "CompensationError",
    "ConcurrencyConflictError",
    # TTL Cache (#362 Functional Deduplication)
    "TTLCacheBase",
    "CacheStats",
    # Singleflight (#594 Cache-Miss Stampede Protection)
    "Singleflight",
    # Degraded Mode Protocol (#362)
    "DegradedModeProtocol",
    # Execution Outcome Protocol (#362)
    "ExecutionOutcome",
    # SerializableMixin (#363)
    "SerializableMixin",
    # Settings Dependency Graph (#372)
    "CycleDetectedError",
    "DependencyType",
    "SettingsDependency",
    "SettingsInvariant",
    "SettingsDependencyGraph",
    "get_dependency_graph",
    "reset_dependency_graph",
    # Constraint Engine (#372)
    "ConstraintEngine",
    "ConstraintResult",
    "ConstraintViolation",
    "get_constraint_engine",
    "reset_constraint_engine",
    # Time Series Forecasters (#599 D3 — cross-tier primitive)
    "ForecastDataPoint",
    "HoltLinearForecaster",
    "EWMAForecaster",
    "HoltWintersForecaster",
]
