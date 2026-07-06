"""RecoverySessionData domain model unit tests.

Tests for the framework-agnostic Recovery Session domain model:
- Contract: RecoveryStatus (7 values), TriggerLevel (3 values), VALID_TRANSITIONS structure,
  field count (17), field defaults
- Behavior: state transitions (all mark_* methods + approve), _validate_transition,
  is_terminal, get_step_count, get_total_steps, to_summary_dict,
  serialization roundtrip, add_step_result
"""

from __future__ import annotations

from enum import Enum

import pytest

from baldur.core.exceptions import InvalidStateTransitionError
from baldur.core.serializable import SerializableMixin
from baldur.models.recovery_session import (
    VALID_TRANSITIONS,
    RecoverySessionData,
    RecoveryStatus,
    RecoveryStepData,
    TriggerLevel,
)

# =============================================================================
# Contract Tests — Enums
# =============================================================================


class TestRecoveryStatusContract:
    """RecoveryStatus enum contract verification."""

    def test_has_seven_members(self):
        """RecoveryStatus enum has exactly 7 members."""
        assert len(RecoveryStatus) == 7

    def test_values_match_spec(self):
        """RecoveryStatus values match 366 design spec."""
        expected = {
            "not_started",
            "in_progress",
            "health_check",
            "ready_to_restore",
            "completed",
            "failed",
            "aborted",
        }
        actual = {s.value for s in RecoveryStatus}
        assert actual == expected

    def test_is_str_enum(self):
        """RecoveryStatus inherits from (str, Enum)."""
        assert issubclass(RecoveryStatus, str)
        assert issubclass(RecoveryStatus, Enum)


class TestTriggerLevelContract:
    """TriggerLevel enum contract verification."""

    def test_has_three_members(self):
        """TriggerLevel enum has exactly 3 members."""
        assert len(TriggerLevel) == 3

    def test_values_match_spec(self):
        """TriggerLevel values match 366 design spec."""
        expected = {"LEVEL_1", "LEVEL_2", "LEVEL_3"}
        actual = {t.value for t in TriggerLevel}
        assert actual == expected


class TestValidTransitionsContract:
    """VALID_TRANSITIONS state machine contract verification."""

    def test_all_states_have_entries(self):
        """Every RecoveryStatus is a key in VALID_TRANSITIONS."""
        for status in RecoveryStatus:
            assert status in VALID_TRANSITIONS

    def test_terminal_states_have_no_outgoing(self):
        """Terminal states (COMPLETED, FAILED, ABORTED) have empty transition sets."""
        for terminal in (
            RecoveryStatus.COMPLETED,
            RecoveryStatus.FAILED,
            RecoveryStatus.ABORTED,
        ):
            assert VALID_TRANSITIONS[terminal] == set()

    def test_not_started_transitions(self):
        """NOT_STARTED can transition to IN_PROGRESS or ABORTED."""
        assert VALID_TRANSITIONS[RecoveryStatus.NOT_STARTED] == {
            RecoveryStatus.IN_PROGRESS,
            RecoveryStatus.ABORTED,
        }

    def test_in_progress_transitions(self):
        """IN_PROGRESS can transition to HEALTH_CHECK, COMPLETED, FAILED, ABORTED."""
        assert VALID_TRANSITIONS[RecoveryStatus.IN_PROGRESS] == {
            RecoveryStatus.HEALTH_CHECK,
            RecoveryStatus.COMPLETED,
            RecoveryStatus.FAILED,
            RecoveryStatus.ABORTED,
        }

    def test_health_check_transitions(self):
        """HEALTH_CHECK can transition to READY_TO_RESTORE, FAILED, ABORTED."""
        assert VALID_TRANSITIONS[RecoveryStatus.HEALTH_CHECK] == {
            RecoveryStatus.READY_TO_RESTORE,
            RecoveryStatus.FAILED,
            RecoveryStatus.ABORTED,
        }

    def test_ready_to_restore_transitions(self):
        """READY_TO_RESTORE can transition to COMPLETED or ABORTED."""
        assert VALID_TRANSITIONS[RecoveryStatus.READY_TO_RESTORE] == {
            RecoveryStatus.COMPLETED,
            RecoveryStatus.ABORTED,
        }


# =============================================================================
# Contract Tests — RecoverySessionData fields
# =============================================================================


class TestRecoverySessionDataContract:
    """RecoverySessionData structural contract verification."""

    def test_inherits_serializable_mixin(self):
        """RecoverySessionData inherits from SerializableMixin."""
        assert issubclass(RecoverySessionData, SerializableMixin)

    def test_to_dict_contains_all_seventeen_keys(self):
        """to_dict() output contains exactly 17 contract keys."""
        expected_keys = {
            "session_id",
            "namespace",
            "trigger_level",
            "status",
            "initiated_by",
            "steps_data",
            "started_at",
            "completed_at",
            "duration_seconds",
            "abort_reason",
            "cascade_event_id",
            "requires_approval",
            "approved_by",
            "approved_at",
            "metadata",
            "created_at",
            "updated_at",
        }
        data = RecoverySessionData(
            session_id="s-1",
            namespace="global",
            trigger_level="LEVEL_1",
        )
        assert set(data.to_dict().keys()) == expected_keys

    def test_default_status_is_not_started(self):
        """Default status is 'not_started'."""
        data = RecoverySessionData(
            session_id="s-1", namespace="global", trigger_level="LEVEL_1"
        )
        assert data.status == "not_started"

    def test_default_initiated_by_is_system(self):
        """Default initiated_by is 'system'."""
        data = RecoverySessionData(
            session_id="s-1", namespace="global", trigger_level="LEVEL_1"
        )
        assert data.initiated_by == "system"


# =============================================================================
# Contract Tests — RecoveryStepData fields
# =============================================================================


class TestRecoveryStepDataContract:
    """RecoveryStepData structural contract verification."""

    def test_inherits_serializable_mixin(self):
        """RecoveryStepData inherits from SerializableMixin."""
        assert issubclass(RecoveryStepData, SerializableMixin)

    def test_to_dict_contains_all_ten_keys(self):
        """to_dict() output contains exactly 10 contract keys."""
        expected_keys = {
            "step_type",
            "order",
            "status",
            "wait_after_seconds",
            "params",
            "started_at",
            "completed_at",
            "error_message",
            "execution_time_ms",
            "retry_count",
        }
        data = RecoveryStepData(step_type="health_check", order=1, status="completed")
        assert set(data.to_dict().keys()) == expected_keys


# =============================================================================
# Behavior Tests — State Transitions
# =============================================================================


class TestRecoverySessionStateTransitionBehavior:
    """RecoverySessionData state transition behavior verification."""

    def _make_session(self, status: str = "not_started") -> RecoverySessionData:
        return RecoverySessionData(
            session_id="test-session",
            namespace="global",
            trigger_level="LEVEL_1",
            status=status,
        )

    def test_mark_started_from_not_started(self):
        """mark_started() transitions NOT_STARTED → IN_PROGRESS with started_at set."""
        session = self._make_session("not_started")
        session.mark_started()
        assert session.status == RecoveryStatus.IN_PROGRESS.value
        assert session.started_at is not None

    def test_mark_completed_from_in_progress(self):
        """mark_completed() transitions IN_PROGRESS → COMPLETED with duration calculated."""
        session = self._make_session("not_started")
        session.mark_started()
        session.mark_completed()
        assert session.status == RecoveryStatus.COMPLETED.value
        assert session.completed_at is not None
        assert session.duration_seconds is not None
        assert session.duration_seconds >= 0

    def test_mark_failed_from_in_progress(self):
        """mark_failed() transitions IN_PROGRESS → FAILED with reason recorded."""
        session = self._make_session("not_started")
        session.mark_started()
        session.mark_failed("timeout")
        assert session.status == RecoveryStatus.FAILED.value
        assert session.abort_reason == "timeout"
        assert session.completed_at is not None

    def test_mark_aborted_from_not_started(self):
        """mark_aborted() transitions NOT_STARTED → ABORTED."""
        session = self._make_session("not_started")
        session.mark_aborted("cancelled by operator")
        assert session.status == RecoveryStatus.ABORTED.value
        assert session.abort_reason == "cancelled by operator"

    def test_mark_ready_to_restore_from_health_check(self):
        """mark_ready_to_restore() transitions HEALTH_CHECK → READY_TO_RESTORE."""
        session = self._make_session("health_check")
        session.mark_ready_to_restore()
        assert session.status == RecoveryStatus.READY_TO_RESTORE.value
        assert session.requires_approval is True

    def test_approve_from_ready_to_restore(self):
        """approve() transitions READY_TO_RESTORE → COMPLETED with approval fields set."""
        session = self._make_session("ready_to_restore")
        session.approve("admin-user")
        assert session.status == RecoveryStatus.COMPLETED.value
        assert session.approved_by == "admin-user"
        assert session.approved_at is not None

    def test_invalid_transition_from_completed_raises(self):
        """mark_started() from COMPLETED raises InvalidStateTransitionError."""
        session = self._make_session("completed")
        with pytest.raises(InvalidStateTransitionError) as exc_info:
            session.mark_started()
        assert "completed" in str(exc_info.value)

    def test_invalid_transition_from_failed_raises(self):
        """mark_completed() from FAILED raises InvalidStateTransitionError."""
        session = self._make_session("failed")
        with pytest.raises(InvalidStateTransitionError):
            session.mark_completed()

    def test_invalid_transition_not_started_to_completed_raises(self):
        """mark_completed() from NOT_STARTED raises InvalidStateTransitionError."""
        session = self._make_session("not_started")
        with pytest.raises(InvalidStateTransitionError):
            session.mark_completed()

    def test_all_terminal_states_reject_all_transitions(self):
        """All mark_* methods raise from terminal states."""
        for terminal in ("completed", "failed", "aborted"):
            session = self._make_session(terminal)
            with pytest.raises(InvalidStateTransitionError):
                session.mark_started()


# =============================================================================
# Behavior Tests — Query Methods
# =============================================================================


class TestRecoverySessionQueryBehavior:
    """RecoverySessionData query method behavior verification."""

    def test_is_terminal_for_terminal_states(self):
        """is_terminal() returns True for COMPLETED, FAILED, ABORTED."""
        for status in ("completed", "failed", "aborted"):
            data = RecoverySessionData(
                session_id="s-1",
                namespace="global",
                trigger_level="LEVEL_1",
                status=status,
            )
            assert data.is_terminal() is True

    def test_is_terminal_for_non_terminal_states(self):
        """is_terminal() returns False for non-terminal states."""
        for status in (
            "not_started",
            "in_progress",
            "health_check",
            "ready_to_restore",
        ):
            data = RecoverySessionData(
                session_id="s-1",
                namespace="global",
                trigger_level="LEVEL_1",
                status=status,
            )
            assert data.is_terminal() is False

    def test_get_step_count_returns_list_length(self):
        """get_step_count() returns length of steps_data list."""
        data = RecoverySessionData(
            session_id="s-1",
            namespace="global",
            trigger_level="LEVEL_1",
            steps_data=[{"step": 1}, {"step": 2}],
        )
        assert data.get_step_count() == 2

    def test_get_step_count_empty_returns_zero(self):
        """get_step_count() returns 0 for empty steps."""
        data = RecoverySessionData(
            session_id="s-1", namespace="global", trigger_level="LEVEL_1"
        )
        assert data.get_step_count() == 0

    def test_get_total_steps_from_metadata(self):
        """get_total_steps() reads total_steps from metadata."""
        data = RecoverySessionData(
            session_id="s-1",
            namespace="global",
            trigger_level="LEVEL_1",
            metadata={"total_steps": 5},
        )
        assert data.get_total_steps() == 5

    def test_get_total_steps_missing_returns_zero(self):
        """get_total_steps() returns 0 when metadata has no total_steps."""
        data = RecoverySessionData(
            session_id="s-1", namespace="global", trigger_level="LEVEL_1"
        )
        assert data.get_total_steps() == 0

    def test_to_summary_dict_contains_computed_fields(self):
        """to_summary_dict() includes step_count and total_steps computed fields."""
        data = RecoverySessionData(
            session_id="s-1",
            namespace="global",
            trigger_level="LEVEL_1",
            steps_data=[{"step": 1}],
            metadata={"total_steps": 3},
        )
        summary = data.to_summary_dict()
        assert summary["step_count"] == 1
        assert summary["total_steps"] == 3
        assert summary["session_id"] == "s-1"

    def test_to_summary_dict_has_thirteen_keys(self):
        """to_summary_dict() returns exactly 13 keys."""
        data = RecoverySessionData(
            session_id="s-1", namespace="global", trigger_level="LEVEL_1"
        )
        assert len(data.to_summary_dict()) == 13

    def test_add_step_result_appends_to_steps_data(self):
        """add_step_result() appends step data to steps_data list."""
        data = RecoverySessionData(
            session_id="s-1", namespace="global", trigger_level="LEVEL_1"
        )
        data.add_step_result({"step_type": "budget_reset", "status": "completed"})
        data.add_step_result({"step_type": "health_check", "status": "completed"})
        assert len(data.steps_data) == 2
        assert data.steps_data[0]["step_type"] == "budget_reset"


# =============================================================================
# Behavior Tests — Serialization
# =============================================================================


class TestRecoverySessionSerializationBehavior:
    """RecoverySessionData serialization roundtrip verification."""

    def test_roundtrip_preserves_all_fields(self):
        """to_dict → from_dict roundtrip preserves all fields."""
        from baldur.utils.time import utc_now

        now = utc_now()
        original = RecoverySessionData(
            session_id="recovery-abc123",
            namespace="seoul",
            trigger_level=TriggerLevel.LEVEL_3.value,
            status=RecoveryStatus.IN_PROGRESS.value,
            initiated_by="admin",
            steps_data=[{"step_type": "budget_reset", "order": 1}],
            started_at=now,
            abort_reason="",
            cascade_event_id="cascade-evt-001",
            requires_approval=True,
            approved_by="manager",
            metadata={"region": "ap-northeast-2"},
        )

        serialized = original.to_dict()
        restored = RecoverySessionData.from_dict(serialized)

        assert restored.session_id == original.session_id
        assert restored.namespace == original.namespace
        assert restored.trigger_level == original.trigger_level
        assert restored.status == original.status
        assert restored.initiated_by == original.initiated_by
        assert restored.steps_data == original.steps_data
        assert restored.started_at == original.started_at
        assert restored.cascade_event_id == original.cascade_event_id
        assert restored.requires_approval == original.requires_approval
        assert restored.metadata == original.metadata

    def test_step_data_roundtrip(self):
        """RecoveryStepData to_dict → from_dict roundtrip."""
        original = RecoveryStepData(
            step_type="health_check",
            order=2,
            status="completed",
            wait_after_seconds=60,
            params={"duration_minutes": 5},
            started_at="2026-01-23T10:00:00+00:00",
            completed_at="2026-01-23T10:05:00+00:00",
            execution_time_ms=300000,
            retry_count=1,
        )
        data = original.to_dict()
        restored = RecoveryStepData.from_dict(data)
        assert restored.step_type == original.step_type
        assert restored.execution_time_ms == original.execution_time_ms
        assert restored.retry_count == original.retry_count


# =============================================================================
# Behavior Tests — InvalidStateTransitionError
# =============================================================================


class TestInvalidStateTransitionErrorBehavior:
    """InvalidStateTransitionError behavior verification."""

    def test_extra_context_contains_state_info(self):
        """extra_context() returns current_state, target_state, entity_id."""
        err = InvalidStateTransitionError(
            current="completed", target="in_progress", entity_id="s-123"
        )
        ctx = err.extra_context()
        assert ctx["current_state"] == "completed"
        assert ctx["target_state"] == "in_progress"
        assert ctx["entity_id"] == "s-123"

    def test_message_includes_states_and_entity(self):
        """Error message includes current, target, and entity_id."""
        err = InvalidStateTransitionError(
            current="completed", target="in_progress", entity_id="s-123"
        )
        msg = str(err)
        assert "completed" in msg
        assert "in_progress" in msg
        assert "s-123" in msg

    def test_inherits_baldur_error(self):
        """InvalidStateTransitionError inherits from BaldurError."""
        from baldur.core.exceptions import BaldurError

        assert issubclass(InvalidStateTransitionError, BaldurError)
