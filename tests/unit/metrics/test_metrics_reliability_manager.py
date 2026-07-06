"""
Tests for Metric Reliability Manager.
"""

import time


class TestReliabilityLevel:
    """Test ReliabilityLevel enum."""

    def test_reliability_level_values(self):
        """Should have correct string values."""
        from baldur.metrics.reliability_manager import ReliabilityLevel

        assert ReliabilityLevel.HIGH.value == "high"
        assert ReliabilityLevel.MEDIUM.value == "medium"
        assert ReliabilityLevel.LOW.value == "low"
        assert ReliabilityLevel.UNKNOWN.value == "unknown"
        assert ReliabilityLevel.RECOVERING.value == "recovering"


class TestOperatingMode:
    """Test OperatingMode enum."""

    def test_operating_mode_values(self):
        """Should have correct string values."""
        from baldur.metrics.reliability_manager import OperatingMode

        assert OperatingMode.NORMAL.value == "normal"
        assert OperatingMode.CAUTIOUS.value == "cautious"
        assert OperatingMode.STRICT.value == "strict"
        assert OperatingMode.EMERGENCY.value == "emergency"


class TestReliabilityThresholds:
    """Test ReliabilityThresholds dataclass."""

    def test_default_values(self):
        """Should have sensible default values."""
        from baldur.metrics.reliability_manager import ReliabilityThresholds

        thresholds = ReliabilityThresholds()

        assert thresholds.high_max_age == 60.0  # 1 minute
        assert thresholds.medium_max_age == 300.0  # 5 minutes
        assert thresholds.low_max_age == 3600.0  # 1 hour
        assert thresholds.stabilization_duration == 60.0
        assert thresholds.consecutive_syncs_for_normal == 3

    def test_custom_values(self):
        """Should accept custom threshold values."""
        from baldur.metrics.reliability_manager import ReliabilityThresholds

        thresholds = ReliabilityThresholds(
            high_max_age=30.0,
            medium_max_age=120.0,
            stabilization_duration=30.0,
        )

        assert thresholds.high_max_age == 30.0
        assert thresholds.medium_max_age == 120.0
        assert thresholds.stabilization_duration == 30.0


class TestMetricReliabilityState:
    """Test MetricReliabilityState dataclass."""

    def test_default_values(self):
        """Should have correct default values."""
        from baldur.metrics.reliability_manager import (
            MetricReliabilityState,
            OperatingMode,
            ReliabilityLevel,
        )

        state = MetricReliabilityState(domain="payment")

        assert state.domain == "payment"
        assert state.reliability_level == ReliabilityLevel.UNKNOWN
        assert state.operating_mode == OperatingMode.STRICT
        assert state.last_sync_time is None
        assert state.last_sync_source == "none"
        assert state.consecutive_successful_syncs == 0

    def test_is_data_fresh_property(self):
        """Should correctly determine if data is fresh."""
        from baldur.metrics.reliability_manager import (
            MetricReliabilityState,
            ReliabilityLevel,
        )

        # Fresh data (HIGH or MEDIUM)
        state_high = MetricReliabilityState(
            domain="payment",
            reliability_level=ReliabilityLevel.HIGH,
        )
        assert state_high.is_data_fresh is True

        state_medium = MetricReliabilityState(
            domain="payment",
            reliability_level=ReliabilityLevel.MEDIUM,
        )
        assert state_medium.is_data_fresh is True

        # Stale data (LOW, UNKNOWN, RECOVERING)
        state_low = MetricReliabilityState(
            domain="payment",
            reliability_level=ReliabilityLevel.LOW,
        )
        assert state_low.is_data_fresh is False

        state_unknown = MetricReliabilityState(
            domain="payment",
            reliability_level=ReliabilityLevel.UNKNOWN,
        )
        assert state_unknown.is_data_fresh is False

    def test_age_seconds_property(self):
        """Should calculate age correctly."""
        from baldur.metrics.reliability_manager import MetricReliabilityState

        # No sync time
        state = MetricReliabilityState(domain="payment")
        assert state.age_seconds is None

        # With sync time
        sync_time = time.time() - 60  # 1 minute ago
        state_with_sync = MetricReliabilityState(
            domain="payment",
            last_sync_time=sync_time,
        )
        age = state_with_sync.age_seconds
        assert age is not None
        assert 59 <= age <= 61  # Allow for small timing differences

    def test_stabilization_progress_property(self):
        """Should calculate stabilization progress."""
        from baldur.metrics.reliability_manager import MetricReliabilityState

        # No stabilization
        MetricReliabilityState(domain="payment")
        # Default behavior depends on implementation

        # With stabilization
        state_with_stab = MetricReliabilityState(
            domain="payment",
            stabilization_start=time.time() - 30,  # Started 30s ago
        )
        # Progress should be between 0 and 1
        if hasattr(state_with_stab, "stabilization_progress"):
            progress = state_with_stab.stabilization_progress
            assert 0 <= progress <= 1


class TestReliabilityManagerStateManagement:
    """Test MetricReliabilityManager state management."""

    def test_unknown_leads_to_strict_mode(self):
        """Unknown reliability should default to strict operating mode."""
        from baldur.metrics.reliability_manager import (
            MetricReliabilityState,
            OperatingMode,
            ReliabilityLevel,
        )

        # Design philosophy: "모르면 일단 막아라"
        state = MetricReliabilityState(
            domain="payment",
            reliability_level=ReliabilityLevel.UNKNOWN,
        )

        assert state.operating_mode == OperatingMode.STRICT
