"""
Tests for audit resilience module.

Tests for:
- Circuit Breaker
- Audit Metrics
- Syslog Fallback
- Degraded Mode Manager
"""

import sys
import threading
import time
from io import StringIO

from baldur.audit.resilience import (
    AuditCircuitBreakerConfig,
    AuditMetrics,
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitState,
    DegradedModeManager,
    SyslogFallback,
    get_audit_metrics,
    get_circuit_breaker,
    get_syslog_fallback,
    log_critical_to_syslog,
)


class TestCircuitBreaker:
    """Tests for CircuitBreaker class."""

    def test_initial_state_is_closed(self):
        """Test that circuit starts in closed state."""
        cb = CircuitBreaker("test")
        assert cb.state == CircuitState.CLOSED

    def test_can_execute_when_closed(self):
        """Test that calls are allowed when closed."""
        cb = CircuitBreaker("test")
        assert cb.can_execute() is True

    def test_opens_after_failure_threshold(self):
        """Test that circuit opens after reaching failure threshold."""
        config = AuditCircuitBreakerConfig(failure_threshold=3)
        cb = CircuitBreaker("test", config)

        # Record failures up to threshold
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_cannot_execute_when_open(self):
        """Test that calls are blocked when open."""
        config = AuditCircuitBreakerConfig(failure_threshold=1)
        cb = CircuitBreaker("test", config)

        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.can_execute() is False

    def test_transitions_to_half_open_after_timeout(self):
        """Test transition to half-open after timeout."""
        config = AuditCircuitBreakerConfig(
            failure_threshold=1,
            timeout_seconds=0.1,  # 100ms for testing
        )
        cb = CircuitBreaker("test", config)

        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        # Wait for timeout
        time.sleep(0.15)

        # Checking state should trigger transition
        assert cb.state == CircuitState.HALF_OPEN

    def test_can_execute_when_half_open(self):
        """Test that limited calls are allowed when half-open."""
        config = AuditCircuitBreakerConfig(
            failure_threshold=1,
            timeout_seconds=0.01,
        )
        cb = CircuitBreaker("test", config)

        cb.record_failure()
        time.sleep(0.02)

        assert cb.state == CircuitState.HALF_OPEN
        assert cb.can_execute() is True

    def test_closes_after_success_in_half_open(self):
        """Test that circuit closes after successes in half-open."""
        config = AuditCircuitBreakerConfig(
            failure_threshold=1,
            success_threshold=2,
            timeout_seconds=0.01,
        )
        cb = CircuitBreaker("test", config)

        cb.record_failure()
        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN

        cb.record_success()
        assert cb.state == CircuitState.HALF_OPEN

        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_reopens_on_failure_in_half_open(self):
        """Test that circuit reopens on failure in half-open."""
        config = AuditCircuitBreakerConfig(
            failure_threshold=1,
            timeout_seconds=0.01,
        )
        cb = CircuitBreaker("test", config)

        cb.record_failure()
        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN

        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_success_resets_failure_count(self):
        """Test that success in closed state resets failure count."""
        config = AuditCircuitBreakerConfig(failure_threshold=3)
        cb = CircuitBreaker("test", config)

        cb.record_failure()
        cb.record_failure()
        cb.record_success()

        # Failure count should be reset
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_manual_reset(self):
        """Test manual circuit reset."""
        config = AuditCircuitBreakerConfig(failure_threshold=1)
        cb = CircuitBreaker("test", config)

        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        cb.reset()
        assert cb.state == CircuitState.CLOSED

    def test_force_open(self):
        """Test manual force open."""
        cb = CircuitBreaker("test")
        assert cb.state == CircuitState.CLOSED

        cb.force_open()
        assert cb.state == CircuitState.OPEN

    def test_get_stats(self):
        """Test getting circuit breaker statistics."""
        cb = CircuitBreaker("test-cb")
        cb.record_success()
        cb.record_failure()

        stats = cb.get_stats()

        assert stats["name"] == "test-cb"
        assert stats["state"] == "closed"
        assert stats["total_successes"] == 1
        assert stats["total_failures"] == 1
        assert "config" in stats

    def test_thread_safety(self):
        """Test thread safety of circuit breaker."""
        config = AuditCircuitBreakerConfig(failure_threshold=100)
        cb = CircuitBreaker("test", config)

        def record_operations():
            for _ in range(50):
                cb.record_success()
                cb.record_failure()

        threads = [threading.Thread(target=record_operations) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = cb.get_stats()
        assert stats["total_successes"] == 500
        assert stats["total_failures"] == 500


class TestCircuitBreakerRegistry:
    """Tests for CircuitBreakerRegistry."""

    def test_get_or_create(self):
        """Test creating and retrieving circuit breakers."""
        # Reset singleton for testing
        CircuitBreakerRegistry._instance = None
        registry = CircuitBreakerRegistry.get_instance()

        cb1 = registry.get_or_create("backend1")
        cb2 = registry.get_or_create("backend1")

        assert cb1 is cb2

        # Cleanup
        CircuitBreakerRegistry._instance = None

    def test_get_nonexistent(self):
        """Test getting non-existent circuit breaker."""
        CircuitBreakerRegistry._instance = None
        registry = CircuitBreakerRegistry.get_instance()

        cb = registry.get("nonexistent")
        assert cb is None

        # Cleanup
        CircuitBreakerRegistry._instance = None

    def test_get_all_stats(self):
        """Test getting all circuit breaker stats."""
        CircuitBreakerRegistry._instance = None
        registry = CircuitBreakerRegistry.get_instance()

        registry.get_or_create("backend1")
        registry.get_or_create("backend2")

        stats = registry.get_all_stats()
        assert "backend1" in stats
        assert "backend2" in stats

        # Cleanup
        CircuitBreakerRegistry._instance = None

    def test_get_open_circuits(self):
        """Test getting open circuit names."""
        CircuitBreakerRegistry._instance = None
        registry = CircuitBreakerRegistry.get_instance()

        cb1 = registry.get_or_create(
            "backend1", AuditCircuitBreakerConfig(failure_threshold=1)
        )
        registry.get_or_create("backend2")

        cb1.record_failure()  # Opens backend1

        open_circuits = registry.get_open_circuits()
        assert "backend1" in open_circuits
        assert "backend2" not in open_circuits

        # Cleanup
        CircuitBreakerRegistry._instance = None

    def test_reset_all(self):
        """Test resetting all circuit breakers."""
        CircuitBreakerRegistry._instance = None
        registry = CircuitBreakerRegistry.get_instance()

        cb1 = registry.get_or_create(
            "backend1", AuditCircuitBreakerConfig(failure_threshold=1)
        )
        cb2 = registry.get_or_create(
            "backend2", AuditCircuitBreakerConfig(failure_threshold=1)
        )

        cb1.record_failure()
        cb2.record_failure()

        registry.reset_all()

        assert cb1.state == CircuitState.CLOSED
        assert cb2.state == CircuitState.CLOSED

        # Cleanup
        CircuitBreakerRegistry._instance = None


class TestAuditMetrics:
    """Tests for AuditMetrics class."""

    def test_record_write_success(self):
        """Test recording successful writes."""
        AuditMetrics._instance = None
        metrics = AuditMetrics.get_instance()
        metrics.reset()

        metrics.record_write("LocalFile", success=True, duration_ms=10.5)

        data = metrics.get_metrics()
        assert data["audit_write_total"]["LocalFile"]["success"] == 1

        # Cleanup
        AuditMetrics._instance = None

    def test_record_write_failure(self):
        """Test recording failed writes."""
        AuditMetrics._instance = None
        metrics = AuditMetrics.get_instance()
        metrics.reset()

        metrics.record_write("CloudWatch", success=False)

        data = metrics.get_metrics()
        assert data["audit_write_total"]["CloudWatch"]["failure"] == 1

        # Cleanup
        AuditMetrics._instance = None

    def test_record_failure_with_error_type(self):
        """Test recording failures with error types."""
        AuditMetrics._instance = None
        metrics = AuditMetrics.get_instance()
        metrics.reset()

        metrics.record_failure("CloudWatch", "timeout")
        metrics.record_failure("CloudWatch", "connection_error")
        metrics.record_failure("CloudWatch", "timeout")

        data = metrics.get_metrics()
        assert data["audit_failure_total"]["CloudWatch"]["timeout"] == 2
        assert data["audit_failure_total"]["CloudWatch"]["connection_error"] == 1

        # Cleanup
        AuditMetrics._instance = None

    def test_set_circuit_state(self):
        """Test setting circuit state."""
        AuditMetrics._instance = None
        metrics = AuditMetrics.get_instance()
        metrics.reset()

        metrics.set_circuit_state("CloudWatch", "open")

        data = metrics.get_metrics()
        assert data["audit_circuit_state"]["CloudWatch"] == "open"

        # Cleanup
        AuditMetrics._instance = None

    def test_degraded_mode(self):
        """Test degraded mode tracking."""
        AuditMetrics._instance = None
        metrics = AuditMetrics.get_instance()
        metrics.reset()

        assert metrics.is_degraded() is False

        metrics.set_degraded_mode(True)
        assert metrics.is_degraded() is True

        data = metrics.get_metrics()
        assert data["audit_degraded_mode"] == 1
        assert data["audit_degraded_since"] is not None

        metrics.set_degraded_mode(False)
        assert metrics.is_degraded() is False

        # Cleanup
        AuditMetrics._instance = None

    def test_prometheus_format(self):
        """Test Prometheus text format output."""
        AuditMetrics._instance = None
        metrics = AuditMetrics.get_instance()
        metrics.reset()

        metrics.record_write("LocalFile", success=True)
        metrics.set_circuit_state("CloudWatch", "open")

        prometheus_output = metrics.get_prometheus_format()

        assert "audit_write_total" in prometheus_output
        assert 'backend="LocalFile"' in prometheus_output
        assert "audit_circuit_state" in prometheus_output
        assert "audit_degraded_mode" in prometheus_output

        # Cleanup
        AuditMetrics._instance = None

    def test_duration_stats(self):
        """Test duration statistics."""
        AuditMetrics._instance = None
        metrics = AuditMetrics.get_instance()
        metrics.reset()

        metrics.record_write("LocalFile", success=True, duration_ms=10)
        metrics.record_write("LocalFile", success=True, duration_ms=20)
        metrics.record_write("LocalFile", success=True, duration_ms=30)

        data = metrics.get_metrics()
        duration_stats = data["audit_write_duration"]["LocalFile"]

        assert duration_stats["avg_ms"] == 20.0
        assert duration_stats["min_ms"] == 10
        assert duration_stats["max_ms"] == 30
        assert duration_stats["count"] == 3

        # Cleanup
        AuditMetrics._instance = None


class TestSyslogFallback:
    """Tests for SyslogFallback class."""

    def test_is_critical_event(self):
        """Test critical event detection."""
        SyslogFallback._instance = None
        syslog = SyslogFallback.get_instance()

        assert syslog.is_critical_event("security_policy_change") is True
        assert syslog.is_critical_event("authentication_config_change") is True
        assert syslog.is_critical_event("all_backends_failed") is True
        assert syslog.is_critical_event("normal_config_change") is False

        # Cleanup
        SyslogFallback._instance = None

    def test_log_critical_writes_to_stderr(self, monkeypatch):
        """Test that critical events are written to stderr."""
        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        SyslogFallback._instance = None
        syslog = SyslogFallback.get_instance()

        # Capture stderr
        old_stderr = sys.stderr
        sys.stderr = StringIO()

        try:
            result = syslog.log_critical(
                event_type="security_policy_change",
                message="Test critical event",
                config_type="SECURITY",
                user="admin",
            )

            output = sys.stderr.getvalue()

            assert result is True
            assert "AUDIT_CRITICAL" in output
            assert "security_policy_change" in output
            assert "Test critical event" in output

        finally:
            sys.stderr = old_stderr
            SyslogFallback._instance = None

    def test_log_backend_failure(self, monkeypatch):
        """Test logging backend failures."""
        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        SyslogFallback._instance = None
        syslog = SyslogFallback.get_instance()

        old_stderr = sys.stderr
        sys.stderr = StringIO()

        try:
            syslog.log_backend_failure("CloudWatch", "Connection timeout")

            output = sys.stderr.getvalue()
            assert "CloudWatch" in output
            assert "all_backends_failed" in output

        finally:
            sys.stderr = old_stderr
            SyslogFallback._instance = None

    def test_log_circuit_open(self, monkeypatch):
        """Test logging circuit breaker opening."""
        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        SyslogFallback._instance = None
        syslog = SyslogFallback.get_instance()

        old_stderr = sys.stderr
        sys.stderr = StringIO()

        try:
            syslog.log_circuit_open("Datadog")

            output = sys.stderr.getvalue()
            assert "Datadog" in output
            assert "circuit_breaker_open" in output

        finally:
            sys.stderr = old_stderr
            SyslogFallback._instance = None


class TestDegradedModeManager:
    """Tests for DegradedModeManager class."""

    def test_initial_state_not_degraded(self):
        """Test initial state is not degraded."""
        DegradedModeManager._instance = None
        CircuitBreakerRegistry._instance = None
        AuditMetrics._instance = None

        manager = DegradedModeManager.get_instance()

        assert manager.is_degraded is False

        # Cleanup
        DegradedModeManager._instance = None
        CircuitBreakerRegistry._instance = None
        AuditMetrics._instance = None

    def test_enter_degraded_mode(self):
        """Test entering degraded mode."""
        DegradedModeManager._instance = None
        CircuitBreakerRegistry._instance = None
        AuditMetrics._instance = None
        SyslogFallback._instance = None

        # Capture stderr
        old_stderr = sys.stderr
        sys.stderr = StringIO()

        try:
            manager = DegradedModeManager.get_instance()

            manager.enter_degraded_mode("Test reason")

            assert manager.is_degraded is True

            status = manager.get_status()
            assert status["degraded"] is True
            assert status["reason"] == "Test reason"
            assert status["since"] is not None

        finally:
            sys.stderr = old_stderr
            DegradedModeManager._instance = None
            CircuitBreakerRegistry._instance = None
            AuditMetrics._instance = None
            SyslogFallback._instance = None

    def test_exit_degraded_mode(self):
        """Test exiting degraded mode."""
        DegradedModeManager._instance = None
        CircuitBreakerRegistry._instance = None
        AuditMetrics._instance = None
        SyslogFallback._instance = None

        old_stderr = sys.stderr
        sys.stderr = StringIO()

        try:
            manager = DegradedModeManager.get_instance()

            manager.enter_degraded_mode("Test")
            assert manager.is_degraded is True

            manager.exit_degraded_mode()
            assert manager.is_degraded is False

        finally:
            sys.stderr = old_stderr
            DegradedModeManager._instance = None
            CircuitBreakerRegistry._instance = None
            AuditMetrics._instance = None
            SyslogFallback._instance = None

    def test_force_degraded(self):
        """Test forcing degraded mode."""
        DegradedModeManager._instance = None
        CircuitBreakerRegistry._instance = None
        AuditMetrics._instance = None
        SyslogFallback._instance = None

        old_stderr = sys.stderr
        sys.stderr = StringIO()

        try:
            manager = DegradedModeManager.get_instance()

            manager.force_degraded("Manual test")

            status = manager.get_status()
            assert status["degraded"] is True
            assert status["auto_recovery_enabled"] is False

        finally:
            sys.stderr = old_stderr
            DegradedModeManager._instance = None
            CircuitBreakerRegistry._instance = None
            AuditMetrics._instance = None
            SyslogFallback._instance = None

    def test_force_normal(self):
        """Test forcing normal mode."""
        DegradedModeManager._instance = None
        CircuitBreakerRegistry._instance = None
        AuditMetrics._instance = None
        SyslogFallback._instance = None

        old_stderr = sys.stderr
        sys.stderr = StringIO()

        try:
            manager = DegradedModeManager.get_instance()

            manager.force_degraded("Test")
            manager.force_normal()

            status = manager.get_status()
            assert status["degraded"] is False
            assert status["auto_recovery_enabled"] is True

        finally:
            sys.stderr = old_stderr
            DegradedModeManager._instance = None
            CircuitBreakerRegistry._instance = None
            AuditMetrics._instance = None
            SyslogFallback._instance = None


class TestConvenienceFunctions:
    """Tests for convenience functions."""

    def test_get_circuit_breaker(self):
        """Test get_circuit_breaker function."""
        CircuitBreakerRegistry._instance = None

        cb = get_circuit_breaker("test-backend")
        assert cb is not None
        assert cb.name == "test-backend"

        CircuitBreakerRegistry._instance = None

    def test_get_audit_metrics(self):
        """Test get_audit_metrics function."""
        AuditMetrics._instance = None

        metrics = get_audit_metrics()
        assert metrics is not None
        assert isinstance(metrics, AuditMetrics)

        AuditMetrics._instance = None

    def test_get_syslog_fallback(self):
        """Test get_syslog_fallback function."""
        SyslogFallback._instance = None

        syslog = get_syslog_fallback()
        assert syslog is not None
        assert isinstance(syslog, SyslogFallback)

        SyslogFallback._instance = None

    def test_log_critical_to_syslog(self, monkeypatch):
        """Test log_critical_to_syslog function."""
        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        SyslogFallback._instance = None

        old_stderr = sys.stderr
        sys.stderr = StringIO()

        try:
            result = log_critical_to_syslog(
                event_type="test_event",
                message="Test message",
            )
            assert result is True

        finally:
            sys.stderr = old_stderr
            SyslogFallback._instance = None
