"""
Tests for RateController external level bridge (#413 PX4).

Covers:
- _handle_throttle_sla_critical: sets BackpressureLevel.HIGH + TTL
- _adjust_rate: max(queue, external) policy
- TTL expiry resets external level to NONE
- _subscribe_throttle_sla_events: fail-open on ImportError
- BackpressureSettings.external_level_ttl_seconds default and bounds
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from baldur.scaling.config import (
    BackpressureLevel,
    BackpressureSettings,
)
from baldur.scaling.rate_controller import (
    RateController,
)

# =============================================================================
# Contract: BackpressureSettings.external_level_ttl_seconds
# =============================================================================


class TestExternalLevelTTLSettingsContract:
    """external_level_ttl_seconds design contract values."""

    def test_default_is_30(self):
        """Default TTL is 30.0 seconds."""
        settings = BackpressureSettings()
        assert settings.external_level_ttl_seconds == 30.0

    def test_minimum_boundary_rejects_below_5(self):
        """Values below 5.0 are rejected by Pydantic validation."""
        with pytest.raises(Exception):
            BackpressureSettings(external_level_ttl_seconds=4.9)

    def test_maximum_boundary_rejects_above_120(self):
        """Values above 120.0 are rejected by Pydantic validation."""
        with pytest.raises(Exception):
            BackpressureSettings(external_level_ttl_seconds=120.1)


# =============================================================================
# Behavior: _handle_throttle_sla_critical
# =============================================================================


class TestHandleThrottleSlaCriticalBehavior:
    """_handle_throttle_sla_critical sets external level with TTL."""

    def _make_controller(self, **kwargs) -> RateController:
        settings = BackpressureSettings(**kwargs)
        return RateController(settings=settings)

    def test_sets_external_level_to_high(self):
        """Event sets _external_level to BackpressureLevel.HIGH."""
        ctrl = self._make_controller()
        event = MagicMock()

        ctrl._handle_throttle_sla_critical(event)

        assert ctrl._external_level == BackpressureLevel.HIGH

    def test_sets_ttl_based_on_settings(self):
        """Event sets _external_level_until based on external_level_ttl_seconds."""
        ctrl = self._make_controller(external_level_ttl_seconds=60.0)
        before = time.time()
        event = MagicMock()

        ctrl._handle_throttle_sla_critical(event)

        assert ctrl._external_level_until >= before + 60.0
        assert ctrl._external_level_until <= time.time() + 60.0

    def test_repeated_events_renew_ttl(self):
        """Each event reception renews the TTL (lease pattern)."""
        ctrl = self._make_controller(external_level_ttl_seconds=30.0)
        event = MagicMock()

        ctrl._handle_throttle_sla_critical(event)
        first_until = ctrl._external_level_until

        # Simulate time passing
        ctrl._handle_throttle_sla_critical(event)
        second_until = ctrl._external_level_until

        assert second_until >= first_until


# =============================================================================
# Behavior: _adjust_rate with external level
# =============================================================================


class TestAdjustRateExternalLevelBehavior:
    """_adjust_rate uses max(queue_level, external_level) policy."""

    def _make_controller(self, queue_size: int = 0, **kwargs) -> RateController:
        settings = BackpressureSettings(**kwargs)
        return RateController(
            settings=settings,
            queue_size_provider=lambda: queue_size,
        )

    def test_external_level_overrides_low_queue(self):
        """External HIGH overrides queue-based NONE when queue is empty."""
        ctrl = self._make_controller(queue_size=0)

        # Set external level
        ctrl._external_level = BackpressureLevel.HIGH
        ctrl._external_level_until = time.time() + 60

        ctrl._adjust_rate()

        # max(NONE, HIGH) = HIGH
        assert ctrl._level == BackpressureLevel.HIGH

    def test_queue_level_overrides_when_higher(self):
        """Queue CRITICAL overrides external HIGH."""
        ctrl = self._make_controller(queue_size=99999)

        ctrl._external_level = BackpressureLevel.HIGH
        ctrl._external_level_until = time.time() + 60

        ctrl._adjust_rate()

        # max(CRITICAL, HIGH) = CRITICAL
        assert ctrl._level == BackpressureLevel.CRITICAL

    def test_expired_ttl_resets_external_level_to_none(self):
        """Expired TTL resets _external_level to NONE during _adjust_rate."""
        ctrl = self._make_controller(queue_size=0)

        # Set expired external level
        ctrl._external_level = BackpressureLevel.HIGH
        ctrl._external_level_until = time.time() - 1  # Already expired

        ctrl._adjust_rate()

        assert ctrl._external_level == BackpressureLevel.NONE
        assert ctrl._level == BackpressureLevel.NONE

    def test_no_external_level_uses_queue_only(self):
        """Without external level, behavior matches original queue-only mode."""
        ctrl = self._make_controller(queue_size=0)

        ctrl._adjust_rate()

        assert ctrl._level == BackpressureLevel.NONE
        assert ctrl._external_level == BackpressureLevel.NONE


# =============================================================================
# Behavior: _subscribe_throttle_sla_events fail-open
# =============================================================================


class TestSubscribeThrottleSlaEventsBehavior:
    """_subscribe_throttle_sla_events is fail-open."""

    def test_import_error_does_not_propagate(self):
        """ImportError during subscription does not propagate."""
        settings = BackpressureSettings()
        ctrl = RateController(settings=settings)

        with patch(
            "baldur.services.event_bus.get_event_bus",
            side_effect=ImportError("no event bus"),
        ):
            # Should not raise
            ctrl._subscribe_throttle_sla_events()
