"""
Lifecycle EventBus Coverage — EventType Contract Tests (doc 483).

Verifies:
- 4 new ``DLQ_CONSUMER_*`` enum entries (D1)
- 7 new ``CANARY_ROLLOUT_*`` enum entries (D2)
- All 11 string values are unique
- Member name = uppercase of value (project naming convention)

Reference: 483
"""

from __future__ import annotations

from baldur.services.event_bus.bus.event_types import EventType


class TestDLQConsumerEventTypeContract:
    """4 new DLQ Consumer lifecycle EventType members (doc 483 D1)."""

    def test_dlq_consumer_started_value(self):
        assert EventType.DLQ_CONSUMER_STARTED.value == "dlq_consumer_started"

    def test_dlq_consumer_stopped_value(self):
        assert EventType.DLQ_CONSUMER_STOPPED.value == "dlq_consumer_stopped"

    def test_dlq_consumer_leadership_acquired_value(self):
        assert (
            EventType.DLQ_CONSUMER_LEADERSHIP_ACQUIRED.value
            == "dlq_consumer_leadership_acquired"
        )

    def test_dlq_consumer_leadership_lost_value(self):
        assert (
            EventType.DLQ_CONSUMER_LEADERSHIP_LOST.value
            == "dlq_consumer_leadership_lost"
        )

    def test_dlq_consumer_event_types_are_str_enum(self):
        # str-Enum inheritance is the project pattern for JSON serialization
        assert isinstance(EventType.DLQ_CONSUMER_STARTED, str)
        assert isinstance(EventType.DLQ_CONSUMER_STOPPED, str)
        assert isinstance(EventType.DLQ_CONSUMER_LEADERSHIP_ACQUIRED, str)
        assert isinstance(EventType.DLQ_CONSUMER_LEADERSHIP_LOST, str)


class TestCanaryRolloutEventTypeContract:
    """7 new Canary Rollout lifecycle EventType members (doc 483 D2)."""

    def test_canary_rollout_started_value(self):
        assert EventType.CANARY_ROLLOUT_STARTED.value == "canary_rollout_started"

    def test_canary_rollout_promoted_value(self):
        assert EventType.CANARY_ROLLOUT_PROMOTED.value == "canary_rollout_promoted"

    def test_canary_rollout_completed_value(self):
        assert EventType.CANARY_ROLLOUT_COMPLETED.value == "canary_rollout_completed"

    def test_canary_rollout_rolled_back_value(self):
        assert (
            EventType.CANARY_ROLLOUT_ROLLED_BACK.value == "canary_rollout_rolled_back"
        )

    def test_canary_rollout_paused_value(self):
        assert EventType.CANARY_ROLLOUT_PAUSED.value == "canary_rollout_paused"

    def test_canary_rollout_resumed_value(self):
        assert EventType.CANARY_ROLLOUT_RESUMED.value == "canary_rollout_resumed"

    def test_canary_rollout_cancelled_value(self):
        assert EventType.CANARY_ROLLOUT_CANCELLED.value == "canary_rollout_cancelled"

    def test_canary_rollout_event_types_are_str_enum(self):
        assert isinstance(EventType.CANARY_ROLLOUT_STARTED, str)
        assert isinstance(EventType.CANARY_ROLLOUT_PROMOTED, str)
        assert isinstance(EventType.CANARY_ROLLOUT_COMPLETED, str)
        assert isinstance(EventType.CANARY_ROLLOUT_ROLLED_BACK, str)
        assert isinstance(EventType.CANARY_ROLLOUT_PAUSED, str)
        assert isinstance(EventType.CANARY_ROLLOUT_RESUMED, str)
        assert isinstance(EventType.CANARY_ROLLOUT_CANCELLED, str)


class TestLifecycleEventTypeUniquenessContract:
    """All 11 new lifecycle event values must be unique against the rest of the enum."""

    def test_new_lifecycle_event_values_are_unique(self):
        new_event_values = [
            EventType.DLQ_CONSUMER_STARTED.value,
            EventType.DLQ_CONSUMER_STOPPED.value,
            EventType.DLQ_CONSUMER_LEADERSHIP_ACQUIRED.value,
            EventType.DLQ_CONSUMER_LEADERSHIP_LOST.value,
            EventType.CANARY_ROLLOUT_STARTED.value,
            EventType.CANARY_ROLLOUT_PROMOTED.value,
            EventType.CANARY_ROLLOUT_COMPLETED.value,
            EventType.CANARY_ROLLOUT_ROLLED_BACK.value,
            EventType.CANARY_ROLLOUT_PAUSED.value,
            EventType.CANARY_ROLLOUT_RESUMED.value,
            EventType.CANARY_ROLLOUT_CANCELLED.value,
        ]
        assert len(new_event_values) == len(set(new_event_values))

    def test_new_lifecycle_event_values_unique_against_all_event_types(self):
        new_member_names = {
            "DLQ_CONSUMER_STARTED",
            "DLQ_CONSUMER_STOPPED",
            "DLQ_CONSUMER_LEADERSHIP_ACQUIRED",
            "DLQ_CONSUMER_LEADERSHIP_LOST",
            "CANARY_ROLLOUT_STARTED",
            "CANARY_ROLLOUT_PROMOTED",
            "CANARY_ROLLOUT_COMPLETED",
            "CANARY_ROLLOUT_ROLLED_BACK",
            "CANARY_ROLLOUT_PAUSED",
            "CANARY_ROLLOUT_RESUMED",
            "CANARY_ROLLOUT_CANCELLED",
        }
        new_values = {EventType[name].value for name in new_member_names}
        other_values = {
            member.value for member in EventType if member.name not in new_member_names
        }
        assert new_values.isdisjoint(other_values)

    def test_new_lifecycle_event_member_names_match_uppercase_value(self):
        # Project naming convention: enum member name == value.upper()
        new_member_names = [
            "DLQ_CONSUMER_STARTED",
            "DLQ_CONSUMER_STOPPED",
            "DLQ_CONSUMER_LEADERSHIP_ACQUIRED",
            "DLQ_CONSUMER_LEADERSHIP_LOST",
            "CANARY_ROLLOUT_STARTED",
            "CANARY_ROLLOUT_PROMOTED",
            "CANARY_ROLLOUT_COMPLETED",
            "CANARY_ROLLOUT_ROLLED_BACK",
            "CANARY_ROLLOUT_PAUSED",
            "CANARY_ROLLOUT_RESUMED",
            "CANARY_ROLLOUT_CANCELLED",
        ]
        for name in new_member_names:
            assert EventType[name].value == name.lower()
