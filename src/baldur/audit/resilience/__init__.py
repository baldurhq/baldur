"""
Audit Resilience Package.

Provides fault-tolerant mechanisms for audit logging:
- Circuit Breaker: Prevents cascading failures from slow/dead backends
- Degraded Mode: Automatic fallback to local logging
- Syslog Fallback: OS-level logging for critical events
- Metrics: Prometheus-compatible monitoring
- In-Memory Buffer: Memory fallback when WAL fails

Usage:
    from baldur.audit.resilience import (
        CircuitBreaker,
        CircuitBreakerRegistry,
        AuditMetrics,
        SyslogFallback,
        DegradedModeManager,
        InMemoryAuditBuffer,
    )

    # Circuit Breaker
    cb = get_circuit_breaker("cloudwatch")
    if cb.can_execute():
        try:
            result = external_call()
            cb.record_success()
        except Exception:
            cb.record_failure()

    # Metrics
    metrics = get_audit_metrics()
    metrics.record_write("LocalFile", success=True)
"""

from .buffer import (
    InMemoryAuditBuffer,
    get_inmemory_audit_buffer,
)
from .circuit_breaker import (
    AuditCircuitBreakerConfig,
    CircuitBreaker,
    CircuitBreakerBase,
    CircuitBreakerRegistry,
    CircuitBreakerSnapshot,
    CircuitState,
    get_circuit_breaker,
)
from .degradation_protocol import (
    DegradationBroadcaster,
    DegradationObserver,
    DegradationStatus,
)
from .degraded_mode import (
    DegradedModeManager,
    get_degraded_mode_manager,
)
from .metrics import (
    AuditMetrics,
    get_audit_metrics,
)
from .syslog_fallback import (
    SyslogFallback,
    get_syslog_fallback,
    log_critical_to_syslog,
)

__all__ = [
    # Circuit Breaker
    "CircuitState",
    "AuditCircuitBreakerConfig",
    "CircuitBreakerSnapshot",
    "CircuitBreakerBase",
    "CircuitBreaker",
    "CircuitBreakerRegistry",
    "get_circuit_breaker",
    # Metrics
    "AuditMetrics",
    "get_audit_metrics",
    # Syslog Fallback
    "SyslogFallback",
    "get_syslog_fallback",
    "log_critical_to_syslog",
    # Degradation Protocol
    "DegradationObserver",
    "DegradationBroadcaster",
    "DegradationStatus",
    # Degraded Mode
    "DegradedModeManager",
    "get_degraded_mode_manager",
    # In-Memory Buffer
    "InMemoryAuditBuffer",
    "get_inmemory_audit_buffer",
]
