"""
X-Test Regional 시나리오 단위 테스트.

Global vs Regional 상태 우선순위 검증 및 다중 리전 격리 시나리오를 테스트합니다.
Redis Mock을 사용하여 실제 AtomicStateQuery와 NamespacedEmergencyTracker를 검증합니다.

테스트 케이스:
- test_regional_override_conflict_scenario_execution: 8단계 전체 시나리오 실행
- test_regional_strict_takes_priority_over_global_normal: Regional STRICT 우선
- test_global_strict_overrides_regional: Global STRICT 오버라이드
- test_admin_override_wins_over_global_strict: Admin Override 승리
- test_state_restoration_returns_all_to_normal: 상태 원복 확인
- test_multi_region_isolation_test_scenario_execution: 5단계 격리 시나리오 실행
- test_only_target_region_is_isolated: 타겟 리전만 격리
- test_scenario_registry_contains_regional_scenarios: 레지스트리 등록 확인
"""

import json

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


class MockRedisClient:
    """
    Redis Mock 클라이언트.

    AtomicStateQuery Lua 스크립트 동작을 시뮬레이션합니다.
    실제 Redis 없이 우선순위 로직을 테스트할 수 있습니다.
    """

    def __init__(self):
        self._storage: dict[str, str] = {}

    def get(self, key: str) -> bytes | None:
        """키 조회."""
        value = self._storage.get(key)
        return value.encode("utf-8") if value else None

    def set(self, key: str, value: str, ex: int = None) -> bool:
        """키 저장."""
        self._storage[key] = value if isinstance(value, str) else value.decode("utf-8")
        return True

    def delete(self, key: str) -> int:
        """키 삭제."""
        if key in self._storage:
            del self._storage[key]
            return 1
        return 0

    def keys(self, pattern: str) -> list[bytes]:
        """패턴 매칭 키 조회."""
        import fnmatch

        matched = []
        for key in self._storage.keys():
            if fnmatch.fnmatch(key, pattern.replace("*", "*")):
                matched.append(key.encode("utf-8"))
        return matched

    def eval(self, script: str, num_keys: int, *args) -> list:
        """
        Lua 스크립트 실행 시뮬레이션.

        AtomicStateQuery의 우선순위 로직을 Python으로 구현.
        """
        global_key = args[0]
        regional_key = args[1]
        precedence_level = int(args[2]) if len(args) > 2 else 0

        # 상태 조회
        global_data = self._storage.get(global_key)
        regional_data = self._storage.get(regional_key)

        # 기본값 설정
        if global_data:
            global_state = json.loads(global_data)
        else:
            global_state = {
                "namespace": "global",
                "scope": "global",
                "governance_mode": "NORMAL",
                "is_active": False,
                "emergency_level": "normal",
            }

        if regional_data:
            regional_state = json.loads(regional_data)
        else:
            # namespace 추출
            ns = regional_key.split(":")[1] if ":" in regional_key else "unknown"
            regional_state = {
                "namespace": ns,
                "scope": "regional",
                "governance_mode": "NORMAL",
                "is_active": False,
                "emergency_level": "normal",
            }

        # 우선순위 로직 (AtomicStateQuery Lua 스크립트와 동일)
        # 1순위: Admin Override (precedence >= 2)
        if precedence_level >= 2:
            return [
                json.dumps(regional_state).encode("utf-8"),
                b"ADMIN_OVERRIDE",
                b"Admin override active, using regional state",
            ]

        # 2순위: Safety-Max
        # is_active는 emergency_level != "normal" 으로 판단
        # ScopedEmergencyState.to_dict()는 is_active를 저장하지 않음
        def is_active(state: dict) -> bool:
            """상태가 활성화되어 있는지 확인 (emergency_level 기반)."""
            level = state.get("emergency_level", "normal")
            # emergency_level이 "normal" 이 아니면 활성화
            return level != "normal" and level is not None

        global_is_strict = (
            is_active(global_state) and global_state.get("governance_mode") == "STRICT"
        )
        regional_is_strict = (
            is_active(regional_state)
            and regional_state.get("governance_mode") == "STRICT"
        )

        if global_is_strict and regional_is_strict:
            return [
                json.dumps(global_state).encode("utf-8"),
                b"GLOBAL_OVERRIDE",
                b"Both Global and Regional STRICT, using Global state",
            ]
        elif global_is_strict:
            return [
                json.dumps(global_state).encode("utf-8"),
                b"GLOBAL_OVERRIDE",
                f"Global STRICT overrides regional {regional_state.get('namespace', 'unknown')}".encode(),
            ]
        elif regional_is_strict:
            return [
                json.dumps(regional_state).encode("utf-8"),
                b"REGIONAL_STRICT",
                b"Regional STRICT active",
            ]
        else:
            return [
                json.dumps(regional_state).encode("utf-8"),
                b"REGIONAL_DEFAULT",
                b"Both states NORMAL, using regional",
            ]

    def script_load(self, script: str) -> str:
        """스크립트 로드 (SHA 반환)."""
        return "mock_script_sha"


@pytest.fixture
def mock_redis_client():
    """Mock Redis 클라이언트 fixture."""
    return MockRedisClient()


class TestRegionalOverrideConflictScenario:
    """Regional Override Conflict 시나리오 테스트."""

    @pytest.fixture(scope="class")
    def scenario(self):
        """RegionalOverrideConflictScenario 인스턴스 생성 (Redis Mock 주입)."""
        from baldur.api.django.views.xtest.scenarios import (
            RegionalOverrideConflictScenario,
        )

        mock_redis = MockRedisClient()
        return RegionalOverrideConflictScenario(
            service_name="test-service",
            config={
                "target_region": "seoul",
                "redis_client": mock_redis,
            },
        )

    @pytest.fixture(scope="class")
    def scenario_result(self, scenario):
        """시나리오 실행 결과 캐싱 (클래스 내 1회만 실행)."""
        return scenario.run()

    def test_scenario_name_is_correct(self, scenario):
        """시나리오 이름이 올바른지 확인."""
        assert scenario.scenario_name == "regional_override_conflict"

    def test_regional_override_conflict_scenario_execution(self, scenario_result):
        """8단계 전체 시나리오가 성공적으로 실행되는지 확인."""
        result = scenario_result

        assert result is not None
        assert result.scenario == "regional_override_conflict"
        assert result.service_name == "test-service"
        assert result.status.value == "completed"
        assert len(result.steps) == 8

        # 각 단계 성공 확인
        for step in result.steps:
            assert step.success is True, f"Step {step.step} failed: {step.error}"

    def test_initial_state_is_normal(self, scenario_result):
        """Step 1: 초기 상태가 NORMAL인지 확인."""
        result = scenario_result

        step1 = result.steps[0]
        assert step1.action == "check_initial_state"
        assert "NORMAL" in step1.actual
        assert step1.success is True

    def test_regional_strict_setting(self, scenario_result):
        """Step 2: Regional STRICT 설정 확인."""
        result = scenario_result

        step2 = result.steps[1]
        assert step2.action == "set_regional_strict"
        assert "STRICT" in step2.actual
        assert step2.success is True

    def test_regional_strict_takes_priority_over_global_normal(self, scenario_result):
        """Step 3: Global NORMAL일 때 Regional STRICT가 우선하는지 확인."""
        result = scenario_result

        step3 = result.steps[2]
        assert step3.action == "get_effective_state_regional_priority"
        assert "STRICT" in step3.actual
        assert "regional" in step3.actual
        assert step3.success is True

    def test_global_strict_setting(self, scenario_result):
        """Step 4: Global STRICT 설정 확인."""
        result = scenario_result

        step4 = result.steps[3]
        assert step4.action == "set_global_strict"
        assert "STRICT" in step4.actual
        assert step4.success is True

    def test_global_strict_overrides_regional(self, scenario_result):
        """Step 5: Global STRICT가 Regional을 오버라이드하는지 확인."""
        result = scenario_result

        step5 = result.steps[4]
        assert step5.action == "get_effective_state_global_override"
        assert "STRICT" in step5.actual
        assert "global" in step5.actual
        assert step5.success is True

    def test_admin_override_setting(self, scenario_result):
        """Step 6: Admin Override 설정 확인."""
        result = scenario_result

        step6 = result.steps[5]
        assert step6.action == "set_admin_override"
        assert "ADMIN_OVERRIDE" in step6.actual
        assert step6.success is True

    def test_admin_override_wins_over_global_strict(self, scenario_result):
        """Step 7: Admin Override가 Global STRICT를 이기는지 확인."""
        result = scenario_result

        step7 = result.steps[6]
        assert step7.action == "get_effective_state_admin_wins"
        assert "NORMAL" in step7.actual
        assert "regional" in step7.actual
        assert step7.success is True

    def test_state_restoration_returns_all_to_normal(self, scenario_result):
        """Step 8: 상태 원복 후 모두 NORMAL인지 확인."""
        result = scenario_result

        step8 = result.steps[7]
        assert step8.action == "restore_all_states"
        assert "NORMAL" in step8.actual
        assert step8.success is True

    def test_state_transitions_recorded(self, scenario_result):
        """상태 전환 이력이 기록되는지 확인."""
        result = scenario_result

        assert result.config is not None
        assert "state_transitions" in result.config
        transitions = result.config["state_transitions"]
        assert len(transitions) >= 4  # get_effective_state 호출 기록들

    def test_timeline_has_all_events(self, scenario_result):
        """타임라인에 모든 이벤트가 기록되는지 확인."""
        result = scenario_result

        assert len(result.timeline) == 8
        for i, event in enumerate(result.timeline):
            assert event.step == i + 1


class TestMultiRegionIsolationTestScenario:
    """Multi-Region Isolation Test 시나리오 테스트."""

    @pytest.fixture(scope="class")
    def scenario(self):
        """MultiRegionIsolationTestScenario 인스턴스 생성 (Redis Mock 주입)."""
        from baldur.api.django.views.xtest.scenarios import (
            MultiRegionIsolationTestScenario,
        )

        mock_redis = MockRedisClient()
        return MultiRegionIsolationTestScenario(
            service_name="test-service",
            config={
                "target_region": "seoul",
                "other_region": "tokyo",
                "redis_client": mock_redis,
            },
        )

    @pytest.fixture(scope="class")
    def scenario_result(self, scenario):
        """시나리오 실행 결과 캐싱 (클래스 내 1회만 실행)."""
        return scenario.run()

    def test_scenario_name_is_correct(self, scenario):
        """시나리오 이름이 올바른지 확인."""
        assert scenario.scenario_name == "multi_region_isolation_test"

    def test_multi_region_isolation_test_scenario_execution(self, scenario_result):
        """5단계 전체 시나리오가 성공적으로 실행되는지 확인."""
        result = scenario_result

        assert result is not None
        assert result.scenario == "multi_region_isolation_test"
        assert result.service_name == "test-service"
        assert result.status.value == "completed"
        assert len(result.steps) == 5

        # 각 단계 성공 확인
        for step in result.steps:
            assert step.success is True, f"Step {step.step} failed: {step.error}"

    def test_current_region_check(self, scenario_result):
        """Step 1: 현재 리전 확인."""
        result = scenario_result

        step1 = result.steps[0]
        assert step1.action == "check_current_region"
        assert "seoul" in step1.actual
        assert step1.success is True

    def test_target_region_isolation(self, scenario_result):
        """Step 2: 타겟 리전 격리 설정 확인."""
        result = scenario_result

        step2 = result.steps[1]
        assert step2.action == "set_region_strict"
        assert "seoul" in step2.actual
        assert "STRICT" in step2.actual
        assert step2.success is True

    def test_other_region_remains_normal(self, scenario_result):
        """Step 3: 다른 리전이 NORMAL 상태인지 확인."""
        result = scenario_result

        step3 = result.steps[2]
        assert step3.action == "check_other_region_normal"
        assert "tokyo" in step3.actual
        assert "NORMAL" in step3.actual
        assert step3.success is True

    def test_only_target_region_is_isolated(self, scenario_result):
        """Step 4: 타겟 리전만 격리되었는지 확인."""
        result = scenario_result

        step4 = result.steps[3]
        assert step4.action == "verify_isolation_state"
        assert "seoul" in step4.actual
        assert "seoul_isolated: True" in step4.actual
        assert "tokyo_isolated: False" in step4.actual
        assert step4.success is True

    def test_region_restore(self, scenario_result):
        """Step 5: 격리 해제 확인."""
        result = scenario_result

        step5 = result.steps[4]
        assert step5.action == "restore_region"
        assert "NORMAL" in step5.actual
        assert step5.success is True


class TestScenarioRegistry:
    """시나리오 레지스트리 테스트."""

    def test_scenario_registry_contains_regional_scenarios(self):
        """레지스트리에 새 리전 시나리오가 등록되어 있는지 확인."""
        from baldur.api.django.views.xtest.scenarios import (
            SCENARIO_REGISTRY,
            list_available_scenarios,
        )

        assert "regional_override_conflict" in SCENARIO_REGISTRY
        assert "multi_region_isolation_test" in SCENARIO_REGISTRY

        available = list_available_scenarios()
        assert "regional_override_conflict" in available
        assert "multi_region_isolation_test" in available

    def test_get_scenario_class_returns_correct_class(self):
        """get_scenario_class가 올바른 클래스를 반환하는지 확인."""
        from baldur.api.django.views.xtest.scenarios import (
            MultiRegionIsolationTestScenario,
            RegionalOverrideConflictScenario,
            get_scenario_class,
        )

        assert (
            get_scenario_class("regional_override_conflict")
            == RegionalOverrideConflictScenario
        )
        assert (
            get_scenario_class("multi_region_isolation_test")
            == MultiRegionIsolationTestScenario
        )

    def test_regional_scenarios_extend_integration_scenario(self):
        """새 시나리오들이 IntegrationScenario를 상속하는지 확인."""
        from baldur.api.django.views.xtest.scenarios import (
            IntegrationScenario,
            MultiRegionIsolationTestScenario,
            RegionalOverrideConflictScenario,
        )

        assert issubclass(RegionalOverrideConflictScenario, IntegrationScenario)
        assert issubclass(MultiRegionIsolationTestScenario, IntegrationScenario)


class TestStateTransitionMatrix:
    """상태 전환 매트릭스 테스트 (AtomicStateQuery 우선순위 로직 검증)."""

    @pytest.fixture(scope="class")
    def scenario_result(self):
        """시나리오 실행 결과 캐싱 (클래스 내 1회만 실행)."""
        from baldur.api.django.views.xtest.scenarios import (
            RegionalOverrideConflictScenario,
        )

        mock_redis = MockRedisClient()
        scenario = RegionalOverrideConflictScenario(
            service_name="test-service",
            config={
                "target_region": "seoul",
                "redis_client": mock_redis,
            },
        )
        return scenario.run()

    def test_global_normal_regional_normal_returns_normal(self, scenario_result):
        """Global NORMAL + Regional NORMAL → NORMAL."""
        # Step 1에서 초기 상태 확인
        step1 = scenario_result.steps[0]
        assert "NORMAL" in step1.actual

    def test_global_normal_regional_strict_returns_strict(self, scenario_result):
        """Global NORMAL + Regional STRICT → STRICT."""
        # Step 3에서 Regional STRICT 상태 확인
        step3 = scenario_result.steps[2]
        assert "STRICT" in step3.actual
        assert "regional" in step3.actual

    def test_global_strict_regional_normal_returns_strict(self, scenario_result):
        """Global STRICT + Regional NORMAL → STRICT (Global 오버라이드)."""
        # Step 5에서 Global STRICT 오버라이드 확인
        step5 = scenario_result.steps[4]
        assert "STRICT" in step5.actual
        assert "global" in step5.actual

    def test_admin_override_returns_regional_state(self, scenario_result):
        """Admin Override 시 Regional 상태 반환."""
        # Step 7에서 Admin Override로 NORMAL 확인
        step7 = scenario_result.steps[6]
        assert "NORMAL" in step7.actual
        assert "regional" in step7.actual


class TestAtomicStateQueryIntegration:
    """AtomicStateQuery 통합 검증 테스트."""

    @pytest.fixture
    def mock_redis_client(self):
        """Mock Redis 클라이언트 fixture."""
        return MockRedisClient()

    def test_atomic_query_uses_lua_script_logic(self, mock_redis_client):
        """AtomicStateQuery가 Lua 스크립트 로직을 사용하는지 검증."""
        from baldur.services.regional_emergency.atomic_query import (
            AtomicStateQuery,
        )

        atomic_query = AtomicStateQuery(redis_client=mock_redis_client)

        # 초기 상태 (둘 다 NORMAL)
        state, decision_type, reason = atomic_query.query_effective_state("seoul")

        assert state["governance_mode"] == "NORMAL"
        assert decision_type == "REGIONAL_DEFAULT"

    def test_atomic_query_global_override(self, mock_redis_client):
        """Global STRICT가 Regional을 오버라이드하는지 검증."""
        import json

        from baldur.services.regional_emergency.atomic_query import (
            AtomicStateQuery,
        )

        # Global STRICT 상태 설정
        mock_redis_client.set(
            "baldur:governance:emergency_state",
            json.dumps(
                {
                    "namespace": "global",
                    "scope": "global",
                    "governance_mode": "STRICT",
                    "is_active": True,
                    "emergency_level": 3,
                }
            ),
        )

        atomic_query = AtomicStateQuery(redis_client=mock_redis_client)
        state, decision_type, reason = atomic_query.query_effective_state("seoul")

        assert state["governance_mode"] == "STRICT"
        assert decision_type == "GLOBAL_OVERRIDE"

    def test_atomic_query_admin_override(self, mock_redis_client):
        """Admin Override가 작동하는지 검증."""
        import json

        from baldur.services.regional_emergency.atomic_query import (
            AtomicStateQuery,
        )

        # Global STRICT 상태 설정
        mock_redis_client.set(
            "baldur:governance:emergency_state",
            json.dumps(
                {
                    "namespace": "global",
                    "scope": "global",
                    "governance_mode": "STRICT",
                    "is_active": True,
                    "emergency_level": 3,
                }
            ),
        )

        # Regional NORMAL 상태 설정
        mock_redis_client.set(
            "baldur:seoul:governance:emergency_state",
            json.dumps(
                {
                    "namespace": "seoul",
                    "scope": "regional",
                    "governance_mode": "NORMAL",
                    "is_active": False,
                    "emergency_level": "normal",
                }
            ),
        )

        atomic_query = AtomicStateQuery(redis_client=mock_redis_client)
        state, decision_type, reason = atomic_query.query_effective_state(
            "seoul", precedence="ADMIN_OVERRIDE"
        )

        assert state["governance_mode"] == "NORMAL"
        assert state["namespace"] == "seoul"
        assert decision_type == "ADMIN_OVERRIDE"


class TestHelperMethods:
    """헬퍼 메서드 테스트."""

    @pytest.fixture
    def mock_redis_client(self):
        """Mock Redis 클라이언트 fixture."""
        return MockRedisClient()

    def test_set_global_state_helper(self, mock_redis_client):
        """_set_global_state() 헬퍼 메서드 테스트."""
        pytest.importorskip("baldur_pro")
        from baldur.api.django.views.xtest.scenarios import (
            RegionalOverrideConflictScenario,
        )
        from baldur.api.django.views.xtest.scenarios.regional import (
            MockStateBackend,
        )
        from baldur.services.regional_emergency.atomic_query import (
            AtomicStateQuery,
        )
        from baldur.services.regional_emergency.tracker import (
            NamespacedEmergencyTracker,
        )
        from baldur_pro.services.emergency_mode.enums import EmergencyLevel

        scenario = RegionalOverrideConflictScenario(
            service_name="test-service",
            config={"target_region": "seoul", "redis_client": mock_redis_client},
        )

        backend = MockStateBackend(redis_client=mock_redis_client)
        atomic_query = AtomicStateQuery(redis_client=mock_redis_client)
        tracker = NamespacedEmergencyTracker(
            backend=backend,
            atomic_query=atomic_query,
        )

        # _set_global_state 호출
        transition = scenario._set_global_state(tracker, EmergencyLevel.LEVEL_3)

        assert transition["action"] == "set_global_state"
        assert transition["new_state"] == "STRICT"
        assert transition["region"] == "global"

    def test_set_regional_state_helper(self, mock_redis_client):
        """_set_regional_state() 헬퍼 메서드 테스트."""
        pytest.importorskip("baldur_pro")
        from baldur.api.django.views.xtest.scenarios import (
            RegionalOverrideConflictScenario,
        )
        from baldur.api.django.views.xtest.scenarios.regional import (
            MockStateBackend,
        )
        from baldur.services.regional_emergency.atomic_query import (
            AtomicStateQuery,
        )
        from baldur.services.regional_emergency.tracker import (
            NamespacedEmergencyTracker,
        )
        from baldur_pro.services.emergency_mode.enums import EmergencyLevel

        scenario = RegionalOverrideConflictScenario(
            service_name="test-service",
            config={"target_region": "seoul", "redis_client": mock_redis_client},
        )

        backend = MockStateBackend(redis_client=mock_redis_client)
        atomic_query = AtomicStateQuery(redis_client=mock_redis_client)
        tracker = NamespacedEmergencyTracker(
            backend=backend,
            atomic_query=atomic_query,
        )

        # _set_regional_state 호출
        transition = scenario._set_regional_state(
            tracker, "seoul", EmergencyLevel.LEVEL_2
        )

        assert transition["action"] == "set_regional_state"
        assert transition["new_state"] == "STRICT"
        assert transition["region"] == "seoul"

    def test_set_admin_override_helper(self, mock_redis_client):
        """_set_admin_override() 헬퍼 메서드 테스트."""
        from baldur.api.django.views.xtest.scenarios import (
            RegionalOverrideConflictScenario,
        )
        from baldur.api.django.views.xtest.scenarios.regional import (
            MockStateBackend,
        )
        from baldur.services.regional_emergency.atomic_query import (
            AtomicStateQuery,
        )
        from baldur.services.regional_emergency.tracker import (
            NamespacedEmergencyTracker,
        )

        scenario = RegionalOverrideConflictScenario(
            service_name="test-service",
            config={"target_region": "seoul", "redis_client": mock_redis_client},
        )

        backend = MockStateBackend(redis_client=mock_redis_client)
        atomic_query = AtomicStateQuery(redis_client=mock_redis_client)
        tracker = NamespacedEmergencyTracker(
            backend=backend,
            atomic_query=atomic_query,
        )

        # _set_admin_override 호출
        transition = scenario._set_admin_override(tracker, "seoul", True)

        assert transition["action"] == "set_admin_override"
        assert transition["new_state"] == "ON"
        assert transition["region"] == "seoul"
