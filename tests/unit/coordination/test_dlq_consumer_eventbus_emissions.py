"""
DLQConsumerCoordinator — EventBus emission unit tests (doc 483).

Verifies the 4 lifecycle EventBus emission sites added by 483:
- ``start()``    → ``DLQ_CONSUMER_STARTED`` (after successful elector start, D4b)
- ``stop()``     → ``DLQ_CONSUMER_STOPPED`` (after elector stop)
- ``_on_become_leader()`` → ``DLQ_CONSUMER_LEADERSHIP_ACQUIRED``
- ``_on_lose_leader()``   → ``DLQ_CONSUMER_LEADERSHIP_LOST``

Mock pattern follows ``tests/unit/circuit_breaker/test_cb_eventbus_emissions.py``
— ``patch.object(coord, "_emit_event", side_effect=capture)`` to capture
emitted ``(event_type, data)`` tuples.

Reference: 483
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.coordination.base import LeadershipState
from baldur.coordination.dlq_consumer import DLQConsumerCoordinator
from baldur.services.event_bus.bus.event_types import EventType

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_leader_elector():
    elector = MagicMock()
    elector.is_leader.return_value = False
    elector.state = LeadershipState.NOT_STARTED
    elector.resource_name = "dlq-consumer-test"
    elector.get_fencing_token.return_value = 1

    on_become_callbacks: list = []
    on_lose_callbacks: list = []

    def on_become_leader(callback):
        on_become_callbacks.append(callback)
        return callback

    def on_lose_leader(callback):
        on_lose_callbacks.append(callback)
        return callback

    elector.on_become_leader.side_effect = on_become_leader
    elector.on_lose_leader.side_effect = on_lose_leader
    elector._on_become_callbacks = on_become_callbacks
    elector._on_lose_callbacks = on_lose_callbacks
    return elector


@pytest.fixture
def coordinator(mock_leader_elector):
    with (
        patch(
            "baldur.coordination.dlq_consumer.get_leader_elector",
            return_value=mock_leader_elector,
        ),
        patch("baldur.coordination.dlq_consumer.register_for_graceful_shutdown"),
    ):
        coord = DLQConsumerCoordinator(
            resource_name="dlq-consumer-test",
            process_interval_seconds=0.1,
            batch_size=5,
        )
    return coord


def _capture(captured: list):
    def _side_effect(event_type, data, **kwargs):
        captured.append((event_type, data))

    return _side_effect


# =============================================================================
# Behavior tests
# =============================================================================


class TestDLQConsumerEventBusEmissionsBehavior:
    """4 lifecycle EventBus emissions in DLQConsumerCoordinator."""

    def test_start_emits_dlq_consumer_started_after_elector_start(
        self, coordinator, mock_leader_elector
    ):
        # Given
        captured: list = []

        # When
        with patch.object(coordinator, "_emit_event", side_effect=_capture(captured)):
            coordinator.start()

        # Then — exactly one STARTED event emitted, after _elector.start() ran
        started_events = [
            (et, data) for et, data in captured if et == EventType.DLQ_CONSUMER_STARTED
        ]
        assert len(started_events) == 1
        assert mock_leader_elector.start.called

    def test_start_does_not_emit_when_elector_start_raises(
        self, coordinator, mock_leader_elector
    ):
        """D4b phantom-event guard: emit must NOT fire if _elector.start() raises."""
        # Given — elector.start() raises
        mock_leader_elector.start.side_effect = RuntimeError("elector boom")
        captured: list = []

        # When
        with patch.object(coordinator, "_emit_event", side_effect=_capture(captured)):
            with pytest.raises(RuntimeError, match="elector boom"):
                coordinator.start()

        # Then — no STARTED event was emitted
        started_events = [
            (et, _) for et, _ in captured if et == EventType.DLQ_CONSUMER_STARTED
        ]
        assert started_events == []

    def test_stop_emits_dlq_consumer_stopped_after_elector_stop(
        self, coordinator, mock_leader_elector
    ):
        # Given — coordinator started
        with patch.object(coordinator, "_emit_event"):
            coordinator.start()
        captured: list = []

        # When
        with patch.object(coordinator, "_emit_event", side_effect=_capture(captured)):
            coordinator.stop()

        # Then — exactly one STOPPED event emitted, elector stopped
        stopped_events = [
            (et, data) for et, data in captured if et == EventType.DLQ_CONSUMER_STOPPED
        ]
        assert len(stopped_events) == 1
        assert mock_leader_elector.stop.called

    def test_on_become_leader_emits_leadership_acquired(self, coordinator):
        # Given
        captured: list = []

        # When
        with patch.object(coordinator, "_emit_event", side_effect=_capture(captured)):
            coordinator._on_become_leader()

        # Then
        acquired_events = [
            (et, data)
            for et, data in captured
            if et == EventType.DLQ_CONSUMER_LEADERSHIP_ACQUIRED
        ]
        assert len(acquired_events) == 1
        assert coordinator.is_consuming is True

        # Cleanup — stop the consume thread spawned by _on_become_leader
        coordinator._consuming = False
        coordinator._stop_event.set()
        if coordinator._consume_thread:
            coordinator._consume_thread.join(timeout=2.0)

    def test_on_lose_leader_emits_leadership_lost(self, coordinator):
        # Given
        captured: list = []

        # When
        with patch.object(coordinator, "_emit_event", side_effect=_capture(captured)):
            coordinator._on_lose_leader()

        # Then
        lost_events = [
            (et, data)
            for et, data in captured
            if et == EventType.DLQ_CONSUMER_LEADERSHIP_LOST
        ]
        assert len(lost_events) == 1
        assert coordinator.is_consuming is False


class TestDLQConsumerEventBusEmissionsContract:
    """Contract: emit data shape is exactly {'resource_name'} (D1 payload)."""

    def test_emit_data_keys_are_resource_name_only(self, coordinator):
        captured: list = []

        with patch.object(coordinator, "_emit_event", side_effect=_capture(captured)):
            coordinator.start()
            coordinator._on_become_leader()
            coordinator._on_lose_leader()
            coordinator.stop()

        assert len(captured) == 4
        for _event_type, data in captured:
            assert set(data.keys()) == {"resource_name"}
            assert data["resource_name"] == "dlq-consumer-test"

        # Cleanup — _on_become_leader spawned a consume thread
        if coordinator._consume_thread:
            coordinator._consume_thread.join(timeout=2.0)

    def test_event_source_is_dlq_consumer(self):
        # _event_source is what EventEmitterMixin passes as bus.emit(source=...)
        assert DLQConsumerCoordinator._event_source == "dlq_consumer"
