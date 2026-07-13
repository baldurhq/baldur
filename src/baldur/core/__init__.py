"""
Core module - Framework-agnostic business logic

This module contains pure Python implementations without any framework dependencies.

Backoff API:
    - ExponentialBackoff, LinearBackoff, etc.: Strategy pattern implementations
      Usage: strategy = ExponentialBackoff(base=2); strategy.calculate(attempt)

Status: Internal
"""

# Lazy barrel — register names in `_LAZY_IMPORTS`; never add an eager
# top-level `from baldur.X import ...` here (defeats the lazy import path
# and is caught by the import-weight gate).

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
    from baldur.core.timeout_executor import (
        LockExtendable,
        TimeoutExecutor,
    )
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
    from baldur.core.ttl_cache import (
        CacheStats,
        TTLCacheBase,
    )
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
    from baldur.interfaces.repositories import CircuitBreakerStateEnum as CircuitState

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "Action": ("baldur.core.action_executor", "Action"),
    "ActionExecutor": ("baldur.core.action_executor", "ActionExecutor"),
    "ActionResult": ("baldur.core.action_executor", "ActionResult"),
    "execute_action": ("baldur.core.action_executor", "execute_action"),
    "get_action_executor": ("baldur.core.action_executor", "get_action_executor"),
    "AdaptiveJitter": ("baldur.core.adaptive_jitter", "AdaptiveJitter"),
    "BackoffStrategy": ("baldur.core.backoff", "BackoffStrategy"),
    "ConstantBackoff": ("baldur.core.backoff", "ConstantBackoff"),
    "DecorrelatedJitterBackoff": ("baldur.core.backoff", "DecorrelatedJitterBackoff"),
    "ExponentialBackoff": ("baldur.core.backoff", "ExponentialBackoff"),
    "LinearBackoff": ("baldur.core.backoff", "LinearBackoff"),
    "get_backoff_calculator": ("baldur.core.backoff", "get_backoff_calculator"),
    "CertificateAlertManager": ("baldur.core.cert_monitor", "CertificateAlertManager"),
    "CertificateExpiryMonitor": (
        "baldur.core.cert_monitor",
        "CertificateExpiryMonitor",
    ),
    "CertificateInfo": ("baldur.core.cert_monitor", "CertificateInfo"),
    "CertificateStatus": ("baldur.core.cert_monitor", "CertificateStatus"),
    "ConnectionHealth": ("baldur.core.connection_health", "ConnectionHealth"),
    "ConnectionHealthMonitor": (
        "baldur.core.connection_health",
        "ConnectionHealthMonitor",
    ),
    "ConnectionStatus": ("baldur.core.connection_health", "ConnectionStatus"),
    "ConnectionType": ("baldur.core.connection_health", "ConnectionType"),
    "DefaultConnectionHealthMonitor": (
        "baldur.core.connection_health",
        "DefaultConnectionHealthMonitor",
    ),
    "PartitionState": ("baldur.core.connection_health", "PartitionState"),
    "ConstraintEngine": ("baldur.core.constraint_engine", "ConstraintEngine"),
    "ConstraintResult": ("baldur.core.constraint_engine", "ConstraintResult"),
    "ConstraintViolation": ("baldur.core.constraint_engine", "ConstraintViolation"),
    "get_constraint_engine": ("baldur.core.constraint_engine", "get_constraint_engine"),
    "reset_constraint_engine": (
        "baldur.core.constraint_engine",
        "reset_constraint_engine",
    ),
    "DecisionBoundaryEventType": (
        "baldur.core.decision_logger",
        "DecisionBoundaryEventType",
    ),
    "DecisionLogger": ("baldur.core.decision_logger", "DecisionLogger"),
    "ReasonCode": ("baldur.core.decision_logger", "ReasonCode"),
    "log_enter_pre_decision_zone": (
        "baldur.core.decision_logger",
        "log_enter_pre_decision_zone",
    ),
    "log_exit_pre_decision_zone": (
        "baldur.core.decision_logger",
        "log_exit_pre_decision_zone",
    ),
    "log_intervention_evaluated": (
        "baldur.core.decision_logger",
        "log_intervention_evaluated",
    ),
    "DegradedModeHandler": ("baldur.core.degraded_mode_handler", "DegradedModeHandler"),
    "DegradedModeProtocol": (
        "baldur.core.degraded_mode_protocol",
        "DegradedModeProtocol",
    ),
    "EntitlementClaims": ("baldur.core.entitlement", "EntitlementClaims"),
    "EntitlementError": ("baldur.core.entitlement", "EntitlementError"),
    "EntitlementResult": ("baldur.core.entitlement", "EntitlementResult"),
    "EntitlementStatus": ("baldur.core.entitlement", "EntitlementStatus"),
    "get_entitlement_status": ("baldur.core.entitlement", "get_entitlement_status"),
    "reset_entitlement_status": ("baldur.core.entitlement", "reset_entitlement_status"),
    "CompensationError": ("baldur.core.exceptions", "CompensationError"),
    "ConcurrencyConflictError": ("baldur.core.exceptions", "ConcurrencyConflictError"),
    "StepExecutionError": ("baldur.core.exceptions", "StepExecutionError"),
    "StepTimeoutError": ("baldur.core.exceptions", "StepTimeoutError"),
    "ExecutionMode": ("baldur.core.execution_mode", "ExecutionMode"),
    "ExecutionModeType": ("baldur.core.execution_mode", "ExecutionModeType"),
    "clear_execution_mode_override": (
        "baldur.core.execution_mode",
        "clear_execution_mode_override",
    ),
    "get_execution_mode": ("baldur.core.execution_mode", "get_execution_mode"),
    "set_execution_mode": ("baldur.core.execution_mode", "set_execution_mode"),
    "ExecutionOutcome": ("baldur.core.execution_protocol", "ExecutionOutcome"),
    "CacheFirstFallback": ("baldur.core.fallback_strategy", "CacheFirstFallback"),
    "FallbackMode": ("baldur.core.fallback_strategy", "FallbackMode"),
    "FallbackResult": ("baldur.core.fallback_strategy", "FallbackResult"),
    "FallbackStrategy": ("baldur.core.fallback_strategy", "FallbackStrategy"),
    "PartitionAwareFallback": (
        "baldur.core.fallback_strategy",
        "PartitionAwareFallback",
    ),
    "SimpleFallback": ("baldur.core.fallback_strategy", "SimpleFallback"),
    "IdempotencyCheckResult": (
        "baldur.core.idempotency_gate",
        "IdempotencyCheckResult",
    ),
    "IdempotencyDecision": ("baldur.core.idempotency_gate", "IdempotencyDecision"),
    "IdempotencyGate": ("baldur.core.idempotency_gate", "IdempotencyGate"),
    "get_idempotency_gate": ("baldur.core.idempotency_gate", "get_idempotency_gate"),
    "reset_idempotency_gate": (
        "baldur.core.idempotency_gate",
        "reset_idempotency_gate",
    ),
    "PoolRecoveryAction": ("baldur.core.pool_watchdog", "PoolRecoveryAction"),
    "PoolRecoveryHandler": ("baldur.core.pool_watchdog", "PoolRecoveryHandler"),
    "PoolRecoveryResult": ("baldur.core.pool_watchdog", "PoolRecoveryResult"),
    "PoolWatchdog": ("baldur.core.pool_watchdog", "PoolWatchdog"),
    "RequestLifecycleContext": (
        "baldur.core.request_context",
        "RequestLifecycleContext",
    ),
    "track_request": ("baldur.core.request_context", "track_request"),
    "SerializableMixin": ("baldur.core.serializable", "SerializableMixin"),
    "CycleDetectedError": ("baldur.core.settings_dependency", "CycleDetectedError"),
    "DependencyType": ("baldur.core.settings_dependency", "DependencyType"),
    "SettingsDependency": ("baldur.core.settings_dependency", "SettingsDependency"),
    "SettingsDependencyGraph": (
        "baldur.core.settings_dependency",
        "SettingsDependencyGraph",
    ),
    "SettingsInvariant": ("baldur.core.settings_dependency", "SettingsInvariant"),
    "get_dependency_graph": ("baldur.core.settings_dependency", "get_dependency_graph"),
    "reset_dependency_graph": (
        "baldur.core.settings_dependency",
        "reset_dependency_graph",
    ),
    "GracefulShutdownCoordinator": (
        "baldur.core.shutdown_coordinator",
        "GracefulShutdownCoordinator",
    ),
    "RequestState": ("baldur.core.shutdown_coordinator", "RequestState"),
    "RequestTracker": ("baldur.core.shutdown_coordinator", "RequestTracker"),
    "ShutdownHandler": ("baldur.core.shutdown_coordinator", "ShutdownHandler"),
    "ShutdownPhase": ("baldur.core.shutdown_coordinator", "ShutdownPhase"),
    "ShutdownStats": ("baldur.core.shutdown_coordinator", "ShutdownStats"),
    "TrackedRequest": ("baldur.core.shutdown_coordinator", "TrackedRequest"),
    "Singleflight": ("baldur.core.singleflight", "Singleflight"),
    "CBStateCache": ("baldur.core.state_cache", "CBStateCache"),
    "CompensationFailure": ("baldur.core.step_execution_engine", "CompensationFailure"),
    "CompensationResult": ("baldur.core.step_execution_engine", "CompensationResult"),
    "FailureAction": ("baldur.core.step_execution_engine", "FailureAction"),
    "ForwardResult": ("baldur.core.step_execution_engine", "ForwardResult"),
    "LockConfig": ("baldur.core.step_execution_engine", "LockConfig"),
    "SkipDecision": ("baldur.core.step_execution_engine", "SkipDecision"),
    "StepExecutionEngine": ("baldur.core.step_execution_engine", "StepExecutionEngine"),
    "TestModeContext": ("baldur.core.test_mode_context", "TestModeContext"),
    "get_synthetic_session_id": (
        "baldur.core.test_mode_context",
        "get_synthetic_session_id",
    ),
    "is_synthetic_context": ("baldur.core.test_mode_context", "is_synthetic_context"),
    "synthetic_context": ("baldur.core.test_mode_context", "synthetic_context"),
    "FrozenTime": ("baldur.core.time_provider", "FrozenTime"),
    "MockTimeProvider": ("baldur.core.time_provider", "MockTimeProvider"),
    "SystemTimeProvider": ("baldur.core.time_provider", "SystemTimeProvider"),
    "TimeProvider": ("baldur.core.time_provider", "TimeProvider"),
    "get_time_provider": ("baldur.core.time_provider", "get_time_provider"),
    "is_within_clock_skew": ("baldur.core.time_provider", "is_within_clock_skew"),
    "reset_time_provider": ("baldur.core.time_provider", "reset_time_provider"),
    "set_global_time_provider": ("baldur.core.time_provider", "set_time_provider"),
    "EWMAForecaster": ("baldur.core.time_series", "EWMAForecaster"),
    "ForecastDataPoint": ("baldur.core.time_series", "ForecastDataPoint"),
    "HoltLinearForecaster": ("baldur.core.time_series", "HoltLinearForecaster"),
    "HoltWintersForecaster": ("baldur.core.time_series", "HoltWintersForecaster"),
    "LockExtendable": ("baldur.core.timeout_executor", "LockExtendable"),
    "TimeoutExecutor": ("baldur.core.timeout_executor", "TimeoutExecutor"),
    "TLSConfig": ("baldur.core.tls", "TLSConfig"),
    "get_tls_config": ("baldur.core.tls", "get_tls_config"),
    "reset_tls_config": ("baldur.core.tls", "reset_tls_config"),
    "TLSErrorClassifier": ("baldur.core.tls_handler", "TLSErrorClassifier"),
    "TLSErrorInfo": ("baldur.core.tls_handler", "TLSErrorInfo"),
    "TLSErrorSeverity": ("baldur.core.tls_handler", "TLSErrorSeverity"),
    "TLSErrorType": ("baldur.core.tls_handler", "TLSErrorType"),
    "CacheStats": ("baldur.core.ttl_cache", "CacheStats"),
    "TTLCacheBase": ("baldur.core.ttl_cache", "TTLCacheBase"),
    "ConnectionInfo": ("baldur.interfaces.pool_monitor", "ConnectionInfo"),
    "ConnectionPoolMonitor": (
        "baldur.interfaces.pool_monitor",
        "ConnectionPoolMonitor",
    ),
    "LeakReport": ("baldur.interfaces.pool_monitor", "LeakReport"),
    "PoolHealthStatus": ("baldur.interfaces.pool_monitor", "PoolHealthStatus"),
    "PoolStats": ("baldur.interfaces.pool_monitor", "PoolStats"),
    "PoolStatsProvider": ("baldur.interfaces.pool_monitor", "PoolStatsProvider"),
    "CircuitBreakerStateData": (
        "baldur.interfaces.repositories",
        "CircuitBreakerStateData",
    ),
    "FailedOperationData": ("baldur.interfaces.repositories", "FailedOperationData"),
    "CircuitState": ("baldur.interfaces.repositories", "CircuitBreakerStateEnum"),
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
