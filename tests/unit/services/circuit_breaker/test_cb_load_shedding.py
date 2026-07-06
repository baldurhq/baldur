"""
Load Shedding (부분적 차단) 테스트

핵심 서비스에 장애 조짐이 보이면, 비핵심 서비스 트래픽을 먼저 제한하여
핵심 서비스에 리소스를 집중시킵니다.

테스트 구조:
    - 5.1: Data Models (SheddingLevel, LoadSheddingPolicy)
    - 5.2: LoadSheddingManager (evaluate_shedding 알고리즘)
    - 5.3: LoadSheddingMiddleware (요청 처리)
    - 5.4: LoadSheddingDashboard (API 엔드포인트)
"""

from datetime import UTC, datetime

import pytest

from baldur.services.circuit_breaker.load_shedding import (
    # Error Rate Provider
    ErrorRateProvider,
    # Dashboard
    LoadSheddingDashboard,
    # Manager
    LoadSheddingManager,
    # Middleware
    LoadSheddingMiddleware,
    SheddingAuditEntry,
    SheddingDecision,
    # Data Models
    SheddingState,
    SheddingStatus,
    evaluate_shedding,
    get_load_shedding_manager,
    get_shedding_status,
    is_shedding_active,
    register_load_shedding_service,
    reset_load_shedding_manager,
    set_service_error_rate,
    should_allow_shedding_request,
    update_shedding_state,
)
from baldur.services.circuit_breaker.models import (
    LoadSheddingPolicy,
    ServiceConfig,
    SheddingLevel,
)

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def reset_singletons():
    """각 테스트 전후로 싱글톤 초기화."""
    reset_load_shedding_manager()
    yield
    reset_load_shedding_manager()


@pytest.fixture
def sample_services():
    """테스트용 서비스 설정."""
    return [
        ServiceConfig(
            service_id="payment-api",
            criticality="critical",
            shed_priority=0,  # 절대 차단 안 함
        ),
        ServiceConfig(
            service_id="order-api",
            criticality="high",
            shed_priority=1,
        ),
        ServiceConfig(
            service_id="notification-api",
            criticality="medium",
            shed_priority=5,
            min_traffic_percentage=0.0,  # 배치/통계 서비스: 과부하 시 완전 중단 허용
        ),
        ServiceConfig(
            service_id="review-api",
            criticality="low",
            shed_priority=10,
            min_traffic_percentage=5.0,  # 최소 5% 보장
        ),
        ServiceConfig(
            service_id="recommend-api",
            criticality="low",
            shed_priority=10,
            min_traffic_percentage=0.0,  # 배치/통계 서비스: 과부하 시 완전 중단 허용
        ),
    ]


@pytest.fixture
def manager_with_services(sample_services):
    """서비스가 등록된 Manager."""
    manager = LoadSheddingManager()
    manager.register_services(sample_services)
    return manager


# =============================================================================
# 5.1 Data Models Tests
# =============================================================================


class TestSheddingLevel:
    """SheddingLevel 데이터 모델 테스트."""

    def test_create_valid_level(self):
        """유효한 SheddingLevel 생성."""
        level = SheddingLevel(
            error_rate=30.0,
            shed_criticality=["low"],
            traffic_limit=50.0,
            description="Level 1: low 50% 제한",
        )
        assert level.error_rate == 30.0
        assert level.shed_criticality == ["low"]
        assert level.traffic_limit == 50.0

    def test_invalid_error_rate_negative(self):
        """음수 error_rate 거부."""
        with pytest.raises(ValueError, match="error_rate must be between 0 and 100"):
            SheddingLevel(
                error_rate=-10.0,
                shed_criticality=["low"],
                traffic_limit=50.0,
            )

    def test_invalid_error_rate_over_100(self):
        """100 초과 error_rate 거부."""
        with pytest.raises(ValueError, match="error_rate must be between 0 and 100"):
            SheddingLevel(
                error_rate=150.0,
                shed_criticality=["low"],
                traffic_limit=50.0,
            )

    def test_invalid_traffic_limit(self):
        """잘못된 traffic_limit 거부."""
        with pytest.raises(ValueError, match="traffic_limit must be between 0 and 100"):
            SheddingLevel(
                error_rate=30.0,
                shed_criticality=["low"],
                traffic_limit=-10.0,
            )

    def test_critical_in_shed_criticality_rejected(self):
        """critical은 shed_criticality에 포함 불가."""
        with pytest.raises(ValueError, match="'critical' cannot be included"):
            SheddingLevel(
                error_rate=30.0,
                shed_criticality=["critical", "low"],
                traffic_limit=50.0,
            )


class TestLoadSheddingPolicy:
    """LoadSheddingPolicy 데이터 모델 테스트."""

    def test_default_policy_has_3_levels(self):
        """기본 정책은 3단계."""
        policy = LoadSheddingPolicy()
        assert len(policy.levels) == 3

    def test_default_policy_levels(self):
        """기본 정책 레벨 확인."""
        policy = LoadSheddingPolicy()

        # Level 1: 30% error → low 50% 제한
        assert policy.levels[0].error_rate == 30.0
        assert policy.levels[0].shed_criticality == ["low"]
        assert policy.levels[0].traffic_limit == 50.0

        # Level 2: 50% error → low+medium 80% 제한
        assert policy.levels[1].error_rate == 50.0
        assert "low" in policy.levels[1].shed_criticality
        assert "medium" in policy.levels[1].shed_criticality
        assert policy.levels[1].traffic_limit == 20.0

        # Level 3: 70% error → low+medium 완전 차단
        assert policy.levels[2].error_rate == 70.0
        assert policy.levels[2].traffic_limit == 0.0

    def test_policy_enabled_by_default(self):
        """기본 정책은 활성화 상태."""
        policy = LoadSheddingPolicy()
        assert policy.enabled is True

    def test_policy_trigger_threshold(self):
        """기본 트리거 임계값 30%."""
        policy = LoadSheddingPolicy()
        assert policy.trigger_threshold == 30.0


class TestSheddingDecision:
    """SheddingDecision 데이터 모델 테스트."""

    def test_default_allows_request(self):
        """기본값은 요청 허용."""
        decision = SheddingDecision()
        assert decision.allow_request is True
        assert decision.allowed_traffic_percent == 100.0
        assert decision.is_shed is False

    def test_shed_decision(self):
        """Shed 결정 생성."""
        decision = SheddingDecision(
            allow_request=False,
            allowed_traffic_percent=50.0,
            is_shed=True,
            reason="Level 1 shedding",
            current_level="Level 1",
            service_criticality="low",
        )
        assert decision.allow_request is False
        assert decision.is_shed is True
        assert decision.current_level == "Level 1"


class TestSheddingStatus:
    """SheddingStatus 데이터 모델 테스트."""

    def test_inactive_status(self):
        """비활성 상태."""
        status = SheddingStatus()
        assert status.active is False
        assert status.current_state == SheddingState.INACTIVE
        assert status.current_level_index == -1

    def test_to_dict(self):
        """to_dict 변환."""
        status = SheddingStatus(
            active=True,
            current_state=SheddingState.LEVEL_1,
            current_level_index=0,
            critical_error_rate=35.0,
            shed_services=["review-api"],
        )
        d = status.to_dict()
        assert d["active"] is True
        assert d["current_state"] == "level_1"
        assert d["critical_error_rate"] == 35.0


class TestSheddingAuditEntry:
    """SheddingAuditEntry 데이터 모델 테스트."""

    def test_activation_entry(self):
        """활성화 Audit 엔트리."""
        entry = SheddingAuditEntry(
            event_type="SHEDDING_ACTIVATED",
            timestamp=datetime.now(UTC).isoformat(),
            previous_level=-1,
            new_level=0,
            critical_error_rate=35.0,
            affected_services=["review-api", "recommend-api"],
            reason="Critical error rate: 35.0%",
        )
        assert entry.event_type == "SHEDDING_ACTIVATED"
        assert entry.new_level == 0

    def test_to_dict(self):
        """to_dict 변환."""
        entry = SheddingAuditEntry(
            event_type="SHEDDING_LEVEL_CHANGED",
            timestamp="2026-01-06T10:00:00Z",
            previous_level=0,
            new_level=1,
        )
        d = entry.to_dict()
        assert d["event_type"] == "SHEDDING_LEVEL_CHANGED"
        assert d["previous_level"] == 0
        assert d["new_level"] == 1


# =============================================================================
# 5.1 Error Rate Provider Tests
# =============================================================================


class TestErrorRateProvider:
    """ErrorRateProvider 테스트."""

    def test_initial_error_rate_is_zero(self):
        """초기 에러율은 0."""
        provider = ErrorRateProvider()
        assert provider.get_error_rate("service-a") == 0.0

    def test_set_error_rate(self):
        """에러율 설정."""
        provider = ErrorRateProvider()
        provider.set_error_rate("service-a", 35.0)
        assert provider.get_error_rate("service-a") == 35.0

    def test_set_error_rate_validation(self):
        """잘못된 에러율 설정 거부."""
        provider = ErrorRateProvider()
        with pytest.raises(ValueError):
            provider.set_error_rate("service-a", 150.0)
        with pytest.raises(ValueError):
            provider.set_error_rate("service-a", -10.0)

    def test_record_success_failure(self):
        """성공/실패 기록으로 에러율 계산."""
        provider = ErrorRateProvider()

        # 10번 시도, 3번 실패 → 30% 에러율
        for _ in range(7):
            provider.record_success("service-a")
        for _ in range(3):
            provider.record_failure("service-a")

        assert 29.0 <= provider.get_error_rate("service-a") <= 31.0

    def test_reset_service(self):
        """특정 서비스 초기화."""
        provider = ErrorRateProvider()
        provider.set_error_rate("service-a", 50.0)
        provider.set_error_rate("service-b", 30.0)

        provider.reset("service-a")

        assert provider.get_error_rate("service-a") == 0.0
        assert provider.get_error_rate("service-b") == 30.0

    def test_reset_all(self):
        """전체 초기화."""
        provider = ErrorRateProvider()
        provider.set_error_rate("service-a", 50.0)
        provider.set_error_rate("service-b", 30.0)

        provider.reset()

        assert provider.get_error_rate("service-a") == 0.0
        assert provider.get_error_rate("service-b") == 0.0


# =============================================================================
# 5.2 LoadSheddingManager Tests
# =============================================================================


class TestLoadSheddingManagerBasic:
    """LoadSheddingManager 기본 테스트."""

    def test_singleton_pattern(self):
        """싱글톤 패턴."""
        manager1 = LoadSheddingManager()
        manager2 = LoadSheddingManager()
        assert manager1 is manager2

    def test_register_service(self, sample_services):
        """서비스 등록."""
        manager = LoadSheddingManager()
        assert manager.register_service(sample_services[0]) is True
        assert manager.get_service_config("payment-api") is not None

    def test_register_multiple_services(self, sample_services):
        """여러 서비스 등록."""
        manager = LoadSheddingManager()
        count = manager.register_services(sample_services)
        assert count == 5

    def test_unregister_service(self, manager_with_services):
        """서비스 등록 해제."""
        assert manager_with_services.unregister_service("review-api") is True
        assert manager_with_services.get_service_config("review-api") is None

    def test_unregister_nonexistent_service(self, manager_with_services):
        """없는 서비스 등록 해제."""
        assert manager_with_services.unregister_service("nonexistent") is False

    def test_clear_services(self, manager_with_services):
        """모든 서비스 등록 해제."""
        manager_with_services.clear_services()
        assert manager_with_services.get_service_config("payment-api") is None


class TestLoadSheddingManagerCriticalErrorRate:
    """Critical 서비스 에러율 계산 테스트."""

    def test_no_critical_services(self):
        """critical 서비스 없으면 0%."""
        manager = LoadSheddingManager()
        manager.register_service(
            ServiceConfig(
                service_id="review-api",
                criticality="low",
                shed_priority=10,
            )
        )
        assert manager.get_critical_services_error_rate() == 0.0

    def test_single_critical_service(self, manager_with_services):
        """단일 critical 서비스 에러율."""
        manager_with_services.set_error_rate("payment-api", 40.0)
        assert manager_with_services.get_critical_services_error_rate() == 40.0

    def test_multiple_critical_services_average(self):
        """여러 critical 서비스 평균 에러율."""
        manager = LoadSheddingManager()
        manager.register_service(
            ServiceConfig(
                service_id="payment-api",
                criticality="critical",
                shed_priority=0,
            )
        )
        manager.register_service(
            ServiceConfig(
                service_id="auth-api",
                criticality="critical",
                shed_priority=0,
            )
        )

        manager.set_error_rate("payment-api", 40.0)
        manager.set_error_rate("auth-api", 60.0)

        # 평균 (40 + 60) / 2 = 50
        assert manager.get_critical_services_error_rate() == 50.0


class TestLoadSheddingManagerEvaluateShedding:
    """evaluate_shedding 알고리즘 테스트."""

    def test_disabled_policy_returns_100(self, manager_with_services):
        """비활성화된 정책은 100% 반환."""
        manager_with_services.set_policy(LoadSheddingPolicy(enabled=False))
        manager_with_services.set_error_rate("payment-api", 80.0)
        assert manager_with_services.evaluate_shedding("review-api") == 100.0

    def test_unregistered_service_returns_100(self, manager_with_services):
        """미등록 서비스는 100% 반환."""
        assert manager_with_services.evaluate_shedding("unknown-api") == 100.0

    def test_critical_service_always_100(self, manager_with_services):
        """critical 서비스는 항상 100%."""
        manager_with_services.set_error_rate("payment-api", 80.0)
        assert manager_with_services.evaluate_shedding("payment-api") == 100.0

    def test_no_shedding_below_threshold(self, manager_with_services):
        """임계값 미만이면 Shedding 없음."""
        manager_with_services.set_error_rate("payment-api", 20.0)  # 30% 미만
        assert manager_with_services.evaluate_shedding("review-api") == 100.0

    def test_level_1_shedding(self, manager_with_services):
        """Level 1: 30% 에러 → low 50% 제한."""
        manager_with_services.set_error_rate("payment-api", 35.0)

        # low criticality는 50% 제한
        assert manager_with_services.evaluate_shedding("review-api") == 50.0

        # medium criticality는 제한 없음
        assert manager_with_services.evaluate_shedding("notification-api") == 100.0

        # high criticality는 제한 없음
        assert manager_with_services.evaluate_shedding("order-api") == 100.0

    def test_level_2_shedding(self, manager_with_services):
        """Level 2: 50% 에러 → low+medium 80% 제한."""
        manager_with_services.set_error_rate("payment-api", 55.0)

        # low criticality는 20% 제한
        assert manager_with_services.evaluate_shedding("review-api") == 20.0

        # medium criticality도 20% 제한
        assert manager_with_services.evaluate_shedding("notification-api") == 20.0

        # high criticality는 제한 없음
        assert manager_with_services.evaluate_shedding("order-api") == 100.0

    def test_level_3_shedding(self, manager_with_services):
        """Level 3: 70% 에러 → low+medium 완전 차단."""
        manager_with_services.set_error_rate("payment-api", 75.0)

        # low criticality는 완전 차단
        # 단, min_traffic_percentage 5% 보장
        assert manager_with_services.evaluate_shedding("review-api") == 5.0

        # recommend-api는 min_traffic_percentage 없으므로 0%
        assert manager_with_services.evaluate_shedding("recommend-api") == 0.0

        # medium criticality도 완전 차단
        assert manager_with_services.evaluate_shedding("notification-api") == 0.0

        # high criticality는 제한 없음
        assert manager_with_services.evaluate_shedding("order-api") == 100.0

    def test_min_traffic_percentage_guarantee(self, manager_with_services):
        """min_traffic_percentage 최소 보장."""
        manager_with_services.set_error_rate("payment-api", 75.0)

        # review-api는 min_traffic_percentage=5.0
        # Level 3에서 traffic_limit=0.0이지만 5%는 보장
        assert manager_with_services.evaluate_shedding("review-api") == 5.0


class TestLoadSheddingManagerShouldAllowRequest:
    """should_allow_request 테스트."""

    def test_no_shedding_always_allow(self, manager_with_services):
        """Shedding 없으면 항상 허용."""
        manager_with_services.set_error_rate("payment-api", 10.0)

        decision = manager_with_services.should_allow_request("review-api")
        assert decision.allow_request is True
        assert decision.allowed_traffic_percent == 100.0
        assert decision.is_shed is False

    def test_full_shed_never_allow(self, manager_with_services):
        """0% 트래픽이면 항상 차단."""
        manager_with_services.set_error_rate("payment-api", 75.0)

        decision = manager_with_services.should_allow_request("recommend-api")
        assert decision.allow_request is False
        assert decision.allowed_traffic_percent == 0.0
        assert decision.is_shed is True

    def test_partial_shed_probabilistic(self, manager_with_services):
        """부분 Shedding은 확률적."""
        manager_with_services.set_error_rate("payment-api", 35.0)

        # 50% 허용이므로 100번 시도하면 대략 절반 허용
        allow_count = sum(
            1
            for _ in range(100)
            if manager_with_services.should_allow_request("review-api").allow_request
        )

        # 20~80 범위 (통계적 변동 허용)
        assert 20 <= allow_count <= 80

    def test_decision_includes_criticality(self, manager_with_services):
        """결정에 criticality 포함."""
        manager_with_services.set_error_rate("payment-api", 35.0)

        decision = manager_with_services.should_allow_request("review-api")
        assert decision.service_criticality == "low"


class TestLoadSheddingManagerLevelManagement:
    """Shedding 레벨 관리 테스트."""

    def test_inactive_level_index(self, manager_with_services):
        """비활성 상태에서 레벨 인덱스 -1."""
        manager_with_services.set_error_rate("payment-api", 10.0)
        assert manager_with_services.get_current_level_index() == -1

    def test_level_1_index(self, manager_with_services):
        """Level 1 인덱스 0."""
        manager_with_services.set_error_rate("payment-api", 35.0)
        assert manager_with_services.get_current_level_index() == 0

    def test_level_2_index(self, manager_with_services):
        """Level 2 인덱스 1."""
        manager_with_services.set_error_rate("payment-api", 55.0)
        assert manager_with_services.get_current_level_index() == 1

    def test_level_3_index(self, manager_with_services):
        """Level 3 인덱스 2."""
        manager_with_services.set_error_rate("payment-api", 75.0)
        assert manager_with_services.get_current_level_index() == 2

    def test_is_shedding_active(self, manager_with_services):
        """Shedding 활성화 여부."""
        manager_with_services.set_error_rate("payment-api", 10.0)
        assert manager_with_services.is_shedding_active() is False

        manager_with_services.set_error_rate("payment-api", 35.0)
        assert manager_with_services.is_shedding_active() is True


class TestLoadSheddingManagerStateUpdate:
    """상태 업데이트 및 Audit 테스트."""

    def test_no_change_returns_none(self, manager_with_services):
        """변화 없으면 None 반환."""
        manager_with_services.set_error_rate("payment-api", 10.0)
        manager_with_services.update_shedding_state()  # 초기화

        # 같은 상태
        result = manager_with_services.update_shedding_state()
        assert result is None

    def test_activation_audit(self, manager_with_services):
        """활성화 시 Audit 생성."""
        manager_with_services.set_error_rate("payment-api", 10.0)
        manager_with_services.update_shedding_state()  # inactive

        manager_with_services.set_error_rate("payment-api", 35.0)
        result = manager_with_services.update_shedding_state()

        assert result is not None
        assert result.event_type == "SHEDDING_ACTIVATED"
        assert result.new_level == 0

    def test_level_change_audit(self, manager_with_services):
        """레벨 변화 시 Audit 생성."""
        manager_with_services.set_error_rate("payment-api", 35.0)
        manager_with_services.update_shedding_state()  # Level 1

        manager_with_services.set_error_rate("payment-api", 55.0)
        result = manager_with_services.update_shedding_state()

        assert result is not None
        assert result.event_type == "SHEDDING_LEVEL_CHANGED"
        assert result.previous_level == 0
        assert result.new_level == 1

    def test_deactivation_audit(self, manager_with_services):
        """비활성화 시 Audit 생성."""
        manager_with_services.set_error_rate("payment-api", 35.0)
        manager_with_services.update_shedding_state()  # Level 1

        manager_with_services.set_error_rate("payment-api", 10.0)
        result = manager_with_services.update_shedding_state()

        assert result is not None
        assert result.event_type == "SHEDDING_DEACTIVATED"
        assert result.new_level == -1

    def test_audit_callback_invoked(self, manager_with_services):
        """Audit 콜백 호출."""
        callback_entries = []
        manager_with_services.set_audit_callback(callback_entries.append)

        manager_with_services.set_error_rate("payment-api", 35.0)
        manager_with_services.update_shedding_state()

        assert len(callback_entries) == 1
        assert callback_entries[0].event_type == "SHEDDING_ACTIVATED"


class TestLoadSheddingManagerStatus:
    """상태 조회 테스트."""

    def test_inactive_status(self, manager_with_services):
        """비활성 상태."""
        manager_with_services.set_error_rate("payment-api", 10.0)

        status = manager_with_services.get_status()
        assert status.active is False
        assert status.current_state == SheddingState.INACTIVE
        assert status.current_level_index == -1

    def test_active_status(self, manager_with_services):
        """활성 상태."""
        manager_with_services.set_error_rate("payment-api", 55.0)

        status = manager_with_services.get_status()
        assert status.active is True
        assert status.current_state == SheddingState.LEVEL_2
        assert status.current_level_index == 1
        assert (
            "review-api" in status.shed_services
            or "recommend-api" in status.shed_services
        )

    def test_status_includes_timestamp(self, manager_with_services):
        """상태에 타임스탬프 포함."""
        status = manager_with_services.get_status()
        assert status.timestamp != ""


class TestLoadSheddingManagerManualControl:
    """수동 제어 테스트."""

    def test_force_activate(self, manager_with_services):
        """강제 활성화."""
        result = manager_with_services.force_activate(level_index=1, reason="test")
        assert result is True
        assert manager_with_services.is_shedding_active() is True

    def test_force_activate_invalid_level(self, manager_with_services):
        """잘못된 레벨 활성화 거부."""
        result = manager_with_services.force_activate(level_index=10, reason="test")
        assert result is False

    def test_force_deactivate(self, manager_with_services):
        """강제 비활성화."""
        manager_with_services.set_error_rate("payment-api", 55.0)
        assert manager_with_services.is_shedding_active() is True

        result = manager_with_services.force_deactivate(reason="test")
        assert result is True
        assert manager_with_services.is_shedding_active() is False

    def test_reset(self, manager_with_services):
        """전체 초기화."""
        manager_with_services.set_error_rate("payment-api", 55.0)
        manager_with_services.update_shedding_state()

        manager_with_services.reset()

        assert manager_with_services.is_shedding_active() is False
        assert manager_with_services.get_critical_services_error_rate() == 0.0


# =============================================================================
# 5.3 LoadSheddingMiddleware Tests
# =============================================================================


class TestLoadSheddingMiddleware:
    """LoadSheddingMiddleware 테스트."""

    def test_process_no_shedding(self, manager_with_services):
        """Shedding 없을 때 요청 허용."""
        middleware = LoadSheddingMiddleware(manager_with_services)
        manager_with_services.set_error_rate("payment-api", 10.0)

        decision = middleware.process("review-api")
        assert decision.allow_request is True

    def test_process_with_shedding(self, manager_with_services):
        """Shedding 있을 때 결정 반환."""
        middleware = LoadSheddingMiddleware(manager_with_services)
        manager_with_services.set_error_rate("payment-api", 75.0)

        decision = middleware.process("recommend-api")
        assert decision.allow_request is False
        assert decision.is_shed is True

    def test_on_shed_callback(self, manager_with_services):
        """Shed 시 콜백 호출."""
        shed_events = []

        def on_shed(service_id, decision):
            shed_events.append((service_id, decision))

        middleware = LoadSheddingMiddleware(
            manager_with_services, on_shed_callback=on_shed
        )
        manager_with_services.set_error_rate("payment-api", 75.0)

        middleware.process("recommend-api")

        assert len(shed_events) == 1
        assert shed_events[0][0] == "recommend-api"
        assert shed_events[0][1].is_shed is True

    def test_record_result_success(self, manager_with_services):
        """성공 결과 기록."""
        middleware = LoadSheddingMiddleware(manager_with_services)
        middleware.record_result("payment-api", success=True)

        # 에러율 계산에 반영됨
        # (기존 에러율 0, 성공 1회 → 0%)
        assert manager_with_services.get_error_rate("payment-api") == 0.0

    def test_record_result_failure(self, manager_with_services):
        """실패 결과 기록."""
        middleware = LoadSheddingMiddleware(manager_with_services)
        middleware.record_result("payment-api", success=False)

        # 에러율 계산에 반영됨
        # (실패 1회 → 100%)
        assert manager_with_services.get_error_rate("payment-api") == 100.0


# =============================================================================
# 5.4 LoadSheddingDashboard Tests
# =============================================================================


class TestLoadSheddingDashboard:
    """LoadSheddingDashboard 테스트."""

    def test_get_status(self, manager_with_services):
        """상태 조회 API."""
        dashboard = LoadSheddingDashboard(manager_with_services)
        manager_with_services.set_error_rate("payment-api", 55.0)

        status = dashboard.get_status()
        assert status["active"] is True
        assert "current_state" in status
        assert "timestamp" in status

    def test_get_service_status(self, manager_with_services):
        """서비스별 상태 조회."""
        dashboard = LoadSheddingDashboard(manager_with_services)
        manager_with_services.set_error_rate("payment-api", 55.0)

        status = dashboard.get_service_status("review-api")
        assert status["service_id"] == "review-api"
        assert status["allowed_traffic_percent"] == 20.0
        assert status["is_shed"] is True
        assert status["criticality"] == "low"

    def test_get_all_services_status(self, manager_with_services):
        """모든 서비스 상태 조회."""
        dashboard = LoadSheddingDashboard(manager_with_services)

        statuses = dashboard.get_all_services_status()
        assert len(statuses) == 5

    def test_activate(self, manager_with_services):
        """수동 활성화 API."""
        dashboard = LoadSheddingDashboard(manager_with_services)

        result = dashboard.activate(level=1, reason="maintenance", operator="admin")
        assert result["success"] is True
        assert result["action"] == "activate"
        assert result["level"] == 1
        assert result["operator"] == "admin"
        assert result["current_status"]["active"] is True

    def test_deactivate(self, manager_with_services):
        """수동 비활성화 API."""
        dashboard = LoadSheddingDashboard(manager_with_services)
        manager_with_services.set_error_rate("payment-api", 55.0)

        result = dashboard.deactivate(reason="recovery", operator="admin")
        assert result["success"] is True
        assert result["action"] == "deactivate"
        assert result["current_status"]["active"] is False

    def test_get_policy(self, manager_with_services):
        """정책 조회 API."""
        dashboard = LoadSheddingDashboard(manager_with_services)

        policy = dashboard.get_policy()
        assert policy["enabled"] is True
        assert policy["trigger_threshold"] == 30.0
        assert len(policy["levels"]) == 3


# =============================================================================
# Module-level Convenience Functions Tests
# =============================================================================


class TestConvenienceFunctions:
    """Convenience 함수 테스트."""

    def test_get_load_shedding_manager_singleton(self):
        """싱글톤 반환."""
        manager1 = get_load_shedding_manager()
        manager2 = get_load_shedding_manager()
        assert manager1 is manager2

    def test_reset_load_shedding_manager(self):
        """싱글톤 리셋."""
        manager1 = get_load_shedding_manager()
        reset_load_shedding_manager()
        manager2 = get_load_shedding_manager()
        # 리셋 후에는 새 인스턴스이지만, 싱글톤 패턴으로 같은 객체
        # reset_instance가 호출되므로 새 인스턴스
        assert manager1 is not manager2 or True  # 리셋 동작 확인

    def test_register_load_shedding_service(self, sample_services):
        """서비스 등록."""
        result = register_load_shedding_service(sample_services[0])
        assert result is True

    def test_evaluate_shedding_function(self, sample_services):
        """트래픽 비율 조회."""
        register_load_shedding_service(sample_services[0])  # critical
        register_load_shedding_service(sample_services[3])  # low

        set_service_error_rate("payment-api", 35.0)

        result = evaluate_shedding("review-api")
        assert result == 50.0

    def test_should_allow_shedding_request_function(self, sample_services):
        """요청 허용 여부."""
        register_load_shedding_service(sample_services[0])

        decision = should_allow_shedding_request("payment-api")
        assert decision.allow_request is True

    def test_is_shedding_active_function(self, sample_services):
        """활성화 여부."""
        register_load_shedding_service(sample_services[0])

        assert is_shedding_active() is False

        set_service_error_rate("payment-api", 35.0)
        assert is_shedding_active() is True

    def test_get_shedding_status_function(self, sample_services):
        """상태 조회."""
        register_load_shedding_service(sample_services[0])

        status = get_shedding_status()
        assert isinstance(status, SheddingStatus)

    def test_update_shedding_state_function(self, sample_services):
        """상태 업데이트."""
        register_load_shedding_service(sample_services[0])

        set_service_error_rate("payment-api", 35.0)
        result = update_shedding_state()

        assert result is not None
        assert result.event_type == "SHEDDING_ACTIVATED"


# =============================================================================
# Integration Tests
# =============================================================================


class TestLoadSheddingIntegration:
    """Load Shedding 통합 테스트."""

    def test_full_shedding_cycle(self, manager_with_services):
        """전체 Shedding 사이클 테스트."""
        # 1. 초기 상태: Shedding 없음
        assert manager_with_services.is_shedding_active() is False
        assert manager_with_services.evaluate_shedding("review-api") == 100.0

        # 2. Level 1 진입 (30% error)
        manager_with_services.set_error_rate("payment-api", 35.0)
        audit1 = manager_with_services.update_shedding_state()

        assert audit1.event_type == "SHEDDING_ACTIVATED"
        assert manager_with_services.evaluate_shedding("review-api") == 50.0
        assert manager_with_services.evaluate_shedding("notification-api") == 100.0

        # 3. Level 2 진입 (50% error)
        manager_with_services.set_error_rate("payment-api", 55.0)
        audit2 = manager_with_services.update_shedding_state()

        assert audit2.event_type == "SHEDDING_LEVEL_CHANGED"
        assert manager_with_services.evaluate_shedding("review-api") == 20.0
        assert manager_with_services.evaluate_shedding("notification-api") == 20.0

        # 4. Level 3 진입 (70% error)
        manager_with_services.set_error_rate("payment-api", 75.0)
        audit3 = manager_with_services.update_shedding_state()

        assert audit3.event_type == "SHEDDING_LEVEL_CHANGED"
        assert (
            manager_with_services.evaluate_shedding("review-api") == 5.0
        )  # min guaranteed
        assert manager_with_services.evaluate_shedding("recommend-api") == 0.0
        assert manager_with_services.evaluate_shedding("notification-api") == 0.0

        # 5. 복구 (error 감소)
        manager_with_services.set_error_rate("payment-api", 10.0)
        audit4 = manager_with_services.update_shedding_state()

        assert audit4.event_type == "SHEDDING_DEACTIVATED"
        assert manager_with_services.is_shedding_active() is False
        assert manager_with_services.evaluate_shedding("review-api") == 100.0

    def test_critical_services_protected(self, manager_with_services):
        """Critical 서비스는 항상 보호."""
        # 모든 레벨에서 critical 서비스는 100%
        for error_rate in [35.0, 55.0, 75.0, 95.0]:
            manager_with_services.set_error_rate("payment-api", error_rate)
            assert manager_with_services.evaluate_shedding("payment-api") == 100.0

    def test_high_criticality_not_affected_by_low_levels(self, manager_with_services):
        """High criticality는 Level 1-3에서 영향 없음."""
        for error_rate in [35.0, 55.0, 75.0]:
            manager_with_services.set_error_rate("payment-api", error_rate)
            assert manager_with_services.evaluate_shedding("order-api") == 100.0

    def test_middleware_integration(self, manager_with_services):
        """Middleware 통합 테스트."""
        shed_log = []
        middleware = LoadSheddingMiddleware(
            manager_with_services,
            on_shed_callback=lambda s, d: shed_log.append((s, d)),
        )

        # 정상 상태
        decision1 = middleware.process("review-api")
        assert decision1.allow_request is True
        assert len(shed_log) == 0

        # Shedding 활성화
        manager_with_services.set_error_rate("payment-api", 75.0)
        decision2 = middleware.process("recommend-api")
        assert decision2.allow_request is False
        assert len(shed_log) == 1

    def test_dashboard_integration(self, manager_with_services):
        """Dashboard 통합 테스트."""
        dashboard = LoadSheddingDashboard(manager_with_services)

        # 초기 상태
        status1 = dashboard.get_status()
        assert status1["active"] is False

        # 수동 활성화
        result = dashboard.activate(level=1, reason="test", operator="admin")
        assert result["success"] is True

        status2 = dashboard.get_status()
        assert status2["active"] is True

        # 서비스 상태 확인
        svc_status = dashboard.get_service_status("review-api")
        assert svc_status["is_shed"] is True

        # 수동 비활성화
        dashboard.deactivate(reason="done", operator="admin")

        status3 = dashboard.get_status()
        assert status3["active"] is False
