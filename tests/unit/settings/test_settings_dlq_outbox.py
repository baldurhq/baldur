"""DLQOutboxSettings unit tests (impl doc 486 D3).

Test targets:
    - baldur.settings.dlq_outbox.DLQOutboxSettings (Pydantic v2)
    - get_dlq_outbox_settings / reset_dlq_outbox_settings singleton pair

Test Categories:
    A. Contract — design defaults from impl doc 486 D3
    B. Behavior — boundary validation (ge/le constraints)
    C. Behavior — singleton pair (services_group cached_property)
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings.dlq_outbox import (
    DLQOutboxSettings,
    get_dlq_outbox_settings,
    reset_dlq_outbox_settings,
)

# =============================================================================
# A. Contract — defaults from impl doc 486 D3
# =============================================================================


class TestDLQOutboxSettingsContract:
    """Default values declared in impl doc 486 D3."""

    def test_enabled_default_is_true(self):
        """Plan 2026-05-08: async-default flip — enabled=True."""
        s = DLQOutboxSettings()
        assert s.enabled is True

    def test_capacity_default(self):
        s = DLQOutboxSettings()
        assert s.capacity == 10_000

    def test_batch_size_default(self):
        s = DLQOutboxSettings()
        assert s.batch_size == 50

    def test_flush_interval_seconds_default(self):
        s = DLQOutboxSettings()
        assert s.flush_interval_seconds == 0.1

    def test_drop_rate_threshold_default(self):
        s = DLQOutboxSettings()
        assert s.drop_rate_threshold == 0.01

    def test_join_timeout_seconds_default(self):
        s = DLQOutboxSettings()
        assert s.join_timeout_seconds == 5.0

    def test_durable_default_is_false(self):
        """OSS default — DiskPersistentBuffer is PRO opt-in."""
        s = DLQOutboxSettings()
        assert s.durable is False


# =============================================================================
# B. Behavior — boundary validation
# =============================================================================


class TestDLQOutboxCapacityBoundaryBehavior:
    """``capacity`` field constraints: ge=100, le=1_000_000."""

    def test_minimum_boundary_accepted(self):
        DLQOutboxSettings(capacity=100)

    def test_below_minimum_rejected(self):
        with pytest.raises(ValidationError):
            DLQOutboxSettings(capacity=99)

    def test_maximum_boundary_accepted(self):
        DLQOutboxSettings(capacity=1_000_000)

    def test_above_maximum_rejected(self):
        with pytest.raises(ValidationError):
            DLQOutboxSettings(capacity=1_000_001)


class TestDLQOutboxBatchSizeBoundaryBehavior:
    """``batch_size`` field constraints: ge=1, le=10_000."""

    def test_minimum_boundary_accepted(self):
        DLQOutboxSettings(batch_size=1)

    def test_below_minimum_rejected(self):
        with pytest.raises(ValidationError):
            DLQOutboxSettings(batch_size=0)

    def test_maximum_boundary_accepted(self):
        DLQOutboxSettings(batch_size=10_000)

    def test_above_maximum_rejected(self):
        with pytest.raises(ValidationError):
            DLQOutboxSettings(batch_size=10_001)


class TestDLQOutboxFlushIntervalBoundaryBehavior:
    """``flush_interval_seconds`` field constraints: ge=0.01, le=60.0."""

    def test_minimum_boundary_accepted(self):
        DLQOutboxSettings(flush_interval_seconds=0.01)

    def test_below_minimum_rejected(self):
        with pytest.raises(ValidationError):
            DLQOutboxSettings(flush_interval_seconds=0.001)

    def test_maximum_boundary_accepted(self):
        DLQOutboxSettings(flush_interval_seconds=60.0)

    def test_above_maximum_rejected(self):
        with pytest.raises(ValidationError):
            DLQOutboxSettings(flush_interval_seconds=60.1)


class TestDLQOutboxDropRateThresholdBoundaryBehavior:
    """``drop_rate_threshold`` field constraints: ge=0.0, le=1.0."""

    def test_minimum_boundary_accepted(self):
        DLQOutboxSettings(drop_rate_threshold=0.0)

    def test_below_minimum_rejected(self):
        with pytest.raises(ValidationError):
            DLQOutboxSettings(drop_rate_threshold=-0.01)

    def test_maximum_boundary_accepted(self):
        DLQOutboxSettings(drop_rate_threshold=1.0)

    def test_above_maximum_rejected(self):
        with pytest.raises(ValidationError):
            DLQOutboxSettings(drop_rate_threshold=1.01)


class TestDLQOutboxJoinTimeoutBoundaryBehavior:
    """``join_timeout_seconds`` field constraints: ge=0.1, le=60.0."""

    def test_minimum_boundary_accepted(self):
        DLQOutboxSettings(join_timeout_seconds=0.1)

    def test_below_minimum_rejected(self):
        with pytest.raises(ValidationError):
            DLQOutboxSettings(join_timeout_seconds=0.05)

    def test_maximum_boundary_accepted(self):
        DLQOutboxSettings(join_timeout_seconds=60.0)

    def test_above_maximum_rejected(self):
        with pytest.raises(ValidationError):
            DLQOutboxSettings(join_timeout_seconds=60.1)


# =============================================================================
# C. Behavior — singleton pair (services_group cached_property)
# =============================================================================


class TestDLQOutboxSettingsSingletonBehavior:
    """``get_dlq_outbox_settings`` / ``reset_dlq_outbox_settings`` pair."""

    def test_get_returns_settings_instance(self):
        s = get_dlq_outbox_settings()
        assert isinstance(s, DLQOutboxSettings)

    def test_get_is_cached_within_services_group(self):
        s1 = get_dlq_outbox_settings()
        s2 = get_dlq_outbox_settings()
        assert s1 is s2

    def test_reset_invalidates_cache(self):
        s1 = get_dlq_outbox_settings()
        reset_dlq_outbox_settings()
        s2 = get_dlq_outbox_settings()
        # New instance after reset
        assert s2 is not s1

    def test_reset_is_idempotent(self):
        # First reset
        reset_dlq_outbox_settings()
        # Second reset on absent key must not raise
        reset_dlq_outbox_settings()
