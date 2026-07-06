"""
Circuit Breaker Blast Radius 및 연쇄 장애 방지 테스트.

Test Coverage:
- BlastRadiusIntegration: 의존성 관리, 영향 평가, 자동 OPEN 차단
- 통합 테스트: ServiceConfig + BlastRadius 연동
"""

from baldur.services.circuit_breaker.models import (
    ServiceConfig,
)

# =============================================================================
# 3.2 BlastRadiusIntegration Tests
# =============================================================================


class TestBlastRadiusIntegration:
    """BlastRadiusIntegration 테스트."""

    def setup_method(self):
        """테스트 전 싱글톤 초기화."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            reset_blast_radius_integration,
        )

        reset_blast_radius_integration()

    def teardown_method(self):
        """테스트 후 정리."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            reset_blast_radius_integration,
        )

        reset_blast_radius_integration()

    def test_singleton_pattern(self):
        """싱글톤 패턴 동작 확인."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            BlastRadiusIntegration,
            get_blast_radius_integration,
        )

        integration1 = BlastRadiusIntegration()
        integration2 = get_blast_radius_integration()

        assert integration1 is integration2

    def test_register_dependency(self):
        """의존성 등록."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            get_blast_radius_integration,
        )

        integration = get_blast_radius_integration()
        integration.register_dependency(
            service_id="order-api",
            depends_on=["payment-api", "inventory-api"],
            criticality="high",
        )

        status = integration.get_status()
        assert status["registered_services"] >= 1

    def test_assess_impact_minimal(self):
        """최소 영향 평가."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            BlastRadiusLevel,
            get_blast_radius_integration,
        )

        integration = get_blast_radius_integration()
        # 의존성 없는 서비스
        integration.register_dependency("standalone-api", depends_on=[])

        assessment = integration.assess_impact(
            trigger_service="standalone-api",
            trigger_event="test",
        )

        assert assessment.level == BlastRadiusLevel.MINIMAL
        assert assessment.affected_count == 0
        assert assessment.should_block_auto_open() is False

    def test_assess_impact_moderate(self):
        """중간 영향 평가."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            BlastRadiusLevel,
            get_blast_radius_integration,
        )

        integration = get_blast_radius_integration()
        integration.configure(moderate_threshold=2)

        # payment-api에 2개 서비스 의존
        integration.register_dependency("payment-api", criticality="critical")
        integration.register_dependency(
            "order-api", depends_on=["payment-api"], criticality="high"
        )
        integration.register_dependency(
            "cart-api", depends_on=["payment-api"], criticality="medium"
        )

        assessment = integration.assess_impact(
            trigger_service="payment-api",
            trigger_event="timeout errors",
        )

        assert assessment.level == BlastRadiusLevel.MODERATE
        assert assessment.affected_count == 2
        assert "order-api" in assessment.affected_services
        assert "cart-api" in assessment.affected_services

    def test_assess_impact_extensive(self):
        """광범위한 영향 평가."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            BlastRadiusLevel,
            get_blast_radius_integration,
        )

        integration = get_blast_radius_integration()
        integration.configure(
            moderate_threshold=2,
            extensive_threshold=4,
            critical_threshold=6,
        )

        # payment-api에 4개 서비스 의존
        integration.register_dependency("payment-api", criticality="high")
        integration.register_dependency("order-api", depends_on=["payment-api"])
        integration.register_dependency("cart-api", depends_on=["payment-api"])
        integration.register_dependency("invoice-api", depends_on=["payment-api"])
        integration.register_dependency("refund-api", depends_on=["payment-api"])

        assessment = integration.assess_impact(
            trigger_service="payment-api",
            trigger_event="connection errors",
        )

        assert assessment.level == BlastRadiusLevel.EXTENSIVE
        assert assessment.affected_count == 4

    def test_assess_impact_critical_by_count(self):
        """영향 서비스 수로 인한 CRITICAL."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            BlastRadiusLevel,
            get_blast_radius_integration,
        )

        integration = get_blast_radius_integration()
        integration.configure(critical_threshold=6)

        # 6개 서비스 의존
        integration.register_dependency("core-api", criticality="high")
        for i in range(6):
            integration.register_dependency(f"service-{i}", depends_on=["core-api"])

        assessment = integration.assess_impact(
            trigger_service="core-api",
            trigger_event="system failure",
        )

        assert assessment.level == BlastRadiusLevel.CRITICAL
        assert assessment.affected_count == 6
        assert assessment.should_block_auto_open() is True

    def test_assess_impact_critical_by_critical_service(self):
        """critical 서비스 영향으로 인한 CRITICAL."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            BlastRadiusLevel,
            get_blast_radius_integration,
        )

        integration = get_blast_radius_integration()

        # payment-api(critical)가 db-api에 의존
        integration.register_dependency("db-api", criticality="high")
        integration.register_dependency(
            "payment-api", depends_on=["db-api"], criticality="critical"
        )

        assessment = integration.assess_impact(
            trigger_service="db-api",
            trigger_event="database connection failure",
        )

        assert assessment.level == BlastRadiusLevel.CRITICAL
        assert "payment-api" in assessment.critical_services_affected
        assert assessment.should_block_auto_open() is True


class TestBlastRadiusAutoOpenDecision:
    """BlastRadiusIntegration 자동 OPEN 결정 테스트."""

    def setup_method(self):
        """테스트 전 싱글톤 초기화."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            reset_blast_radius_integration,
        )

        reset_blast_radius_integration()

    def teardown_method(self):
        """테스트 후 정리."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            reset_blast_radius_integration,
        )

        reset_blast_radius_integration()

    def test_should_auto_open_allowed(self):
        """자동 OPEN 허용."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            get_blast_radius_integration,
        )

        integration = get_blast_radius_integration()
        integration.register_dependency("standalone-api", depends_on=[])

        allowed, reason, assessment = integration.should_auto_open(
            service_id="standalone-api",
            trigger_event="threshold exceeded",
        )

        assert allowed is True
        assert reason is None

    def test_should_auto_open_blocked_critical(self):
        """CRITICAL로 인한 자동 OPEN 차단."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            get_blast_radius_integration,
        )

        integration = get_blast_radius_integration()
        integration.configure(critical_threshold=2, block_on_critical=True)

        integration.register_dependency("core-api", criticality="high")
        integration.register_dependency("service-1", depends_on=["core-api"])
        integration.register_dependency("service-2", depends_on=["core-api"])

        allowed, reason, assessment = integration.should_auto_open(
            service_id="core-api",
            trigger_event="failures detected",
        )

        assert allowed is False
        assert "CRITICAL" in reason
        assert assessment.should_block_auto_open() is True

    def test_should_auto_open_not_blocked_when_disabled(self):
        """block_on_critical=False 시 CRITICAL이어도 허용."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            get_blast_radius_integration,
        )

        integration = get_blast_radius_integration()
        integration.configure(critical_threshold=2, block_on_critical=False)

        integration.register_dependency("core-api", criticality="high")
        integration.register_dependency("service-1", depends_on=["core-api"])
        integration.register_dependency("service-2", depends_on=["core-api"])

        allowed, reason, assessment = integration.should_auto_open(
            service_id="core-api",
            trigger_event="failures detected",
        )

        assert allowed is True


class TestServiceDependencyGraph:
    """ServiceDependencyGraph 테스트."""

    def test_get_dependents(self):
        """의존하는 서비스 조회."""
        from baldur.core.dependency_graph import (
            ServiceDependencyGraph,
        )

        graph = ServiceDependencyGraph()
        graph.register_service("payment-api", depends_on=[])
        graph.register_service("order-api", depends_on=["payment-api"])
        graph.register_service("cart-api", depends_on=["payment-api"])

        dependents = graph.get_dependents("payment-api")

        assert len(dependents) == 2
        assert "order-api" in dependents
        assert "cart-api" in dependents

    def test_get_cascading_affected(self):
        """연쇄적으로 영향받는 서비스 조회."""
        from baldur.core.dependency_graph import (
            ServiceDependencyGraph,
        )

        graph = ServiceDependencyGraph()
        # A -> B -> C (체인)
        graph.register_service("service-a", depends_on=[])
        graph.register_service("service-b", depends_on=["service-a"])
        graph.register_service("service-c", depends_on=["service-b"])

        affected = graph.get_cascading_affected("service-a")

        # B와 C 둘 다 영향받음
        assert len(affected) == 2
        assert "service-b" in affected
        assert "service-c" in affected

    def test_get_cascading_affected_prevents_cycles(self):
        """순환 의존성 방지."""
        from baldur.core.dependency_graph import (
            ServiceDependencyGraph,
        )

        graph = ServiceDependencyGraph()
        # 순환: A -> B -> A (수동 설정)
        graph.register_service("service-a", depends_on=[])
        graph.register_service("service-b", depends_on=["service-a"])
        # 수동으로 순환 추가 (비정상 상황)
        graph._dependencies["service-a"].dependents.append("service-b")
        graph._dependencies["service-b"].dependents.append("service-a")

        # 무한 루프 없이 완료되어야 함
        affected = graph.get_cascading_affected("service-a")

        assert "service-b" in affected

    def test_get_critical_dependents(self):
        """critical 의존 서비스 조회."""
        from baldur.core.dependency_graph import (
            ServiceDependencyGraph,
        )

        graph = ServiceDependencyGraph()
        graph.register_service("db-api", depends_on=[], criticality="high")
        graph.register_service(
            "payment-api", depends_on=["db-api"], criticality="critical"
        )
        graph.register_service(
            "analytics-api", depends_on=["db-api"], criticality="low"
        )

        critical = graph.get_critical_dependents("db-api")

        assert len(critical) == 1
        assert "payment-api" in critical


# =============================================================================
# 3.3 통합 테스트
# =============================================================================


class TestCascadePreventionIntegration:
    """ServiceConfig + BlastRadius 통합 테스트."""

    def setup_method(self):
        """테스트 전 싱글톤 초기화."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            reset_blast_radius_integration,
        )
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()
        reset_blast_radius_integration()

    def teardown_method(self):
        """테스트 후 정리."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            reset_blast_radius_integration,
        )
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()
        reset_blast_radius_integration()

    def test_service_config_and_blast_radius_sync(self):
        """ServiceConfig와 BlastRadius criticality 동기화."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            get_blast_radius_integration,
        )
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        # 서비스 설정
        config_manager = get_service_config_manager()
        config_manager.register_services(
            [
                ServiceConfig(
                    service_id="payment-api", criticality="critical", shed_priority=0
                ),
                ServiceConfig(
                    service_id="order-api", criticality="high", shed_priority=1
                ),
                ServiceConfig(
                    service_id="review-api", criticality="low", shed_priority=10
                ),
            ]
        )

        # BlastRadius에 동일한 criticality로 등록
        integration = get_blast_radius_integration()
        for config in config_manager.get_all_services():
            integration.register_dependency(
                service_id=config.service_id,
                criticality=config.criticality,
            )

        # 의존성 추가
        integration.register_dependency(
            "order-api", depends_on=["payment-api"], criticality="high"
        )
        integration.register_dependency(
            "review-api", depends_on=["order-api"], criticality="low"
        )

        # payment-api OPEN 시 영향 평가
        assessment = integration.assess_impact("payment-api")

        assert assessment.affected_count == 2
        assert "order-api" in assessment.affected_services

    def test_load_shedding_targets_and_blast_radius(self):
        """Load Shedding 대상과 Blast Radius 조합."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            BlastRadiusLevel,
            get_blast_radius_integration,
        )
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        # 서비스 설정
        config_manager = get_service_config_manager()
        config_manager.register_services(
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
            ]
        )

        # Load Shedding 대상 조회
        shedding_targets = config_manager.get_shedding_targets(["low"])

        assert len(shedding_targets) == 2

        # 각 Shedding 대상의 Blast Radius 평가
        integration = get_blast_radius_integration()
        for target in shedding_targets:
            integration.register_dependency(
                target.service_id, criticality=target.criticality
            )

        # low criticality 서비스는 영향이 작음
        for target in shedding_targets:
            assessment = integration.assess_impact(target.service_id)
            # low 서비스는 보통 MINIMAL 영향
            assert assessment.level in [
                BlastRadiusLevel.MINIMAL,
                BlastRadiusLevel.MODERATE,
            ]

    def test_critical_service_protection(self):
        """critical 서비스 보호 확인."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            BlastRadiusLevel,
            get_blast_radius_integration,
        )
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
            is_critical_service,
        )

        config_manager = get_service_config_manager()
        config_manager.register_services(
            [
                ServiceConfig(
                    service_id="payment-api", criticality="critical", shed_priority=0
                ),
                ServiceConfig(service_id="db-api", criticality="high", shed_priority=0),
            ]
        )

        # critical 서비스 확인
        assert is_critical_service("payment-api") is True
        assert is_critical_service("db-api") is False

        # critical 서비스에 영향주면 CRITICAL 레벨
        integration = get_blast_radius_integration()
        integration.register_dependency("db-api", criticality="high")
        integration.register_dependency(
            "payment-api", depends_on=["db-api"], criticality="critical"
        )

        assessment = integration.assess_impact("db-api")

        assert assessment.level == BlastRadiusLevel.CRITICAL
        assert "payment-api" in assessment.critical_services_affected


class TestModuleLevelConvenienceFunctions:
    """모듈 레벨 편의 함수 테스트."""

    def setup_method(self):
        """테스트 전 싱글톤 초기화."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            reset_blast_radius_integration,
        )
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()
        reset_blast_radius_integration()

    def teardown_method(self):
        """테스트 후 정리."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            reset_blast_radius_integration,
        )
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )

        reset_service_config_manager()
        reset_blast_radius_integration()

    def test_service_config_convenience_functions(self):
        """ServiceConfig 편의 함수."""
        from baldur.services.circuit_breaker.service_config import (
            get_service_config,
            get_services_by_criticality,
            get_shedding_targets,
            is_critical_service,
            register_service,
        )

        # 등록
        register_service(
            ServiceConfig(
                service_id="payment-api",
                criticality="critical",
            )
        )
        register_service(
            ServiceConfig(
                service_id="review-api",
                criticality="low",
                shed_priority=10,
            )
        )

        # 조회
        config = get_service_config("payment-api")
        assert config is not None
        assert config.criticality == "critical"

        # criticality 조회
        critical = get_services_by_criticality("critical")
        assert len(critical) == 1

        # Shedding 대상
        targets = get_shedding_targets(["low"])
        assert len(targets) == 1

        # critical 여부
        assert is_critical_service("payment-api") is True
        assert is_critical_service("review-api") is False

    def test_blast_radius_convenience_functions(self):
        """BlastRadius 편의 함수."""
        from baldur.services.circuit_breaker.blast_radius_integration import (
            assess_cb_open_impact,
            register_service_dependency,
            should_allow_cb_auto_open,
        )

        # 의존성 등록
        register_service_dependency("payment-api", criticality="critical")
        register_service_dependency(
            "order-api", depends_on=["payment-api"], criticality="high"
        )

        # 영향 평가
        assessment = assess_cb_open_impact("payment-api", "test event")
        assert assessment.affected_count >= 1

        # 자동 OPEN 허용 여부
        allowed, reason = should_allow_cb_auto_open("payment-api")
        # 결과 확인 (CRITICAL 여부에 따라 다름)
        assert isinstance(allowed, bool)


class TestExportsFromInit:
    """__init__.py exports 테스트."""

    def test_cascade_prevention_exports_available(self):
        """연쇄 장애 방지 exports가 __init__.py에서 사용 가능한지 확인."""
        from baldur.services.circuit_breaker import (
            BlastRadiusIntegration,
            # Blast Radius
            BlastRadiusLevel,
            # Service Config
            ServiceConfigManager,
        )

        # 모두 import 가능
        assert ServiceConfigManager is not None
        assert BlastRadiusLevel is not None
        assert BlastRadiusIntegration is not None
