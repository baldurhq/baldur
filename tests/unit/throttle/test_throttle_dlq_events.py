"""
Tests for ThrottleDLQIntegration event emission (381).

THROTTLE_REJECTION_STORED / REPLAY_STARTED / REPLAY_COMPLETED / REPLAY_FAILED
이벤트 발행 및 EventBus fail-safe 동작을 검증합니다.
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from unittest.mock import MagicMock, patch

import pytest

from baldur.services.event_bus.bus.event_types import EventType
from baldur_pro.services.throttle.dlq_integration import (
    ThrottleDLQConfig,
    ThrottleDLQIntegration,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_dlq_service():
    """Mock DLQ service with successful store and replay."""
    svc = MagicMock()
    svc.store_failure.return_value = MagicMock(success=True, entry_id=42)
    svc.replay.return_value = MagicMock(processed=10, success=8, failed=2, errors=[])
    return svc


@pytest.fixture
def mock_event_bus():
    return MagicMock()


@pytest.fixture
def integration(mock_dlq_service, mock_event_bus):
    """ThrottleDLQIntegration with pre-injected DLQ service and EventBus."""
    config = ThrottleDLQConfig(dlq_domain="throttle")
    integ = ThrottleDLQIntegration(config=config)
    integ._dlq_service = mock_dlq_service
    integ._event_bus = mock_event_bus
    return integ


# =============================================================================
# EventBus Fail-Safe (§6)
# =============================================================================


class TestThrottleDLQEventBusFailSafeBehavior:
    """EventBus lazy getter and _emit_event fail-safe behavior."""

    def test_get_event_bus_returns_none_on_failure(self):
        """EventBus initialization failure returns None."""
        integ = ThrottleDLQIntegration()

        with patch(
            "baldur.services.event_bus.get_event_bus",
            side_effect=RuntimeError("unavailable"),
        ):
            result = integ._get_event_bus()

        assert result is None

    def test_emit_event_silently_skips_when_bus_is_none(self):
        """_emit_event does nothing when EventBus is unavailable."""
        integ = ThrottleDLQIntegration()
        integ._event_bus = None

        with patch.object(integ, "_get_event_bus", return_value=None):
            integ._emit_event("test_event", {"key": "value"})

    def test_emit_event_catches_bus_emit_exception(self, mock_event_bus):
        """_emit_event catches and logs bus.emit exceptions."""
        mock_event_bus.emit.side_effect = RuntimeError("bus down")
        integ = ThrottleDLQIntegration()
        integ._event_bus = mock_event_bus

        # Should not raise
        integ._emit_event("test_event", {"key": "value"})

    def test_emit_event_passes_source_throttle_dlq(self, mock_event_bus):
        """_emit_event passes _event_source to bus.emit."""
        integ = ThrottleDLQIntegration()
        integ._event_bus = mock_event_bus

        integ._emit_event("test_event", {"k": "v"})

        mock_event_bus.emit.assert_called_once_with(
            "test_event",
            data={"k": "v"},
            source=ThrottleDLQIntegration._event_source,
        )


# =============================================================================
# THROTTLE_REJECTION_STORED (§7)
# =============================================================================


class TestThrottleRejectionStoredEventBehavior:
    """store_denied_request THROTTLE_REJECTION_STORED emission."""

    def test_successful_store_emits_stored_event(self, integration, mock_event_bus):
        """Successful DLQ store emits THROTTLE_REJECTION_STORED."""
        result = integration.store_denied_request(
            service_name="payment_api",
            request_key="192.168.1.1:user_1",
            request_data={"order": 1},
            throttle_limit=100,
            current_count=105,
        )

        assert result is not None
        assert result.dlq_entry_id == 42

        stored_calls = [
            c
            for c in mock_event_bus.emit.call_args_list
            if c[0][0] == EventType.THROTTLE_REJECTION_STORED
        ]
        assert len(stored_calls) == 1
        data = stored_calls[0][1]["data"]
        assert data["entry_id"] == 42
        assert data["service_name"] == "payment_api"
        assert data["reason"] == "rate_limit_exceeded"
        assert data["domain"] == "throttle"
        assert data["throttle_limit"] == 100
        assert data["current_count"] == 105

    def test_failed_store_does_not_emit_event(
        self, integration, mock_dlq_service, mock_event_bus
    ):
        """Failed DLQ store does not emit STORED event."""
        mock_dlq_service.store_failure.return_value = MagicMock(
            success=False, message="store failed"
        )

        result = integration.store_denied_request(
            service_name="svc",
            request_key="key",
            request_data={},
            throttle_limit=10,
            current_count=15,
        )

        assert result is None
        mock_event_bus.emit.assert_not_called()

    def test_disabled_config_does_not_emit_event(self, mock_event_bus):
        """Disabled config skips store and does not emit event."""
        config = ThrottleDLQConfig(enabled=False)
        integ = ThrottleDLQIntegration(config=config)
        integ._event_bus = mock_event_bus

        result = integ.store_denied_request(
            service_name="svc",
            request_key="key",
            request_data={},
            throttle_limit=10,
            current_count=15,
        )

        assert result is None
        mock_event_bus.emit.assert_not_called()


# =============================================================================
# THROTTLE_REJECTION_REPLAY_STARTED / COMPLETED / FAILED (§8, §9, §10)
# =============================================================================


class TestThrottleRejectionReplayEventsBehavior:
    """replay_denied_requests STARTED/COMPLETED/FAILED emissions."""

    def test_replay_emits_started_before_replay_call(self, integration, mock_event_bus):
        """THROTTLE_REJECTION_REPLAY_STARTED emitted before dlq_service.replay()."""
        integration.replay_denied_requests(service_name="payment_api")

        started_calls = [
            c
            for c in mock_event_bus.emit.call_args_list
            if c[0][0] == EventType.THROTTLE_REJECTION_REPLAY_STARTED
        ]
        assert len(started_calls) == 1
        data = started_calls[0][1]["data"]
        assert data["domain"] == "throttle"
        assert data["batch_size"] == integration.config.replay_batch_size
        assert data["service_name"] == "payment_api"
        assert "pending_count" in data

    def test_replay_started_pending_count_uses_service_count(
        self, integration, mock_event_bus
    ):
        """STARTED event uses per-service pending count when service_name provided."""
        # Given
        integration._pending_counts["payment_api"] = 15

        # When
        integration.replay_denied_requests(service_name="payment_api")

        # Then
        started_data = [
            c[1]["data"]
            for c in mock_event_bus.emit.call_args_list
            if c[0][0] == EventType.THROTTLE_REJECTION_REPLAY_STARTED
        ][0]
        assert started_data["pending_count"] == 15

    def test_replay_started_pending_count_sums_all_when_no_service(
        self, integration, mock_event_bus
    ):
        """STARTED event sums all pending counts when service_name is None."""
        integration._pending_counts["svc_a"] = 5
        integration._pending_counts["svc_b"] = 10

        integration.replay_denied_requests(service_name=None)

        started_data = [
            c[1]["data"]
            for c in mock_event_bus.emit.call_args_list
            if c[0][0] == EventType.THROTTLE_REJECTION_REPLAY_STARTED
        ][0]
        assert started_data["pending_count"] == 15

    def test_successful_replay_emits_completed_event(self, integration, mock_event_bus):
        """Successful replay emits THROTTLE_REJECTION_REPLAY_COMPLETED."""
        integration.replay_denied_requests()

        completed_calls = [
            c
            for c in mock_event_bus.emit.call_args_list
            if c[0][0] == EventType.THROTTLE_REJECTION_REPLAY_COMPLETED
        ]
        assert len(completed_calls) == 1
        data = completed_calls[0][1]["data"]
        assert data["domain"] == "throttle"
        assert data["processed"] == 10
        assert data["success"] == 8
        assert data["failed"] == 2

    def test_replay_exception_emits_failed_event(
        self, integration, mock_dlq_service, mock_event_bus
    ):
        """Replay exception emits THROTTLE_REJECTION_REPLAY_FAILED."""
        mock_dlq_service.replay.side_effect = ConnectionError("Redis down")

        result = integration.replay_denied_requests(service_name="payment_api")

        assert "error" in result
        failed_calls = [
            c
            for c in mock_event_bus.emit.call_args_list
            if c[0][0] == EventType.THROTTLE_REJECTION_REPLAY_FAILED
        ]
        assert len(failed_calls) == 1
        data = failed_calls[0][1]["data"]
        assert data["domain"] == "throttle"
        assert data["error_type"] == "ConnectionError"
        assert "Redis down" in data["error"]
        assert data["service_name"] == "payment_api"

    def test_replay_exception_does_not_emit_completed(
        self, integration, mock_dlq_service, mock_event_bus
    ):
        """Replay exception should NOT emit COMPLETED, only FAILED."""
        mock_dlq_service.replay.side_effect = RuntimeError("crash")

        integration.replay_denied_requests()

        completed_calls = [
            c
            for c in mock_event_bus.emit.call_args_list
            if c[0][0] == EventType.THROTTLE_REJECTION_REPLAY_COMPLETED
        ]
        assert len(completed_calls) == 0

    def test_disabled_replay_does_not_emit_any_event(self, mock_event_bus):
        """Disabled auto_replay skips replay and emits no events."""
        config = ThrottleDLQConfig(auto_replay_on_cb_close=False)
        integ = ThrottleDLQIntegration(config=config)
        integ._event_bus = mock_event_bus

        result = integ.replay_denied_requests()

        assert result["skipped"] is True
        mock_event_bus.emit.assert_not_called()
