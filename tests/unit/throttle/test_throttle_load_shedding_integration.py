"""
AdaptiveThrottle Load Shedding 연동 단위 테스트.

테스트 대상:
1. __init__: _shedding_suggested_limit, _shedding_affected_services 초기화
2. reset_all: Shedding 상태 초기화
3. conservative_limit: _shedding_suggested_limit 참여 (Min-Winner)
4. _subscribe_load_shedding_events: EventBus 구독 등록
5. _handle_shedding_changed: Shedding 활성화/비활성화 이벤트 처리
6. check(): context.service_id 기반 Load Shedding limit 적용
7. shedding_compensation_factor 보상 계수 적용
8. check(): self._service_name fallback 기반 Shedding 매칭
9. critical tier 요청의 Shedding 보호
10. Emergency Mode + Shedding 동시 활성 시 Conservative Limit (Min-Winner)
"""

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from unittest.mock import MagicMock, patch

from baldur.services.event_bus.bus import (
    BaldurEvent,
    EventPriority,
    EventType,
)
from baldur.settings.throttle import ThrottleSettings
from baldur_pro.services.throttle.adaptive import (
    AdaptiveThrottle,
    reset_adaptive_throttle,
)
from baldur_pro.services.throttle.config import ThrottleConfig


class TestSheddingStateInitialization:
    """__init__ 시 Load Shedding 연동 상태 초기화 검증."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_shedding_suggested_limit_initialized_to_max_limit(self):
        """_shedding_suggested_limit 초기값 = config.max_limit."""
        config = ThrottleConfig(max_limit=500)
        throttle = AdaptiveThrottle(config)
        assert throttle._shedding_suggested_limit == config.max_limit

    def test_shedding_affected_services_initialized_empty(self):
        """_shedding_affected_services 초기값 = 빈 set."""
        throttle = AdaptiveThrottle(ThrottleConfig())
        assert throttle._shedding_affected_services == set()
        assert isinstance(throttle._shedding_affected_services, set)


class TestSheddingResetAll:
    """reset_all()에서 Load Shedding 상태 초기화 검증."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_reset_all_clears_shedding_suggested_limit(self):
        """reset_all() 후 _shedding_suggested_limit = max_limit."""
        config = ThrottleConfig(max_limit=500)
        throttle = AdaptiveThrottle(config)

        # Shedding 상태 변경
        throttle._shedding_suggested_limit = 100
        throttle._shedding_affected_services = {"order-api"}

        throttle.reset_all()

        assert throttle._shedding_suggested_limit == config.max_limit
        assert throttle._shedding_affected_services == set()


class TestConservativeLimitWithShedding:
    """conservative_limit에 _shedding_suggested_limit 참여 검증."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_shedding_limit_participates_in_min_winner(self):
        """_shedding_suggested_limit이 Min-Winner 정책에 참여."""
        config = ThrottleConfig(initial_limit=100, max_limit=500)
        throttle = AdaptiveThrottle(config)
        throttle._conservative_enabled = True

        # Shedding limit을 다른 suggested limit보다 낮게 설정
        throttle._shedding_suggested_limit = 50
        throttle._rtt_suggested_limit = 200
        throttle._429_suggested_limit = 500

        assert throttle.conservative_limit == 50

    def test_shedding_max_limit_does_not_affect_min_winner(self):
        """_shedding_suggested_limit = max_limit이면 min()에 영향 없음."""
        config = ThrottleConfig(initial_limit=100, max_limit=500)
        throttle = AdaptiveThrottle(config)
        throttle._conservative_enabled = True

        # 기본값(max_limit)이면 min()에 영향 없어야 함
        assert throttle._shedding_suggested_limit == config.max_limit

        throttle._rtt_suggested_limit = 100
        throttle._429_suggested_limit = 500

        # rtt_suggested_limit이 최소값이어야 함
        assert throttle.conservative_limit == throttle._rtt_suggested_limit

    def test_conservative_disabled_ignores_shedding(self):
        """conservative 비활성화 시 _shedding_suggested_limit 무시."""
        config = ThrottleConfig(initial_limit=100, max_limit=500)
        throttle = AdaptiveThrottle(config)
        throttle._conservative_enabled = False

        throttle._shedding_suggested_limit = 10

        # conservative 비활성화 시 _current_limit 그대로 반환
        assert throttle.conservative_limit == throttle._current_limit


class TestHandleSheddingChanged:
    """_handle_shedding_changed() 이벤트 핸들러 검증."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def _make_event(self, new_level, traffic_limit=100.0, affected_services=None):
        """테스트용 BaldurEvent 생성."""
        return BaldurEvent(
            event_type=EventType.LOAD_SHEDDING_LEVEL_CHANGED,
            data={
                "new_level": new_level,
                "traffic_limit": traffic_limit,
                "affected_services": affected_services or [],
            },
            source="load_shedding_manager",
            priority=EventPriority.HIGH,
        )

    def test_shedding_activated_sets_affected_services(self):
        """Shedding 활성화 시 affected_services 설정."""
        config = ThrottleConfig(max_limit=500)
        throttle = AdaptiveThrottle(config)

        event = self._make_event(
            new_level=0,
            traffic_limit=50.0,
            affected_services=["order-api", "review-api"],
        )
        throttle._handle_shedding_changed(event)

        assert throttle._shedding_affected_services == {"order-api", "review-api"}

    def test_shedding_activated_applies_compensation_factor(self):
        """Shedding 활성화 시 보상 계수가 적용되어 이중 차단 완화."""
        settings = ThrottleSettings()
        compensation_factor = settings.shedding_compensation_factor
        config = ThrottleConfig(max_limit=500, min_limit=10)
        throttle = AdaptiveThrottle(config)

        event = self._make_event(
            new_level=0,
            traffic_limit=50.0,
            affected_services=["review-api"],
        )
        throttle._handle_shedding_changed(event)

        raw_limit = int(config.max_limit * (50.0 / 100.0))  # 250
        compensated = min(config.max_limit, int(raw_limit * compensation_factor))
        expected = max(compensated, config.min_limit)

        assert throttle._shedding_suggested_limit == expected

    def test_shedding_activated_respects_min_limit(self):
        """Shedding 활성화 시 min_limit 이상 보장."""
        config = ThrottleConfig(max_limit=500, min_limit=10)
        throttle = AdaptiveThrottle(config)

        # 극단적으로 낮은 traffic_limit
        event = self._make_event(
            new_level=2,
            traffic_limit=0.1,
            affected_services=["review-api"],
        )
        throttle._handle_shedding_changed(event)

        assert throttle._shedding_suggested_limit >= config.min_limit

    def test_shedding_activated_respects_max_limit(self):
        """Shedding 활성화 시 max_limit 초과 불가."""
        config = ThrottleConfig(max_limit=500, min_limit=10)
        throttle = AdaptiveThrottle(config)

        # traffic_limit 100%에 compensation_factor 적용해도 max_limit 초과 불가
        event = self._make_event(
            new_level=0,
            traffic_limit=100.0,
            affected_services=["review-api"],
        )
        throttle._handle_shedding_changed(event)

        assert throttle._shedding_suggested_limit <= config.max_limit

    def test_shedding_deactivated_restores_max_limit(self):
        """Shedding 해제 시 shedding_suggested_limit = max_limit 복구."""
        config = ThrottleConfig(max_limit=500)
        throttle = AdaptiveThrottle(config)

        # 먼저 활성화
        activate_event = self._make_event(
            new_level=0,
            traffic_limit=50.0,
            affected_services=["review-api"],
        )
        throttle._handle_shedding_changed(activate_event)
        assert throttle._shedding_suggested_limit < config.max_limit

        # 해제 (new_level < 0)
        deactivate_event = self._make_event(new_level=-1)
        throttle._handle_shedding_changed(deactivate_event)

        assert throttle._shedding_suggested_limit == config.max_limit
        assert throttle._shedding_affected_services == set()

    def test_shedding_deactivated_starts_recovery_dampening(self):
        """Shedding 해제 시 다른 제한 미활성화면 Recovery Dampening 시작."""
        config = ThrottleConfig(max_limit=500)
        throttle = AdaptiveThrottle(config)
        throttle._emergency_mode_active = False
        throttle._429_reduction_active = False

        with patch.object(throttle, "start_recovery_dampening") as mock_dampening:
            event = self._make_event(new_level=-1)
            throttle._handle_shedding_changed(event)

            mock_dampening.assert_called_once_with(apply_jitter=True)

    def test_shedding_deactivated_skips_dampening_when_emergency_active(self):
        """Emergency 활성 시 Shedding 해제해도 Dampening 미시작."""
        config = ThrottleConfig(max_limit=500)
        throttle = AdaptiveThrottle(config)
        throttle._emergency_mode_active = True

        with patch.object(throttle, "start_recovery_dampening") as mock_dampening:
            event = self._make_event(new_level=-1)
            throttle._handle_shedding_changed(event)

            mock_dampening.assert_not_called()

    def test_shedding_deactivated_skips_dampening_when_429_active(self):
        """429 감소 활성 시 Shedding 해제해도 Dampening 미시작."""
        config = ThrottleConfig(max_limit=500)
        throttle = AdaptiveThrottle(config)
        throttle._429_reduction_active = True

        with patch.object(throttle, "start_recovery_dampening") as mock_dampening:
            event = self._make_event(new_level=-1)
            throttle._handle_shedding_changed(event)

            mock_dampening.assert_not_called()


class TestCheckWithServiceIdShedding:
    """check()에서 context.service_id 기반 Load Shedding limit 분기 검증."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_shedding_affected_service_uses_reduced_limit(self):
        """Shedding 대상 service_id의 요청은 감소된 limit 적용."""
        config = ThrottleConfig(initial_limit=100, max_limit=500, window_seconds=60)
        throttle = AdaptiveThrottle(config)

        throttle._shedding_affected_services = {"order-api"}
        throttle._shedding_suggested_limit = 5  # 매우 낮은 limit

        # 대상 서비스: shedding_suggested_limit 적용 (동일 key 반복)
        context = {"service_id": "order-api"}
        results = [throttle.check("same_key", context=context) for _ in range(10)]
        allowed = sum(1 for r in results if r.allowed)
        assert allowed == 5  # shedding limit에 의해 제한

    def test_non_affected_service_uses_full_limit(self):
        """Shedding 비대상 service_id의 요청은 기본 limit 적용."""
        config = ThrottleConfig(initial_limit=100, max_limit=500, window_seconds=60)
        throttle = AdaptiveThrottle(config)

        throttle._shedding_affected_services = {"order-api"}
        throttle._shedding_suggested_limit = 5

        # 비대상 서비스: 기본 limit 적용
        context = {"service_id": "payment-api"}
        results = [throttle.check(f"key_{i}", context=context) for i in range(10)]
        allowed = sum(1 for r in results if r.allowed)
        # 기본 initial_limit=100이므로 10개 모두 허용
        assert allowed == 10

    def test_no_context_uses_full_limit(self):
        """context 없는 요청은 기본 limit 적용."""
        config = ThrottleConfig(initial_limit=100, max_limit=500, window_seconds=60)
        throttle = AdaptiveThrottle(config)

        throttle._shedding_affected_services = {"order-api"}
        throttle._shedding_suggested_limit = 5

        results = [throttle.check(f"key_{i}") for i in range(10)]
        allowed = sum(1 for r in results if r.allowed)
        assert allowed == 10

    def test_no_service_id_in_context_uses_full_limit(self):
        """context에 service_id가 없으면 기본 limit 적용."""
        config = ThrottleConfig(initial_limit=100, max_limit=500, window_seconds=60)
        throttle = AdaptiveThrottle(config)

        throttle._shedding_affected_services = {"order-api"}
        throttle._shedding_suggested_limit = 5

        context = {"domain": "shopping"}  # service_id 없음
        results = [throttle.check(f"key_{i}", context=context) for i in range(10)]
        allowed = sum(1 for r in results if r.allowed)
        assert allowed == 10

    def test_empty_affected_services_uses_full_limit(self):
        """affected_services 비어있으면 모든 요청에 기본 limit 적용."""
        config = ThrottleConfig(initial_limit=100, max_limit=500, window_seconds=60)
        throttle = AdaptiveThrottle(config)

        throttle._shedding_affected_services = set()
        throttle._shedding_suggested_limit = 5  # 이 값은 무의미

        context = {"service_id": "order-api"}
        results = [throttle.check(f"key_{i}", context=context) for i in range(10)]
        allowed = sum(1 for r in results if r.allowed)
        assert allowed == 10

    def test_shedding_limit_swap_restores_original_limit(self):
        """임시 swap 후 원래 _current_limit이 복구됨."""
        config = ThrottleConfig(initial_limit=100, max_limit=500, window_seconds=60)
        throttle = AdaptiveThrottle(config)

        original_limit = throttle._current_limit
        throttle._shedding_affected_services = {"order-api"}
        throttle._shedding_suggested_limit = 5

        context = {"service_id": "order-api"}
        throttle.check("key_1", context=context)

        # 원래 limit 복구 확인
        assert throttle._current_limit == original_limit


class TestSubscribeLoadSheddingEvents:
    """_subscribe_load_shedding_events() 구독 등록 검증."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_subscribes_to_load_shedding_level_changed(self):
        """LOAD_SHEDDING_LEVEL_CHANGED 이벤트 구독 등록."""
        mock_bus = MagicMock()

        with patch("baldur.services.event_bus.get_event_bus", return_value=mock_bus):
            AdaptiveThrottle(ThrottleConfig())
            # __init__에서 _subscribe_load_shedding_events() 호출됨

            # subscribe 호출 확인 (여러 subscribe 중 LOAD_SHEDDING 확인)
            subscribe_calls = mock_bus.subscribe.call_args_list
            shedding_calls = [
                c
                for c in subscribe_calls
                if c[0][0] == EventType.LOAD_SHEDDING_LEVEL_CHANGED
            ]
            assert len(shedding_calls) == 1

    def test_subscribe_fails_gracefully_on_import_error(self):
        """EventBus import 실패 시 예외 발생하지 않음."""
        # _subscribe_load_shedding_events 내부의 import를 실패시킴
        with patch.dict("sys.modules", {"baldur.services.event_bus": None}):
            # 예외가 발생하지 않아야 함 (Fail-Open)
            throttle = AdaptiveThrottle(ThrottleConfig())
            assert throttle._shedding_suggested_limit == throttle.config.max_limit


class TestCheckWithServiceNameSheddingFallback:
    """check()에서 self._service_name fallback 기반 Load Shedding 매칭 검증.

    ThrottleRegistry 경로에서 context 없이 self._service_name으로
    affected_services 매칭 여부를 확인한다.
    """

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_service_name_in_affected_applies_shedding_limit(self):
        """self._service_name이 affected_services에 포함되면 shedding limit 적용."""
        from baldur.metrics.registry import sanitize_label_value

        config = ThrottleConfig(
            initial_limit=100,
            max_limit=500,
            window_seconds=60,
            service_name="order-api",
        )
        throttle = AdaptiveThrottle(config)

        # affected_services에 sanitized name 사용 (self._service_name과 일치)
        sanitized_name = sanitize_label_value("order-api")
        throttle._shedding_affected_services = {sanitized_name}
        throttle._shedding_suggested_limit = 5

        # context 미전달 → self._service_name fallback 사용
        results = [throttle.check("same_key") for _ in range(10)]
        allowed = sum(1 for r in results if r.allowed)
        assert allowed == 5

    def test_service_name_not_in_affected_uses_full_limit(self):
        """self._service_name이 affected_services에 없으면 기본 limit 적용."""
        from baldur.metrics.registry import sanitize_label_value

        config = ThrottleConfig(
            initial_limit=100,
            max_limit=500,
            window_seconds=60,
            service_name="payment-api",
        )
        throttle = AdaptiveThrottle(config)

        sanitized_other = sanitize_label_value("order-api")
        throttle._shedding_affected_services = {sanitized_other}
        throttle._shedding_suggested_limit = 5

        # payment-api는 affected가 아님
        results = [throttle.check(f"key_{i}") for i in range(10)]
        allowed = sum(1 for r in results if r.allowed)
        assert allowed == 10

    def test_context_service_id_takes_precedence_over_service_name(self):
        """context의 service_id가 self._service_name보다 우선."""
        config = ThrottleConfig(
            initial_limit=100,
            max_limit=500,
            window_seconds=60,
            service_name="payment-api",
        )
        throttle = AdaptiveThrottle(config)

        # self._service_name = "payment_api" (sanitized) → affected 아님
        # context service_id = "order-api" → affected
        throttle._shedding_affected_services = {"order-api"}
        throttle._shedding_suggested_limit = 5

        context = {"service_id": "order-api"}
        results = [throttle.check("same_key", context=context) for _ in range(10)]
        allowed = sum(1 for r in results if r.allowed)
        assert allowed == 5

    def test_service_name_fallback_restores_original_limit(self):
        """self._service_name fallback 후 _current_limit 복구."""
        from baldur.metrics.registry import sanitize_label_value

        config = ThrottleConfig(
            initial_limit=100,
            max_limit=500,
            window_seconds=60,
            service_name="order-api",
        )
        throttle = AdaptiveThrottle(config)
        original_limit = throttle._current_limit

        sanitized_name = sanitize_label_value("order-api")
        throttle._shedding_affected_services = {sanitized_name}
        throttle._shedding_suggested_limit = 5

        throttle.check("key_1")
        assert throttle._current_limit == original_limit


class TestCriticalTierProtectionDuringShedding:
    """critical tier 요청이 Shedding 활성 시에도 보호되는지 검증.

    Load Shedding은 critical 서비스를 affected_services에 포함하지 않으므로
    (manager.py evaluate_shedding: critical → 100%), critical 서비스의
    요청은 shedding limit의 영향을 받지 않아야 한다.
    """

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_critical_service_not_in_affected_gets_full_limit(self):
        """critical 서비스(affected 미포함)는 shedding limit 미적용."""
        config = ThrottleConfig(initial_limit=100, max_limit=500, window_seconds=60)
        throttle = AdaptiveThrottle(config)

        # low/medium만 affected, critical 서비스는 제외
        throttle._shedding_affected_services = {"review-api", "search-api"}
        throttle._shedding_suggested_limit = 5

        # critical 서비스(payment-api)는 affected가 아님
        context = {"service_id": "payment-api"}
        results = [
            throttle.check(f"key_{i}", tier_id="critical", context=context)
            for i in range(10)
        ]
        allowed = sum(1 for r in results if r.allowed)
        assert allowed == 10

    def test_critical_tier_with_429_takes_precedence_over_shedding(self):
        """429 감소 + Shedding 동시 활성 시 critical tier는 429 보호 경로 진입."""
        from baldur_pro.services.throttle.adaptive import PROTECTED_TIERS_ON_429

        config = ThrottleConfig(initial_limit=100, max_limit=500, window_seconds=60)
        throttle = AdaptiveThrottle(config)

        # 429 감소 활성화 + Shedding 활성화
        throttle._429_reduction_active = True
        throttle._limit_before_429 = 100  # 429 감소 전 limit
        throttle._shedding_affected_services = {"order-api"}
        throttle._shedding_suggested_limit = 5

        # critical tier → 429 보호 경로 (shedding 분기 미진입)
        assert "critical" in PROTECTED_TIERS_ON_429

        context = {"service_id": "order-api"}
        results = [
            throttle.check(f"key_{i}", tier_id="critical", context=context)
            for i in range(10)
        ]
        allowed = sum(1 for r in results if r.allowed)
        # 429 보호로 pre-429 limit (100) 적용 → 10개 모두 허용
        assert allowed == 10

    def test_non_critical_affected_service_gets_shedding_limit(self):
        """non-critical 서비스(affected 포함)는 shedding limit 적용."""
        config = ThrottleConfig(initial_limit=100, max_limit=500, window_seconds=60)
        throttle = AdaptiveThrottle(config)

        throttle._shedding_affected_services = {"review-api"}
        throttle._shedding_suggested_limit = 5

        # non-critical 서비스: shedding limit 적용
        context = {"service_id": "review-api"}
        results = [
            throttle.check("same_key", tier_id="standard", context=context)
            for _ in range(10)
        ]
        allowed = sum(1 for r in results if r.allowed)
        assert allowed == 5


class TestEmergencyAndSheddingMinWinner:
    """Emergency Mode + Shedding 동시 활성 시 Conservative Limit 검증.

    conservative_limit은 min(rtt, 429, error_budget, shedding)으로 계산되며,
    Emergency 상태와 Shedding 상태가 동시에 활성화될 때도 Min-Winner 정상 동작해야 한다.
    """

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_shedding_wins_when_lowest(self):
        """Shedding limit이 가장 낮을 때 conservative_limit 반환."""
        config = ThrottleConfig(initial_limit=100, max_limit=500)
        throttle = AdaptiveThrottle(config)
        throttle._conservative_enabled = True

        # Emergency 상태 시뮬레이션
        throttle._emergency_mode_active = True
        throttle._emergency_level = 1

        throttle._rtt_suggested_limit = 200
        throttle._429_suggested_limit = 500
        throttle._shedding_suggested_limit = 50  # 가장 낮음

        assert throttle.conservative_limit == 50

    def test_emergency_rtt_wins_when_lowest(self):
        """Emergency 시 RTT limit이 가장 낮으면 RTT가 conservative_limit."""
        config = ThrottleConfig(initial_limit=100, max_limit=500)
        throttle = AdaptiveThrottle(config)
        throttle._conservative_enabled = True

        throttle._emergency_mode_active = True
        throttle._emergency_level = 2

        throttle._rtt_suggested_limit = 30  # 가장 낮음
        throttle._429_suggested_limit = 500
        throttle._shedding_suggested_limit = 100

        assert throttle.conservative_limit == 30

    def test_error_budget_wins_with_shedding_active(self):
        """Error Budget limit이 Shedding limit보다 낮으면 Error Budget 우선."""
        config = ThrottleConfig(initial_limit=100, max_limit=500)
        throttle = AdaptiveThrottle(config)
        throttle._conservative_enabled = True

        # Error Budget 감소 활성화
        throttle._error_budget_limit_reduction_active = True
        throttle._error_budget_multiplier = 0.3
        throttle._limit_before_error_budget_reduction = 100

        throttle._rtt_suggested_limit = 200
        throttle._429_suggested_limit = 500
        throttle._shedding_suggested_limit = 80

        # error_budget_limit = int(100 * 0.3) = 30 < shedding 80
        error_budget_limit = int(
            throttle._limit_before_error_budget_reduction
            * throttle._error_budget_multiplier
        )
        assert throttle.conservative_limit == error_budget_limit

    def test_all_factors_active_returns_minimum(self):
        """RTT + 429 + Error Budget + Shedding 모두 활성 시 최소값 반환."""
        config = ThrottleConfig(initial_limit=100, max_limit=500)
        throttle = AdaptiveThrottle(config)
        throttle._conservative_enabled = True

        # Emergency 상태
        throttle._emergency_mode_active = True
        throttle._emergency_level = 1

        # 429 감소
        throttle._429_reduction_active = True
        throttle._429_suggested_limit = 150

        # Error Budget 감소
        throttle._error_budget_limit_reduction_active = True
        throttle._error_budget_multiplier = 0.8
        throttle._limit_before_error_budget_reduction = 200

        # Shedding
        throttle._shedding_suggested_limit = 120

        # RTT
        throttle._rtt_suggested_limit = 180

        # min(180, 150, int(200*0.8)=160, 120) = 120 → Shedding wins
        assert throttle.conservative_limit == 120

    def test_shedding_deactivation_removes_from_min_winner(self):
        """Shedding 해제 후 shedding_suggested_limit = max_limit으로 Min-Winner 불참."""
        config = ThrottleConfig(initial_limit=100, max_limit=500)
        throttle = AdaptiveThrottle(config)
        throttle._conservative_enabled = True

        throttle._emergency_mode_active = True
        throttle._rtt_suggested_limit = 200
        throttle._429_suggested_limit = 500
        throttle._shedding_suggested_limit = 50

        assert throttle.conservative_limit == 50

        # Shedding 해제 시뮬레이션
        event = BaldurEvent(
            event_type=EventType.LOAD_SHEDDING_LEVEL_CHANGED,
            data={"new_level": -1, "traffic_limit": 100.0, "affected_services": []},
            source="load_shedding_manager",
            priority=EventPriority.HIGH,
        )
        throttle._handle_shedding_changed(event)

        # shedding_suggested_limit = max_limit (500) → min(200, 500, 500, 500) = 200
        assert throttle._shedding_suggested_limit == config.max_limit
        assert throttle.conservative_limit == throttle._rtt_suggested_limit
