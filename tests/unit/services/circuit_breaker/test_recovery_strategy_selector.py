"""
Circuit Breaker Recovery Strategy Selector 테스트.

Test Coverage:
- RecoveryStrategySelector: immediate vs canary 선택, criticality 기반 선택
- 통합 테스트: 복구 전략 통합 흐름
- 편의 함수 테스트
"""

from unittest.mock import MagicMock

from baldur.services.circuit_breaker.models import (
    CanaryRecoveryStageConfig,
    RecoveryStrategy,
    ServiceConfig,
)

# =============================================================================
# 4.3 RecoveryStrategySelector Tests
# =============================================================================


class TestRecoveryStrategySelector:
    """RecoveryStrategySelector 테스트."""

    def setup_method(self):
        """테스트 전 싱글톤 초기화."""
        from baldur.services.circuit_breaker.canary_recovery import (
            reset_canary_recovery_manager,
        )
        from baldur.services.circuit_breaker.recovery_strategy import (
            reset_recovery_strategy_selector,
        )
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )
        from baldur.services.circuit_breaker.stale_cache_integration import (
            reset_canary_stale_cache_service,
        )

        reset_recovery_strategy_selector()
        reset_canary_recovery_manager()
        reset_canary_stale_cache_service()
        reset_service_config_manager()

    def teardown_method(self):
        """테스트 후 정리."""
        from baldur.services.circuit_breaker.canary_recovery import (
            reset_canary_recovery_manager,
        )
        from baldur.services.circuit_breaker.recovery_strategy import (
            reset_recovery_strategy_selector,
        )
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )
        from baldur.services.circuit_breaker.stale_cache_integration import (
            reset_canary_stale_cache_service,
        )

        reset_recovery_strategy_selector()
        reset_canary_recovery_manager()
        reset_canary_stale_cache_service()
        reset_service_config_manager()

    def test_singleton_pattern(self):
        """싱글톤 패턴 동작 확인."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            RecoveryStrategySelector,
            get_recovery_strategy_selector,
        )

        selector1 = RecoveryStrategySelector()
        selector2 = get_recovery_strategy_selector()

        assert selector1 is selector2

    def test_select_strategy_from_service_config(self):
        """서비스 설정에서 전략 선택."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            get_recovery_strategy_selector,
        )
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        # 서비스 설정 등록
        config_manager = get_service_config_manager()
        custom_strategy = RecoveryStrategy(type="immediate")
        config = ServiceConfig(
            service_id="payment-api",
            criticality="critical",
            recovery_strategy=custom_strategy,
        )
        config_manager.register_service(config)

        selector = get_recovery_strategy_selector()
        selection = selector.select_strategy("payment-api")

        assert selection.strategy_type == "immediate"
        assert selection.source == "service_config"

    def test_select_strategy_from_criticality(self):
        """criticality 기반 전략 선택."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            get_recovery_strategy_selector,
        )
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        # 서비스 설정 등록 (recovery_strategy 없음)
        config_manager = get_service_config_manager()
        config = ServiceConfig(
            service_id="review-api",
            criticality="low",
        )
        config_manager.register_service(config)

        selector = get_recovery_strategy_selector()
        selection = selector.select_strategy("review-api")

        # low criticality는 immediate
        assert selection.strategy_type == "immediate"
        assert selection.source == "criticality_based"

    def test_select_strategy_default(self):
        """기본 전략 선택."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            get_recovery_strategy_selector,
        )

        selector = get_recovery_strategy_selector()
        selection = selector.select_strategy("unknown-api")

        assert selection.source == "default"

    def test_critical_service_uses_strict_canary(self):
        """critical 서비스는 strict canary 사용."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            get_recovery_strategy_selector,
        )
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        config_manager = get_service_config_manager()
        config = ServiceConfig(
            service_id="payment-api",
            criticality="critical",
        )
        config_manager.register_service(config)

        selector = get_recovery_strategy_selector()
        selection = selector.select_strategy("payment-api")

        assert selection.strategy_type == "canary"
        assert selection.strategy.strict_mode is True

    def test_start_recovery_with_canary(self):
        """Canary 전략으로 복구 시작."""
        from baldur.services.circuit_breaker.canary_recovery import (
            get_canary_recovery_manager,
        )
        from baldur.services.circuit_breaker.recovery_strategy import (
            get_recovery_strategy_selector,
        )

        selector = get_recovery_strategy_selector()
        selector.set_default_strategy(RecoveryStrategy(type="canary"))

        selection = selector.start_recovery("payment-api")

        assert selection.strategy_type == "canary"
        assert selector.is_in_recovery("payment-api")

        # Canary 매니저에도 등록됨
        manager = get_canary_recovery_manager()
        assert manager.is_in_canary_recovery("payment-api")

    def test_start_recovery_with_immediate(self):
        """Immediate 전략으로 복구 시작."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            get_recovery_strategy_selector,
        )

        selector = get_recovery_strategy_selector()
        selector.set_default_strategy(RecoveryStrategy(type="immediate"))

        selection = selector.start_recovery("review-api")

        assert selection.strategy_type == "immediate"
        assert selector.is_in_recovery("review-api")

    def test_handle_half_open_request_immediate(self):
        """Immediate 전략의 HALF_OPEN 요청 처리."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            get_recovery_strategy_selector,
        )

        selector = get_recovery_strategy_selector()
        selector.set_default_strategy(RecoveryStrategy(type="immediate"))
        selector.start_recovery("review-api")

        decision = selector.handle_half_open_request("review-api")

        assert decision.allow_backend is True
        assert decision.strategy_type == "immediate"
        assert decision.traffic_percent == 100.0

    def test_handle_half_open_request_canary(self):
        """Canary 전략의 HALF_OPEN 요청 처리."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            get_recovery_strategy_selector,
        )

        selector = get_recovery_strategy_selector()
        strategy = RecoveryStrategy(
            type="canary",
            canary_stages=[
                CanaryRecoveryStageConfig(
                    traffic_percent=100.0,
                    duration_seconds=5,
                    required_success_rate=90.0,
                ),
            ],
        )
        selector.set_default_strategy(strategy)
        selector.start_recovery("payment-api")

        decision = selector.handle_half_open_request("payment-api")

        assert decision.strategy_type == "canary"
        # 100% 트래픽이므로 항상 허용
        assert decision.allow_backend is True

    def test_record_success_advances_stage(self):
        """성공 기록이 단계 전이를 유발."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            get_recovery_strategy_selector,
        )

        selector = get_recovery_strategy_selector()
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
        selector.set_default_strategy(strategy)
        selector.start_recovery("payment-api")

        # 충분한 성공 기록
        for _ in range(10):
            result = selector.record_success("payment-api")

        # 단계가 전이되거나 완료됨
        assert (
            result is not None or selector.get_recovery_type("payment-api") == "canary"
        )

    def test_record_failure_may_fail_recovery(self):
        """실패 기록이 복구 실패를 유발할 수 있음."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            get_recovery_strategy_selector,
        )

        selector = get_recovery_strategy_selector()
        strategy = RecoveryStrategy(
            type="canary",
            canary_stages=[
                CanaryRecoveryStageConfig(
                    traffic_percent=10.0, duration_seconds=0, required_success_rate=90.0
                ),
            ],
        )
        selector.set_default_strategy(strategy)
        selector.start_recovery("payment-api")

        # 많은 실패 기록
        for _ in range(10):
            result = selector.record_failure("payment-api")

        # 성공률 0%이므로 복구 실패
        if result:
            assert result.failed

    def test_stop_recovery(self):
        """복구 중단."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            get_recovery_strategy_selector,
        )

        selector = get_recovery_strategy_selector()
        selector.start_recovery("payment-api")

        result = selector.stop_recovery("payment-api", "manual")

        assert result is True
        assert not selector.is_in_recovery("payment-api")

    def test_get_active_recoveries(self):
        """활성 복구 목록."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            get_recovery_strategy_selector,
        )

        selector = get_recovery_strategy_selector()
        selector.start_recovery("payment-api")
        selector.start_recovery("order-api")

        active = selector.get_active_recoveries()

        assert "payment-api" in active
        assert "order-api" in active

    def test_get_recovery_status(self):
        """복구 상태 조회."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            get_recovery_strategy_selector,
        )

        selector = get_recovery_strategy_selector()
        selector.set_default_strategy(RecoveryStrategy(type="canary"))
        selector.start_recovery("payment-api")

        status = selector.get_recovery_status("payment-api")

        assert status is not None
        assert status["service_id"] == "payment-api"
        assert status["strategy_type"] == "canary"
        assert "canary_state" in status

    def test_record_success_returns_canary_result_and_cleans_up_on_completion(self):
        """record_success on a canary recovery forwards to the canary manager,
        returns its result, and removes the service from active recoveries once
        the result is complete.

        Pins three behaviors the prior assertions left unverified: the
        canary-only gate direction, the forwarded result value, and the
        post-completion cleanup of the active-recovery map (a missed cleanup
        leaves a recovered service pinned as forever-recovering)."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            get_recovery_strategy_selector,
        )

        selector = get_recovery_strategy_selector()
        selector.set_default_strategy(
            RecoveryStrategy(
                type="canary",
                canary_stages=[
                    CanaryRecoveryStageConfig(
                        traffic_percent=100.0,
                        duration_seconds=0,
                        required_success_rate=90.0,
                    ),
                ],
            )
        )
        selector.start_recovery("payment-api")
        assert selector.is_in_recovery("payment-api")

        completed = MagicMock()
        completed.completed = True
        selector._canary_manager.record_success = MagicMock(return_value=completed)

        result = selector.record_success("payment-api")

        selector._canary_manager.record_success.assert_called_once_with("payment-api")
        assert result is completed
        assert not selector.is_in_recovery("payment-api")

    def test_record_success_is_noop_for_immediate_strategy(self):
        """record_success must not delegate to the canary manager for a
        non-canary (immediate) recovery — it returns None without forwarding."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            get_recovery_strategy_selector,
        )

        selector = get_recovery_strategy_selector()
        selector.set_default_strategy(RecoveryStrategy(type="immediate"))
        selector.start_recovery("review-api")
        selector._canary_manager.record_success = MagicMock()

        result = selector.record_success("review-api")

        assert result is None
        selector._canary_manager.record_success.assert_not_called()

    def test_record_failure_returns_canary_result_and_cleans_up_on_failure(self):
        """record_failure on a canary recovery forwards to the canary manager,
        returns its result, and removes the service from active recoveries once
        the recovery has failed."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            get_recovery_strategy_selector,
        )

        selector = get_recovery_strategy_selector()
        selector.set_default_strategy(
            RecoveryStrategy(
                type="canary",
                canary_stages=[
                    CanaryRecoveryStageConfig(
                        traffic_percent=100.0,
                        duration_seconds=0,
                        required_success_rate=90.0,
                    ),
                ],
            )
        )
        selector.start_recovery("payment-api")
        assert selector.is_in_recovery("payment-api")

        failed = MagicMock()
        failed.failed = True
        selector._canary_manager.record_failure = MagicMock(return_value=failed)

        result = selector.record_failure("payment-api")

        selector._canary_manager.record_failure.assert_called_once_with("payment-api")
        assert result is failed
        assert not selector.is_in_recovery("payment-api")

    def test_record_failure_is_noop_for_immediate_strategy(self):
        """record_failure must not delegate to the canary manager for a
        non-canary (immediate) recovery — it returns None without forwarding."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            get_recovery_strategy_selector,
        )

        selector = get_recovery_strategy_selector()
        selector.set_default_strategy(RecoveryStrategy(type="immediate"))
        selector.start_recovery("review-api")
        selector._canary_manager.record_failure = MagicMock()

        result = selector.record_failure("review-api")

        assert result is None
        selector._canary_manager.record_failure.assert_not_called()

    def test_handle_half_open_auto_starts_when_no_active_recovery(self):
        """handle_half_open_request must auto-start recovery when the service has
        none yet (the ``strategy_type is None`` branch) and then decide with the
        started strategy — not fall through to the canary path with no recovery."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            get_recovery_strategy_selector,
        )

        selector = get_recovery_strategy_selector()
        selector.set_default_strategy(RecoveryStrategy(type="immediate"))

        # No start_recovery() first — handle_half_open_request must start it.
        decision = selector.handle_half_open_request("fresh-service")

        assert decision.strategy_type == "immediate"
        assert decision.allow_backend is True
        assert selector.is_in_recovery("fresh-service")


# =============================================================================
# Integration Tests
# =============================================================================


class TestRecoveryStrategyIntegration:
    """복구 전략 통합 테스트."""

    def setup_method(self):
        """테스트 전 싱글톤 초기화."""
        from baldur.services.circuit_breaker.canary_recovery import (
            reset_canary_recovery_manager,
        )
        from baldur.services.circuit_breaker.recovery_strategy import (
            reset_recovery_strategy_selector,
        )
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )
        from baldur.services.circuit_breaker.stale_cache_integration import (
            reset_canary_stale_cache_service,
        )

        reset_recovery_strategy_selector()
        reset_canary_recovery_manager()
        reset_canary_stale_cache_service()
        reset_service_config_manager()

    def teardown_method(self):
        """테스트 후 정리."""
        from baldur.services.circuit_breaker.canary_recovery import (
            reset_canary_recovery_manager,
        )
        from baldur.services.circuit_breaker.recovery_strategy import (
            reset_recovery_strategy_selector,
        )
        from baldur.services.circuit_breaker.service_config import (
            reset_service_config_manager,
        )
        from baldur.services.circuit_breaker.stale_cache_integration import (
            reset_canary_stale_cache_service,
        )

        reset_recovery_strategy_selector()
        reset_canary_recovery_manager()
        reset_canary_stale_cache_service()
        reset_service_config_manager()

    def test_full_canary_recovery_flow(self):
        """전체 Canary 복구 흐름 테스트."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            get_recovery_strategy_selector,
        )
        from baldur.services.circuit_breaker.stale_cache_integration import (
            get_canary_stale_cache_service,
        )

        selector = get_recovery_strategy_selector()
        get_canary_stale_cache_service()

        # 빠른 테스트를 위한 전략 설정
        strategy = RecoveryStrategy(
            type="canary",
            canary_stages=[
                CanaryRecoveryStageConfig(
                    traffic_percent=100.0,
                    duration_seconds=0,
                    required_success_rate=80.0,
                ),
            ],
        )
        selector.set_default_strategy(strategy)

        # 1. 복구 시작
        selection = selector.start_recovery("payment-api")
        assert selection.strategy_type == "canary"

        # 2. 요청 처리
        decision = selector.handle_half_open_request("payment-api")
        assert decision.allow_backend is True

        # 3. 성공 기록
        for _ in range(5):
            result = selector.record_success("payment-api")

        # 4. 복구 완료 확인
        assert (
            result is None or result.completed or selector.is_in_recovery("payment-api")
        )

    def test_canary_with_stale_cache_flow(self):
        """Canary + Stale Cache 통합 흐름."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            get_recovery_strategy_selector,
        )
        from baldur.services.circuit_breaker.stale_cache_integration import (
            get_canary_stale_cache_service,
        )

        selector = get_recovery_strategy_selector()
        stale_cache = get_canary_stale_cache_service()

        # 0% 트래픽 (모든 요청이 stale cache)
        strategy = RecoveryStrategy(
            type="canary",
            canary_stages=[
                CanaryRecoveryStageConfig(
                    traffic_percent=0.0, duration_seconds=5, required_success_rate=90.0
                ),
            ],
        )
        selector.set_default_strategy(strategy)

        # 캐시 설정
        stale_cache.update_cache("payment:123", {"amount": 500})

        # 복구 시작
        selector.start_recovery("payment-api")

        # 요청 처리 (cache_key 포함)
        decision = selector.handle_half_open_request(
            service_id="payment-api",
            cache_key="payment:123",
        )

        # 0% 트래픽이므로 stale cache 사용
        assert decision.use_stale_cache is True
        assert decision.stale_data == {"amount": 500}

    def test_criticality_based_strategy_selection(self):
        """Criticality 기반 전략 선택 통합 테스트."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            get_recovery_strategy_selector,
        )
        from baldur.services.circuit_breaker.service_config import (
            get_service_config_manager,
        )

        config_manager = get_service_config_manager()

        # 서비스 등록
        config_manager.register_service(
            ServiceConfig(
                service_id="payment-api",
                criticality="critical",
            )
        )
        config_manager.register_service(
            ServiceConfig(
                service_id="order-api",
                criticality="high",
            )
        )
        config_manager.register_service(
            ServiceConfig(
                service_id="review-api",
                criticality="low",
            )
        )

        selector = get_recovery_strategy_selector()

        # Critical: canary + strict
        payment_selection = selector.select_strategy("payment-api")
        assert payment_selection.strategy_type == "canary"
        assert payment_selection.strategy.strict_mode is True

        # High: canary (not strict)
        order_selection = selector.select_strategy("order-api")
        assert order_selection.strategy_type == "canary"
        assert order_selection.strategy.strict_mode is False

        # Low: immediate
        review_selection = selector.select_strategy("review-api")
        assert review_selection.strategy_type == "immediate"


# =============================================================================
# Convenience Function Tests
# =============================================================================


class TestConvenienceFunctions:
    """편의 함수 테스트."""

    def setup_method(self):
        """테스트 전 싱글톤 초기화."""
        from baldur.services.circuit_breaker.canary_recovery import (
            reset_canary_recovery_manager,
        )
        from baldur.services.circuit_breaker.recovery_strategy import (
            reset_recovery_strategy_selector,
        )
        from baldur.services.circuit_breaker.stale_cache_integration import (
            reset_canary_stale_cache_service,
        )

        reset_recovery_strategy_selector()
        reset_canary_recovery_manager()
        reset_canary_stale_cache_service()

    def teardown_method(self):
        """테스트 후 정리."""
        from baldur.services.circuit_breaker.canary_recovery import (
            reset_canary_recovery_manager,
        )
        from baldur.services.circuit_breaker.recovery_strategy import (
            reset_recovery_strategy_selector,
        )
        from baldur.services.circuit_breaker.stale_cache_integration import (
            reset_canary_stale_cache_service,
        )

        reset_recovery_strategy_selector()
        reset_canary_recovery_manager()
        reset_canary_stale_cache_service()

    def test_canary_convenience_functions(self):
        """Canary 편의 함수."""
        from baldur.services.circuit_breaker.canary_recovery import (
            canary_record_failure,
            canary_record_success,
            canary_should_allow_request,
            get_canary_recovery_state,
            is_in_canary_recovery,
            start_canary_recovery,
            stop_canary_recovery,
        )

        # 시작
        state = start_canary_recovery("test-api")
        assert is_in_canary_recovery("test-api")

        # 결정
        decision = canary_should_allow_request("test-api")
        assert decision is not None

        # 성공/실패 기록
        canary_record_success("test-api")
        canary_record_failure("test-api")

        # 상태 조회
        state = get_canary_recovery_state("test-api")
        assert state is not None

        # 중단
        stop_canary_recovery("test-api")
        assert not is_in_canary_recovery("test-api")

    def test_stale_cache_convenience_functions(self):
        """Stale Cache 편의 함수."""
        from baldur.services.circuit_breaker.stale_cache_integration import (
            should_allow_with_fallback as canary_should_allow_with_fallback,
        )
        from baldur.services.circuit_breaker.stale_cache_integration import (
            update_stale_cache,
        )

        # 캐시 업데이트
        entry = update_stale_cache("key1", {"data": "value"})
        assert entry.value == {"data": "value"}

        # 결정 (CLOSED 상태)
        decision = canary_should_allow_with_fallback(
            service_id="test-api",
            cache_key="key1",
            cb_state="closed",
        )
        assert decision.allow_backend is True

    def test_recovery_strategy_convenience_functions(self):
        """Recovery Strategy 편의 함수."""
        from baldur.services.circuit_breaker.recovery_strategy import (
            handle_half_open,
            record_recovery_failure,
            record_recovery_success,
            select_recovery_strategy,
            start_service_recovery,
            stop_service_recovery,
        )

        # 전략 선택
        selection = select_recovery_strategy("test-api")
        assert selection is not None

        # 복구 시작
        selection = start_service_recovery("test-api")
        assert selection is not None

        # 요청 처리
        decision = handle_half_open("test-api")
        assert decision is not None

        # 성공/실패 기록
        record_recovery_success("test-api")
        record_recovery_failure("test-api")

        # 복구 중단
        stop_service_recovery("test-api")
