"""
Baldur Audit Logging Package.

Provides comprehensive audit logging for configuration changes with:
- Privacy-compliant IP masking (GDPR/CCPA)
- Hash chain integrity for tamper detection
- Trace ID correlation
- Single AuditLogAdapter hierarchy via ProviderRegistry.audit (416 Part 6)

Usage:
    from baldur.audit import log_config_change, get_audit_logger

    # Simple usage
    log_config_change(
        config_type="RETRY_CONFIG",
        config_key="max_retries",
        old_value=3,
        new_value=5,
        user="admin",
        request=request,  # Django request object
    )

    # Advanced usage
    logger = get_audit_logger()
    logger.log_config_update(...)

Status: Internal
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from baldur.audit.integrity import HashChainManager

# =============================================================================
# CORE API - direct import (11) - the most frequently used core API
# NOTE: the self_audit function shares its name with a submodule, so lazy
#       import is impossible; a direct import is required (Python import
#       system constraint)
# =============================================================================
from baldur.audit.logger import (
    AuditLogger,
    get_audit_logger,
    log_config_change,
)
from baldur.audit.masking import (
    hash_for_audit,
    mask_email,
    mask_ip,
)

# self_audit shares its name with the module, so __getattr__ lazy import is
# impossible - import it directly
from baldur.audit.self_audit import self_audit as self_audit
from baldur.audit.trace import (
    generate_trace_id,
    get_trace_id,
    set_trace_id,
)

# =============================================================================
# LAZY IMPORTS - 106 symbols
# =============================================================================
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # logger (additional)
    "AuditConfigChangeEvent": ("baldur.audit.logger", "AuditConfigChangeEvent"),
    "ConfigChangeEvent": (
        "baldur.audit.logger",
        "ConfigChangeEvent",
    ),  # deprecated alias
    "ConfigAuditAction": ("baldur.audit.logger", "ConfigAuditAction"),
    # masking (additional)
    "mask_sensitive_fields": ("baldur.audit.masking", "mask_sensitive_fields"),
    "extract_ip_from_request": ("baldur.audit.masking", "extract_ip_from_request"),
    # trace (additional)
    "TraceContext": ("baldur.audit.trace", "TraceContext"),
    "trace_id_middleware": ("baldur.audit.trace", "trace_id_middleware"),
    # integrity (additional)
    "HashChainVerifier": ("baldur.audit.integrity", "HashChainVerifier"),
    "verify_audit_log_integrity": (
        "baldur.audit.integrity",
        "verify_audit_log_integrity",
    ),
    # 416: audit/backends/ package was deleted (H1/H2 unification).
    # resilience (15)
    "CircuitBreaker": ("baldur.audit.resilience", "CircuitBreaker"),
    "AuditCircuitBreakerConfig": (
        "baldur.audit.resilience",
        "AuditCircuitBreakerConfig",
    ),
    "CircuitBreakerRegistry": (
        "baldur.audit.resilience",
        "CircuitBreakerRegistry",
    ),
    "CircuitBreakerSnapshot": (
        "baldur.audit.resilience",
        "CircuitBreakerSnapshot",
    ),
    "CircuitState": ("baldur.audit.resilience", "CircuitState"),
    "AuditMetrics": ("baldur.audit.resilience", "AuditMetrics"),
    "SyslogFallback": ("baldur.audit.resilience", "SyslogFallback"),
    "DegradedModeManager": ("baldur.audit.resilience", "DegradedModeManager"),
    "InMemoryAuditBuffer": ("baldur.audit.resilience", "InMemoryAuditBuffer"),
    "get_circuit_breaker": ("baldur.audit.resilience", "get_circuit_breaker"),
    "get_audit_metrics": ("baldur.audit.resilience", "get_audit_metrics"),
    "get_syslog_fallback": ("baldur.audit.resilience", "get_syslog_fallback"),
    "get_degraded_mode_manager": (
        "baldur.audit.resilience",
        "get_degraded_mode_manager",
    ),
    "get_inmemory_audit_buffer": (
        "baldur.audit.resilience",
        "get_inmemory_audit_buffer",
    ),
    "log_critical_to_syslog": (
        "baldur.audit.resilience",
        "log_critical_to_syslog",
    ),
    # env_snapshot (5)
    "collect_env_snapshot": ("baldur.audit.env_snapshot", "collect_env_snapshot"),
    "log_env_snapshot_to_audit": (
        "baldur.audit.env_snapshot",
        "log_env_snapshot_to_audit",
    ),
    "get_env_snapshot_summary": (
        "baldur.audit.env_snapshot",
        "get_env_snapshot_summary",
    ),
    "TRACKED_PREFIXES": ("baldur.audit.env_snapshot", "TRACKED_PREFIXES"),
    "SENSITIVE_KEYWORDS": ("baldur.audit.env_snapshot", "SENSITIVE_KEYWORDS"),
    # config (3)
    "AuditConfig": ("baldur.audit.config", "AuditConfig"),
    "COMPLIANCE_RETENTION_DAYS": (
        "baldur.audit.config",
        "COMPLIANCE_RETENTION_DAYS",
    ),
    "get_recommended_retention": (
        "baldur.audit.config",
        "get_recommended_retention",
    ),
    # continuous_audit (1)
    "ContinuousAuditRecorder": (
        "baldur.audit.continuous_audit",
        "ContinuousAuditRecorder",
    ),
    # ring_buffer (3)
    "RingBuffer": ("baldur.audit.ring_buffer", "RingBuffer"),
    "RingBufferStats": ("baldur.audit.ring_buffer", "RingBufferStats"),
    "BackpressureStrategy": ("baldur.scaling.config", "BackpressureStrategy"),
    # self_audit (3) - the self_audit function is imported directly (name clash)
    "SelfAuditLogger": ("baldur.audit.self_audit", "SelfAuditLogger"),
    "SelfAuditEvent": ("baldur.audit.self_audit", "SelfAuditEvent"),
    "SelfAuditStats": ("baldur.audit.self_audit", "SelfAuditStats"),
    # checksum (10)
    "compute_crc32": ("baldur.audit.checksum", "compute_crc32"),
    "compute_sha256": ("baldur.audit.checksum", "compute_sha256"),
    "verify_crc32": ("baldur.audit.checksum", "verify_crc32"),
    "verify_sha256": ("baldur.audit.checksum", "verify_sha256"),
    "compute_checksum": ("baldur.audit.checksum", "compute_checksum"),
    "verify_checksum": ("baldur.audit.checksum", "verify_checksum"),
    "ChecksumResult": ("baldur.audit.checksum", "ChecksumResult"),
    "checksum_dict": ("baldur.audit.checksum", "checksum_dict"),
    "checksum_file": ("baldur.audit.checksum", "checksum_file"),
    "verify_file_checksum": ("baldur.audit.checksum", "verify_file_checksum"),
    # resilient_recorder (2)
    "ResilientContinuousAuditRecorder": (
        "baldur.audit.resilient_recorder",
        "ResilientContinuousAuditRecorder",
    ),
    "ResilientRecorderConfig": (
        "baldur.audit.resilient_recorder",
        "ResilientRecorderConfig",
    ),
    # wal (8)
    "WriteAheadLog": ("baldur.audit.wal", "WriteAheadLog"),
    "WALConfig": ("baldur.audit.wal", "WALConfig"),
    "WALEntry": ("baldur.audit.wal", "WALEntry"),
    "WALError": ("baldur.audit.wal", "WALError"),
    "WALCorruptionError": ("baldur.audit.wal", "WALCorruptionError"),
    "WALState": ("baldur.audit.wal", "WALState"),
    "WALStats": ("baldur.audit.wal", "WALStats"),
    "create_wal": ("baldur.audit.wal", "create_wal"),
    # audit_watchdog (9)
    "AuditWatchdog": ("baldur.audit.audit_watchdog", "AuditWatchdog"),
    "AuditWatchdogConfig": ("baldur.audit.audit_watchdog", "AuditWatchdogConfig"),
    "AuditWatchdogStatus": ("baldur.audit.audit_watchdog", "AuditWatchdogStatus"),
    "WatchdogStats": ("baldur.audit.audit_watchdog", "WatchdogStats"),
    "HeartbeatTarget": ("baldur.audit.audit_watchdog", "HeartbeatTarget"),
    "WatchdogChecker": ("baldur.audit.audit_watchdog", "WatchdogChecker"),
    "get_watchdog": ("baldur.audit.audit_watchdog", "get_watchdog"),
    "start_watchdog": ("baldur.audit.audit_watchdog", "start_watchdog"),
    "stop_watchdog": ("baldur.audit.audit_watchdog", "stop_watchdog"),
    # verify_audit_integrity (4)
    "AuditIntegrityVerifier": (
        "baldur.audit.verify_audit_integrity",
        "AuditIntegrityVerifier",
    ),
    "VerificationResult": (
        "baldur.audit.verify_audit_integrity",
        "VerificationResult",
    ),
    "VerificationSummary": (
        "baldur.audit.verify_audit_integrity",
        "VerificationSummary",
    ),
    "OutputFormat": ("baldur.audit.verify_audit_integrity", "OutputFormat"),
    # audit_integration (10)
    "EventSeverity": ("baldur.utils.async_logger", "EventSeverity"),
    "AsyncLoggerConfig": ("baldur.audit.audit_integration", "AsyncLoggerConfig"),
    "AsyncLoggerAdapter": ("baldur.audit.audit_integration", "AsyncLoggerAdapter"),
    "AuditObserverEventType": (
        "baldur.audit.audit_integration",
        "AuditObserverEventType",
    ),
    "AuditEventData": ("baldur.audit.audit_integration", "AuditEventData"),
    "AuditEventObserver": ("baldur.audit.audit_integration", "AuditEventObserver"),
    "AsyncLoggerObserver": (
        "baldur.audit.audit_integration",
        "AsyncLoggerObserver",
    ),
    "IntegratedAuditRecorder": (
        "baldur.audit.audit_integration",
        "IntegratedAuditRecorder",
    ),
    "configure_integration": (
        "baldur.audit.audit_integration",
        "configure_integration",
    ),
    "create_command_center_callback": (
        "baldur.audit.audit_integration",
        "create_command_center_callback",
    ),
    # export (5)
    "AuditExporter": ("baldur.audit.export", "AuditExporter"),
    "ExportFormat": ("baldur.audit.export", "ExportFormat"),
    "ExportTarget": ("baldur.audit.export", "ExportTarget"),
    "ExportOptions": ("baldur.audit.export", "ExportOptions"),
    "ExportStats": ("baldur.audit.export", "ExportStats"),
    # signed_manifest (5)
    "MerkleTree": ("baldur.audit.signed_manifest", "MerkleTree"),
    "RFC3161Timestamp": ("baldur.audit.signed_manifest", "RFC3161Timestamp"),
    "RFC3161Client": ("baldur.audit.signed_manifest", "RFC3161Client"),
    "SignedManifest": ("baldur.audit.signed_manifest", "SignedManifest"),
    "ManifestEntry": ("baldur.audit.signed_manifest", "ManifestEntry"),
    # event_buffer (5)
    "AuditEventType": ("baldur.audit.event_buffer", "AuditEventType"),
    "BufferEventType": (
        "baldur.audit.event_buffer",
        "AuditEventType",
    ),  # backward-compat alias
    "AuditEvent": ("baldur.audit.event_buffer", "AuditEvent"),
    "RequestAuditBuffer": ("baldur.audit.event_buffer", "RequestAuditBuffer"),
    "add_audit_event": ("baldur.audit.event_buffer", "add_audit_event"),
    # checkpoint
    "CheckpointError": ("baldur.audit.checkpoint.strategy", "CheckpointError"),
}

# Cache for loaded symbols
_loaded_symbols: dict[str, object] = {}


def reset_loaded_symbols() -> None:
    """Reset lazy import cache for test isolation."""
    global _loaded_symbols
    _loaded_symbols.clear()


def __getattr__(name: str) -> object:
    """Lazy import for audit symbols."""
    if name in _LAZY_IMPORTS:
        if name not in _loaded_symbols:
            module_path, attr_name = _LAZY_IMPORTS[name]
            module = importlib.import_module(module_path)
            _loaded_symbols[name] = getattr(module, attr_name)
        return _loaded_symbols[name]

    raise AttributeError(f"module 'baldur.audit' has no attribute '{name}'")


def __dir__() -> list[str]:
    """List available symbols for IDE autocompletion."""
    return list(__all__)


# TYPE_CHECKING block for IDE support
if TYPE_CHECKING:
    from baldur.audit.audit_integration import (
        AsyncLoggerAdapter,
        AsyncLoggerConfig,
        AsyncLoggerObserver,
        AuditEventData,
        AuditEventObserver,
        AuditObserverEventType,
        EventSeverity,
        IntegratedAuditRecorder,
        configure_integration,
        create_command_center_callback,
    )
    from baldur.audit.audit_watchdog import (
        AuditWatchdog,
        AuditWatchdogConfig,
        AuditWatchdogStatus,
        HeartbeatTarget,
        WatchdogChecker,
        WatchdogStats,
        get_watchdog,
        start_watchdog,
        stop_watchdog,
    )
    from baldur.audit.checksum import (
        ChecksumResult,
        checksum_dict,
        checksum_file,
        compute_checksum,
        compute_crc32,
        compute_sha256,
        verify_checksum,
        verify_crc32,
        verify_file_checksum,
        verify_sha256,
    )
    from baldur.audit.config import (
        COMPLIANCE_RETENTION_DAYS,
        AuditConfig,
        get_recommended_retention,
    )
    from baldur.audit.continuous_audit import ContinuousAuditRecorder
    from baldur.audit.env_snapshot import (
        SENSITIVE_KEYWORDS,
        TRACKED_PREFIXES,
        collect_env_snapshot,
        get_env_snapshot_summary,
        log_env_snapshot_to_audit,
    )
    from baldur.audit.event_buffer import (
        AuditEvent,
        AuditEventType,
        RequestAuditBuffer,
        add_audit_event,
    )
    from baldur.audit.export import (
        AuditExporter,
        ExportFormat,
        ExportOptions,
        ExportStats,
        ExportTarget,
    )
    from baldur.audit.integrity import (
        HashChainVerifier,
        verify_audit_log_integrity,
    )
    from baldur.audit.logger import (
        AuditConfigChangeEvent,
        ConfigAuditAction,
        ConfigChangeEvent,
    )
    from baldur.audit.masking import extract_ip_from_request, mask_sensitive_fields
    from baldur.audit.resilience import (
        AuditCircuitBreakerConfig,
        AuditMetrics,
        CircuitBreaker,
        CircuitBreakerRegistry,
        CircuitBreakerSnapshot,
        CircuitState,
        DegradedModeManager,
        InMemoryAuditBuffer,
        SyslogFallback,
        get_audit_metrics,
        get_circuit_breaker,
        get_degraded_mode_manager,
        get_inmemory_audit_buffer,
        get_syslog_fallback,
        log_critical_to_syslog,
    )
    from baldur.audit.resilient_recorder import (
        ResilientContinuousAuditRecorder,
        ResilientRecorderConfig,
    )
    from baldur.audit.ring_buffer import (
        BackpressureStrategy,
        RingBuffer,
        RingBufferStats,
    )
    from baldur.audit.self_audit import (
        SelfAuditEvent,
        SelfAuditLogger,
        SelfAuditStats,
        self_audit,
    )
    from baldur.audit.signed_manifest import (
        ManifestEntry,
        MerkleTree,
        RFC3161Client,
        RFC3161Timestamp,
        SignedManifest,
    )
    from baldur.audit.trace import TraceContext, trace_id_middleware
    from baldur.audit.verify_audit_integrity import (
        AuditIntegrityVerifier,
        OutputFormat,
        VerificationResult,
        VerificationSummary,
    )
    from baldur.audit.wal import (
        WALConfig,
        WALCorruptionError,
        WALEntry,
        WALError,
        WALState,
        WALStats,
        WriteAheadLog,
        create_wal,
    )


__all__ = [
    # Main API (direct import)
    "AuditLogger",
    "get_audit_logger",
    "log_config_change",
    "AuditConfigChangeEvent",
    "ConfigChangeEvent",  # deprecated alias
    "ConfigAuditAction",
    # Masking utilities
    "mask_ip",
    "mask_email",
    "hash_for_audit",
    "mask_sensitive_fields",
    "extract_ip_from_request",
    # Integrity
    "HashChainManager",
    "HashChainVerifier",
    "verify_audit_log_integrity",
    # Trace ID
    "generate_trace_id",
    "get_trace_id",
    "set_trace_id",
    "TraceContext",
    "trace_id_middleware",
    # 416: Backend re-exports removed (audit/backends/ package deleted).
    # Resilience
    "CircuitBreaker",
    "AuditCircuitBreakerConfig",
    "CircuitBreakerRegistry",
    "CircuitBreakerSnapshot",
    "CircuitState",
    "AuditMetrics",
    "SyslogFallback",
    "DegradedModeManager",
    "InMemoryAuditBuffer",
    "get_circuit_breaker",
    "get_audit_metrics",
    "get_syslog_fallback",
    "get_degraded_mode_manager",
    "get_inmemory_audit_buffer",
    "log_critical_to_syslog",
    # Environment Snapshot
    "collect_env_snapshot",
    "log_env_snapshot_to_audit",
    "get_env_snapshot_summary",
    "TRACKED_PREFIXES",
    "SENSITIVE_KEYWORDS",
    # Continuous Audit (Big 4 Style)
    "AuditConfig",
    "ContinuousAuditRecorder",
    "COMPLIANCE_RETENTION_DAYS",
    "get_recommended_retention",
    # Ring Buffer
    "RingBuffer",
    "RingBufferStats",
    "BackpressureStrategy",
    # Self-Audit
    "SelfAuditLogger",
    "SelfAuditEvent",
    "SelfAuditStats",
    "self_audit",
    # Checksum Utilities
    "compute_crc32",
    "compute_sha256",
    "verify_crc32",
    "verify_sha256",
    "compute_checksum",
    "verify_checksum",
    "ChecksumResult",
    "checksum_dict",
    "checksum_file",
    "verify_file_checksum",
    # Resilient Recorder
    "ResilientContinuousAuditRecorder",
    "ResilientRecorderConfig",
    # WAL (Write-Ahead Log)
    "WriteAheadLog",
    "WALConfig",
    "WALEntry",
    "WALError",
    "WALCorruptionError",
    "WALState",
    "WALStats",
    "create_wal",
    # Audit Watchdog (Dead Man's Switch)
    "AuditWatchdog",
    "AuditWatchdogConfig",
    "AuditWatchdogStatus",
    "WatchdogStats",
    "HeartbeatTarget",
    "WatchdogChecker",
    "get_watchdog",
    "start_watchdog",
    "stop_watchdog",
    # Audit Integrity Verifier (CLI Tool)
    "AuditIntegrityVerifier",
    "VerificationResult",
    "VerificationSummary",
    "OutputFormat",
    # Audit Integration (AsyncLogger + ContinuousAudit)
    "EventSeverity",
    "AsyncLoggerConfig",
    "AsyncLoggerAdapter",
    "AuditObserverEventType",
    "AuditEventData",
    "AuditEventObserver",
    "AsyncLoggerObserver",
    "IntegratedAuditRecorder",
    "configure_integration",
    "create_command_center_callback",
    # Export CLI Tool
    "AuditExporter",
    "ExportFormat",
    "ExportTarget",
    "ExportOptions",
    "ExportStats",
    # Signed Manifest (Merkle Tree + RFC 3161)
    "MerkleTree",
    "RFC3161Timestamp",
    "RFC3161Client",
    "SignedManifest",
    "ManifestEntry",
    # Event Buffer (RequestAuditBuffer for Gateway Pipeline)
    "AuditEvent",
    "BufferEventType",
    "RequestAuditBuffer",
    "add_audit_event",
    # Checkpoint
    "CheckpointError",
]
