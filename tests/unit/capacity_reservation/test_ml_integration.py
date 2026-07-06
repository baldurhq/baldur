"""
ML Integration & PoolWatchdog shrink_guard Unit Tests.

Test Categories:
    A. Behavior — PoolWatchdog shrink_guard: suppression/allow/reason propagation
    C. Contract — EventType: SCHEDULED_EVENT_STARTED/ENDED existence

SpikeClassifier context tests moved to
tests/dormant/unit/services/test_spike_classifier_context.py (599 D14 —
the predictive_forecaster feature relocated to the private distribution).
"""

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from unittest.mock import MagicMock

from baldur.core.pool_watchdog import (
    PoolRecoveryAction,
    PoolWatchdog,
)
from baldur.services.event_bus.bus import EventType
from baldur_pro.services.pool_monitor import PoolHealthStatus

# =============================================================================
# A. Behavior — PoolWatchdog shrink_guard
# =============================================================================


class TestPoolWatchdogShrinkGuardBehavior:
    """shrink_guard Callback Guard 동작 검증."""

    def _make_watchdog(self, shrink_guard=None, expanded_by=5):
        """PoolWatchdog를 테스트용으로 생성."""
        monitor = MagicMock()
        monitor.check_health.return_value = (
            PoolHealthStatus.HEALTHY,
            MagicMock(usage_percent=30.0, max_connections=20),
        )
        handler = MagicMock()
        handler.shrink_pool.return_value = True

        watchdog = PoolWatchdog(
            monitor=monitor,
            recovery_handler=handler,
            auto_expand=True,
            shrink_guard=shrink_guard,
        )
        watchdog._expanded_by = expanded_by
        return watchdog, handler

    def test_shrink_guard_suppresses_shrink_with_reason(self):
        """guard가 reason 반환 시 shrink 미수행 + message에 reason 포함."""

        def guard():
            return "ScheduledEvent"

        watchdog, handler = self._make_watchdog(shrink_guard=guard)

        result = watchdog.check_and_recover()

        handler.shrink_pool.assert_not_called()
        assert result.action == PoolRecoveryAction.NONE
        assert "ScheduledEvent" in result.message
        assert result.success is True

    def test_shrink_guard_none_allows_normal_shrink(self):
        """guard가 None 반환 시 기존 shrink 로직 정상 동작."""

        def guard():
            return None

        watchdog, handler = self._make_watchdog(shrink_guard=guard)

        watchdog.check_and_recover()

        handler.shrink_pool.assert_called_once()

    def test_no_shrink_guard_allows_normal_shrink(self):
        """shrink_guard가 설정되지 않으면 기존 shrink 로직 정상 동작."""
        watchdog, handler = self._make_watchdog(shrink_guard=None)

        watchdog.check_and_recover()

        handler.shrink_pool.assert_called_once()

    def test_shrink_guard_emergency_mode_reason(self):
        """guard가 EmergencyMode reason 반환 시 message에 포함."""

        def guard():
            return "EmergencyMode"

        watchdog, _ = self._make_watchdog(shrink_guard=guard)

        result = watchdog.check_and_recover()

        assert "EmergencyMode" in result.message

    def test_shrink_guard_not_called_when_no_expansion(self):
        """expanded_by == 0이면 guard가 호출되지 않음 (shrink 시도 자체 안 함)."""
        call_count = 0

        def counting_guard():
            nonlocal call_count
            call_count += 1
            return "ShouldNotBeHere"

        watchdog, _ = self._make_watchdog(shrink_guard=counting_guard, expanded_by=0)
        watchdog.check_and_recover()
        assert call_count == 0


# =============================================================================
# C. Contract — EventType
# =============================================================================


class TestCapacityReservationEventTypeContract:
    """EventType에 Capacity Reservation 이벤트 존재 확인."""

    def test_scheduled_event_started_exists(self):
        """SCHEDULED_EVENT_STARTED 이벤트 타입 존재."""
        assert EventType.SCHEDULED_EVENT_STARTED.value == "scheduled_event_started"

    def test_scheduled_event_ended_exists(self):
        """SCHEDULED_EVENT_ENDED 이벤트 타입 존재."""
        assert EventType.SCHEDULED_EVENT_ENDED.value == "scheduled_event_ended"
