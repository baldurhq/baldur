"""
Circuit Breaker ServiceConfigManager 테스트.

Test Coverage:
- ServiceConfigManager: 서비스 등록, criticality 조회, Load Shedding 대상 선택
"""

import pytest

from baldur.services.circuit_breaker.models import (
    RecoveryStrategy,
    ServiceConfig,
)

# =============================================================================
# 3.1 ServiceConfigManager Tests
# =============================================================================


class TestServiceConfigManager:
    """ServiceConfigManager 테스트."""

    def setup_method(self):
        """테스트 전 싱글톤 초기화."""
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()

    def teardown_method(self):
        """테스트 후 정리."""
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()

    def test_singleton_pattern(self):
        """싱글톤 패턴 동작 확인."""
        from baldur.services.circuit_breaker.service_config import (
            ServiceConfigManager,
            get_service_config_manager,
        )

        manager1 = ServiceConfigManager()
        manager2 = get_service_config_manager()

        assert manager1 is manager2

    def test_register_service_success(self):
        """서비스 등록 성공."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        config = ServiceConfig(
            service_id="payment-api",
            criticality="critical",
            shed_priority=0,
        )

        result = manager.register_service(config)

        assert result is True
        assert manager.get_service_count() == 1
        assert manager.get_service_config("payment-api") is not None

    def test_register_services_bulk(self):
        """여러 서비스 일괄 등록."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        configs = [
            ServiceConfig(
                service_id="payment-api", criticality="critical", shed_priority=0
            ),
            ServiceConfig(service_id="order-api", criticality="high", shed_priority=1),
            ServiceConfig(service_id="review-api", criticality="low", shed_priority=10),
        ]

        count = manager.register_services(configs)

        assert count == 3
        assert manager.get_service_count() == 3

    def test_unregister_service(self):
        """서비스 등록 해제."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_service(
            ServiceConfig(
                service_id="test-api",
                criticality="low",
            )
        )

        result = manager.unregister_service("test-api")

        assert result is True
        assert manager.get_service_config("test-api") is None

    def test_unregister_nonexistent_service(self):
        """존재하지 않는 서비스 등록 해제."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        result = manager.unregister_service("nonexistent")

        assert result is False

    def test_get_services_by_criticality(self):
        """criticality별 서비스 조회."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_services(
            [
                ServiceConfig(service_id="payment-api", criticality="critical"),
                ServiceConfig(service_id="auth-api", criticality="critical"),
                ServiceConfig(service_id="order-api", criticality="high"),
                ServiceConfig(service_id="review-api", criticality="low"),
            ]
        )

        critical_services = manager.get_services_by_criticality("critical")

        assert len(critical_services) == 2
        assert all(s.criticality == "critical" for s in critical_services)

    def test_get_critical_services(self):
        """critical 서비스 조회 편의 메서드."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_services(
            [
                ServiceConfig(service_id="payment-api", criticality="critical"),
                ServiceConfig(service_id="review-api", criticality="low"),
            ]
        )

        critical = manager.get_critical_services()

        assert len(critical) == 1
        assert critical[0].service_id == "payment-api"

    def test_get_non_critical_services(self):
        """비핵심 서비스 조회."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_services(
            [
                ServiceConfig(service_id="payment-api", criticality="critical"),
                ServiceConfig(service_id="order-api", criticality="high"),
                ServiceConfig(service_id="review-api", criticality="low"),
            ]
        )

        non_critical = manager.get_non_critical_services()

        assert len(non_critical) == 2
        assert all(s.criticality != "critical" for s in non_critical)


# =============================================================================
# 3.1.1 ServiceConfig 입력 검증 테스트 (P1 - 외부 입력 방어)
# =============================================================================


class TestServiceConfigInputValidation:
    """ServiceConfig 입력 검증 테스트 - 잘못된 입력에 대한 방어."""

    def test_invalid_criticality_raises_error(self):
        """잘못된 criticality 값은 ValueError 발생."""
        with pytest.raises(ValueError) as exc_info:
            ServiceConfig(service_id="test-api", criticality="invalid")

        assert "Invalid criticality" in str(exc_info.value)
        assert "invalid" in str(exc_info.value)

    def test_criticality_typo_raises_error(self):
        """criticality 오타도 ValueError 발생 (Critical vs critical)."""
        with pytest.raises(ValueError):
            ServiceConfig(service_id="test-api", criticality="Critical")  # 대문자

        with pytest.raises(ValueError):
            ServiceConfig(service_id="test-api", criticality="HIGH")  # 전체 대문자

    def test_negative_shed_priority_raises_error(self):
        """음수 shed_priority는 ValueError 발생."""
        with pytest.raises(ValueError) as exc_info:
            ServiceConfig(
                service_id="test-api",
                criticality="low",
                shed_priority=-1,
            )

        assert "shed_priority" in str(exc_info.value)
        assert "non-negative" in str(exc_info.value) or "-1" in str(exc_info.value)

    def test_min_traffic_percentage_below_zero_raises_error(self):
        """min_traffic_percentage가 0 미만이면 ValueError 발생."""
        with pytest.raises(ValueError) as exc_info:
            ServiceConfig(
                service_id="test-api",
                criticality="low",
                min_traffic_percentage=-10.0,
            )

        assert "min_traffic_percentage" in str(exc_info.value)

    def test_min_traffic_percentage_above_100_raises_error(self):
        """min_traffic_percentage가 100 초과면 ValueError 발생."""
        with pytest.raises(ValueError) as exc_info:
            ServiceConfig(
                service_id="test-api",
                criticality="low",
                min_traffic_percentage=150.0,
            )

        assert "min_traffic_percentage" in str(exc_info.value)

    def test_valid_criticality_values_accepted(self):
        """유효한 criticality 값들은 정상 생성."""
        valid_levels = ["critical", "high", "medium", "low"]

        for level in valid_levels:
            config = ServiceConfig(service_id=f"test-{level}", criticality=level)
            assert config.criticality == level

    def test_boundary_min_traffic_percentage_accepted(self):
        """경계값 min_traffic_percentage (0, 100)은 정상 생성."""
        config_zero = ServiceConfig(
            service_id="test-zero",
            criticality="low",
            min_traffic_percentage=0.0,
        )
        assert config_zero.min_traffic_percentage == 0.0

        config_hundred = ServiceConfig(
            service_id="test-hundred",
            criticality="low",
            min_traffic_percentage=100.0,
        )
        assert config_hundred.min_traffic_percentage == 100.0


class TestServiceConfigLoadShedding:
    """ServiceConfigManager Load Shedding 테스트."""

    def setup_method(self):
        """테스트 전 싱글톤 초기화."""
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()

    def teardown_method(self):
        """테스트 후 정리."""
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()

    def test_get_shedding_targets(self):
        """Load Shedding 대상 조회."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_services(
            [
                ServiceConfig(
                    service_id="payment-api", criticality="critical", shed_priority=0
                ),
                ServiceConfig(
                    service_id="review-api", criticality="low", shed_priority=10
                ),
                ServiceConfig(
                    service_id="recommend-api", criticality="low", shed_priority=5
                ),
                ServiceConfig(
                    service_id="analytics-api", criticality="medium", shed_priority=3
                ),
            ]
        )

        targets = manager.get_shedding_targets(["low"])

        assert len(targets) == 2
        # shed_priority 내림차순 정렬
        assert targets[0].service_id == "review-api"
        assert targets[1].service_id == "recommend-api"

    def test_get_shedding_targets_multiple_criticality(self):
        """여러 criticality에 대한 Shedding 대상 조회."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_services(
            [
                ServiceConfig(
                    service_id="payment-api", criticality="critical", shed_priority=0
                ),
                ServiceConfig(
                    service_id="review-api", criticality="low", shed_priority=10
                ),
                ServiceConfig(
                    service_id="analytics-api", criticality="medium", shed_priority=5
                ),
            ]
        )

        targets = manager.get_shedding_targets(["low", "medium"])

        assert len(targets) == 2
        assert targets[0].service_id == "review-api"  # priority 10
        assert targets[1].service_id == "analytics-api"  # priority 5

    def test_shed_priority_zero_excluded(self):
        """shed_priority=0인 서비스는 Shedding 대상에서 제외."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_services(
            [
                ServiceConfig(
                    service_id="payment-api", criticality="critical", shed_priority=0
                ),
                ServiceConfig(
                    service_id="review-api", criticality="low", shed_priority=0
                ),  # 제외됨
            ]
        )

        targets = manager.get_shedding_targets(["low", "critical"])

        assert len(targets) == 0

    def test_get_shedding_order(self):
        """전체 Shedding 순서 조회."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_services(
            [
                ServiceConfig(
                    service_id="review-api", criticality="low", shed_priority=10
                ),
                ServiceConfig(
                    service_id="analytics-api", criticality="medium", shed_priority=5
                ),
                ServiceConfig(
                    service_id="payment-api", criticality="critical", shed_priority=0
                ),
            ]
        )

        order = manager.get_shedding_order()

        # shed_priority > 0인 것만, 내림차순
        assert len(order) == 2
        assert order[0].service_id == "review-api"
        assert order[1].service_id == "analytics-api"

    def test_is_sheddable(self):
        """Shedding 대상 여부 확인."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_services(
            [
                ServiceConfig(
                    service_id="payment-api", criticality="critical", shed_priority=0
                ),
                ServiceConfig(
                    service_id="review-api", criticality="low", shed_priority=10
                ),
            ]
        )

        assert manager.is_sheddable("payment-api") is False
        assert manager.is_sheddable("review-api") is True
        assert manager.is_sheddable("nonexistent") is False


class TestServiceConfigRecoveryStrategy:
    """ServiceConfigManager Recovery 전략 테스트."""

    def setup_method(self):
        """테스트 전 싱글톤 초기화."""
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()

    def teardown_method(self):
        """테스트 후 정리."""
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()

    def test_get_recovery_strategy_default(self):
        """기본 Recovery 전략 반환."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_service(
            ServiceConfig(
                service_id="test-api",
                criticality="medium",
            )
        )

        strategy = manager.get_recovery_strategy("test-api")

        assert strategy.type == "canary"  # 기본값

    def test_get_recovery_strategy_service_override(self):
        """서비스별 Recovery 전략 오버라이드."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_service(
            ServiceConfig(
                service_id="payment-api",
                criticality="critical",
                recovery_strategy=RecoveryStrategy(type="canary", strict_mode=True),
            )
        )

        strategy = manager.get_recovery_strategy("payment-api")

        assert strategy.type == "canary"
        assert strategy.strict_mode is True

    def test_set_default_recovery_strategy(self):
        """기본 Recovery 전략 설정."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.set_default_recovery_strategy(RecoveryStrategy(type="immediate"))
        manager.register_service(
            ServiceConfig(
                service_id="test-api",
                criticality="medium",
            )
        )

        strategy = manager.get_recovery_strategy("test-api")

        assert strategy.type == "immediate"


class TestServiceConfigThresholdOverride:
    """ServiceConfigManager 임계값 오버라이드 테스트."""

    def setup_method(self):
        """테스트 전 싱글톤 초기화."""
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()

    def teardown_method(self):
        """테스트 후 정리."""
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()

    def test_get_failure_threshold_default(self):
        """기본 실패 임계값 반환."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_service(
            ServiceConfig(
                service_id="test-api",
                criticality="medium",
            )
        )

        threshold = manager.get_failure_threshold("test-api", default=5)

        assert threshold == 5

    def test_get_failure_threshold_service_override(self):
        """서비스별 실패 임계값 오버라이드."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_service(
            ServiceConfig(
                service_id="payment-api",
                criticality="critical",
                failure_threshold=10,  # 오버라이드
            )
        )

        threshold = manager.get_failure_threshold("payment-api", default=5)

        assert threshold == 10

    def test_get_window_seconds_service_override(self):
        """서비스별 윈도우 오버라이드."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        manager = get_service_config_manager()
        manager.register_service(
            ServiceConfig(
                service_id="payment-api",
                criticality="critical",
                window_seconds=120,  # 오버라이드
            )
        )

        window = manager.get_window_seconds("payment-api", default=60)

        assert window == 120
