"""
Tests for D1: CB EventBus emission — trigger field convention (#413).

Covers:
- Auto OPEN: emits CIRCUIT_BREAKER_OPENED with trigger="auto" in record_failure()
- Auto CLOSE: emits CIRCUIT_BREAKER_CLOSED with trigger="auto" in record_success()
- Auto HALF_OPEN: emits CIRCUIT_BREAKER_HALF_OPENED with trigger="auto"
- Manual OPEN: emits with trigger="manual" in force_open()
- Manual CLOSE: emits with trigger="manual" in force_close()
- Manual RESET: emits CIRCUIT_BREAKER_CLOSED with trigger="manual_reset" in reset()
- Auto OPEN emission is outside _apply_burn_rate_multiplier try-except
"""

from __future__ import annotations

from unittest.mock import Mock, patch


def _make_service():
    """Create a CircuitBreakerService with mock repository."""
    from baldur.services.circuit_breaker.config import CircuitBreakerConfig
    from baldur.services.circuit_breaker.service import CircuitBreakerService

    mock_repo = Mock()
    config = CircuitBreakerConfig(enabled=True)
    service = CircuitBreakerService(config=config, repository=mock_repo)
    return service, mock_repo


class TestAutoOpenEmissionBehavior:
    """record_failure() emits CIRCUIT_BREAKER_OPENED with trigger='auto'."""

    @patch(
        "baldur.services.circuit_breaker.manual_control._is_system_enabled",
        return_value=True,
    )
    def test_auto_open_emits_with_trigger_auto(self, _mock_enabled):
        """Auto OPEN emits CIRCUIT_BREAKER_OPENED with trigger='auto'."""
        service, mock_repo = _make_service()

        # Given — state is closed with enough failures to trigger open
        mock_state = Mock()
        mock_state.state = "closed"
        mock_state.manually_controlled = False
        mock_state.failure_count = 10
        mock_state.success_count = 0

        mock_repo.get_or_create.return_value = mock_state
        mock_repo.get_state.return_value = mock_state
        mock_repo.record_failure.return_value = mock_state
        mock_repo.update_state.return_value = True

        emitted_events = []

        def capture_emit(event_type, data=None, **kwargs):
            emitted_events.append(
                {"event_type": event_type, "data": data or kwargs.get("data")}
            )

        with patch.object(service, "_emit_event", side_effect=capture_emit):
            service.record_failure("test_svc")

        # Then — find the OPENED event
        from baldur.services.event_bus import EventType

        opened_events = [
            e
            for e in emitted_events
            if e["event_type"] == EventType.CIRCUIT_BREAKER_OPENED
        ]
        assert len(opened_events) >= 1
        assert opened_events[-1]["data"]["trigger"] == "auto"
        assert opened_events[-1]["data"]["service_name"] == "test_svc"


class TestAutoCloseEmissionBehavior:
    """record_success() emits CIRCUIT_BREAKER_CLOSED with trigger='auto'."""

    def test_auto_close_emits_with_trigger_auto(self):
        """Auto CLOSE emits CIRCUIT_BREAKER_CLOSED with trigger='auto'."""
        service, mock_repo = _make_service()

        # Given — state is half_open with enough successes
        mock_state = Mock()
        mock_state.state = "half_open"
        mock_state.manually_controlled = False
        mock_state.success_count = service.config.success_threshold

        mock_repo.get_or_create.return_value = mock_state
        mock_repo.get_state.return_value = mock_state
        mock_repo.record_success.return_value = mock_state
        mock_repo.update_state.return_value = True

        emitted_events = []

        def capture_emit(event_type, data=None, **kwargs):
            emitted_events.append(
                {"event_type": event_type, "data": data or kwargs.get("data")}
            )

        with patch.object(service, "_emit_event", side_effect=capture_emit):
            service.record_success("test_svc")

        from baldur.services.event_bus import EventType

        closed_events = [
            e
            for e in emitted_events
            if e["event_type"] == EventType.CIRCUIT_BREAKER_CLOSED
        ]
        assert len(closed_events) == 1
        assert closed_events[0]["data"]["trigger"] == "auto"
        assert closed_events[0]["data"]["previous_state"] == "half_open"


class TestAutoHalfOpenEmissionContract:
    """HALF_OPEN emission includes trigger='auto' field."""

    def test_half_open_emission_has_trigger_auto(self):
        """Auto HALF_OPEN emits with trigger='auto' in data payload."""
        service, mock_repo = _make_service()

        # Given — state is open and recovery timeout exceeded
        mock_state = Mock()
        mock_state.state = "open"
        mock_state.manually_controlled = False
        mock_state.opened_at = None  # Will be set by should_allow

        mock_repo.get_or_create.return_value = mock_state
        # 476: repository owns the OPEN→HALF_OPEN atomic transition.
        mock_repo.try_acquire_half_open_slot.return_value = (True, "open", "half_open")

        emitted_events = []

        def capture_emit(event_type, data, **kwargs):
            emitted_events.append({"event_type": event_type, "data": data})

        with patch.object(service, "_emit_event", side_effect=capture_emit):
            from datetime import timedelta

            from baldur.core.timezone import now as tz_now

            mock_state.opened_at = tz_now() - timedelta(
                seconds=service.config.recovery_timeout + 1
            )

            service.should_allow("test_svc")

        from baldur.services.event_bus import EventType

        half_open_events = [
            e
            for e in emitted_events
            if e["event_type"] == EventType.CIRCUIT_BREAKER_HALF_OPENED
        ]
        assert len(half_open_events) == 1
        assert half_open_events[0]["data"]["trigger"] == "auto"


class TestManualOpenEmissionBehavior:
    """force_open() emits CIRCUIT_BREAKER_OPENED with trigger='manual'."""

    @patch(
        "baldur.services.circuit_breaker.manual_control._is_system_enabled",
        return_value=True,
    )
    def test_manual_open_emits_with_trigger_manual(self, _mock_enabled):
        """Manual OPEN emits with trigger='manual'."""
        service, mock_repo = _make_service()
        mock_repo.atomic_force_open.return_value = ("open", "closed", "open")

        emitted_events = []

        def capture_emit(event_type, data, **kwargs):
            emitted_events.append({"event_type": event_type, "data": data})

        with patch.object(service, "_emit_event", side_effect=capture_emit):
            service.force_open("test_svc", reason="manual test")

        from baldur.services.event_bus import EventType

        opened_events = [
            e
            for e in emitted_events
            if e["event_type"] == EventType.CIRCUIT_BREAKER_OPENED
        ]
        assert len(opened_events) == 1
        assert opened_events[0]["data"]["trigger"] == "manual"


class TestManualCloseEmissionBehavior:
    """force_close() emits CIRCUIT_BREAKER_CLOSED with trigger='manual'."""

    @patch(
        "baldur.services.circuit_breaker.manual_control._is_system_enabled",
        return_value=True,
    )
    def test_manual_close_emits_with_trigger_manual(self, _mock_enabled):
        """Manual CLOSE emits with trigger='manual'."""
        service, mock_repo = _make_service()
        mock_repo.atomic_force_close.return_value = ("closed", "open", "closed")

        emitted_events = []

        def capture_emit(event_type, data, **kwargs):
            emitted_events.append({"event_type": event_type, "data": data})

        with patch.object(service, "_emit_event", side_effect=capture_emit):
            service.force_close("test_svc", reason="manual test")

        from baldur.services.event_bus import EventType

        closed_events = [
            e
            for e in emitted_events
            if e["event_type"] == EventType.CIRCUIT_BREAKER_CLOSED
        ]
        assert len(closed_events) == 1
        assert closed_events[0]["data"]["trigger"] == "manual"


class TestManualResetEmissionBehavior:
    """reset() emits CIRCUIT_BREAKER_CLOSED with trigger='manual_reset'."""

    def test_reset_emits_with_trigger_manual_reset(self):
        """Manual RESET emits CLOSED with trigger='manual_reset'."""
        service, mock_repo = _make_service()
        mock_repo.atomic_reset.return_value = (True, "open", "closed")

        emitted_events = []

        def capture_emit(event_type, data, **kwargs):
            emitted_events.append({"event_type": event_type, "data": data})

        with patch.object(service, "_emit_event", side_effect=capture_emit):
            service.reset("test_svc", reason="manual reset test")

        from baldur.services.event_bus import EventType

        closed_events = [
            e
            for e in emitted_events
            if e["event_type"] == EventType.CIRCUIT_BREAKER_CLOSED
        ]
        assert len(closed_events) == 1
        assert closed_events[0]["data"]["trigger"] == "manual_reset"


class TestEventTypeContract:
    """CORRUPTION_VIOLATION_CRITICAL EventType exists."""

    def test_corruption_violation_critical_event_type_exists(self):
        """CORRUPTION_VIOLATION_CRITICAL is defined in EventType enum."""
        from baldur.services.event_bus import EventType

        assert hasattr(EventType, "CORRUPTION_VIOLATION_CRITICAL")
        assert (
            EventType.CORRUPTION_VIOLATION_CRITICAL.value
            == "corruption_violation_critical"
        )
