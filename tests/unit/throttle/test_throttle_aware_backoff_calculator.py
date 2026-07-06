"""
ThrottleAwareBackoffCalculator 단위 테스트.

AdaptiveThrottle 상태에 따른 Backoff 배율 적용을 테스트합니다.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.services.backoff_calculator import (
    SYSTEM_TIMEOUT_SECONDS,
    BackoffConfig,
    GlobalThrottleState,
    GlobalThrottleStateManager,
    PushBasedThrottleStateCache,
    ThrottleAwareBackoffCalculator,
    ThrottleState,
)


class TestThrottleState:
    """ThrottleState dataclass 테스트."""

    def test_default_values(self):
        """기본값 확인."""
        state = ThrottleState(current_limit=100, initial_limit=100)

        assert state.current_limit == 100
        assert state.initial_limit == 100
        assert state.emergency_level == 0
        assert state.full_stop_active is False
        assert state.sla_warning_active is False
        assert state.sla_critical_active is False
        assert state.recovery_dampening_active is False
        assert state.error_budget_reduction_active is False

    def test_full_stop_state(self):
        """Full Stop 상태."""
        state = ThrottleState(
            current_limit=0,
            initial_limit=100,
            emergency_level=3,
            full_stop_active=True,
        )

        assert state.full_stop_active is True
        assert state.emergency_level == 3


class TestPushBasedThrottleStateCache:
    """PushBasedThrottleStateCache 테스트."""

    def test_default_values(self):
        """기본값 확인."""
        cache = PushBasedThrottleStateCache()

        assert cache.multiplier == 1.0
        assert cache.reason == "normal"
        assert cache.full_stop_active is False
        assert cache.emergency_level == 0
        assert cache.max_cache_age_seconds == 30.0

    def test_is_stale_when_new(self):
        """새로 생성된 캐시는 stale."""
        cache = PushBasedThrottleStateCache()

        # last_updated가 0이므로 stale
        assert cache.is_stale() is True

    def test_is_stale_after_update(self):
        """업데이트된 캐시는 stale 아님."""
        import time

        cache = PushBasedThrottleStateCache()
        cache.last_updated = time.time()

        assert cache.is_stale() is False


class TestGlobalThrottleState:
    """GlobalThrottleState 테스트."""

    def test_to_dict(self):
        """딕셔너리 변환."""
        state = GlobalThrottleState(
            cluster_avg_rtt_ms=150.0,
            cluster_emergency_level=2,
            cluster_sla_warning_count=3,
            cluster_sla_critical_count=1,
            reporting_pod_count=5,
            last_updated=1234567890.0,
        )

        d = state.to_dict()

        assert d["cluster_avg_rtt_ms"] == 150.0
        assert d["cluster_emergency_level"] == 2
        assert d["cluster_sla_warning_count"] == 3
        assert d["cluster_sla_critical_count"] == 1
        assert d["reporting_pod_count"] == 5
        assert d["last_updated"] == 1234567890.0

    def test_from_dict(self):
        """딕셔너리에서 생성."""
        d = {
            "cluster_avg_rtt_ms": 150.0,
            "cluster_emergency_level": 2,
            "cluster_sla_warning_count": 3,
            "cluster_sla_critical_count": 1,
            "reporting_pod_count": 5,
            "last_updated": 1234567890.0,
        }

        state = GlobalThrottleState.from_dict(d)

        assert state.cluster_avg_rtt_ms == 150.0
        assert state.cluster_emergency_level == 2
        assert state.cluster_sla_warning_count == 3


class TestThrottleAwareBackoffCalculator:
    """ThrottleAwareBackoffCalculator 테스트."""

    def _create_mock_throttle(
        self,
        current_limit: int = 100,
        initial_limit: int = 100,
        sla_warnings: int = 0,
        sla_criticals: int = 0,
        emergency_level: int = 0,
        full_stop_active: bool = False,
        error_budget_reduction_active: bool = False,
    ) -> MagicMock:
        """테스트용 Mock Throttle 생성."""
        mock_throttle = MagicMock()
        mock_throttle.get_stats.return_value = {
            "current_limit": current_limit,
            "adaptive": {"sla_warnings": sla_warnings, "sla_criticals": sla_criticals},
            "emergency": {
                "level": emergency_level,
                "full_stop_active": full_stop_active,
            },
            "recovery": {"dampening_active": False},
        }
        mock_throttle.config.initial_limit = initial_limit
        # 중요: Error Budget 속성을 명시적으로 설정
        mock_throttle._error_budget_limit_reduction_active = (
            error_budget_reduction_active
        )
        return mock_throttle

    def test_normal_state_no_multiplier(self):
        """정상 상태 시 배율 1.0."""
        mock_throttle = self._create_mock_throttle(
            current_limit=100,
            sla_warnings=0,
            sla_criticals=0,
            emergency_level=0,
        )

        calculator = ThrottleAwareBackoffCalculator(
            config=BackoffConfig(base=4, jitter_percent=0),
            throttle_getter=lambda: mock_throttle,
            enable_push_cache=False,
            error_budget_check_enabled=False,
        )

        delay, multiplier, reason = calculator.calculate_with_throttle_context(1)

        assert multiplier == 1.0
        assert reason == "normal"
        assert delay == 4  # base^1

    def test_sla_warning_multiplier(self):
        """SLA Warning 시 1.5배 증가."""
        mock_throttle = self._create_mock_throttle(
            current_limit=80,
            sla_warnings=1,
            sla_criticals=0,
            emergency_level=0,
        )

        calculator = ThrottleAwareBackoffCalculator(
            config=BackoffConfig(base=4, jitter_percent=0),
            throttle_getter=lambda: mock_throttle,
            enable_push_cache=False,
            error_budget_check_enabled=False,
        )

        delay, multiplier, reason = calculator.calculate_with_throttle_context(1)

        assert multiplier == 1.5
        assert reason == "sla_warning"
        assert delay == 6  # 4 * 1.5

    def test_sla_critical_multiplier(self):
        """SLA Critical 시 2배 증가."""
        mock_throttle = self._create_mock_throttle(
            current_limit=60,
            sla_warnings=2,
            sla_criticals=1,
            emergency_level=0,
        )

        calculator = ThrottleAwareBackoffCalculator(
            config=BackoffConfig(base=4, jitter_percent=0),
            throttle_getter=lambda: mock_throttle,
            enable_push_cache=False,
            error_budget_check_enabled=False,
        )

        delay, multiplier, reason = calculator.calculate_with_throttle_context(1)

        assert multiplier == 2.0
        assert reason == "sla_critical"
        assert delay == 8  # 4 * 2

    def test_emergency_level_1_2_multiplier(self):
        """Emergency LEVEL_1~2 시 2.5배 증가."""
        mock_throttle = self._create_mock_throttle(
            current_limit=30,
            sla_warnings=5,
            sla_criticals=3,
            emergency_level=2,
        )

        calculator = ThrottleAwareBackoffCalculator(
            config=BackoffConfig(base=4, jitter_percent=0),
            throttle_getter=lambda: mock_throttle,
            enable_push_cache=False,
            error_budget_check_enabled=False,
        )

        delay, multiplier, reason = calculator.calculate_with_throttle_context(1)

        assert multiplier == 2.5
        assert reason == "emergency_level_2"
        assert delay == 10  # 4 * 2.5

    def test_emergency_level_3_quadruples_delay(self):
        """Emergency LEVEL_3 시 4배 증가."""
        mock_throttle = self._create_mock_throttle(
            current_limit=10,
            sla_warnings=5,
            sla_criticals=3,
            emergency_level=3,
        )

        calculator = ThrottleAwareBackoffCalculator(
            config=BackoffConfig(base=4, jitter_percent=0),
            throttle_getter=lambda: mock_throttle,
            enable_push_cache=False,
            error_budget_check_enabled=False,
        )

        delay, multiplier, reason = calculator.calculate_with_throttle_context(1)

        assert multiplier == 4.0
        assert reason == "emergency_level_3"
        assert delay == 16  # 4 * 4

    def test_full_stop_returns_negative_delay(self):
        """Full Stop 시 -1 반환 (즉시 중단 신호)."""
        mock_throttle = self._create_mock_throttle(
            current_limit=0,
            emergency_level=3,
            full_stop_active=True,
        )

        calculator = ThrottleAwareBackoffCalculator(
            config=BackoffConfig(base=4, jitter_percent=0),
            throttle_getter=lambda: mock_throttle,
            enable_push_cache=False,
            error_budget_check_enabled=False,
        )

        delay, multiplier, reason = calculator.calculate_with_throttle_context(1)

        assert delay == -1
        assert multiplier == float("inf")
        assert reason == "full_stop_active"

    def test_throttle_unavailable_uses_default(self, monkeypatch):
        """Throttle을 가져올 수 없으면 기본 배율 사용."""
        # Patch Emergency Manager to avoid fallback query returning non-zero level
        monkeypatch.setattr(
            "baldur.services.backoff_calculator.calculator.ThrottleAwareBackoffCalculator._get_throttle_state",
            lambda self: None,
        )
        calculator = ThrottleAwareBackoffCalculator(
            config=BackoffConfig(base=4, jitter_percent=0),
            throttle_getter=lambda: None,
            enable_push_cache=False,
            error_budget_check_enabled=False,
        )

        delay, multiplier, reason = calculator.calculate_with_throttle_context(1)

        assert multiplier == 1.0
        assert reason == "throttle_unavailable"
        assert delay == 4  # base^1

    def test_system_timeout_cap(self):
        """SYSTEM_TIMEOUT_SECONDS를 초과하면 cap 적용."""
        mock_throttle = self._create_mock_throttle(
            current_limit=10,
            emergency_level=3,
        )

        # 매우 큰 max_delay로 설정
        calculator = ThrottleAwareBackoffCalculator(
            config=BackoffConfig(base=1000, max_delay=10000, jitter_percent=0),
            throttle_getter=lambda: mock_throttle,
            enable_push_cache=False,
            error_budget_check_enabled=False,
        )

        delay, multiplier, reason = calculator.calculate_with_throttle_context(1)

        # 1000 * 4 = 4000 > SYSTEM_TIMEOUT_SECONDS (1800)
        assert delay == SYSTEM_TIMEOUT_SECONDS

    def test_backoff_multipliers_constant(self):
        """BACKOFF_MULTIPLIERS 상수 확인."""
        expected = {
            "normal": 1.0,
            "sla_warning": 1.5,
            "sla_critical": 2.0,
            "emergency_1_2": 2.5,
            "emergency_3": 4.0,
            "error_budget_critical": 3.0,
        }

        assert ThrottleAwareBackoffCalculator.BACKOFF_MULTIPLIERS == expected

    def test_error_budget_critical_applies_multiplier(self):
        """Error Budget Critical 상태 시 3.0배 배율 적용."""
        mock_throttle = self._create_mock_throttle(
            current_limit=100,
            sla_warnings=0,
            sla_criticals=0,
            emergency_level=0,
        )

        calculator = ThrottleAwareBackoffCalculator(
            config=BackoffConfig(base=4, jitter_percent=0),
            throttle_getter=lambda: mock_throttle,
            enable_push_cache=False,
            error_budget_check_enabled=True,  # Error Budget 체크 활성화
        )

        # Error Budget Critical/Warning 상태 시뮬레이션
        with patch.object(
            calculator, "_check_error_budget_critical_or_warning", return_value=True
        ):
            delay, multiplier, reason = calculator.calculate_with_throttle_context(1)

        assert multiplier == 3.0
        assert delay == 12  # 4 * 3

    def test_error_budget_reduction_active_applies_multiplier(self):
        """Error Budget Reduction Active 시 3.0배 배율 적용."""
        mock_throttle = self._create_mock_throttle(
            current_limit=100,
            sla_warnings=0,
            sla_criticals=0,
            emergency_level=0,
            error_budget_reduction_active=True,  # Error Budget Reduction Flag
        )

        calculator = ThrottleAwareBackoffCalculator(
            config=BackoffConfig(base=4, jitter_percent=0),
            throttle_getter=lambda: mock_throttle,
            enable_push_cache=False,
            error_budget_check_enabled=False,
        )

        delay, multiplier, reason = calculator.calculate_with_throttle_context(1)

        assert multiplier == 3.0
        assert delay == 12  # 4 * 3


class TestGlobalThrottleStateManager:
    """GlobalThrottleStateManager 테스트."""

    def test_no_redis_returns_none(self):
        """Redis 없으면 None 반환."""
        manager = GlobalThrottleStateManager(redis_client=None)

        result = manager.get_global_state()

        assert result is None

    def test_report_local_state_without_redis(self):
        """Redis 없이 report해도 에러 없음."""
        manager = GlobalThrottleStateManager(redis_client=None)
        state = ThrottleState(current_limit=50, initial_limit=100, emergency_level=1)

        # 에러 없이 완료
        manager.report_local_state(state, pod_id="test-pod-1")

    def test_get_global_state_with_mock_redis(self):
        """Redis 데이터로 글로벌 상태 계산."""
        mock_redis = MagicMock()
        mock_redis.keys.return_value = [
            "baldur:throttle:global_state:pod:pod-1",
            "baldur:throttle:global_state:pod:pod-2",
        ]
        mock_redis.get.side_effect = [
            '{"emergency_level": 1, "sla_warning": true, "sla_critical": false}',
            '{"emergency_level": 2, "sla_warning": true, "sla_critical": true}',
        ]

        manager = GlobalThrottleStateManager(redis_client=mock_redis)

        result = manager.get_global_state()

        assert result is not None
        assert result.reporting_pod_count == 2
        assert result.cluster_sla_warning_count == 2
        assert result.cluster_sla_critical_count == 1
