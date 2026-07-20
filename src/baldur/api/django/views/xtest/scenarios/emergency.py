"""
Integration test scenarios for Emergency and SafetyInterlock.

Provides the Emergency Recovery Flow and SafetyInterlock Canary Rollback
scenarios.
"""

import time
import uuid

from .base import (
    IntegrationScenario,
)


class FullEmergencyRecoveryScenario(IntegrationScenario):
    """
    Scenario: Emergency LEVEL_3 -> SafetyInterlock rollback -> the
    RecoveryCoordinator's 4-step reverse-order recovery.

    Steps:
    1. Check the initial state (Emergency: NORMAL)
    2. Inject Emergency LEVEL_3
    3. SafetyInterlock check (action: ROLLBACK)
    4. Confirm the Canary ROLLBACK
    5. Call RecoveryCoordinator.start_recovery()
    6. Step 1: run BUDGET_RESET (multiplier: 1.0x)
    7. Step 2: run HEALTH_CHECK (health_passed: true)
    8. Step 3: run CANARY_RESUME (canary_resumed: true)
    9. Step 4: run GOVERNANCE_NORMAL (governance: NORMAL)
    10. Check the final state (all components healthy)

    Config options:
    - skip_wait: bool - skip the HEALTH_CHECK wait (faster tests)
    - wait_seconds: int - custom wait duration (default: 5s, 0 when
      skip_wait=True)
    """

    scenario_name = "full_emergency_recovery_flow"
    max_timeout_seconds = 120

    def execute(self) -> None:  # noqa: C901, PLR0912, PLR0915
        try:
            from baldur_pro.services.canary.interlock import (
                CanarySafetyInterlock,
            )
        except ImportError:
            CanarySafetyInterlock = None  # type: ignore[assignment,misc]
        try:
            from baldur_pro.services.coordination.enums import RecoveryStatus
        except ImportError:
            RecoveryStatus = None  # type: ignore[assignment,misc]
        try:
            from baldur_pro.services.coordination.recovery_state import (
                RecoveryStepType,
            )
        except ImportError:
            RecoveryStepType = None  # type: ignore[assignment,misc]
        from baldur.models.emergency import EmergencyLevel

        skip_wait = self.config.get("skip_wait", True)
        wait_seconds = self.config.get("wait_seconds", 0 if skip_wait else 5)

        # Set up the mock dependencies
        mock_tracker = None

        class MockEmergencyTracker:
            """Mock tracker that simulates the Emergency state."""

            def __init__(self):
                self._level = EmergencyLevel.NORMAL
                self._namespace = "global"

            def set_level(self, level: EmergencyLevel) -> None:
                self._level = level

            def get_effective_state(self, namespace=None):
                class MockState:
                    def __init__(inner_self):
                        inner_self.emergency_level = self._level
                        inner_self.namespace = namespace or self._namespace

                return MockState()

        mock_tracker = MockEmergencyTracker()

        # Track the Canary rollback state
        rollback_triggered = False
        canary_resumed = False

        class MockCanaryService:
            """Canary service mock."""

            def rollback(
                inner_self, rollout_id: str, reason: str = "", **kwargs
            ) -> bool:
                nonlocal rollback_triggered
                rollback_triggered = True
                return True

            def pause(inner_self, rollout_id: str, reason: str = "", **kwargs) -> bool:
                return True

            def resume(inner_self, rollout_id: str) -> bool:
                nonlocal canary_resumed
                canary_resumed = True
                return True

        mock_canary_service = MockCanaryService()

        # Track the recovery session
        recovery_session_id = None
        recovery_steps_completed = []

        class MockRecoveryCoordinator:
            """RecoveryCoordinator mock - simulates 4-step reverse recovery."""

            def __init__(inner_self):
                inner_self.current_step = 0
                inner_self.session_id = None
                inner_self.steps = [
                    {
                        "type": RecoveryStepType.BUDGET_RESET,
                        "result": {"multiplier": 1.0},
                    },
                    {
                        "type": RecoveryStepType.HEALTH_CHECK,
                        "result": {"health_passed": True},
                    },
                    {
                        "type": RecoveryStepType.CANARY_RESUME,
                        "result": {"canary_resumed": True},
                    },
                    {
                        "type": RecoveryStepType.GOVERNANCE_NORMAL,
                        "result": {"governance": "NORMAL"},
                    },
                ]

            def start_recovery(
                inner_self,
                namespace: str,
                trigger_level: str,
                initiated_by: str = "system",
            ):
                inner_self.session_id = f"recovery-{uuid.uuid4().hex[:12]}"
                inner_self.current_step = 0

                class MockSession:
                    def __init__(session_self):
                        session_self.id = inner_self.session_id
                        session_self.namespace = namespace
                        session_self.trigger_level = trigger_level
                        session_self.status = RecoveryStatus.IN_PROGRESS

                return MockSession()

            def execute_next_step(inner_self, namespace: str):
                if inner_self.current_step >= len(inner_self.steps):
                    return None

                step_info = inner_self.steps[inner_self.current_step]

                class MockStep:
                    def __init__(step_self):
                        step_self.step_type = step_info["type"]
                        step_self.status = RecoveryStatus.COMPLETED
                        step_self.result = step_info["result"]

                inner_self.current_step += 1
                return MockStep()

        mock_coordinator = MockRecoveryCoordinator()

        # =====================================================================
        # Step 1: check the initial state (Emergency: NORMAL)
        # =====================================================================
        def step1():
            state = mock_tracker.get_effective_state()
            return f"Emergency: {state.emergency_level.value}"

        if not self._execute_step(
            1, "check_initial_state", "emergency", "Emergency: normal", step1
        ):
            return

        # =====================================================================
        # Step 2: inject Emergency LEVEL_3
        # =====================================================================
        def step2():
            mock_tracker.set_level(EmergencyLevel.LEVEL_3)
            state = mock_tracker.get_effective_state()
            return f"state: {state.emergency_level.value}"

        if not self._execute_step(
            2, "inject_emergency_level3", "emergency", "state: level_3", step2
        ):
            return

        # =====================================================================
        # Step 3: SafetyInterlock check (action: ROLLBACK)
        # =====================================================================
        def step3():
            interlock = CanarySafetyInterlock(
                emergency_tracker_factory=lambda: mock_tracker
            )
            result = interlock.check(operation="promote", rollout_id="test-rollout-001")
            return f"action: {result.action.value.upper()}"

        if not self._execute_step(
            3, "check_safety_interlock", "interlock", "action: ROLLBACK", step3
        ):
            return

        # =====================================================================
        # Step 4: confirm the Canary ROLLBACK
        # =====================================================================
        def step4():
            interlock = CanarySafetyInterlock(
                emergency_tracker_factory=lambda: mock_tracker
            )
            interlock.check_and_apply(
                canary_service=mock_canary_service,
                rollout_id="test-rollout-001",
                operation="promote",
            )
            return f"rollback_triggered: {rollback_triggered}"

        if not self._execute_step(
            4, "confirm_canary_rollback", "canary", "rollback_triggered: True", step4
        ):
            return

        # =====================================================================
        # Step 5: call RecoveryCoordinator.start_recovery()
        # =====================================================================
        def step5():
            nonlocal recovery_session_id
            session = mock_coordinator.start_recovery(
                namespace="global",
                trigger_level="LEVEL_3",
                initiated_by="xtest",
            )
            recovery_session_id = session.id
            return f"session_id: {session.id}"

        if not self._execute_step(
            5, "start_recovery", "recovery", "session_id:", step5
        ):
            return

        # Store the recovery session ID
        if self.result:
            self.result.config = self.result.config or {}
            self.result.config["recovery_session_id"] = recovery_session_id

        # =====================================================================
        # Step 6: run BUDGET_RESET (multiplier: 1.0x)
        # =====================================================================
        def step6():
            step = mock_coordinator.execute_next_step("global")
            if step and step.step_type == RecoveryStepType.BUDGET_RESET:
                recovery_steps_completed.append("BUDGET_RESET")
                return f"multiplier: {step.result['multiplier']}x"
            return "BUDGET_RESET failed"

        if not self._execute_step(
            6, "execute_budget_reset", "recovery", "multiplier: 1.0x", step6
        ):
            return

        # =====================================================================
        # Step 7: run HEALTH_CHECK (health_passed: true)
        # =====================================================================
        def step7():
            # Time simulation: wait unless skip_wait is set
            if not skip_wait and wait_seconds > 0:
                time.sleep(wait_seconds)

            step = mock_coordinator.execute_next_step("global")
            if step and step.step_type == RecoveryStepType.HEALTH_CHECK:
                recovery_steps_completed.append("HEALTH_CHECK")
                return f"health_passed: {str(step.result['health_passed']).lower()}"
            return "HEALTH_CHECK failed"

        if not self._execute_step(
            7, "execute_health_check", "recovery", "health_passed: true", step7
        ):
            return

        # =====================================================================
        # Step 8: run CANARY_RESUME (canary_resumed: true)
        # =====================================================================
        def step8():
            step = mock_coordinator.execute_next_step("global")
            if step and step.step_type == RecoveryStepType.CANARY_RESUME:
                recovery_steps_completed.append("CANARY_RESUME")
                # Call resume on the mock
                mock_canary_service.resume("test-rollout-001")
                return f"canary_resumed: {str(canary_resumed).lower()}"
            return "CANARY_RESUME failed"

        if not self._execute_step(
            8, "execute_canary_resume", "recovery", "canary_resumed: true", step8
        ):
            return

        # =====================================================================
        # Step 9: run GOVERNANCE_NORMAL (governance: NORMAL)
        # =====================================================================
        def step9():
            step = mock_coordinator.execute_next_step("global")
            if step and step.step_type == RecoveryStepType.GOVERNANCE_NORMAL:
                recovery_steps_completed.append("GOVERNANCE_NORMAL")
                # Restore the Emergency level
                mock_tracker.set_level(EmergencyLevel.NORMAL)
                return f"governance: {step.result['governance']}"
            return "GOVERNANCE_NORMAL failed"

        if not self._execute_step(
            9, "execute_governance_normal", "recovery", "governance: NORMAL", step9
        ):
            return

        # =====================================================================
        # Step 10: check the final state (all components healthy)
        # =====================================================================
        def step10():
            state = mock_tracker.get_effective_state()
            all_steps_completed = len(recovery_steps_completed) == 4
            return f"emergency: {state.emergency_level.value}, recovery_completed: {all_steps_completed}"

        self._execute_step(
            10,
            "verify_final_state",
            "all",
            "emergency: NORMAL, recovery_completed: True",
            step10,
        )

        return


class SafetyInterlockCanaryRollbackScenario(IntegrationScenario):
    """
    Scenario: SafetyInterlock LEVEL_2 -> PAUSE -> LEVEL_3 -> ROLLBACK
    escalation.

    Steps:
    1. Simulate a Canary rollout in progress (canary_active: true)
    2. Inject Emergency LEVEL_2 (state: LEVEL_2)
    3. SafetyInterlock.check_and_apply() (action: PAUSE)
    4. Confirm the Canary is paused (canary_paused: true)
    5. Escalate Emergency to LEVEL_3 (state: LEVEL_3)
    6. SafetyInterlock.check_and_apply() (action: ROLLBACK)
    7. Confirm the Canary rollback (canary_rollback: true)
    """

    scenario_name = "safety_interlock_canary_rollback"
    max_timeout_seconds = 60

    def execute(self) -> None:  # noqa: C901
        try:
            from baldur_pro.services.canary.interlock import (
                CanarySafetyInterlock,
            )
        except ImportError:
            CanarySafetyInterlock = None  # type: ignore[assignment,misc]
        from baldur.models.emergency import EmergencyLevel

        # Set up the mock dependencies
        class MockEmergencyTracker:
            """Mock tracker that simulates the Emergency state."""

            def __init__(self):
                self._level = EmergencyLevel.NORMAL
                self._namespace = "global"

            def set_level(self, level: EmergencyLevel) -> None:
                self._level = level

            def get_effective_state(self, namespace=None):
                class MockState:
                    def __init__(inner_self):
                        inner_self.emergency_level = self._level
                        inner_self.namespace = namespace or self._namespace

                return MockState()

        mock_tracker = MockEmergencyTracker()

        # Track the Canary state
        canary_active = False
        canary_paused = False
        canary_rollback = False

        class MockCanaryService:
            """Canary service mock."""

            def start(inner_self, rollout_id: str) -> bool:
                nonlocal canary_active
                canary_active = True
                return True

            def pause(inner_self, rollout_id: str, reason: str = "", **kwargs) -> bool:
                nonlocal canary_paused
                canary_paused = True
                return True

            def rollback(
                inner_self, rollout_id: str, reason: str = "", **kwargs
            ) -> bool:
                nonlocal canary_rollback
                canary_rollback = True
                return True

        mock_canary_service = MockCanaryService()

        # =====================================================================
        # Step 1: simulate a Canary rollout in progress
        # =====================================================================
        def step1():
            mock_canary_service.start("test-rollout-001")
            return f"canary_active: {str(canary_active).lower()}"

        if not self._execute_step(
            1, "start_canary_rollout", "canary", "canary_active: true", step1
        ):
            return

        # =====================================================================
        # Step 2: inject Emergency LEVEL_2
        # =====================================================================
        def step2():
            mock_tracker.set_level(EmergencyLevel.LEVEL_2)
            state = mock_tracker.get_effective_state()
            return f"state: {state.emergency_level.value}"

        if not self._execute_step(
            2, "inject_emergency_level2", "emergency", "state: level_2", step2
        ):
            return

        # =====================================================================
        # Step 3: SafetyInterlock.check_and_apply() - PAUSE
        # =====================================================================
        def step3():
            interlock = CanarySafetyInterlock(
                emergency_tracker_factory=lambda: mock_tracker
            )
            result = interlock.check_and_apply(
                canary_service=mock_canary_service,
                rollout_id="test-rollout-001",
                operation="promote",
            )
            return f"action: {result.action.value.upper()}"

        if not self._execute_step(
            3, "check_safety_interlock_pause", "interlock", "action: PAUSE", step3
        ):
            return

        # =====================================================================
        # Step 4: confirm the Canary is paused
        # =====================================================================
        def step4():
            return f"canary_paused: {str(canary_paused).lower()}"

        if not self._execute_step(
            4, "confirm_canary_paused", "canary", "canary_paused: true", step4
        ):
            return

        # =====================================================================
        # Step 5: escalate Emergency to LEVEL_3
        # =====================================================================
        def step5():
            mock_tracker.set_level(EmergencyLevel.LEVEL_3)
            state = mock_tracker.get_effective_state()
            return f"state: {state.emergency_level.value}"

        if not self._execute_step(
            5, "escalate_to_level3", "emergency", "state: level_3", step5
        ):
            return

        # =====================================================================
        # Step 6: SafetyInterlock.check_and_apply() - ROLLBACK
        # =====================================================================
        def step6():
            interlock = CanarySafetyInterlock(
                emergency_tracker_factory=lambda: mock_tracker
            )
            result = interlock.check_and_apply(
                canary_service=mock_canary_service,
                rollout_id="test-rollout-001",
                operation="promote",
            )
            return f"action: {result.action.value.upper()}"

        if not self._execute_step(
            6, "check_safety_interlock_rollback", "interlock", "action: ROLLBACK", step6
        ):
            return

        # =====================================================================
        # Step 7: confirm the Canary rollback
        # =====================================================================
        def step7():
            return f"canary_rollback: {str(canary_rollback).lower()}"

        self._execute_step(
            7, "confirm_canary_rollback", "canary", "canary_rollback: true", step7
        )

        return


__all__ = [
    "FullEmergencyRecoveryScenario",
    "SafetyInterlockCanaryRollbackScenario",
]
