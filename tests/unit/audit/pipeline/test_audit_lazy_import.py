"""
Tests for Audit Lazy Import Pattern.

Validates that:
1. Core API (10 symbols) are directly available
2. Extended symbols are lazily loaded via __getattr__
3. __all__ contains all symbols
4. Backward compatibility with existing imports
5. __dir__ returns all available symbols
"""

import pytest


class TestAuditLazyImport:
    """Test Lazy Import implementation for audit module."""

    def test_core_api_direct_import(self):
        """Core API symbols should be directly importable."""
        from baldur.audit import (
            AuditLogger,
            HashChainManager,
            generate_trace_id,
            get_audit_logger,
            get_trace_id,
            hash_for_audit,
            log_config_change,
            mask_email,
            mask_ip,
            set_trace_id,
        )

        # All core symbols should be available
        assert AuditLogger is not None
        assert callable(get_audit_logger)
        assert callable(log_config_change)
        assert callable(mask_ip)
        assert callable(mask_email)
        assert callable(hash_for_audit)
        assert callable(get_trace_id)
        assert callable(set_trace_id)
        assert callable(generate_trace_id)
        assert HashChainManager is not None

    def test_lazy_import_logger_extras(self):
        """Extra logger symbols should be lazily loaded."""
        from baldur.audit import (
            ConfigAuditAction,
            ConfigChangeEvent,
        )

        assert ConfigChangeEvent is not None
        assert ConfigAuditAction is not None

    def test_lazy_import_masking_extras(self):
        """Extra masking symbols should be lazily loaded."""
        from baldur.audit import (
            extract_ip_from_request,
            mask_sensitive_fields,
        )

        assert callable(mask_sensitive_fields)
        assert callable(extract_ip_from_request)

    def test_lazy_import_trace_extras(self):
        """Extra trace symbols should be lazily loaded."""
        from baldur.audit import (
            TraceContext,
            trace_id_middleware,
        )

        assert TraceContext is not None
        assert callable(trace_id_middleware)

    def test_lazy_import_integrity_extras(self):
        """Extra integrity symbols should be lazily loaded."""
        from baldur.audit import (
            HashChainVerifier,
            verify_audit_log_integrity,
        )

        assert HashChainVerifier is not None
        assert callable(verify_audit_log_integrity)

    def test_legacy_backend_symbols_removed(self):
        """416 Part 6: audit/backends/ package was deleted (H1/H2 unification).

        Legacy backend symbols (AuditBackend, LocalFileBackend, etc.) are
        no longer importable from ``baldur.audit``. The single
        AuditLogAdapter hierarchy via ProviderRegistry.audit replaces
        the entire H2 hierarchy.
        """
        from baldur import audit

        for symbol in (
            "AuditBackend",
            "AsyncAuditBackend",
            "BackendHealth",
            "BackendStatus",
            "BufferedBackend",
            "CloudWatchBackend",
            "CompositeBackend",
            "DatadogBackend",
            "LocalFileBackend",
            "RemoteAuditBackend",
            "S3WORMBackend",
            "create_composite_backend",
            "get_default_backend",
        ):
            assert symbol not in audit.__all__, (
                f"{symbol} should be removed from baldur.audit (416 Part 6)"
            )
            with pytest.raises(AttributeError):
                _ = getattr(audit, symbol)

    def test_lazy_import_resilience(self):
        """Resilience symbols should be lazily loaded."""
        from baldur.audit import (
            AuditCircuitBreakerConfig,
            AuditMetrics,
            CircuitBreaker,
            CircuitBreakerRegistry,
            CircuitState,
            DegradedModeManager,
            SyslogFallback,
            get_audit_metrics,
            get_circuit_breaker,
            get_degraded_mode_manager,
            get_syslog_fallback,
            log_critical_to_syslog,
        )

        assert CircuitBreaker is not None
        assert AuditCircuitBreakerConfig is not None
        assert CircuitBreakerRegistry is not None
        assert CircuitState is not None
        assert AuditMetrics is not None
        assert SyslogFallback is not None
        assert DegradedModeManager is not None
        assert callable(get_circuit_breaker)
        assert callable(get_audit_metrics)
        assert callable(get_syslog_fallback)
        assert callable(get_degraded_mode_manager)
        assert callable(log_critical_to_syslog)

    def test_lazy_import_env_snapshot(self):
        """Env snapshot symbols should be lazily loaded."""
        from baldur.audit import (
            SENSITIVE_KEYWORDS,
            TRACKED_PREFIXES,
            collect_env_snapshot,
            get_env_snapshot_summary,
            log_env_snapshot_to_audit,
        )

        assert callable(collect_env_snapshot)
        assert callable(log_env_snapshot_to_audit)
        assert callable(get_env_snapshot_summary)
        assert isinstance(TRACKED_PREFIXES, (list, tuple))
        assert isinstance(SENSITIVE_KEYWORDS, (list, tuple))

    def test_lazy_import_config(self):
        """Config symbols should be lazily loaded."""
        from baldur.audit import (
            COMPLIANCE_RETENTION_DAYS,
            AuditConfig,
            get_recommended_retention,
        )

        assert AuditConfig is not None
        assert isinstance(COMPLIANCE_RETENTION_DAYS, dict)
        assert callable(get_recommended_retention)

    def test_lazy_import_continuous_audit(self):
        """Continuous audit symbols should be lazily loaded."""
        from baldur.audit import ContinuousAuditRecorder

        assert ContinuousAuditRecorder is not None

    def test_lazy_import_ring_buffer(self):
        """Ring buffer symbols should be lazily loaded."""
        from baldur.audit import (
            BackpressureStrategy,
            RingBuffer,
            RingBufferStats,
        )

        assert RingBuffer is not None
        assert RingBufferStats is not None
        assert BackpressureStrategy is not None

    def test_lazy_import_self_audit(self):
        """Self-audit symbols should be lazily loaded."""
        from baldur.audit import (
            SelfAuditEvent,
            SelfAuditLogger,
            SelfAuditStats,
            self_audit,
        )

        assert SelfAuditLogger is not None
        assert SelfAuditEvent is not None
        assert SelfAuditStats is not None
        assert callable(self_audit)

    def test_lazy_import_checksum(self):
        """Checksum symbols should be lazily loaded."""
        from baldur.audit import (
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

        assert callable(compute_crc32)
        assert callable(compute_sha256)
        assert callable(verify_crc32)
        assert callable(verify_sha256)
        assert callable(compute_checksum)
        assert callable(verify_checksum)
        assert ChecksumResult is not None
        assert callable(checksum_dict)
        assert callable(checksum_file)
        assert callable(verify_file_checksum)

    def test_lazy_import_resilient_recorder(self):
        """Resilient recorder symbols should be lazily loaded."""
        from baldur.audit import (
            ResilientContinuousAuditRecorder,
            ResilientRecorderConfig,
        )

        assert ResilientContinuousAuditRecorder is not None
        assert ResilientRecorderConfig is not None

    def test_lazy_import_wal(self):
        """WAL symbols should be lazily loaded."""
        from baldur.audit import (
            WALConfig,
            WALCorruptionError,
            WALEntry,
            WALError,
            WALState,
            WALStats,
            WriteAheadLog,
            create_wal,
        )

        assert WriteAheadLog is not None
        assert WALConfig is not None
        assert WALEntry is not None
        assert WALError is not None
        assert WALCorruptionError is not None
        assert WALState is not None
        assert WALStats is not None
        assert callable(create_wal)

    def test_lazy_import_watchdog(self):
        """Watchdog symbols should be lazily loaded."""
        from baldur.audit import (
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

        assert AuditWatchdog is not None
        assert AuditWatchdogConfig is not None
        assert AuditWatchdogStatus is not None
        assert WatchdogStats is not None
        assert HeartbeatTarget is not None
        assert WatchdogChecker is not None
        assert callable(get_watchdog)
        assert callable(start_watchdog)
        assert callable(stop_watchdog)

    def test_lazy_import_verify_integrity(self):
        """Verify integrity symbols should be lazily loaded."""
        from baldur.audit import (
            AuditIntegrityVerifier,
            OutputFormat,
            VerificationResult,
            VerificationSummary,
        )

        assert AuditIntegrityVerifier is not None
        assert VerificationResult is not None
        assert VerificationSummary is not None
        assert OutputFormat is not None

    def test_lazy_import_integration(self):
        """Integration symbols should be lazily loaded."""
        from baldur.audit import (
            AsyncLoggerAdapter,
            AsyncLoggerConfig,
            AsyncLoggerObserver,
            AuditEventData,
            AuditEventObserver,
            AuditEventType,
            EventSeverity,
            IntegratedAuditRecorder,
            configure_integration,
            create_command_center_callback,
        )

        assert EventSeverity is not None
        assert AsyncLoggerConfig is not None
        assert AsyncLoggerAdapter is not None
        assert AuditEventType is not None
        assert AuditEventData is not None
        assert AuditEventObserver is not None
        assert AsyncLoggerObserver is not None
        assert IntegratedAuditRecorder is not None
        assert callable(configure_integration)
        assert callable(create_command_center_callback)

    def test_lazy_import_export(self):
        """Export symbols should be lazily loaded."""
        from baldur.audit import (
            AuditExporter,
            ExportFormat,
            ExportOptions,
            ExportStats,
            ExportTarget,
        )

        assert AuditExporter is not None
        assert ExportFormat is not None
        assert ExportTarget is not None
        assert ExportOptions is not None
        assert ExportStats is not None

    def test_lazy_import_signed_manifest(self):
        """Signed manifest symbols should be lazily loaded."""
        from baldur.audit import (
            ManifestEntry,
            MerkleTree,
            RFC3161Client,
            RFC3161Timestamp,
            SignedManifest,
        )

        assert MerkleTree is not None
        assert RFC3161Timestamp is not None
        assert RFC3161Client is not None
        assert SignedManifest is not None
        assert ManifestEntry is not None

    def test_lazy_import_event_buffer(self):
        """Event buffer symbols should be lazily loaded."""
        from baldur.audit import (
            AuditEvent,
            BufferEventType,
            RequestAuditBuffer,
            add_audit_event,
        )

        assert AuditEvent is not None
        assert BufferEventType is not None
        assert RequestAuditBuffer is not None
        assert callable(add_audit_event)

    def test_module_all_contains_all_symbols(self):
        """__all__ should contain all symbols."""
        from baldur import audit

        # Check that __all__ is properly defined
        assert hasattr(audit, "__all__")

        # 416 Part 6: 13 backend symbols were removed; the floor drops to 85.
        assert len(audit.__all__) >= 85

        # Core symbols must be in __all__
        core_symbols = [
            "AuditLogger",
            "get_audit_logger",
            "log_config_change",
            "mask_ip",
            "mask_email",
            "hash_for_audit",
            "get_trace_id",
            "set_trace_id",
            "generate_trace_id",
            "HashChainManager",
        ]
        for symbol in core_symbols:
            assert symbol in audit.__all__, f"{symbol} not in __all__"

    def test_dir_returns_all_symbols(self):
        """__dir__ should return all available symbols."""
        from baldur import audit

        available = dir(audit)

        # Core symbols must be in dir()
        core_symbols = [
            "AuditLogger",
            "get_audit_logger",
            "log_config_change",
            "mask_ip",
            "HashChainManager",
        ]
        for symbol in core_symbols:
            assert symbol in available, f"{symbol} not in dir()"

    def test_invalid_attribute_raises_error(self):
        """Accessing invalid attribute should raise AttributeError."""
        from baldur import audit

        with pytest.raises(AttributeError) as excinfo:
            _ = audit.NonExistentSymbol

        assert "NonExistentSymbol" in str(excinfo.value)

    def test_lazy_import_caching(self):
        """Lazy imports should be cached after first access."""
        from baldur import audit

        # Access the same symbol twice — pick a surviving lazy symbol.
        first_access = audit.AuditWatchdog
        second_access = audit.AuditWatchdog

        # Both should be the same object (cached)
        assert first_access is second_access


class TestAuditLazyImportIntegration:
    """Integration tests for audit lazy import."""

    def test_audit_logger_works(self):
        """AuditLogger should work correctly."""
        from baldur.audit import AuditLogger, get_audit_logger

        logger = get_audit_logger()
        assert isinstance(logger, AuditLogger)

    def test_masking_functions_work(self):
        """Masking functions should work correctly."""
        from baldur.audit import hash_for_audit, mask_email, mask_ip

        # Test mask_ip
        masked_ip = mask_ip("192.168.1.100")
        assert masked_ip != "192.168.1.100"

        # Test mask_email
        masked_email = mask_email("test@example.com")
        assert masked_email != "test@example.com"

        # Test hash_for_audit
        hashed = hash_for_audit("sensitive_data")
        assert hashed is not None

    def test_trace_id_functions_work(self):
        """Trace ID functions should work correctly."""
        from baldur.audit import (
            generate_trace_id,
            get_trace_id,
            set_trace_id,
        )

        # Generate a trace ID
        trace_id = generate_trace_id()
        assert trace_id is not None

        # Set and get trace ID
        set_trace_id(trace_id)
        retrieved = get_trace_id()
        assert retrieved == trace_id

    def test_circuit_breaker_enum_works(self):
        """CircuitState enum should have correct values."""
        from baldur.audit import CircuitState

        # Check enum values exist
        assert hasattr(CircuitState, "CLOSED")
        assert hasattr(CircuitState, "OPEN")
        assert hasattr(CircuitState, "HALF_OPEN")
