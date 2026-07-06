"""
368 Django Settings Decoupling — new settings fields unit tests.

Tests for fields added to SecuritySettings, AuditSettings, StressTestSettings,
MetricsSettings, CircuitBreakerSettings, and the new CascadeSettings class.

Test Categories:
    A. Contract: Default values, cross-field validation boundaries
    B. Behavior: Singleton lifecycle, env var binding
"""

import pytest
from pydantic import ValidationError

from baldur.settings.audit import (
    AuditSettings,
    get_audit_settings,
    reset_audit_settings,
)
from baldur.settings.cascade import (
    CascadeSettings,
    get_cascade_settings,
    reset_cascade_settings,
)
from baldur.settings.circuit_breaker import CircuitBreakerSettings
from baldur.settings.metrics import MetricsSettings
from baldur.settings.security import SecuritySettings
from baldur.settings.stress_test import StressTestSettings

# =============================================================================
# A. Contract Tests
# =============================================================================


class TestSecuritySettingsNewFieldsContract:
    """SecuritySettings 368 fields: session_engine, session_cookie_age."""

    def test_session_engine_default(self):
        """session_engine default is django.contrib.sessions.backends.db."""
        s = SecuritySettings()
        assert s.session_engine == "django.contrib.sessions.backends.db"

    def test_session_cookie_age_default(self):
        """session_cookie_age default is 1209600 (14 days)."""
        s = SecuritySettings()
        assert s.session_cookie_age == 1209600


class TestAuditSettingsNewFieldsContract:
    """AuditSettings 368 fields: read_paths + backpressure fields."""

    def test_read_paths_default_empty(self):
        """read_paths default is empty list."""
        s = AuditSettings()
        assert s.read_paths == []

    def test_load_shedding_enabled_default_true(self):
        """load_shedding_enabled default is True."""
        s = AuditSettings()
        assert s.load_shedding_enabled is True

    def test_buffer_warning_threshold_default(self):
        """buffer_warning_threshold default is 0.7."""
        s = AuditSettings()
        assert s.buffer_warning_threshold == 0.7

    def test_buffer_critical_threshold_default(self):
        """buffer_critical_threshold default is 0.9."""
        s = AuditSettings()
        assert s.buffer_critical_threshold == 0.9

    def test_max_events_per_second_default(self):
        """max_events_per_second default is 1000."""
        s = AuditSettings()
        assert s.max_events_per_second == 1000

    def test_fallback_enabled_default_true(self):
        """fallback_enabled default is True."""
        s = AuditSettings()
        assert s.fallback_enabled is True

    def test_metrics_enabled_default_true(self):
        """metrics_enabled default is True."""
        s = AuditSettings()
        assert s.metrics_enabled is True


class TestAuditSettingsCrossFieldValidationContract:
    """AuditSettings buffer threshold cross-field validation."""

    def test_warning_ge_critical_raises_validation_error(self):
        """buffer_warning_threshold >= buffer_critical_threshold raises ValueError."""
        with pytest.raises(ValidationError):
            AuditSettings(buffer_warning_threshold=0.9, buffer_critical_threshold=0.7)

    def test_equal_thresholds_raises_validation_error(self):
        """Equal warning and critical thresholds raise ValueError."""
        with pytest.raises(ValidationError):
            AuditSettings(buffer_warning_threshold=0.8, buffer_critical_threshold=0.8)

    def test_valid_thresholds_accepted(self):
        """Valid threshold ordering is accepted."""
        s = AuditSettings(buffer_warning_threshold=0.5, buffer_critical_threshold=0.8)
        assert s.buffer_warning_threshold == 0.5
        assert s.buffer_critical_threshold == 0.8


class TestStressTestSettingsNewFieldContract:
    """StressTestSettings 368 field: table."""

    def test_table_default(self):
        """table default is 'baldur_failedoperation'."""
        s = StressTestSettings()
        assert s.table == "baldur_failedoperation"


class TestMetricsSettingsNewFieldContract:
    """MetricsSettings 368 field: snapshot_dir."""

    def test_snapshot_dir_default_none(self):
        """snapshot_dir default is None."""
        s = MetricsSettings()
        assert s.snapshot_dir is None


class TestCircuitBreakerSettingsNewFieldContract:
    """CircuitBreakerSettings 368 field: monitored_services."""

    def test_monitored_services_default_empty(self):
        """monitored_services default is empty list."""
        s = CircuitBreakerSettings()
        assert s.monitored_services == []

    def test_cluster_state_propagation_enabled_default_false(self):
        """656 D5: cluster-wide CB state propagation flag defaults off.

        Default-off keeps the admission read path L1-only (no Redis I/O); the
        active peer-side listener is PRO-tier. A default-True here would be an
        claim-wiring false guarantee (the OSS no-op cannot deliver propagation).
        """
        s = CircuitBreakerSettings()
        assert s.cluster_state_propagation_enabled is False


class TestCascadeSettingsContract:
    """CascadeSettings default values and cross-field validation."""

    def test_max_depth_default(self):
        """max_depth default is 10."""
        s = CascadeSettings()
        assert s.max_depth == 10

    def test_warn_depth_default(self):
        """warn_depth default is 7."""
        s = CascadeSettings()
        assert s.warn_depth == 7

    def test_block_on_exceed_default_true(self):
        """block_on_exceed default is True."""
        s = CascadeSettings()
        assert s.block_on_exceed is True

    def test_detect_cycles_default_true(self):
        """detect_cycles default is True."""
        s = CascadeSettings()
        assert s.detect_cycles is True

    def test_warn_depth_ge_max_depth_raises(self):
        """warn_depth >= max_depth raises ValueError."""
        with pytest.raises(ValidationError):
            CascadeSettings(max_depth=5, warn_depth=5)

    def test_warn_depth_gt_max_depth_raises(self):
        """warn_depth > max_depth raises ValueError."""
        with pytest.raises(ValidationError):
            CascadeSettings(max_depth=5, warn_depth=8)

    def test_valid_depth_ordering_accepted(self):
        """Valid warn_depth < max_depth is accepted."""
        s = CascadeSettings(max_depth=10, warn_depth=3)
        assert s.warn_depth == 3
        assert s.max_depth == 10


# =============================================================================
# B. Behavior Tests
# =============================================================================


class TestCascadeSettingsBoundaryBehavior:
    """CascadeSettings field boundary validation."""

    def test_max_depth_minimum_boundary(self):
        """max_depth ge=1: value 0 rejected, value 1 accepted."""
        with pytest.raises(ValidationError):
            CascadeSettings(max_depth=0, warn_depth=0)
        # min valid: max_depth=2, warn_depth=1
        s = CascadeSettings(max_depth=2, warn_depth=1)
        assert s.max_depth == 2

    def test_max_depth_maximum_boundary(self):
        """max_depth le=100: value 100 accepted, 101 rejected."""
        s = CascadeSettings(max_depth=100, warn_depth=50)
        assert s.max_depth == 100
        with pytest.raises(ValidationError):
            CascadeSettings(max_depth=101, warn_depth=50)


class TestCascadeSettingsSingletonBehavior:
    """CascadeSettings singleton lifecycle."""

    def test_get_returns_instance(self):
        """get_cascade_settings returns a CascadeSettings instance."""
        reset_cascade_settings()
        s = get_cascade_settings()
        assert isinstance(s, CascadeSettings)

    def test_reset_clears_cache(self):
        """reset_cascade_settings allows new instance creation."""
        get_cascade_settings()
        reset_cascade_settings()
        s2 = get_cascade_settings()
        # Both should be CascadeSettings; identity may differ after reset
        assert isinstance(s2, CascadeSettings)


class TestAuditSettingsSingletonBehavior:
    """AuditSettings singleton lifecycle for new fields."""

    def test_get_returns_instance_with_new_fields(self):
        """get_audit_settings returns instance with backpressure fields."""
        reset_audit_settings()
        s = get_audit_settings()
        assert hasattr(s, "load_shedding_enabled")
        assert hasattr(s, "buffer_warning_threshold")
        assert hasattr(s, "read_paths")
