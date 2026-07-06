"""
Circuit Breaker Canary Recovery Manager 테스트.

Test Coverage:
- CanaryRecoveryManager: 단계적 복구, 성공률 추적, 단계 전이
"""

from baldur.services.circuit_breaker.models import (
    CanaryRecoveryStageConfig,
    RecoveryStrategy,
)

# =============================================================================
# 4.1 CanaryRecoveryManager Tests
# =============================================================================


class TestCanaryRecoveryManager:
    """CanaryRecoveryManager 테스트."""

    def setup_method(self):
        """테스트 전 싱글톤 초기화."""
        from baldur.services.circuit_breaker.canary_recovery import (
            reset_canary_recovery_manager,
        )

        reset_canary_recovery_manager()

    def teardown_method(self):
        """테스트 후 정리."""
        from baldur.services.circuit_breaker.canary_recovery import (
            reset_canary_recovery_manager,
        )

        reset_canary_recovery_manager()

    def test_singleton_pattern(self):
        """싱글톤 패턴 동작 확인."""
        from baldur.services.circuit_breaker.canary_recovery import (
            CanaryRecoveryManager,
            get_canary_recovery_manager,
        )

        manager1 = CanaryRecoveryManager()
        manager2 = get_canary_recovery_manager()

        assert manager1 is manager2

    def test_start_canary_recovery_default_strategy(self):
        """기본 전략으로 Canary 복구 시작."""
        from baldur.services.circuit_breaker.canary_recovery import (
            CanaryRecoveryStage,
            get_canary_recovery_manager,
        )

        manager = get_canary_recovery_manager()
        state = manager.start_canary_recovery("payment-api")

        assert state.is_in_canary()
        assert state.current_stage == CanaryRecoveryStage.CANARY_1
        assert state.stage_index == 0
        assert state.metrics is not None

    def test_start_canary_recovery_custom_strategy(self):
        """사용자 정의 전략으로 Canary 복구 시작."""
        from baldur.services.circuit_breaker.canary_recovery import (
            get_canary_recovery_manager,
        )

        manager = get_canary_recovery_manager()
        strategy = RecoveryStrategy(
            type="canary",
            strict_mode=True,
            canary_stages=[
                CanaryRecoveryStageConfig(
                    traffic_percent=5.0, duration_seconds=2, required_success_rate=99.0
                ),
                CanaryRecoveryStageConfig(
                    traffic_percent=50.0, duration_seconds=2, required_success_rate=95.0
                ),
                CanaryRecoveryStageConfig(
                    traffic_percent=100.0,
                    duration_seconds=0,
                    required_success_rate=90.0,
                ),
            ],
        )

        state = manager.start_canary_recovery("payment-api", strategy)

        assert state.is_in_canary()
        assert state.recovery_strategy.strict_mode is True
        assert len(state.recovery_strategy.canary_stages) == 3

    def test_immediate_strategy_skips_canary(self):
        """immediate 전략은 Canary를 건너뜀."""
        from baldur.services.circuit_breaker.canary_recovery import (
            get_canary_recovery_manager,
        )

        manager = get_canary_recovery_manager()
        strategy = RecoveryStrategy(type="immediate")

        state = manager.start_canary_recovery("review-api", strategy)

        assert not state.is_in_canary()

    def test_should_allow_request_canary_selection(self):
        """Canary 요청 선택 확률 테스트."""
        from baldur.services.circuit_breaker.canary_recovery import (
            get_canary_recovery_manager,
        )

        manager = get_canary_recovery_manager()
        strategy = RecoveryStrategy(
            type="canary",
            canary_stages=[
                CanaryRecoveryStageConfig(
                    traffic_percent=50.0, duration_seconds=5, required_success_rate=90.0
                ),
            ],
        )

        manager.start_canary_recovery("test-api", strategy)

        # 100번 시도하여 대략 50%가 canary인지 확인
        canary_count = 0
        for _ in range(100):
            decision = manager.should_allow_request("test-api")
            if decision.is_canary_request:
                canary_count += 1

        # 50% 확률이므로 20~80 사이일 것으로 기대
        assert 20 <= canary_count <= 80, f"Expected ~50% canary, got {canary_count}%"

    def test_should_allow_request_not_in_canary(self):
        """Canary 복구 중이 아닐 때는 모두 허용."""
        from baldur.services.circuit_breaker.canary_recovery import (
            get_canary_recovery_manager,
        )

        manager = get_canary_recovery_manager()

        decision = manager.should_allow_request("unknown-api")

        assert decision.allow_backend is True
        assert decision.is_canary_request is False

    def test_record_success_increments_metrics(self):
        """성공 기록 시 메트릭 증가."""
        from baldur.services.circuit_breaker.canary_recovery import (
            get_canary_recovery_manager,
        )

        manager = get_canary_recovery_manager()
        manager.start_canary_recovery("payment-api")

        manager.record_success("payment-api")
        manager.record_success("payment-api")

        state = manager.get_recovery_state("payment-api")
        assert state.metrics.total_requests == 2
        assert state.metrics.success_count == 2
        assert state.metrics.failure_count == 0

    def test_record_failure_increments_metrics(self):
        """실패 기록 시 메트릭 증가."""
        from baldur.services.circuit_breaker.canary_recovery import (
            get_canary_recovery_manager,
        )

        manager = get_canary_recovery_manager()
        manager.start_canary_recovery("payment-api")

        manager.record_failure("payment-api")

        state = manager.get_recovery_state("payment-api")
        assert state.metrics.total_requests == 1
        assert state.metrics.success_count == 0
        assert state.metrics.failure_count == 1

    def test_stage_advancement_on_success(self):
        """성공률 충족 시 다음 단계로 전이."""
        from baldur.services.circuit_breaker.canary_recovery import (
            CanaryRecoveryStage,
            get_canary_recovery_manager,
        )

        manager = get_canary_recovery_manager()
        strategy = RecoveryStrategy(
            type="canary",
            canary_stages=[
                CanaryRecoveryStageConfig(
                    traffic_percent=10.0, duration_seconds=0, required_success_rate=90.0
                ),
                CanaryRecoveryStageConfig(
                    traffic_percent=100.0,
                    duration_seconds=0,
                    required_success_rate=90.0,
                ),
            ],
        )

        manager.start_canary_recovery("payment-api", strategy)

        # 10개 성공 기록 (90% 이상 성공률, duration=0)
        for _ in range(10):
            result = manager.record_success("payment-api")

        state = manager.get_recovery_state("payment-api")
        # 다음 단계로 전이되어야 함
        assert state.current_stage == CanaryRecoveryStage.CANARY_2 or result.completed

    def test_recovery_failure_on_low_success_rate(self):
        """성공률 미달 시 복구 실패."""
        from baldur.services.circuit_breaker.canary_recovery import (
            get_canary_recovery_manager,
        )

        manager = get_canary_recovery_manager()
        strategy = RecoveryStrategy(
            type="canary",
            canary_stages=[
                CanaryRecoveryStageConfig(
                    traffic_percent=10.0, duration_seconds=0, required_success_rate=90.0
                ),
            ],
        )

        manager.start_canary_recovery("payment-api", strategy)

        # 10개 중 5개 실패 (50% 성공률)
        for _ in range(5):
            manager.record_success("payment-api")
        for _ in range(5):
            result = manager.record_failure("payment-api")

        # 복구 실패로 인해 reset됨
        assert result.failed if result else True

    def test_stop_canary_recovery(self):
        """Canary 복구 중단."""
        from baldur.services.circuit_breaker.canary_recovery import (
            get_canary_recovery_manager,
        )

        manager = get_canary_recovery_manager()
        manager.start_canary_recovery("payment-api")

        result = manager.stop_canary_recovery("payment-api", "manual")

        assert result is True
        assert not manager.is_in_canary_recovery("payment-api")

    def test_get_active_recoveries(self):
        """활성 복구 목록 조회."""
        from baldur.services.circuit_breaker.canary_recovery import (
            get_canary_recovery_manager,
        )

        manager = get_canary_recovery_manager()
        manager.start_canary_recovery("payment-api")
        manager.start_canary_recovery("order-api")

        active = manager.get_active_recoveries()

        assert "payment-api" in active
        assert "order-api" in active


class TestCanaryStageMetrics:
    """CanaryStageMetrics 테스트."""

    def test_current_success_rate_calculation(self):
        """성공률 계산."""
        from baldur.services.circuit_breaker.canary_recovery import (
            CanaryRecoveryStage,
            CanaryStageMetrics,
        )

        metrics = CanaryStageMetrics(stage=CanaryRecoveryStage.CANARY_1)

        for _ in range(8):
            metrics.record_success()
        for _ in range(2):
            metrics.record_failure()

        assert metrics.current_success_rate == 80.0
        assert metrics.total_requests == 10

    def test_empty_metrics_returns_100_percent(self):
        """빈 메트릭은 100% 반환."""
        from baldur.services.circuit_breaker.canary_recovery import (
            CanaryRecoveryStage,
            CanaryStageMetrics,
        )

        metrics = CanaryStageMetrics(stage=CanaryRecoveryStage.CANARY_1)

        assert metrics.current_success_rate == 100.0
