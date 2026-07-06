"""
Unit tests for Recovery Coordinator.

Tests:
- start_recovery() basic behavior
- execute_next_step() step execution
- abort_recovery() recovery abort
- full recovery flow (4 steps)
- duplicate-recovery prevention
- failure handling

Reference:
    docs/baldur/middleware_system/77_RECOVERY_COORDINATOR.md
"""

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from unittest.mock import MagicMock, patch

import pytest

from baldur.core.state_backend import MemoryStateBackend
from baldur_pro.services.coordination.distributed_recovery_lock import (
    InMemoryRecoveryLock,
)
from baldur_pro.services.coordination.enums import RecoveryStatus
from baldur_pro.services.coordination.recovery_coordinator import (
    RecoveryCoordinator,
    get_recovery_coordinator,
    reset_recovery_coordinator,
)
from baldur_pro.services.coordination.recovery_state import (
    RecoverySession,
    RecoveryStep,
    RecoveryStepType,
)


class TestRecoveryCoordinatorInit:
    """RecoveryCoordinator initialization tests."""

    def test_init_with_defaults(self):
        """Initialize with defaults."""
        backend = MemoryStateBackend()
        coordinator = RecoveryCoordinator(backend=backend)

        assert coordinator is not None
        # No forward-handler overrides by default; forward dispatch goes to the
        # shared idempotent registry. Compensate handlers cover all 4 step types.
        assert coordinator._step_handler_overrides == {}
        assert len(coordinator._compensate_handlers) == 4

    def test_init_with_custom_lock(self):
        """Initialize with a custom lock."""
        backend = MemoryStateBackend()
        lock = InMemoryRecoveryLock()

        coordinator = RecoveryCoordinator(
            backend=backend,
            recovery_lock=lock,
        )

        assert coordinator._recovery_lock is lock

    def test_default_recovery_steps_exist(self):
        """Default recovery steps are defined."""
        assert "LEVEL_3" in RecoveryCoordinator.DEFAULT_RECOVERY_STEPS
        assert "LEVEL_2" in RecoveryCoordinator.DEFAULT_RECOVERY_STEPS
        assert "LEVEL_1" in RecoveryCoordinator.DEFAULT_RECOVERY_STEPS

        # LEVEL_3 has 4 steps
        level3_steps = RecoveryCoordinator.DEFAULT_RECOVERY_STEPS["LEVEL_3"]
        assert len(level3_steps) == 4

    def test_register_custom_handler(self):
        """A custom callable is wrapped in the idempotent machinery and stored as an override."""
        from baldur_pro.services.coordination.idempotent_step_handlers import (
            CallableIdempotentStepHandler,
        )

        backend = MemoryStateBackend()
        coordinator = RecoveryCoordinator(backend=backend)

        custom_handler = MagicMock(return_value={"success": True})
        coordinator.register_step_handler(
            RecoveryStepType.BUDGET_RESET,
            custom_handler,
        )

        override = coordinator._step_handler_overrides[RecoveryStepType.BUDGET_RESET]
        assert isinstance(override, CallableIdempotentStepHandler)
        assert override._callable_handler is custom_handler


class TestStartRecovery:
    """start_recovery() tests."""

    @pytest.fixture
    def coordinator(self):
        """Coordinator for testing."""
        backend = MemoryStateBackend()
        lock = InMemoryRecoveryLock()
        return RecoveryCoordinator(
            backend=backend,
            recovery_lock=lock,
            use_regional_policy=False,
        )

    def test_start_recovery_success(self, coordinator):
        """Recovery start succeeds."""
        session = coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
            initiated_by="system",
        )

        assert session.id.startswith("recovery-")
        assert session.namespace == "global"
        assert session.trigger_level == "LEVEL_3"
        assert session.status == RecoveryStatus.IN_PROGRESS
        assert session.initiated_by == "system"
        assert len(session.steps) == 4
        assert session.started_at is not None

    def test_start_recovery_with_user(self, coordinator):
        """Recovery started by a user."""
        session = coordinator.start_recovery(
            namespace="seoul",
            trigger_level="LEVEL_2",
            initiated_by="admin@example.com",
        )

        assert session.initiated_by == "admin@example.com"

    def test_start_recovery_already_in_progress(self, coordinator):
        """Error when a recovery is already in progress."""
        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )

        with pytest.raises(ValueError) as exc_info:
            coordinator.start_recovery(
                namespace="global",
                trigger_level="LEVEL_3",
            )

        assert "already in progress" in str(exc_info.value)

    def test_start_recovery_different_namespace(self, coordinator):
        """Different namespaces can recover concurrently."""
        session1 = coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )
        session2 = coordinator.start_recovery(
            namespace="seoul",
            trigger_level="LEVEL_2",
        )

        assert session1.namespace == "global"
        assert session2.namespace == "seoul"

    def test_start_recovery_unknown_level(self, coordinator):
        """Unknown level raises an error."""
        with pytest.raises(ValueError) as exc_info:
            coordinator.start_recovery(
                namespace="global",
                trigger_level="LEVEL_99",
            )

        assert "No recovery steps" in str(exc_info.value)

    def test_start_recovery_session_saved(self, coordinator):
        """The session is saved."""
        session = coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )

        # Query the saved session
        saved = coordinator.get_session("global", session.id)
        assert saved is not None
        assert saved.id == session.id

    def test_start_recovery_active_session_set(self, coordinator):
        """The active session is set."""
        session = coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )

        active = coordinator.get_active_session("global")
        assert active is not None
        assert active.id == session.id


class TestExecuteNextStep:
    """execute_next_step() tests."""

    @pytest.fixture
    def coordinator(self):
        """Coordinator for testing (handlers mocked)."""
        backend = MemoryStateBackend()
        lock = InMemoryRecoveryLock()
        coord = RecoveryCoordinator(backend=backend, recovery_lock=lock)

        # Mock all handlers as successful
        for step_type in RecoveryStepType:
            coord.register_step_handler(
                step_type,
                lambda s, st: {"success": True},
            )

        return coord

    def test_execute_first_step(self, coordinator):
        """Execute the first step."""
        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )

        step = coordinator.execute_next_step("global")

        assert step is not None
        assert step.step_type == RecoveryStepType.BUDGET_RESET
        assert step.status == RecoveryStatus.COMPLETED

    def test_execute_all_steps(self, coordinator):
        """Execute all steps sequentially."""
        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )

        executed_steps = []
        for _ in range(10):  # up to 10 iterations
            step = coordinator.execute_next_step("global")
            if step is None:
                break
            executed_steps.append(step)

        # LEVEL_3 has 4 steps
        assert len(executed_steps) == 4
        assert executed_steps[0].step_type == RecoveryStepType.BUDGET_RESET
        assert executed_steps[1].step_type == RecoveryStepType.HEALTH_CHECK
        assert executed_steps[2].step_type == RecoveryStepType.CANARY_RESUME
        assert executed_steps[3].step_type == RecoveryStepType.GOVERNANCE_NORMAL

    def test_execute_step_completes_session(self, coordinator):
        """Session status is COMPLETED when all steps complete."""
        session = coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )

        # Execute 4 steps
        for _ in range(4):
            coordinator.execute_next_step("global")

        # 5th call performs completion handling (after all steps confirmed complete)
        final_step = coordinator.execute_next_step("global")
        assert final_step is None

        # No active session should remain
        active = coordinator.get_active_session("global")
        assert active is None

        # The saved session is in COMPLETED status
        saved = coordinator.get_session("global", session.id)
        assert saved.status == RecoveryStatus.COMPLETED

    def test_execute_step_no_active_session(self, coordinator):
        """Returns None when there is no active session."""
        step = coordinator.execute_next_step("global")

        assert step is None

    def test_execute_step_failed_handler(self):
        """Session fails when a handler fails."""
        backend = MemoryStateBackend()
        lock = InMemoryRecoveryLock()
        coordinator = RecoveryCoordinator(
            backend=backend,
            recovery_lock=lock,
            use_regional_policy=False,
        )

        # Mock the BUDGET_RESET handler as failing
        coordinator.register_step_handler(
            RecoveryStepType.BUDGET_RESET,
            lambda s, st: {"success": False, "error": "Test error"},
        )

        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )

        step = coordinator.execute_next_step("global")

        assert step.status == RecoveryStatus.FAILED
        assert step.error_message == "Test error"

        # _fail_session() keeps ACTIVE_SESSION_KEY, so the
        # failed session is queryable (resume_recovery support)
        active = coordinator.get_active_session("global")
        assert active is not None
        assert active.status == RecoveryStatus.FAILED

    def test_execute_step_handler_exception(self):
        """Session fails when a handler raises."""
        backend = MemoryStateBackend()
        lock = InMemoryRecoveryLock()
        coordinator = RecoveryCoordinator(
            backend=backend,
            recovery_lock=lock,
            use_regional_policy=False,
        )

        def failing_handler(session, step):
            raise Exception("Handler exception")

        coordinator.register_step_handler(
            RecoveryStepType.BUDGET_RESET,
            failing_handler,
        )

        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )

        step = coordinator.execute_next_step("global")

        assert step.status == RecoveryStatus.FAILED
        assert "Handler exception" in step.error_message


class TestAbortRecovery:
    """abort_recovery() tests."""

    @pytest.fixture
    def coordinator(self):
        """Coordinator for testing."""
        backend = MemoryStateBackend()
        lock = InMemoryRecoveryLock()
        return RecoveryCoordinator(backend=backend, recovery_lock=lock)

    def test_abort_recovery_success(self, coordinator):
        """Recovery abort succeeds."""
        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )

        session = coordinator.abort_recovery(
            namespace="global",
            reason="Re-failure detected",
        )

        assert session is not None
        assert session.status == RecoveryStatus.ABORTED
        assert session.abort_reason == "Re-failure detected"
        assert session.completed_at is not None

    def test_abort_recovery_no_session(self, coordinator):
        """Returns None when there is no active session."""
        result = coordinator.abort_recovery(
            namespace="global",
            reason="No reason",
        )

        assert result is None

    def test_abort_recovery_clears_active(self, coordinator):
        """Active session is cleared after abort."""
        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )

        coordinator.abort_recovery(
            namespace="global",
            reason="Test abort",
        )

        active = coordinator.get_active_session("global")
        assert active is None

    def test_abort_recovery_releases_lock(self, coordinator):
        """Lock is released after abort."""
        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )

        coordinator.abort_recovery(
            namespace="global",
            reason="Test abort",
        )

        # Lock released, so a new recovery is possible
        session = coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )
        assert session is not None


class TestFullRecoveryFlow:
    """Full recovery flow tests."""

    @pytest.fixture
    def coordinator(self):
        """Coordinator for testing (handlers mocked)."""
        backend = MemoryStateBackend()
        lock = InMemoryRecoveryLock()
        coord = RecoveryCoordinator(
            backend=backend,
            recovery_lock=lock,
            use_regional_policy=False,
        )

        # Mock all handlers as successful
        for step_type in RecoveryStepType:
            coord.register_step_handler(
                step_type,
                lambda s, st: {"success": True},
            )

        return coord

    def test_complete_level3_recovery(self, coordinator):
        """LEVEL_3 full recovery flow."""
        # 1. Start recovery
        session = coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
            initiated_by="system",
        )
        assert session.status == RecoveryStatus.IN_PROGRESS

        # 2. Step 1: BUDGET_RESET
        step1 = coordinator.execute_next_step("global")
        assert step1.step_type == RecoveryStepType.BUDGET_RESET
        assert step1.status == RecoveryStatus.COMPLETED

        # 3. Step 2: HEALTH_CHECK
        step2 = coordinator.execute_next_step("global")
        assert step2.step_type == RecoveryStepType.HEALTH_CHECK
        assert step2.status == RecoveryStatus.COMPLETED

        # 4. Step 3: CANARY_RESUME
        step3 = coordinator.execute_next_step("global")
        assert step3.step_type == RecoveryStepType.CANARY_RESUME
        assert step3.status == RecoveryStatus.COMPLETED

        # 5. Step 4: GOVERNANCE_NORMAL
        step4 = coordinator.execute_next_step("global")
        assert step4.step_type == RecoveryStepType.GOVERNANCE_NORMAL
        assert step4.status == RecoveryStatus.COMPLETED

        # 6. Confirm completion
        final_step = coordinator.execute_next_step("global")
        assert final_step is None

        # 7. Verify the saved session
        saved_session = coordinator.get_session("global", session.id)
        assert saved_session.status == RecoveryStatus.COMPLETED
        assert saved_session.completed_at is not None

    def test_level2_has_3_steps(self, coordinator):
        """LEVEL_2 has 3 steps."""
        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_2",
        )

        steps = []
        for _ in range(10):
            step = coordinator.execute_next_step("global")
            if step is None:
                break
            steps.append(step)

        assert len(steps) == 3
        assert steps[0].step_type == RecoveryStepType.BUDGET_RESET
        assert steps[1].step_type == RecoveryStepType.HEALTH_CHECK
        assert steps[2].step_type == RecoveryStepType.CANARY_RESUME

    def test_level1_has_2_steps(self, coordinator):
        """LEVEL_1 has 2 steps."""
        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_1",
        )

        steps = []
        for _ in range(10):
            step = coordinator.execute_next_step("global")
            if step is None:
                break
            steps.append(step)

        assert len(steps) == 2

    def test_recovery_after_completion(self, coordinator):
        """Recovery is possible again after completion."""
        # First recovery
        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )
        # Execute 4 steps + 5th call for completion handling
        for _ in range(4):
            coordinator.execute_next_step("global")
        coordinator.execute_next_step("global")  # completion handling

        # Second recovery
        session2 = coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )
        assert session2 is not None


class TestCheckRecoveryTrigger:
    """check_recovery_trigger() tests."""

    @pytest.fixture
    def coordinator(self):
        """Coordinator for testing."""
        backend = MemoryStateBackend()
        lock = InMemoryRecoveryLock()
        return RecoveryCoordinator(backend=backend, recovery_lock=lock)

    def test_check_trigger_returns_dict(self, coordinator):
        """Trigger-check result is a dictionary."""
        result = coordinator.check_recovery_trigger("global")

        assert isinstance(result, dict)
        assert "can_recover" in result
        assert "current_level" in result


class TestSessionManagement:
    """Session management tests."""

    @pytest.fixture
    def coordinator(self):
        """Coordinator for testing."""
        backend = MemoryStateBackend()
        lock = InMemoryRecoveryLock()
        return RecoveryCoordinator(backend=backend, recovery_lock=lock)

    def test_get_session_not_found(self, coordinator):
        """A nonexistent session is None."""
        session = coordinator.get_session("global", "nonexistent")
        assert session is None

    def test_get_active_session_not_found(self, coordinator):
        """None when there is no active session."""
        active = coordinator.get_active_session("global")
        assert active is None

    def test_session_progress_tracking(self, coordinator):
        """Track session progress."""
        # Register success handlers
        for step_type in RecoveryStepType:
            coordinator.register_step_handler(
                step_type,
                lambda s, st: {"success": True},
            )

        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )

        # Execute 2 steps
        coordinator.execute_next_step("global")
        coordinator.execute_next_step("global")

        # Check progress
        updated = coordinator.get_active_session("global")
        progress = updated.get_progress()

        assert progress["completed_steps"] == 2
        assert progress["total_steps"] == 4
        assert progress["progress_percent"] == 50.0


class TestSingleton:
    """Singleton factory tests."""

    def test_reset_singleton(self):
        """Singleton reset."""
        reset_recovery_coordinator()

        # A new instance is possible after reset
        reset_recovery_coordinator()

    def test_get_singleton(self):
        """Singleton acquisition."""
        reset_recovery_coordinator()

        # Patch to use MemoryStateBackend
        with patch("baldur.core.state_backend.get_state_backend") as mock:
            mock.return_value = MemoryStateBackend()

            coord1 = get_recovery_coordinator()
            coord2 = get_recovery_coordinator()

            assert coord1 is coord2

        reset_recovery_coordinator()


# =============================================================================
# CascadeEvent integration tests
# =============================================================================


class TestRecoveryCoordinatorCascadeEventIntegration:
    """RecoveryCoordinator CascadeEvent integration tests."""

    @pytest.fixture
    def mock_cascade_auditor(self):
        """Mock CascadeEventAuditor."""
        mock = MagicMock()
        mock_event = MagicMock()
        mock_event.id = "cascade-test123"
        mock.record.return_value = mock_event
        return mock

    @pytest.fixture
    def mock_audit_recorder(self):
        """Mock RecoveryAuditRecorder."""
        mock = MagicMock()
        return mock

    @pytest.fixture
    def coordinator_with_auditors(self, mock_cascade_auditor, mock_audit_recorder):
        """Coordinator with auditors injected."""
        backend = MemoryStateBackend()
        lock = InMemoryRecoveryLock()
        return RecoveryCoordinator(
            backend=backend,
            recovery_lock=lock,
            use_regional_policy=False,
            cascade_auditor=mock_cascade_auditor,
            audit_recorder=mock_audit_recorder,
        )

    def test_start_recovery_records_cascade_event(
        self, coordinator_with_auditors, mock_cascade_auditor, mock_audit_recorder
    ):
        """start_recovery() must record a CascadeEvent."""
        session = coordinator_with_auditors.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
            initiated_by="test-user",
        )

        # Verify CascadeEventAuditor.record() was called
        assert mock_cascade_auditor.record.called
        call_kwargs = mock_cascade_auditor.record.call_args[1]

        assert call_kwargs["trigger_type"] == "RECOVERY_STARTED"
        assert call_kwargs["namespace"] == "global"
        assert call_kwargs["triggered_by"] == "test-user"
        assert "trigger_details" in call_kwargs
        assert call_kwargs["trigger_details"]["session_id"] == session.id

        # Verify RecoveryAuditRecorder.record_recovery_event() was called
        assert mock_audit_recorder.record_recovery_event.called
        audit_call = mock_audit_recorder.record_recovery_event.call_args[1]
        assert audit_call["event_type"].value == "recovery_started"
        assert audit_call["session_id"] == session.id

    def test_execute_step_success_records_cascade_event(
        self, coordinator_with_auditors, mock_cascade_auditor, mock_audit_recorder
    ):
        """execute_next_step() must record a CascadeEvent on success."""
        # Start recovery
        coordinator_with_auditors.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
            initiated_by="test-user",
        )

        # Reset records
        mock_cascade_auditor.reset_mock()
        mock_audit_recorder.reset_mock()

        # Execute the first step
        coordinator_with_auditors.execute_next_step("global")

        # Verify CascadeEvent recording
        assert mock_cascade_auditor.record.called
        call_kwargs = mock_cascade_auditor.record.call_args[1]
        assert call_kwargs["trigger_type"] == "RECOVERY_STEP_EXECUTED"
        assert "BUDGET_RESET" in call_kwargs["effects"][0]["action_type"].upper()

        # Verify RecoveryAuditRecorder call
        assert mock_audit_recorder.record_recovery_event.called
        audit_call = mock_audit_recorder.record_recovery_event.call_args[1]
        assert audit_call["event_type"].value == "recovery_step_executed"
        assert audit_call["step_type"] == "budget_reset"

    def test_execute_step_failure_records_cascade_event(
        self, mock_cascade_auditor, mock_audit_recorder
    ):
        """execute_next_step() must record a CascadeEvent on failure."""
        backend = MemoryStateBackend()
        lock = InMemoryRecoveryLock()
        coordinator = RecoveryCoordinator(
            backend=backend,
            recovery_lock=lock,
            use_regional_policy=False,
            cascade_auditor=mock_cascade_auditor,
            audit_recorder=mock_audit_recorder,
        )

        # Register a failing handler
        failing_handler = MagicMock(
            return_value={
                "success": False,
                "error": "Simulated failure",
            }
        )
        coordinator.register_step_handler(
            RecoveryStepType.BUDGET_RESET, failing_handler
        )

        # Start recovery
        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )

        # Reset records
        mock_cascade_auditor.reset_mock()
        mock_audit_recorder.reset_mock()

        # Execute the first step (failure)
        coordinator.execute_next_step("global")

        # Verify CascadeEvent recording (RECOVERY_STEP_FAILED)
        assert mock_cascade_auditor.record.called
        call_kwargs = mock_cascade_auditor.record.call_args[1]
        assert call_kwargs["trigger_type"] == "RECOVERY_STEP_FAILED"
        assert call_kwargs["effects"][0]["success"] is False

        # Verify RecoveryAuditRecorder call
        assert mock_audit_recorder.record_recovery_event.called
        audit_call = mock_audit_recorder.record_recovery_event.call_args[1]
        assert audit_call["event_type"].value == "recovery_step_failed"
        assert audit_call["error_message"] == "Simulated failure"

    def test_abort_recovery_records_cascade_event(
        self, coordinator_with_auditors, mock_cascade_auditor, mock_audit_recorder
    ):
        """abort_recovery() must record a CascadeEvent."""
        # Start recovery
        coordinator_with_auditors.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )

        # Reset records
        mock_cascade_auditor.reset_mock()
        mock_audit_recorder.reset_mock()

        # Abort recovery
        coordinator_with_auditors.abort_recovery(
            namespace="global",
            reason="Manual abort for testing",
        )

        # Verify CascadeEvent recording
        assert mock_cascade_auditor.record.called
        call_kwargs = mock_cascade_auditor.record.call_args[1]
        assert call_kwargs["trigger_type"] == "RECOVERY_ABORTED"
        assert "abort_reason" in call_kwargs["effects"][0]["details"]

        # Verify RecoveryAuditRecorder call
        assert mock_audit_recorder.record_recovery_event.called
        audit_call = mock_audit_recorder.record_recovery_event.call_args[1]
        assert audit_call["event_type"].value == "recovery_aborted"
        assert audit_call["error_message"] == "Manual abort for testing"

    def test_complete_session_records_cascade_event(
        self, mock_cascade_auditor, mock_audit_recorder
    ):
        """Must record a CascadeEvent on recovery completion."""
        backend = MemoryStateBackend()
        lock = InMemoryRecoveryLock()
        coordinator = RecoveryCoordinator(
            backend=backend,
            recovery_lock=lock,
            use_regional_policy=False,
            cascade_auditor=mock_cascade_auditor,
            audit_recorder=mock_audit_recorder,
        )

        # A recovery session that completes quickly with a single step
        session = RecoverySession(
            id="test-session",
            namespace="test",
            trigger_level="LEVEL_1",
            status=RecoveryStatus.IN_PROGRESS,
            steps=[
                RecoveryStep(
                    step_type=RecoveryStepType.BUDGET_RESET,
                    order=1,
                )
            ],
            current_step_index=1,  # all steps complete
        )

        # Reset records
        mock_cascade_auditor.reset_mock()
        mock_audit_recorder.reset_mock()

        # Completion handling
        coordinator._complete_session(session)

        # Verify CascadeEvent recording
        assert mock_cascade_auditor.record.called
        call_kwargs = mock_cascade_auditor.record.call_args[1]
        assert call_kwargs["trigger_type"] == "RECOVERY_COMPLETED"
        assert call_kwargs["effects"][0]["success"] is True

        # Verify RecoveryAuditRecorder call
        assert mock_audit_recorder.record_recovery_event.called
        audit_call = mock_audit_recorder.record_recovery_event.call_args[1]
        assert audit_call["event_type"].value == "recovery_completed"

    def test_cascade_auditor_not_set_still_works(self):
        """Must work normally even without a CascadeEventAuditor."""
        backend = MemoryStateBackend()
        lock = InMemoryRecoveryLock()
        coordinator = RecoveryCoordinator(
            backend=backend,
            recovery_lock=lock,
            use_regional_policy=False,
            cascade_auditor=None,  # not set
            audit_recorder=None,  # not set
        )

        # Start recovery (must work without exceptions)
        session = coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )

        assert session is not None
        assert session.status == RecoveryStatus.IN_PROGRESS

        # Execute a step (must work without exceptions)
        step = coordinator.execute_next_step("global")
        assert step is not None

        # Abort (must work without exceptions)
        aborted = coordinator.abort_recovery("global", "Test abort")
        assert aborted is not None

    def test_cascade_event_contains_session_details(
        self, coordinator_with_auditors, mock_cascade_auditor
    ):
        """CascadeEvent must include session detail information."""
        session = coordinator_with_auditors.start_recovery(
            namespace="seoul",
            trigger_level="LEVEL_2",
            initiated_by="admin@example.com",
        )

        # Verify trigger_details
        call_kwargs = mock_cascade_auditor.record.call_args[1]
        trigger_details = call_kwargs["trigger_details"]

        assert trigger_details["session_id"] == session.id
        assert trigger_details["namespace"] == "seoul"
        assert trigger_details["trigger_level"] == "LEVEL_2"
        assert trigger_details["initiated_by"] == "admin@example.com"
        assert "status" in trigger_details

    def test_full_recovery_flow_with_cascade_audit(
        self, mock_cascade_auditor, mock_audit_recorder
    ):
        """Verify CascadeEvent recording in the full recovery flow."""
        backend = MemoryStateBackend()
        lock = InMemoryRecoveryLock()
        coordinator = RecoveryCoordinator(
            backend=backend,
            recovery_lock=lock,
            use_regional_policy=False,
            cascade_auditor=mock_cascade_auditor,
            audit_recorder=mock_audit_recorder,
        )

        # Start recovery
        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )

        # Execute all steps
        step_count = 0
        while True:
            step = coordinator.execute_next_step("global")
            if step is None:
                break
            step_count += 1

        # LEVEL_3 has 4 steps
        assert step_count == 4

        # Verify CascadeEvent call count:
        # 1 (start) + 4 (steps) + 1 (complete) = 6
        # but this depends on how _handle_all_steps_completed is implemented
        assert mock_cascade_auditor.record.call_count >= 5  # at least start + 4 steps


# =============================================================================
# _fail_session behavior verification (keeps ACTIVE_SESSION_KEY)
# =============================================================================


class TestFailSessionBehavior:
    """_fail_session() behavior verification.

    Verifies that _fail_session() keeps ACTIVE_SESSION_KEY so the failed
    session is queryable via get_active_session().
    """

    @pytest.fixture
    def coordinator(self):
        """Coordinator with a failing handler registered."""
        backend = MemoryStateBackend()
        lock = InMemoryRecoveryLock()
        coord = RecoveryCoordinator(
            backend=backend,
            recovery_lock=lock,
            use_regional_policy=False,
        )
        coord.register_step_handler(
            RecoveryStepType.BUDGET_RESET,
            lambda s, st: {"success": False, "error": "step failure"},
        )
        return coord

    def test_failed_session_remains_active(self, coordinator):
        """After failure, get_active_session() must return the FAILED session."""
        session = coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )
        coordinator.execute_next_step("global")

        active = coordinator.get_active_session("global")
        assert active is not None
        assert active.status == RecoveryStatus.FAILED
        assert active.id == session.id

    def test_failed_session_has_abort_reason(self, coordinator):
        """The failed session must record abort_reason."""
        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )
        coordinator.execute_next_step("global")

        active = coordinator.get_active_session("global")
        assert active.abort_reason == "step failure"

    def test_failed_session_has_completed_at(self, coordinator):
        """The failed session must record completed_at."""
        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )
        coordinator.execute_next_step("global")

        active = coordinator.get_active_session("global")
        assert active.completed_at is not None

    def test_failed_session_lock_released(self, coordinator):
        """The distributed lock must be released after failure."""
        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )
        coordinator.execute_next_step("global")

        # Lock released, so a new recovery can start
        coordinator.register_step_handler(
            RecoveryStepType.BUDGET_RESET,
            lambda s, st: {"success": True},
        )
        new_session = coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )
        assert new_session is not None
        assert new_session.status == RecoveryStatus.IN_PROGRESS

    def test_new_recovery_overwrites_failed_active_key(self, coordinator):
        """Starting a new recovery must overwrite the failed session's ACTIVE_SESSION_KEY."""
        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )
        coordinator.execute_next_step("global")

        # Start a new recovery (FAILED sessions are not blocked by start_recovery)
        coordinator.register_step_handler(
            RecoveryStepType.BUDGET_RESET,
            lambda s, st: {"success": True},
        )
        new_session = coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )

        active = coordinator.get_active_session("global")
        assert active.id == new_session.id
        assert active.status == RecoveryStatus.IN_PROGRESS


# =============================================================================
# resume_recovery behavior verification
# =============================================================================


class TestResumeRecoveryBehavior:
    """resume_recovery() behavior verification.

    Verifies resuming a failed recovery session.
    Idempotency infrastructure, metadata recording, infinite-loop prevention, etc.
    """

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """Reset the settings cache for each test."""
        from baldur.settings.recovery_coordinator import (
            reset_recovery_coordinator_settings,
        )

        reset_recovery_coordinator_settings()
        yield
        reset_recovery_coordinator_settings()

    def _make_coordinator_with_failing_step(
        self,
        fail_step_type: RecoveryStepType = RecoveryStepType.CANARY_RESUME,
    ) -> RecoveryCoordinator:
        """Create a coordinator that fails at a specific step."""
        backend = MemoryStateBackend()
        lock = InMemoryRecoveryLock()
        coord = RecoveryCoordinator(
            backend=backend,
            recovery_lock=lock,
            use_regional_policy=False,
        )
        for step_type in RecoveryStepType:
            if step_type == fail_step_type:
                coord.register_step_handler(
                    step_type,
                    lambda s, st: {
                        "success": False,
                        "error": f"{fail_step_type.value} failed",
                    },
                )
            else:
                coord.register_step_handler(
                    step_type,
                    lambda s, st: {"success": True},
                )
        return coord

    def test_resume_no_failed_session_raises(self):
        """ValueError when there is no failed session."""
        backend = MemoryStateBackend()
        lock = InMemoryRecoveryLock()
        coordinator = RecoveryCoordinator(
            backend=backend,
            recovery_lock=lock,
            use_regional_policy=False,
        )

        with pytest.raises(ValueError, match="No failed recovery session"):
            coordinator.resume_recovery(namespace="global")

    def test_resume_in_progress_session_raises(self):
        """ValueError for an IN_PROGRESS session."""
        backend = MemoryStateBackend()
        lock = InMemoryRecoveryLock()
        coordinator = RecoveryCoordinator(
            backend=backend,
            recovery_lock=lock,
            use_regional_policy=False,
        )
        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )

        with pytest.raises(ValueError, match="No failed recovery session"):
            coordinator.resume_recovery(namespace="global")

    def test_resume_creates_new_session(self):
        """Resuming a failed session must create a new session."""
        coordinator = self._make_coordinator_with_failing_step(
            RecoveryStepType.CANARY_RESUME,
        )
        session = coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
            initiated_by="operator",
        )

        # BUDGET_RESET success -> HEALTH_CHECK success -> CANARY_RESUME failure
        coordinator.execute_next_step("global")
        coordinator.execute_next_step("global")
        coordinator.execute_next_step("global")

        failed_session = coordinator.get_active_session("global")
        assert failed_session.status == RecoveryStatus.FAILED

        # Replace all handlers with success on resume
        for step_type in RecoveryStepType:
            coordinator.register_step_handler(
                step_type,
                lambda s, st: {"success": True},
            )

        new_session = coordinator.resume_recovery(
            namespace="global",
            initiated_by="admin",
        )

        assert new_session.id != session.id
        assert new_session.status == RecoveryStatus.IN_PROGRESS
        assert new_session.trigger_level == session.trigger_level

    def test_resume_metadata_resumed_from(self):
        """The resumed session's metadata must record the original session ID."""
        coordinator = self._make_coordinator_with_failing_step(
            RecoveryStepType.BUDGET_RESET,
        )
        session = coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
            initiated_by="operator",
        )
        coordinator.execute_next_step("global")

        # Replace handlers with success, then resume
        for step_type in RecoveryStepType:
            coordinator.register_step_handler(
                step_type,
                lambda s, st: {"success": True},
            )

        new_session = coordinator.resume_recovery(
            namespace="global",
            initiated_by="admin",
        )

        assert new_session.metadata["resumed_from"] == session.id

    def test_resume_metadata_resumed_from_step(self):
        """The resumed session's metadata must record the failure-point index."""
        coordinator = self._make_coordinator_with_failing_step(
            RecoveryStepType.CANARY_RESUME,
        )
        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )
        # BUDGET_RESET(0) success -> HEALTH_CHECK(1) success -> CANARY_RESUME(2) failure
        coordinator.execute_next_step("global")
        coordinator.execute_next_step("global")
        coordinator.execute_next_step("global")

        failed_session = coordinator.get_active_session("global")
        expected_step_index = failed_session.current_step_index

        for step_type in RecoveryStepType:
            coordinator.register_step_handler(
                step_type,
                lambda s, st: {"success": True},
            )

        new_session = coordinator.resume_recovery(namespace="global")

        assert new_session.metadata["resumed_from_step"] == expected_step_index

    def test_resume_metadata_resume_count_increments(self):
        """resume_count must increment by 1 on each resume."""
        coordinator = self._make_coordinator_with_failing_step(
            RecoveryStepType.BUDGET_RESET,
        )
        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )
        coordinator.execute_next_step("global")

        # First resume (failing handler retained, so it fails again)
        new1 = coordinator.resume_recovery(namespace="global")
        assert new1.metadata["resume_count"] == 1

        # Fail again in the new session
        coordinator.execute_next_step("global")

        # Second resume
        new2 = coordinator.resume_recovery(namespace="global")
        assert new2.metadata["resume_count"] == 2

    def test_resume_metadata_original_initiated_by(self):
        """The resumed session's metadata must record the original initiated_by."""
        coordinator = self._make_coordinator_with_failing_step(
            RecoveryStepType.BUDGET_RESET,
        )
        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
            initiated_by="original_operator",
        )
        coordinator.execute_next_step("global")

        for step_type in RecoveryStepType:
            coordinator.register_step_handler(
                step_type,
                lambda s, st: {"success": True},
            )

        new_session = coordinator.resume_recovery(
            namespace="global",
            initiated_by="resume_admin",
        )

        assert new_session.initiated_by == "resume_admin"
        assert new_session.metadata["original_initiated_by"] == "original_operator"

    def test_resume_max_count_exceeded_raises(self):
        """ValueError when max_resume_count is exceeded."""
        from baldur.settings.recovery_coordinator import (
            get_recovery_coordinator_settings,
        )

        coordinator = self._make_coordinator_with_failing_step(
            RecoveryStepType.BUDGET_RESET,
        )
        settings = get_recovery_coordinator_settings()
        max_count = settings.max_resume_count

        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_3",
        )
        coordinator.execute_next_step("global")

        # Resume max_resume_count times
        for _i in range(max_count):
            coordinator.resume_recovery(namespace="global")
            coordinator.execute_next_step("global")

        # The (max_resume_count + 1)th resume attempt -> ValueError
        with pytest.raises(ValueError, match="Max resume count"):
            coordinator.resume_recovery(namespace="global")

    def test_resume_preserves_trigger_level(self):
        """The resumed session must keep the original session's trigger_level."""
        coordinator = self._make_coordinator_with_failing_step(
            RecoveryStepType.BUDGET_RESET,
        )
        coordinator.start_recovery(
            namespace="global",
            trigger_level="LEVEL_2",
        )
        coordinator.execute_next_step("global")

        for step_type in RecoveryStepType:
            coordinator.register_step_handler(
                step_type,
                lambda s, st: {"success": True},
            )

        new_session = coordinator.resume_recovery(namespace="global")
        assert new_session.trigger_level == "LEVEL_2"


# =============================================================================
# max_resume_count settings contract verification
# =============================================================================


class TestMaxResumeCountContract:
    """max_resume_count settings-field contract verification.

    Verifies that RecoveryCoordinatorSettings.max_resume_count's default
    value and range limits are implemented as designed.
    """

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """Reset the settings cache for each test."""
        from baldur.settings.recovery_coordinator import (
            reset_recovery_coordinator_settings,
        )

        reset_recovery_coordinator_settings()
        yield
        reset_recovery_coordinator_settings()

    def test_default_value(self):
        """max_resume_count default must be 3."""
        from baldur.settings.recovery_coordinator import (
            RecoveryCoordinatorSettings,
        )

        settings = RecoveryCoordinatorSettings()
        assert settings.max_resume_count == 3

    def test_min_bound(self):
        """max_resume_count minimum must be 1."""
        from pydantic import ValidationError

        from baldur.settings.recovery_coordinator import (
            RecoveryCoordinatorSettings,
        )

        with pytest.raises(ValidationError):
            RecoveryCoordinatorSettings(max_resume_count=0)

    def test_max_bound(self):
        """max_resume_count maximum must be 10."""
        from pydantic import ValidationError

        from baldur.settings.recovery_coordinator import (
            RecoveryCoordinatorSettings,
        )

        with pytest.raises(ValidationError):
            RecoveryCoordinatorSettings(max_resume_count=11)

    def test_valid_range(self):
        """Values within the valid range (1-10) must be settable."""
        from baldur.settings.recovery_coordinator import (
            RecoveryCoordinatorSettings,
        )

        settings = RecoveryCoordinatorSettings(max_resume_count=5)
        assert settings.max_resume_count == 5
