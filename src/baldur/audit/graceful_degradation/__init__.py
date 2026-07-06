"""
Graceful Degradation Package for Hash Chain Operations.

This package provides multi-tier fallback, degradation management,
circuit breaker, and WAL-based recovery for hash chain operations.

Components:
- DegradationLevel: Degradation level enum
- CircuitState: Circuit breaker state enum
- FallbackConfig: Configuration for fallback chain
- CircuitBreakerConfig: Configuration for circuit breaker → HashChainCircuitBreakerConfig
- HashChainFallbackChain: Multi-tier fallback chain
- DegradedEntryInfo: Info about degraded entries
- DegradedEntryMarker: Marks and tracks degraded entries
- HashChainRecoveryWALEntry: WAL entry dataclass
- HashChainWALRecovery: WAL-based recovery
- HashChainDegradationManager: Degradation level management
- HashChainCircuitBreaker: Circuit breaker for Redis operations
- HashChainGracefulDegradationManager: Unified manager for all components

Usage:
    from baldur.audit.graceful_degradation import (
        HashChainGracefulDegradationManager,
        DegradationLevel,
        CircuitState,
    )

    manager = HashChainGracefulDegradationManager(redis_client)
    manager.initialize()
    entry = manager.add_integrity_with_fallback(entry)
"""

from .circuit_breaker import HashChainCircuitBreaker
from .degradation_manager import HashChainDegradationManager
from .enums import (
    CircuitState,
    DegradationLevel,
    FallbackConfig,
    HashChainCircuitBreakerConfig,
)
from .fallback import HashChainFallbackChain
from .manager import HashChainGracefulDegradationManager
from .marker import DegradedEntryInfo, DegradedEntryMarker
from .wal_recovery import HashChainRecoveryWALEntry, HashChainWALRecovery

__all__ = [
    # Enums and configs
    "DegradationLevel",
    "CircuitState",
    "FallbackConfig",
    "HashChainCircuitBreakerConfig",
    # Fallback chain
    "HashChainFallbackChain",
    # Degraded marker
    "DegradedEntryInfo",
    "DegradedEntryMarker",
    # WAL recovery
    "HashChainRecoveryWALEntry",
    "HashChainWALRecovery",
    # Degradation manager
    "HashChainDegradationManager",
    # Circuit breaker
    "HashChainCircuitBreaker",
    # Unified manager
    "HashChainGracefulDegradationManager",
]
