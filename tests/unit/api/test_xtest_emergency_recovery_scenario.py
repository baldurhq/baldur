"""
X-Test Emergency Recovery 시나리오 단위 테스트.

Emergency LEVEL_3 → SafetyInterlock 롤백 → RecoveryCoordinator 4단계 역순 복구
시나리오를 테스트합니다.

테스트 케이스:
- test_full_emergency_recovery_flow_scenario_execution: 10단계 전체 시나리오 실행
- test_safety_interlock_canary_rollback_scenario_execution: 7단계 에스컬레이션 시나리오 실행
- test_level3_triggers_rollback_action: LEVEL_3에서 ROLLBACK 액션 반환
- test_level2_triggers_pause_action: LEVEL_2에서 PAUSE 액션 반환
- test_recovery_steps_execute_in_order: 4단계 역순 복구 순차 실행
- test_skip_wait_option_skips_health_check_delay: skip_wait 옵션으로 대기 스킵
- test_scenario_registry_contains_new_scenarios: 레지스트리에 새 시나리오 등록 확인
"""

# Django 설정 구성 (테스트용)
import django
import pytest
from django.conf import settings

if not settings.configured:
    settings.configure(
        DEBUG=True,
        DATABASES={},
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
        ],
        REST_FRAMEWORK={},
        SECRET_KEY="test-secret-key",
    )
    django.setup()


class TestFullEmergencyRecoveryScenario:
    """Full Emergency Recovery 시나리오 테스트."""

    @pytest.fixture(scope="class")
    def scenario(self):
        """FullEmergencyRecoveryScenario 인스턴스 생성."""
        from baldur.api.django.views.xtest.scenarios import (
            FullEmergencyRecoveryScenario,
        )

        return FullEmergencyRecoveryScenario(
            service_name="test-service",
            config={"skip_wait": True},
        )

    @pytest.fixture(scope="class")
    def scenario_result(self, scenario):
        """시나리오 실행 결과 캐싱 (클래스 내 1회만 실행)."""
        return scenario.run()

    def test_scenario_name_is_correct(self, scenario):
        """시나리오 이름이 올바른지 확인."""
        assert scenario.scenario_name == "full_emergency_recovery_flow"

    def test_full_emergency_recovery_flow_scenario_execution(self, scenario_result):
        """10단계 전체 시나리오가 성공적으로 실행되는지 확인."""
        pytest.importorskip("baldur_pro")
        result = scenario_result

        assert result is not None
        assert result.scenario == "full_emergency_recovery_flow"
        assert result.service_name == "test-service"
        assert result.status.value == "completed"
        assert len(result.steps) == 10

        # 각 단계 성공 확인
        for step in result.steps:
            assert step.success is True, f"Step {step.step} failed: {step.error}"

    def test_recovery_steps_execute_in_order(self, scenario_result):
        """4단계 역순 복구가 순차적으로 실행되는지 확인."""
        pytest.importorskip("baldur_pro")
        result = scenario_result

        # Step 6-9가 복구 단계
        step_actions = [s.action for s in result.steps[5:9]]
        expected_actions = [
            "execute_budget_reset",
            "execute_health_check",
            "execute_canary_resume",
            "execute_governance_normal",
        ]
        assert step_actions == expected_actions

    def test_skip_wait_option_skips_health_check_delay(self):
        """skip_wait=True 옵션이 HEALTH_CHECK 대기를 스킵하는지 확인."""
        import time

        from baldur.api.django.views.xtest.scenarios import (
            FullEmergencyRecoveryScenario,
        )

        start_time = time.time()

        scenario = FullEmergencyRecoveryScenario(
            service_name="test-service",
            config={"skip_wait": True},
        )
        scenario.run()

        elapsed = time.time() - start_time
        # skip_wait=True면 5초 대기 없이 바로 진행
        assert elapsed < 3.0, f"Scenario took too long: {elapsed:.2f}s (expected < 3s)"

    def test_initial_state_is_emergency_normal(self, scenario_result):
        """초기 상태가 Emergency NORMAL인지 확인."""
        pytest.importorskip("baldur_pro")
        result = scenario_result

        step1 = result.steps[0]
        assert step1.action == "check_initial_state"
        assert "normal" in step1.actual

    def test_level3_injection_changes_state(self, scenario_result):
        """LEVEL_3 주입 후 상태 변경 확인."""
        pytest.importorskip("baldur_pro")
        result = scenario_result

        step2 = result.steps[1]
        assert step2.action == "inject_emergency_level3"
        assert "level_3" in step2.actual

    def test_safety_interlock_returns_rollback_action(self, scenario_result):
        """SafetyInterlock이 LEVEL_3에서 ROLLBACK 액션 반환 확인."""
        pytest.importorskip("baldur_pro")
        result = scenario_result

        step3 = result.steps[2]
        assert step3.action == "check_safety_interlock"
        assert "ROLLBACK" in step3.actual

    def test_canary_rollback_is_triggered(self, scenario_result):
        """Canary 롤백이 트리거되는지 확인."""
        pytest.importorskip("baldur_pro")
        result = scenario_result

        step4 = result.steps[3]
        assert step4.action == "confirm_canary_rollback"
        assert "True" in step4.actual

    def test_recovery_session_id_is_generated(self, scenario_result):
        """RecoveryCoordinator 세션 ID가 생성되는지 확인."""
        pytest.importorskip("baldur_pro")
        result = scenario_result

        step5 = result.steps[4]
        assert step5.action == "start_recovery"
        assert "session_id:" in step5.actual

    def test_budget_reset_sets_multiplier_to_1(self, scenario_result):
        """BUDGET_RESET 단계에서 multiplier가 1.0으로 설정되는지 확인."""
        pytest.importorskip("baldur_pro")
        result = scenario_result

        step6 = result.steps[5]
        assert step6.action == "execute_budget_reset"
        assert "1.0" in step6.actual

    def test_final_state_is_normal(self, scenario_result):
        """최종 상태가 정상(NORMAL)인지 확인."""
        pytest.importorskip("baldur_pro")
        result = scenario_result

        step10 = result.steps[9]
        assert step10.action == "verify_final_state"
        assert "normal" in step10.actual
        assert "True" in step10.actual


class TestSafetyInterlockCanaryRollbackScenario:
    """SafetyInterlock Canary 롤백 시나리오 테스트."""

    @pytest.fixture(scope="class")
    def scenario(self):
        """SafetyInterlockCanaryRollbackScenario 인스턴스 생성."""
        from baldur.api.django.views.xtest.scenarios import (
            SafetyInterlockCanaryRollbackScenario,
        )

        return SafetyInterlockCanaryRollbackScenario(
            service_name="test-service",
        )

    @pytest.fixture(scope="class")
    def scenario_result(self, scenario):
        """시나리오 실행 결과 캐싱 (클래스 내 1회만 실행)."""
        return scenario.run()

    def test_scenario_name_is_correct(self, scenario):
        """시나리오 이름이 올바른지 확인."""
        assert scenario.scenario_name == "safety_interlock_canary_rollback"

    def test_safety_interlock_canary_rollback_scenario_execution(self, scenario_result):
        """7단계 에스컬레이션 시나리오가 성공적으로 실행되는지 확인."""
        pytest.importorskip("baldur_pro")
        result = scenario_result

        assert result is not None
        assert result.scenario == "safety_interlock_canary_rollback"
        assert result.service_name == "test-service"
        assert result.status.value == "completed"
        assert len(result.steps) == 7

        # 각 단계 성공 확인
        for step in result.steps:
            assert step.success is True, f"Step {step.step} failed: {step.error}"

    def test_canary_starts_active(self, scenario_result):
        """Canary 롤아웃이 활성 상태로 시작되는지 확인."""
        result = scenario_result

        step1 = result.steps[0]
        assert step1.action == "start_canary_rollout"
        assert "true" in step1.actual

    def test_level2_triggers_pause_action(self, scenario_result):
        """LEVEL_2에서 PAUSE 액션이 트리거되는지 확인."""
        pytest.importorskip("baldur_pro")
        result = scenario_result

        step3 = result.steps[2]
        assert step3.action == "check_safety_interlock_pause"
        assert "PAUSE" in step3.actual

    def test_canary_is_paused_at_level2(self, scenario_result):
        """LEVEL_2에서 Canary가 일시 중지되는지 확인."""
        pytest.importorskip("baldur_pro")
        result = scenario_result

        step4 = result.steps[3]
        assert step4.action == "confirm_canary_paused"
        assert "true" in step4.actual

    def test_escalation_to_level3(self, scenario_result):
        """LEVEL_3으로 에스컬레이션되는지 확인."""
        pytest.importorskip("baldur_pro")
        result = scenario_result

        step5 = result.steps[4]
        assert step5.action == "escalate_to_level3"
        assert "level_3" in step5.actual

    def test_level3_triggers_rollback_action(self, scenario_result):
        """LEVEL_3에서 ROLLBACK 액션이 트리거되는지 확인."""
        pytest.importorskip("baldur_pro")
        result = scenario_result

        step6 = result.steps[5]
        assert step6.action == "check_safety_interlock_rollback"
        assert "ROLLBACK" in step6.actual

    def test_canary_is_rolled_back_at_level3(self, scenario_result):
        """LEVEL_3에서 Canary가 롤백되는지 확인."""
        pytest.importorskip("baldur_pro")
        result = scenario_result

        step7 = result.steps[6]
        assert step7.action == "confirm_canary_rollback"
        assert "true" in step7.actual


class TestScenarioRegistry:
    """시나리오 레지스트리 테스트."""

    def test_scenario_registry_contains_new_scenarios(self):
        """레지스트리에 새 시나리오가 등록되어 있는지 확인."""
        from baldur.api.django.views.xtest.scenarios import (
            SCENARIO_REGISTRY,
        )

        assert "full_emergency_recovery_flow" in SCENARIO_REGISTRY
        assert "safety_interlock_canary_rollback" in SCENARIO_REGISTRY

    def test_get_scenario_class_returns_correct_class(self):
        """get_scenario_class가 올바른 클래스를 반환하는지 확인."""
        from baldur.api.django.views.xtest.scenarios import (
            FullEmergencyRecoveryScenario,
            SafetyInterlockCanaryRollbackScenario,
            get_scenario_class,
        )

        cls1 = get_scenario_class("full_emergency_recovery_flow")
        cls2 = get_scenario_class("safety_interlock_canary_rollback")

        assert cls1 is FullEmergencyRecoveryScenario
        assert cls2 is SafetyInterlockCanaryRollbackScenario

    def test_list_available_scenarios_includes_new_scenarios(self):
        """list_available_scenarios가 새 시나리오를 포함하는지 확인."""
        from baldur.api.django.views.xtest.scenarios import (
            list_available_scenarios,
        )

        scenarios = list_available_scenarios()

        assert "full_emergency_recovery_flow" in scenarios
        assert "safety_interlock_canary_rollback" in scenarios


class TestInterlockActionMapping:
    """Interlock 액션 매핑 테스트."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_level_to_action_mapping(self):
        """Emergency 레벨별 액션 매핑이 올바른지 확인."""
        from baldur_pro.services.canary.interlock import (
            CanarySafetyInterlock,
            InterlockAction,
        )
        from baldur_pro.services.emergency_mode.enums import EmergencyLevel

        interlock = CanarySafetyInterlock()

        # 기본 정책 확인
        assert interlock.policy[EmergencyLevel.NORMAL.value] == InterlockAction.ALLOW
        assert (
            interlock.policy[EmergencyLevel.LEVEL_1.value]
            == InterlockAction.ALLOW_WITH_WARNING
        )
        assert interlock.policy[EmergencyLevel.LEVEL_2.value] == InterlockAction.PAUSE
        assert (
            interlock.policy[EmergencyLevel.LEVEL_3.value] == InterlockAction.ROLLBACK
        )

    def test_custom_policy_can_be_set(self):
        """커스텀 정책 설정이 가능한지 확인."""
        from baldur_pro.services.canary.interlock import (
            CanarySafetyInterlock,
            InterlockAction,
        )

        custom_policy = {
            "normal": InterlockAction.ALLOW,
            "level_1": InterlockAction.PAUSE,  # LEVEL_1에서 바로 PAUSE
            "level_2": InterlockAction.ROLLBACK,  # LEVEL_2에서 바로 ROLLBACK
            "level_3": InterlockAction.ROLLBACK,
        }

        interlock = CanarySafetyInterlock(policy=custom_policy)

        assert interlock.policy["level_1"] == InterlockAction.PAUSE
        assert interlock.policy["level_2"] == InterlockAction.ROLLBACK


class TestRecoveryStepTypes:
    """복구 단계 유형 테스트."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_recovery_step_types_exist(self):
        """필수 복구 단계 유형이 존재하는지 확인."""
        from baldur_pro.services.coordination.recovery_state import (
            RecoveryStepType,
        )

        # 4단계 역순 복구 단계 확인
        assert hasattr(RecoveryStepType, "BUDGET_RESET")
        assert hasattr(RecoveryStepType, "HEALTH_CHECK")
        assert hasattr(RecoveryStepType, "CANARY_RESUME")
        assert hasattr(RecoveryStepType, "GOVERNANCE_NORMAL")

    def test_recovery_step_order(self):
        """복구 단계 순서가 올바른지 확인 (BUDGET_RESET → GOVERNANCE_NORMAL)."""
        from baldur_pro.services.coordination.recovery_coordinator import (
            RecoveryCoordinator,
        )

        # LEVEL_3 기본 복구 단계
        steps = RecoveryCoordinator.DEFAULT_RECOVERY_STEPS.get("LEVEL_3", [])

        assert len(steps) == 4
        assert steps[0].order == 1  # BUDGET_RESET
        assert steps[1].order == 2  # HEALTH_CHECK
        assert steps[2].order == 3  # CANARY_RESUME
        assert steps[3].order == 4  # GOVERNANCE_NORMAL
