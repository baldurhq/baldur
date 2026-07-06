"""
create_event() factory function unit tests (doc 389).

Testable units:
- create_event(): BaldurEvent factory with automatic trace context enrichment
- BaldurEventBus.emit(): refactored to use create_event()
"""

from __future__ import annotations

from unittest.mock import patch

# =============================================================================
# Behavior: create_event() trace context enrichment
# =============================================================================


class TestCreateEventBehavior:
    """create_event() behavior verification."""

    def test_returns_baldur_event_with_correct_fields(self):
        """create_event() returns BaldurEvent with all fields populated."""
        from baldur.services.event_bus.bus.event_types import (
            EventPriority,
            EventType,
        )
        from baldur.services.event_bus.bus.models import create_event

        event = create_event(
            EventType.CONFIG_UPDATED,
            {"key": "test"},
            source="test_source",
            priority=EventPriority.HIGH,
        )

        assert event.event_type == EventType.CONFIG_UPDATED
        assert event.data == {"key": "test"}
        assert event.source == "test_source"
        assert event.priority == EventPriority.HIGH
        assert event.event_id  # non-empty UUID hex

    def test_default_source_is_unknown(self):
        """create_event() defaults source to 'unknown'."""
        from baldur.services.event_bus.bus.event_types import EventType
        from baldur.services.event_bus.bus.models import create_event

        event = create_event(EventType.CONFIG_UPDATED, {})
        assert event.source == "unknown"

    def test_default_priority_is_normal(self):
        """create_event() defaults priority to NORMAL."""
        from baldur.services.event_bus.bus.event_types import (
            EventPriority,
            EventType,
        )
        from baldur.services.event_bus.bus.models import create_event

        event = create_event(EventType.CONFIG_UPDATED, {})
        assert event.priority == EventPriority.NORMAL

    def test_auto_enriches_correlation_id_from_trace(self):
        """create_event() auto-populates correlation_id from audit.trace."""
        from baldur.services.event_bus.bus.event_types import EventType
        from baldur.services.event_bus.bus.models import create_event

        with patch(
            "baldur.audit.trace.get_trace_id",
            return_value="trace-abc-123",
        ):
            event = create_event(EventType.CONFIG_UPDATED, {})

        assert event.correlation_id == "trace-abc-123"

    def test_explicit_correlation_id_overrides_trace(self):
        """Explicit correlation_id is used and trace is not called."""
        from baldur.services.event_bus.bus.event_types import EventType
        from baldur.services.event_bus.bus.models import create_event

        event = create_event(
            EventType.CONFIG_UPDATED,
            {},
            correlation_id="explicit-id",
        )
        assert event.correlation_id == "explicit-id"

    def test_trace_import_failure_leaves_correlation_id_none(self):
        """When trace import fails, correlation_id remains None."""
        from baldur.services.event_bus.bus.event_types import EventType
        from baldur.services.event_bus.bus.models import create_event

        with patch(
            "baldur.audit.trace.get_trace_id",
            side_effect=ImportError("no trace module"),
        ):
            event = create_event(EventType.CONFIG_UPDATED, {})

        assert event.correlation_id is None

    def test_trace_runtime_error_leaves_correlation_id_none(self):
        """When get_trace_id() raises, correlation_id remains None."""
        from baldur.services.event_bus.bus.event_types import EventType
        from baldur.services.event_bus.bus.models import create_event

        with patch(
            "baldur.audit.trace.get_trace_id",
            side_effect=RuntimeError("context unavailable"),
        ):
            event = create_event(EventType.CONFIG_UPDATED, {})

        assert event.correlation_id is None

    def test_each_call_produces_unique_event_id(self):
        """Each create_event() call produces a unique event_id."""
        from baldur.services.event_bus.bus.event_types import EventType
        from baldur.services.event_bus.bus.models import create_event

        events = [create_event(EventType.CONFIG_UPDATED, {}) for _ in range(10)]
        event_ids = {e.event_id for e in events}
        assert len(event_ids) == 10


# =============================================================================
# Behavior: BaldurEventBus.emit() uses create_event()
# =============================================================================


class TestBaldurEventBusEmitRefactorBehavior:
    """Verify BaldurEventBus.emit() delegates to create_event()."""

    def test_emit_creates_event_and_publishes(self):
        """emit() creates event via create_event() and calls publish()."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.services.event_bus.bus.event_types import EventType

        bus = BaldurEventBus()
        received = []
        bus.subscribe(EventType.CONFIG_UPDATED, lambda e: received.append(e))

        count = bus.emit(EventType.CONFIG_UPDATED, {"key": "v"}, source="test")

        assert count == 1
        assert len(received) == 1
        assert received[0].event_type == EventType.CONFIG_UPDATED
        assert received[0].source == "test"

    def test_emit_passes_correlation_id_to_create_event(self):
        """emit() passes explicit correlation_id through."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.services.event_bus.bus.event_types import EventType

        bus = BaldurEventBus()
        received = []
        bus.subscribe(EventType.CONFIG_UPDATED, lambda e: received.append(e))

        bus.emit(EventType.CONFIG_UPDATED, {}, correlation_id="custom-id")

        assert received[0].correlation_id == "custom-id"
