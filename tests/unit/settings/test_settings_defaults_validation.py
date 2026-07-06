"""
Tests for Settings Modules - Defaults and Validation.

신규 Settings 모듈들의 기본값, 환경변수 오버라이드, 유효성 검증 테스트:
- StressTestSettings
- CleanupSettings
- PrecomputedCacheSettings
- BackoffSettings
- PoolMonitorSettings
- RegionalEmergencySettings
- CanarySettings
- CanaryWatchdogSettings
- ChaosSafetyCapsSettings
- JitterSettings
- GateFaultSettings
- GracefulDegradationSettings
- RingBufferSettings
"""

import pytest
from pydantic import ValidationError

# =============================================================================
# StressTestSettings Tests
# =============================================================================


class TestStressTestSettings:
    """Tests for StressTestSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.stress_test import reset_stress_test_settings

        reset_stress_test_settings()
        yield
        reset_stress_test_settings()

    def test_default_values(self):
        """기본값이 stress_test_service.py와 일치하는지 검증."""
        from baldur.settings.stress_test import StressTestSettings

        settings = StressTestSettings()

        assert settings.default_lock_timeout_ms == 1
        assert settings.max_burst_duration_seconds == 30
        assert settings.max_concurrent_locks == 100
        assert settings.default_leak_hold_seconds == 30
        assert settings.inter_request_sleep_ms == 10

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.stress_test import StressTestSettings

        monkeypatch.setenv("BALDUR_STRESS_TEST_DEFAULT_LOCK_TIMEOUT_MS", "5")
        monkeypatch.setenv("BALDUR_STRESS_TEST_MAX_BURST_DURATION_SECONDS", "60")

        settings = StressTestSettings()

        assert settings.default_lock_timeout_ms == 5
        assert settings.max_burst_duration_seconds == 60

    def test_validation_min_lock_timeout(self):
        """lock_timeout_ms 최소값(1) 검증."""
        from baldur.settings.stress_test import StressTestSettings

        with pytest.raises(ValidationError):
            StressTestSettings(default_lock_timeout_ms=0)

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.stress_test import (
            get_stress_test_settings,
        )

        settings1 = get_stress_test_settings()
        settings2 = get_stress_test_settings()

        assert settings1 is settings2


# =============================================================================
# CleanupSettings Tests
# =============================================================================


class TestCleanupSettings:
    """Tests for CleanupSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.cleanup import reset_cleanup_settings

        reset_cleanup_settings()
        yield
        reset_cleanup_settings()

    def test_default_values(self):
        """기본값이 cleanup_service.py와 일치하는지 검증."""
        from baldur.settings.cleanup import CleanupSettings

        settings = CleanupSettings()

        assert settings.archive_older_than_days == 30
        assert settings.expired_config_hours == 24
        assert settings.approval_expiry_hours == 72
        assert settings.purge_older_than_days == 90

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.cleanup import CleanupSettings

        monkeypatch.setenv("BALDUR_CLEANUP_ARCHIVE_OLDER_THAN_DAYS", "60")
        monkeypatch.setenv("BALDUR_CLEANUP_PURGE_OLDER_THAN_DAYS", "180")

        settings = CleanupSettings()

        assert settings.archive_older_than_days == 60
        assert settings.purge_older_than_days == 180

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.cleanup import get_cleanup_settings

        settings1 = get_cleanup_settings()
        settings2 = get_cleanup_settings()

        assert settings1 is settings2


# =============================================================================
# PrecomputedCacheSettings Tests
# =============================================================================


class TestPrecomputedCacheSettings:
    """Tests for PrecomputedCacheSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.precomputed_cache import (
            reset_precomputed_cache_settings,
        )

        reset_precomputed_cache_settings()
        yield
        reset_precomputed_cache_settings()

    def test_default_values(self):
        """기본값이 precomputed_cache.py와 일치하는지 검증."""
        from baldur.settings.precomputed_cache import PrecomputedCacheSettings

        settings = PrecomputedCacheSettings()

        assert settings.l1_ttl_seconds == 2.0
        assert settings.l1_maxsize == 100
        assert settings.l2_ttl_seconds == 15.0
        assert settings.refresh_interval_seconds == 10.0

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.precomputed_cache import (
            get_precomputed_cache_settings,
        )

        settings1 = get_precomputed_cache_settings()
        settings2 = get_precomputed_cache_settings()

        assert settings1 is settings2


# =============================================================================
# BackoffSettings Tests
# =============================================================================


class TestBackoffSettings:
    """Tests for BackoffSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.backoff import reset_backoff_settings

        reset_backoff_settings()
        yield
        reset_backoff_settings()

    def test_default_values(self):
        """기본값이 core/backoff.py와 일치하는지 검증."""
        from baldur.settings.backoff import BackoffSettings

        settings = BackoffSettings()

        # Exponential
        assert settings.exponential_base_delay == 1.0
        assert settings.exponential_max_delay == 60.0
        assert settings.exponential_multiplier == 2.0
        assert settings.exponential_jitter_factor == 0.2

        # Linear
        assert settings.linear_base_delay == 1.0
        assert settings.linear_increment == 1.0
        assert settings.linear_max_delay == 60.0
        assert settings.linear_jitter_factor == 0.1

        # Constant
        assert settings.constant_delay == 5.0
        assert settings.constant_jitter_factor == 0.1

        # Legacy
        assert settings.legacy_base == 4
        assert settings.legacy_jitter_percent == 25

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.backoff import get_backoff_settings

        settings1 = get_backoff_settings()
        settings2 = get_backoff_settings()

        assert settings1 is settings2


# =============================================================================
# PoolMonitorSettings Tests
# =============================================================================


class TestPoolMonitorSettings:
    """Tests for PoolMonitorSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.pool_monitor import reset_pool_monitor_settings

        reset_pool_monitor_settings()
        yield
        reset_pool_monitor_settings()

    def test_default_values(self):
        """기본값이 core/pool_monitor.py와 일치하는지 검증."""
        from baldur.settings.pool_monitor import PoolMonitorSettings

        settings = PoolMonitorSettings()

        assert settings.warning_threshold == 70.0
        assert settings.critical_threshold == 90.0
        assert settings.leak_threshold_seconds == 300.0
        assert settings.max_history == 5000

    def test_threshold_validation(self):
        """warning_threshold < critical_threshold 검증."""
        from baldur.settings.pool_monitor import PoolMonitorSettings

        # Valid: warning < critical
        settings = PoolMonitorSettings(warning_threshold=60.0, critical_threshold=80.0)
        assert settings.warning_threshold == 60.0

        # Invalid: warning >= critical
        with pytest.raises(ValidationError):
            PoolMonitorSettings(warning_threshold=90.0, critical_threshold=80.0)

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.pool_monitor import get_pool_monitor_settings

        settings1 = get_pool_monitor_settings()
        settings2 = get_pool_monitor_settings()

        assert settings1 is settings2


# =============================================================================
# RegionalEmergencySettings Tests
# =============================================================================


class TestRegionalEmergencySettings:
    """Tests for RegionalEmergencySettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.regional_emergency import (
            reset_regional_emergency_settings,
        )

        reset_regional_emergency_settings()
        yield
        reset_regional_emergency_settings()

    def test_default_values(self):
        """기본값이 regional_emergency/*.py와 일치하는지 검증."""
        from baldur.settings.regional_emergency import RegionalEmergencySettings

        settings = RegionalEmergencySettings()

        assert settings.escalation_threshold == 2
        assert settings.cascade_window_minutes == 30
        assert settings.expiry_hours == 8
        assert settings.cache_ttl_seconds == 30.0
        assert settings.max_buffer_size == 1000

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.regional_emergency import (
            get_regional_emergency_settings,
        )

        settings1 = get_regional_emergency_settings()
        settings2 = get_regional_emergency_settings()

        assert settings1 is settings2


# =============================================================================
# CanarySettings Tests
# =============================================================================


class TestCanarySettings:
    """Tests for CanarySettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.canary import reset_canary_settings

        reset_canary_settings()
        yield
        reset_canary_settings()

    def test_default_values(self):
        """기본값이 canary/*.py와 일치하는지 검증."""
        from baldur.settings.canary import CanarySettings

        settings = CanarySettings()

        assert settings.rollout_ttl_days == 7
        assert settings.lock_timeout_minutes == 30
        assert settings.default_expiry_hours == 24

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.canary import get_canary_settings

        settings1 = get_canary_settings()
        settings2 = get_canary_settings()

        assert settings1 is settings2


# =============================================================================
# CanaryWatchdogSettings Tests
# =============================================================================


class TestCanaryWatchdogSettings:
    """Tests for CanaryWatchdogSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.canary_watchdog import reset_canary_watchdog_settings

        reset_canary_watchdog_settings()
        yield
        reset_canary_watchdog_settings()

    def test_default_values(self):
        """기본값이 canary_watchdog.py와 일치하는지 검증."""
        from baldur.settings.canary_watchdog import CanaryWatchdogSettings

        settings = CanaryWatchdogSettings()

        assert settings.zombie_threshold_minutes == 30
        assert settings.auto_rollback_after_minutes == 60
        assert settings.max_stage_duration_minutes == 15
        assert settings.enable_auto_promote is True
        assert settings.enable_auto_rollback is True
        assert settings.notification_enabled is True
        assert settings.slack_channel == "#baldur-alerts"

    def test_timing_validation(self):
        """auto_rollback_after_minutes > zombie_threshold_minutes 검증."""
        from baldur.settings.canary_watchdog import CanaryWatchdogSettings

        # Valid: auto_rollback > zombie_threshold
        settings = CanaryWatchdogSettings(
            zombie_threshold_minutes=30, auto_rollback_after_minutes=60
        )
        assert settings.zombie_threshold_minutes == 30

        # Invalid: auto_rollback <= zombie_threshold
        with pytest.raises(ValidationError):
            CanaryWatchdogSettings(
                zombie_threshold_minutes=60, auto_rollback_after_minutes=30
            )

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.canary_watchdog import get_canary_watchdog_settings

        settings1 = get_canary_watchdog_settings()
        settings2 = get_canary_watchdog_settings()

        assert settings1 is settings2


# =============================================================================
# JitterSettings Tests
# =============================================================================


class TestJitterSettings:
    """Tests for JitterSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.jitter import reset_jitter_settings

        reset_jitter_settings()
        yield
        reset_jitter_settings()

    def test_default_values(self):
        """기본값이 utils/jitter.py와 일치하는지 검증."""
        from baldur.settings.jitter import JitterSettings

        settings = JitterSettings()

        assert settings.max_delay_seconds == 60.0
        assert settings.min_delay_seconds == 0.0
        assert settings.startup_max_delay_seconds == 30.0
        assert settings.enabled is True

    def test_delay_range_validation(self):
        """min_delay <= max_delay 검증."""
        from baldur.settings.jitter import JitterSettings

        # Valid: min < max
        settings = JitterSettings(min_delay_seconds=10.0, max_delay_seconds=60.0)
        assert settings.min_delay_seconds == 10.0

        # Invalid: min > max
        with pytest.raises(ValidationError):
            JitterSettings(min_delay_seconds=60.0, max_delay_seconds=30.0)

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.jitter import get_jitter_settings

        settings1 = get_jitter_settings()
        settings2 = get_jitter_settings()

        assert settings1 is settings2


# =============================================================================
# GateFaultSettings Tests
# =============================================================================


class TestGateFaultSettings:
    """Tests for GateFaultSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.gate_fault import reset_gate_fault_settings

        reset_gate_fault_settings()
        yield
        reset_gate_fault_settings()

    def test_default_values(self):
        """기본값이 fault_detector.py와 일치하는지 검증."""
        from baldur.settings.gate_fault import GateFaultSettings

        settings = GateFaultSettings()

        assert settings.failure_threshold == 5
        assert settings.recovery_timeout_seconds == 30

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.gate_fault import GateFaultSettings

        monkeypatch.setenv("BALDUR_GATE_FAULT_FAILURE_THRESHOLD", "10")
        monkeypatch.setenv("BALDUR_GATE_FAULT_RECOVERY_TIMEOUT_SECONDS", "60")

        settings = GateFaultSettings()

        assert settings.failure_threshold == 10
        assert settings.recovery_timeout_seconds == 60

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.gate_fault import get_gate_fault_settings

        settings1 = get_gate_fault_settings()
        settings2 = get_gate_fault_settings()

        assert settings1 is settings2


# =============================================================================
# GracefulDegradationSettings Tests
# =============================================================================


class TestGracefulDegradationSettings:
    """Tests for GracefulDegradationSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.graceful_degradation import (
            reset_graceful_degradation_settings,
        )

        reset_graceful_degradation_settings()
        yield
        reset_graceful_degradation_settings()

    def test_default_values(self):
        """기본값이 graceful_degradation/enums.py와 일치하는지 검증."""
        from baldur.settings.graceful_degradation import (
            GracefulDegradationSettings,
        )

        settings = GracefulDegradationSettings()

        # FallbackConfig
        assert settings.redis_timeout_seconds == 5.0
        assert settings.replica_timeout_seconds == 3.0
        assert settings.memory_max_entries == 10000
        assert settings.key_prefix == "baldur:"

        # CircuitBreakerConfig
        assert settings.cb_failure_threshold == 5
        assert settings.cb_recovery_timeout_seconds == 30.0
        assert settings.cb_half_open_requests == 3
        assert settings.cb_success_threshold == 2

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.graceful_degradation import (
            get_graceful_degradation_settings,
        )

        settings1 = get_graceful_degradation_settings()
        settings2 = get_graceful_degradation_settings()

        assert settings1 is settings2


# =============================================================================
# RingBufferSettings Tests
# =============================================================================


class TestRingBufferSettings:
    """Tests for RingBufferSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.ring_buffer import reset_ring_buffer_settings

        reset_ring_buffer_settings()
        yield
        reset_ring_buffer_settings()

    def test_default_values(self):
        """기본값이 ring_buffer.py와 일치하는지 검증."""
        from baldur.settings.ring_buffer import RingBufferSettings

        settings = RingBufferSettings()

        assert settings.capacity == 10000
        assert settings.batch_max_size == 100
        assert settings.strategy == "drop_oldest"

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.ring_buffer import RingBufferSettings

        monkeypatch.setenv("BALDUR_RING_BUFFER_CAPACITY", "50000")
        monkeypatch.setenv("BALDUR_RING_BUFFER_STRATEGY", "drop_newest")

        settings = RingBufferSettings()

        assert settings.capacity == 50000
        assert settings.strategy == "drop_newest"

    def test_strategy_validation(self):
        """strategy가 유효한 값인지 검증."""
        from baldur.settings.ring_buffer import RingBufferSettings

        # Valid strategies
        settings1 = RingBufferSettings(strategy="drop_oldest")
        assert settings1.strategy == "drop_oldest"

        settings2 = RingBufferSettings(strategy="drop_newest")
        assert settings2.strategy == "drop_newest"

        # Invalid strategy
        with pytest.raises(ValidationError):
            RingBufferSettings(strategy="invalid")

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.ring_buffer import get_ring_buffer_settings

        settings1 = get_ring_buffer_settings()
        settings2 = get_ring_buffer_settings()

        assert settings1 is settings2


# =============================================================================
# AuditSyncSettings Tests (668 D1: cursor_stall_alert_cycles)
# =============================================================================


class TestAuditSyncSettings:
    """Tests for AuditSyncSettings cursor-stall-alert field (668 D1)."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.audit_sync import reset_audit_sync_settings

        reset_audit_sync_settings()
        yield
        reset_audit_sync_settings()

    def test_cursor_stall_alert_cycles_default(self):
        """Default matches SyncWorkerConfig.cursor_stall_alert_cycles (5)."""
        from baldur.settings.audit_sync import AuditSyncSettings

        settings = AuditSyncSettings()

        assert settings.cursor_stall_alert_cycles == 5

    def test_cursor_stall_alert_cycles_accepts_inclusive_bounds(self):
        """The ge=1 / le=1000 bounds are inclusive (just-inside passes)."""
        from baldur.settings.audit_sync import AuditSyncSettings

        assert (
            AuditSyncSettings(cursor_stall_alert_cycles=1).cursor_stall_alert_cycles
            == 1
        )
        assert (
            AuditSyncSettings(cursor_stall_alert_cycles=1000).cursor_stall_alert_cycles
            == 1000
        )

    def test_cursor_stall_alert_cycles_rejects_below_min(self):
        """0 (just below ge=1) is rejected."""
        from baldur.settings.audit_sync import AuditSyncSettings

        with pytest.raises(ValidationError):
            AuditSyncSettings(cursor_stall_alert_cycles=0)

    def test_cursor_stall_alert_cycles_rejects_above_max(self):
        """1001 (just above le=1000) is rejected."""
        from baldur.settings.audit_sync import AuditSyncSettings

        with pytest.raises(ValidationError):
            AuditSyncSettings(cursor_stall_alert_cycles=1001)

    def test_cursor_stall_alert_cycles_env_override(self, monkeypatch):
        """BALDUR_AUDIT_SYNC_CURSOR_STALL_ALERT_CYCLES overrides the default."""
        from baldur.settings.audit_sync import AuditSyncSettings

        monkeypatch.setenv("BALDUR_AUDIT_SYNC_CURSOR_STALL_ALERT_CYCLES", "10")

        assert AuditSyncSettings().cursor_stall_alert_cycles == 10

    def test_singleton_pattern(self):
        """get_audit_sync_settings returns a cached singleton."""
        from baldur.settings.audit_sync import get_audit_sync_settings

        assert get_audit_sync_settings() is get_audit_sync_settings()
