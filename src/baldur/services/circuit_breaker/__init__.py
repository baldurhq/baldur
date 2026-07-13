"""
Circuit Breaker Module

Provides circuit breaker functionality for external service protection.

Features:
- Toggle-based circuit breaker (not automatic failure counting)
- Manual force open/close by operators
- Conditional replay trigger when circuit breaker closes
- Rate limit cascade detection (auto-open CB on 429 storm)
- Self-DDoS protection (prevent retry amplification)
- Adaptive Threshold (Emergency Level integration)
- Freeze Mode (freeze state on LOCKDOWN)
- Panic Threshold (declare Emergency Level 3 when 70% OPEN is detected)

Structure:
- config.py: Configuration and types (~125 lines)
- rate_limit_tracker.py: Rate limit tracking (~95 lines)
- protection.py: Protection mixin (~250 lines)
- manual_control.py: Manual control mixin (~350 lines)
- service.py: Main service class (~285 lines)
- convenience.py: Module-level functions (~135 lines)
- models.py: Advanced protection data models
- adaptive_threshold.py: Emergency Level integrated threshold adjustment
- freeze_mode.py: LOCKDOWN Freeze Mode
- panic_threshold.py: prevent system-wide self-destruction

Usage:
    from baldur.services.circuit_breaker import (
        CircuitBreakerService,
        CircuitBreakerConfig,
        CircuitBreakerResult,
        CircuitState,
        should_allow_request,
        force_open_circuit,
    )

    # Extended features are lazily loaded:
    from baldur.services.circuit_breaker import AdaptiveThresholdManager
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

# =============================================================================
# CORE API - direct import (10 symbols) - the most frequently used core API
# =============================================================================
from .config import CircuitBreakerConfig, CircuitBreakerResult, CircuitState
from .convenience import (
    force_open_circuit,
    get_circuit_breaker_service,
    reset_circuit_breaker_service,
    should_allow_request,
)
from .exceptions import CircuitBreakerOpenError
from .policy import AsyncCircuitBreakerPolicy, CircuitBreakerPolicy, circuit_breaker
from .service import CircuitBreakerService

# =============================================================================
# LAZY IMPORTS - 119 symbols
# =============================================================================
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # config (additional types)
    "CircuitBreakerFallbackResult": (".config", "CircuitBreakerFallbackResult"),
    # rate_limit_tracker
    "MemoryRateLimitTracker": (".rate_limit_tracker", "MemoryRateLimitTracker"),
    "RateLimitTracker": (".rate_limit_tracker", "RateLimitTracker"),
    "get_rate_limit_tracker": (".rate_limit_tracker", "get_rate_limit_tracker"),
    "reset_rate_limit_tracker": (".rate_limit_tracker", "reset_rate_limit_tracker"),
    # rate_limit_lua
    "RedisRateLimitBackend": (".rate_limit_lua", "RedisRateLimitBackend"),
    # protection
    "ProtectionMixin": (".protection", "ProtectionMixin"),
    # manual_control
    "ManualControlMixin": (".manual_control", "ManualControlMixin"),
    # convenience (additional functions)
    "force_close_circuit": (".convenience", "force_close_circuit"),
    "record_rate_limit": (".convenience", "record_rate_limit"),
    "should_allow_with_protection": (".convenience", "should_allow_with_protection"),
    "get_protection_status": (".convenience", "get_protection_status"),
    # models
    "ServiceConfig": (".models", "ServiceConfig"),
    "SheddingLevel": (".models", "SheddingLevel"),
    "LoadSheddingPolicy": (".models", "LoadSheddingPolicy"),
    "ThresholdMultiplier": (".models", "ThresholdMultiplier"),
    "AdaptiveThresholdPolicy": (".models", "AdaptiveThresholdPolicy"),
    "OpenStrategy": (".models", "OpenStrategy"),
    "CircuitBreakerAdvancedConfig": (".models", "CircuitBreakerAdvancedConfig"),
    "PanicThresholdConfig": (".models", "PanicThresholdConfig"),
    "FreezeModeState": (".models", "FreezeModeState"),
    # adaptive_threshold
    "AdaptiveThresholdManager": (".adaptive_threshold", "AdaptiveThresholdManager"),
    "AdjustedThreshold": (".adaptive_threshold", "AdjustedThreshold"),
    "get_adaptive_threshold_manager": (
        ".adaptive_threshold",
        "get_adaptive_threshold_manager",
    ),
    "get_adjusted_cb_threshold": (".adaptive_threshold", "get_adjusted_cb_threshold"),
    "should_allow_cb_auto_open": (".adaptive_threshold", "should_allow_cb_auto_open"),
    # freeze_mode
    "FreezeModeManager": (".freeze_mode", "FreezeModeManager"),
    "FreezeReason": (".freeze_mode", "FreezeReason"),
    "get_freeze_mode_manager": (".freeze_mode", "get_freeze_mode_manager"),
    "is_freeze_mode_active": (".freeze_mode", "is_freeze_mode_active"),
    "should_allow_cb_state_change": (".freeze_mode", "should_allow_cb_state_change"),
    # panic_threshold
    "PanicThresholdMonitor": (".panic_threshold", "PanicThresholdMonitor"),
    "PanicThresholdResult": (".panic_threshold", "PanicThresholdResult"),
    "get_panic_threshold_monitor": (".panic_threshold", "get_panic_threshold_monitor"),
    "check_panic_threshold": (".panic_threshold", "check_panic_threshold"),
    "is_panic_threshold_triggered": (
        ".panic_threshold",
        "is_panic_threshold_triggered",
    ),
    # service_config
    "ServiceConfigManager": (".service_config", "ServiceConfigManager"),
    "get_service_config_manager": (".service_config", "get_service_config_manager"),
    "reset_service_config_manager": (".service_config", "reset_service_config_manager"),
    "register_service": (".service_config", "register_service"),
    "get_service_config": (".service_config", "get_service_config"),
    "get_services_by_criticality": (".service_config", "get_services_by_criticality"),
    "get_shedding_targets": (".service_config", "get_shedding_targets"),
    "is_critical_service": (".service_config", "is_critical_service"),
    # blast_radius_integration
    "BlastRadiusLevel": (".blast_radius_integration", "BlastRadiusLevel"),
    "BlastRadiusAssessment": (".blast_radius_integration", "BlastRadiusAssessment"),
    "ServiceDependencyNode": (
        "baldur.core.dependency_graph",
        "ServiceDependencyNode",
    ),
    "ServiceDependencyGraph": (
        "baldur.core.dependency_graph",
        "ServiceDependencyGraph",
    ),
    "BlastRadiusIntegration": (".blast_radius_integration", "BlastRadiusIntegration"),
    "BlastRadiusConfig": (".blast_radius_integration", "BlastRadiusConfig"),
    "get_blast_radius_integration": (
        ".blast_radius_integration",
        "get_blast_radius_integration",
    ),
    "reset_blast_radius_integration": (
        ".blast_radius_integration",
        "reset_blast_radius_integration",
    ),
    "assess_cb_open_impact": (".blast_radius_integration", "assess_cb_open_impact"),
    "should_allow_cb_auto_open_blast": (
        ".blast_radius_integration",
        "should_allow_cb_auto_open",
    ),
    "register_service_dependency": (
        ".blast_radius_integration",
        "register_service_dependency",
    ),
    # load_shedding (17 symbols)
    "SheddingState": (".load_shedding", "SheddingState"),
    "SheddingDecision": (".load_shedding", "SheddingDecision"),
    "SheddingStatus": (".load_shedding", "SheddingStatus"),
    "SheddingAuditEntry": (".load_shedding", "SheddingAuditEntry"),
    "ErrorRateProvider": (".load_shedding", "ErrorRateProvider"),
    "LoadSheddingManager": (".load_shedding", "LoadSheddingManager"),
    "LoadSheddingMiddleware": (".load_shedding", "LoadSheddingMiddleware"),
    "LoadSheddingDashboard": (".load_shedding", "LoadSheddingDashboard"),
    "get_load_shedding_manager": (".load_shedding", "get_load_shedding_manager"),
    "reset_load_shedding_manager": (".load_shedding", "reset_load_shedding_manager"),
    "get_load_shedding_middleware": (".load_shedding", "get_load_shedding_middleware"),
    "get_load_shedding_dashboard": (".load_shedding", "get_load_shedding_dashboard"),
    "register_load_shedding_service": (
        ".load_shedding",
        "register_load_shedding_service",
    ),
    "evaluate_shedding": (".load_shedding", "evaluate_shedding"),
    "should_allow_shedding_request": (
        ".load_shedding",
        "should_allow_shedding_request",
    ),
    "is_shedding_active": (".load_shedding", "is_shedding_active"),
    "get_shedding_status": (".load_shedding", "get_shedding_status"),
    "set_service_error_rate": (".load_shedding", "set_service_error_rate"),
    "update_shedding_state": (".load_shedding", "update_shedding_state"),
}

# Cache for loaded symbols
_loaded_symbols: dict[str, object] = {}


def __getattr__(name: str) -> object:
    """Lazy import for circuit breaker symbols."""
    if name in _LAZY_IMPORTS:
        if name not in _loaded_symbols:
            module_rel_path, attr_name = _LAZY_IMPORTS[name]
            module = importlib.import_module(module_rel_path, __package__)
            _loaded_symbols[name] = getattr(module, attr_name)
        return _loaded_symbols[name]

    raise AttributeError(
        f"module 'baldur.services.circuit_breaker' has no attribute '{name}'"
    )


def __dir__() -> list[str]:
    """List available symbols for IDE autocompletion."""
    return list(__all__)


# TYPE_CHECKING block for IDE support
if TYPE_CHECKING:
    from baldur.core.dependency_graph import (
        ServiceDependencyGraph,
        ServiceDependencyNode,
    )

    from .adaptive_threshold import (
        AdaptiveThresholdManager,
        AdjustedThreshold,
        get_adaptive_threshold_manager,
        get_adjusted_cb_threshold,
        should_allow_cb_auto_open,
    )
    from .blast_radius_integration import (
        BlastRadiusAssessment,
        BlastRadiusConfig,
        BlastRadiusIntegration,
        BlastRadiusLevel,
        assess_cb_open_impact,
        get_blast_radius_integration,
        register_service_dependency,
        reset_blast_radius_integration,
    )
    from .blast_radius_integration import (
        should_allow_cb_auto_open as should_allow_cb_auto_open_blast,
    )
    from .config import CircuitBreakerFallbackResult
    from .convenience import (
        force_close_circuit,
        get_protection_status,
        record_rate_limit,
        should_allow_with_protection,
    )
    from .freeze_mode import (
        FreezeModeManager,
        FreezeReason,
        get_freeze_mode_manager,
        is_freeze_mode_active,
        should_allow_cb_state_change,
    )
    from .load_shedding import (
        ErrorRateProvider,
        LoadSheddingDashboard,
        LoadSheddingManager,
        LoadSheddingMiddleware,
        SheddingAuditEntry,
        SheddingDecision,
        SheddingState,
        SheddingStatus,
        evaluate_shedding,
        get_load_shedding_dashboard,
        get_load_shedding_manager,
        get_load_shedding_middleware,
        get_shedding_status,
        is_shedding_active,
        register_load_shedding_service,
        reset_load_shedding_manager,
        set_service_error_rate,
        should_allow_shedding_request,
        update_shedding_state,
    )
    from .manual_control import ManualControlMixin
    from .models import (
        AdaptiveThresholdPolicy,
        CircuitBreakerAdvancedConfig,
        FreezeModeState,
        LoadSheddingPolicy,
        OpenStrategy,
        PanicThresholdConfig,
        ServiceConfig,
        SheddingLevel,
        ThresholdMultiplier,
    )
    from .panic_threshold import (
        PanicThresholdMonitor,
        PanicThresholdResult,
        check_panic_threshold,
        get_panic_threshold_monitor,
        is_panic_threshold_triggered,
    )
    from .protection import ProtectionMixin
    from .rate_limit_lua import RedisRateLimitBackend
    from .rate_limit_tracker import (
        MemoryRateLimitTracker,
        RateLimitTracker,
        get_rate_limit_tracker,
        reset_rate_limit_tracker,
    )
    from .service_config import (
        ServiceConfigManager,
        get_service_config,
        get_service_config_manager,
        get_services_by_criticality,
        get_shedding_targets,
        is_critical_service,
        register_service,
        reset_service_config_manager,
    )


__all__ = [
    # Core API (direct import)
    "CircuitBreakerConfig",
    "CircuitBreakerResult",
    "CircuitState",
    "CircuitBreakerService",
    "CircuitBreakerPolicy",
    "AsyncCircuitBreakerPolicy",
    "CircuitBreakerOpenError",
    "circuit_breaker",
    "get_circuit_breaker_service",
    "reset_circuit_breaker_service",
    "should_allow_request",
    "force_open_circuit",
    # Config (additional)
    "CircuitBreakerFallbackResult",
    # Rate limit tracking
    "MemoryRateLimitTracker",
    "RateLimitTracker",
    "get_rate_limit_tracker",
    "reset_rate_limit_tracker",
    "RedisRateLimitBackend",
    # Mixins
    "ProtectionMixin",
    "ManualControlMixin",
    # Convenience functions (additional)
    "force_close_circuit",
    "record_rate_limit",
    "should_allow_with_protection",
    "get_protection_status",
    # Advanced Protection Models
    "ServiceConfig",
    "SheddingLevel",
    "LoadSheddingPolicy",
    "ThresholdMultiplier",
    "AdaptiveThresholdPolicy",
    "OpenStrategy",
    "CircuitBreakerAdvancedConfig",
    "PanicThresholdConfig",
    "FreezeModeState",
    # Adaptive Threshold
    "AdaptiveThresholdManager",
    "AdjustedThreshold",
    "get_adaptive_threshold_manager",
    "get_adjusted_cb_threshold",
    "should_allow_cb_auto_open",
    # Freeze Mode
    "FreezeModeManager",
    "FreezeReason",
    "get_freeze_mode_manager",
    "is_freeze_mode_active",
    "should_allow_cb_state_change",
    # Panic Threshold
    "PanicThresholdMonitor",
    "PanicThresholdResult",
    "get_panic_threshold_monitor",
    "check_panic_threshold",
    "is_panic_threshold_triggered",
    # Service Config Manager
    "ServiceConfigManager",
    "get_service_config_manager",
    "reset_service_config_manager",
    "register_service",
    "get_service_config",
    "get_services_by_criticality",
    "get_shedding_targets",
    "is_critical_service",
    # Blast Radius Integration
    "BlastRadiusLevel",
    "BlastRadiusAssessment",
    "ServiceDependencyNode",
    "ServiceDependencyGraph",
    "BlastRadiusIntegration",
    "BlastRadiusConfig",
    "get_blast_radius_integration",
    "reset_blast_radius_integration",
    "assess_cb_open_impact",
    "should_allow_cb_auto_open_blast",
    "register_service_dependency",
    # Load Shedding
    "SheddingState",
    "SheddingDecision",
    "SheddingStatus",
    "SheddingAuditEntry",
    "ErrorRateProvider",
    "LoadSheddingManager",
    "LoadSheddingMiddleware",
    "LoadSheddingDashboard",
    "get_load_shedding_manager",
    "reset_load_shedding_manager",
    "get_load_shedding_middleware",
    "get_load_shedding_dashboard",
    "register_load_shedding_service",
    "evaluate_shedding",
    "should_allow_shedding_request",
    "is_shedding_active",
    "get_shedding_status",
    "set_service_error_rate",
    "update_shedding_state",
]
