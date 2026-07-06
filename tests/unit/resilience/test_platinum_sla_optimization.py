# packages/baldur-python/tests/unit/test_platinum_sla_optimization.py
"""
Platinum SLA 최적화 모듈 단위 테스트

Tests for:
- state_cache.py (CBStateCache)
- async_logger.py (AsyncHealingLogger)
- defaults.py (DegradedModeHandler)
- adaptive_jitter.py (AdaptiveJitter)
- health_checker.py (PortableHealthChecker)
"""

import os
import threading
import time
from unittest.mock import Mock

from baldur.adapters.health_checker import (
    PortableHealthChecker,
    SimpleSocketStrategy,
    TTLCacheStrategy,
)
from baldur.core.adaptive_jitter import AdaptiveJitter
from baldur.core.degraded_mode_handler import DegradedModeHandler
from baldur.core.state_cache import CBStateCache
from baldur.utils.async_logger import AsyncHealingLogger, EventSeverity


# =============================================================================
# CBStateCache Tests
# =============================================================================
class TestCBStateCache:
    """CBStateCache 단위 테스트"""

    def setup_method(self):
        """각 테스트 전 상태 초기화"""
        CBStateCache.reset()

    def teardown_method(self):
        """각 테스트 후 상태 초기화"""
        CBStateCache.reset()

    def test_configure_sets_callback(self):
        """configure()가 콜백을 올바르게 설정"""
        mock_callback = Mock(return_value={"state": "closed"})
        CBStateCache.configure(fetch_callback=mock_callback)

        assert CBStateCache._fetch_callback == mock_callback

    def test_get_state_returns_none_without_callback(self):
        """콜백 없이 get_state() 호출 시 None 반환"""
        result = CBStateCache.get_state("payment")
        assert result is None

    def test_get_state_calls_callback_on_cache_miss(self):
        """캐시 미스 시 콜백 호출"""
        mock_callback = Mock(return_value={"state": "closed"})
        CBStateCache.configure(fetch_callback=mock_callback)

        result = CBStateCache.get_state("payment")

        mock_callback.assert_called_once_with("payment")
        assert result == {"state": "closed"}

    def test_get_state_returns_cached_value(self):
        """캐시 히트 시 콜백 호출 안 함"""
        mock_callback = Mock(return_value={"state": "closed"})
        CBStateCache.configure(fetch_callback=mock_callback)

        # 첫 번째 호출 - 캐시 미스
        CBStateCache.get_state("payment")
        # 두 번째 호출 - 캐시 히트
        result = CBStateCache.get_state("payment")

        assert mock_callback.call_count == 1
        assert result == {"state": "closed"}

    def test_set_state_directly(self):
        """set_state()로 직접 캐시 설정"""
        CBStateCache.set_state("payment", {"state": "open"})

        result = CBStateCache.get_state("payment")
        assert result == {"state": "open"}

    def test_invalidate_removes_cache_entry(self):
        """invalidate()가 특정 캐시 항목 제거"""
        CBStateCache.set_state("payment", {"state": "closed"})
        CBStateCache.set_state("order", {"state": "closed"})

        CBStateCache.invalidate("payment")

        stats = CBStateCache.get_cache_stats()
        assert stats["total_entries"] == 1

    def test_invalidate_all_clears_cache(self):
        """invalidate_all()이 전체 캐시 제거"""
        CBStateCache.set_state("payment", {"state": "closed"})
        CBStateCache.set_state("order", {"state": "closed"})

        CBStateCache.invalidate_all()

        stats = CBStateCache.get_cache_stats()
        assert stats["total_entries"] == 0

    def test_get_cache_stats(self):
        """get_cache_stats()가 올바른 통계 반환"""
        CBStateCache.set_state("payment", {"state": "closed"})
        CBStateCache.set_state("order", {"state": "closed"})

        stats = CBStateCache.get_cache_stats()

        assert stats["total_entries"] == 2
        assert stats["active_entries"] == 2
        assert stats["expired_entries"] == 0

    def test_ttl_jitter_is_applied(self):
        """TTL에 jitter가 적용됨"""
        from baldur.settings.state_cache import get_state_cache_settings

        settings = get_state_cache_settings()

        ttls = set()
        for _ in range(10):
            ttl = CBStateCache._calculate_ttl()
            ttls.add(round(ttl, 2))

        # jitter로 인해 다양한 TTL 값이 생성되어야 함
        assert len(ttls) > 1
        # 모든 TTL은 base_ttl ± jitter_range 범위 내
        for ttl in ttls:
            assert (
                settings.base_ttl - settings.jitter_range
                <= ttl
                <= settings.base_ttl + settings.jitter_range
            )

    def test_callback_failure_triggers_degraded_mode(self):
        """콜백 실패 시 DegradedModeHandler로 폴백 및 degraded mode 진입"""
        DegradedModeHandler.reset()

        mock_callback = Mock(side_effect=Exception("Connection failed"))
        CBStateCache.configure(fetch_callback=mock_callback)

        result = CBStateCache.get_state("payment")

        # DegradedModeHandler의 CB 설정 반환
        assert result is not None
        assert "failure_threshold" in result
        assert DegradedModeHandler.is_degraded()

        DegradedModeHandler.reset()


# =============================================================================
# DegradedModeHandler Tests
# =============================================================================
class TestDegradedModeHandler:
    """DegradedModeHandler 단위 테스트"""

    def setup_method(self):
        """각 테스트 전 상태 초기화"""
        DegradedModeHandler.reset()

    def teardown_method(self):
        """각 테스트 후 상태 초기화"""
        DegradedModeHandler.reset()
        # 환경변수 정리
        for key in DegradedModeHandler.get_all_keys():
            env_key = f"BALDUR_{key}"
            if env_key in os.environ:
                del os.environ[env_key]

    def test_get_returns_default_value(self):
        """get()이 기본값 반환"""
        result = DegradedModeHandler.get("CB_FAILURE_THRESHOLD")
        assert result == 3

    def test_get_returns_custom_default(self):
        """존재하지 않는 키에 대해 커스텀 기본값 반환"""
        result = DegradedModeHandler.get("NON_EXISTENT_KEY", "custom_default")
        assert result == "custom_default"

    def test_set_overrides_default(self):
        """set()이 기본값 오버라이드"""
        DegradedModeHandler.set("CB_FAILURE_THRESHOLD", 10)

        result = DegradedModeHandler.get("CB_FAILURE_THRESHOLD")
        assert result == 10

    def test_environment_variable_priority(self):
        """환경변수가 가장 높은 우선순위"""
        os.environ["BALDUR_CB_FAILURE_THRESHOLD"] = "20"
        DegradedModeHandler.set("CB_FAILURE_THRESHOLD", 10)

        result = DegradedModeHandler.get("CB_FAILURE_THRESHOLD")
        assert result == 20

    def test_parse_value_boolean_true(self):
        """불리언 true 파싱"""
        for value in ["true", "True", "TRUE", "1", "yes", "Yes"]:
            assert DegradedModeHandler._parse_value(value) is True

    def test_parse_value_boolean_false(self):
        """불리언 false 파싱"""
        for value in ["false", "False", "FALSE", "0", "no", "No"]:
            assert DegradedModeHandler._parse_value(value) is False

    def test_parse_value_integer(self):
        """정수 파싱"""
        assert DegradedModeHandler._parse_value("42") == 42

    def test_parse_value_float(self):
        """실수 파싱"""
        assert DegradedModeHandler._parse_value("3.14") == 3.14

    def test_parse_value_string(self):
        """문자열 그대로 반환"""
        assert DegradedModeHandler._parse_value("hello") == "hello"

    def test_get_cb_config(self):
        """get_cb_config()가 CB 설정 딕셔너리 반환"""
        config = DegradedModeHandler.get_cb_config()

        assert "failure_threshold" in config
        assert "recovery_timeout" in config
        assert "half_open_max_calls" in config

    def test_get_rate_limit_config(self):
        """get_rate_limit_config()가 Rate Limit 설정 반환"""
        config = DegradedModeHandler.get_rate_limit_config()

        assert "per_minute" in config
        assert "burst" in config

    def test_enter_degraded_mode_logs_once(self):
        """enter_degraded_mode()가 경고 로그를 1회만 출력"""
        DegradedModeHandler.enter_degraded_mode()
        assert DegradedModeHandler.is_degraded()
        assert DegradedModeHandler._degraded_warned

        # 두 번째 호출에도 상태 유지
        DegradedModeHandler.enter_degraded_mode()
        assert DegradedModeHandler.is_degraded()

    def test_exit_degraded_mode(self):
        """exit_degraded_mode()가 상태 해제"""
        DegradedModeHandler.enter_degraded_mode()
        DegradedModeHandler.exit_degraded_mode()

        assert not DegradedModeHandler.is_degraded()
        assert not DegradedModeHandler._degraded_warned

    def test_get_health_response_healthy(self):
        """정상 상태에서 health response"""
        response = DegradedModeHandler.get_health_response()

        assert response["status"] == "healthy"
        assert response["is_degraded"] is False
        assert response["source"] == "command_center"
        assert "config" in response

    def test_get_health_response_degraded(self):
        """Degraded 상태에서 health response"""
        DegradedModeHandler.enter_degraded_mode()

        response = DegradedModeHandler.get_health_response()

        assert response["status"] == "degraded"
        assert response["is_degraded"] is True
        assert response["source"] == "local_defaults"

    def test_thread_safety(self):
        """Thread-Safe 검증"""
        errors = []

        def reader():
            try:
                for _ in range(100):
                    DegradedModeHandler.get("CB_FAILURE_THRESHOLD")
                    DegradedModeHandler.get_cb_config()
            except Exception as e:
                errors.append(e)

        def writer():
            try:
                for i in range(100):
                    DegradedModeHandler.set("CB_FAILURE_THRESHOLD", i)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(5)] + [
            threading.Thread(target=writer) for _ in range(5)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


# =============================================================================
# AdaptiveJitter Tests
# =============================================================================
class TestAdaptiveJitter:
    """AdaptiveJitter 단위 테스트"""

    def test_calculate_returns_float(self):
        """calculate()가 float 반환"""
        result = AdaptiveJitter.calculate()
        assert isinstance(result, float)

    def test_calculate_ms_returns_int(self):
        """calculate_ms()가 int 반환"""
        result = AdaptiveJitter.calculate_ms()
        assert isinstance(result, int)

    def test_normal_range_without_info(self):
        """정보 없을 때 NORMAL 범위"""
        jitter = AdaptiveJitter.calculate()
        min_val, max_val = AdaptiveJitter.JITTER_MIN_NORMAL

        assert min_val <= jitter <= max_val

    def test_relaxed_range_when_safe(self):
        """여유로운 상황에서 RELAXED 범위"""
        jitter_range = AdaptiveJitter.get_jitter_range(
            error_budget_remaining=0.7,
            current_load=0.2,  # 70% 남음 (> 50%)  # 20% 부하 (< 30%)
        )

        assert jitter_range == AdaptiveJitter.JITTER_MIN_RELAXED

    def test_stressed_range_when_budget_danger(self):
        """에러 버짓 위험 시 STRESSED 범위"""
        jitter_range = AdaptiveJitter.get_jitter_range(
            error_budget_remaining=0.1, current_load=0.5
        )  # 10% 남음 (< 20%)

        assert jitter_range == AdaptiveJitter.JITTER_MIN_STRESSED

    def test_stressed_range_when_load_high(self):
        """부하 높을 때 STRESSED 범위"""
        jitter_range = AdaptiveJitter.get_jitter_range(
            error_budget_remaining=0.5, current_load=0.9
        )  # 90% 부하 (> 80%)

        assert jitter_range == AdaptiveJitter.JITTER_MIN_STRESSED

    def test_normal_range_for_mixed_state(self):
        """중간 상태에서 NORMAL 범위"""
        jitter_range = AdaptiveJitter.get_jitter_range(
            error_budget_remaining=0.4,
            current_load=0.5,  # 40% (between 20% and 50%)  # 50% (between 30% and 80%)
        )

        assert jitter_range == AdaptiveJitter.JITTER_MIN_NORMAL

    def test_get_status_relaxed(self):
        """여유 상태 문자열"""
        status = AdaptiveJitter.get_status(error_budget_remaining=0.7, current_load=0.2)

        assert status == "relaxed"

    def test_get_status_stressed(self):
        """위험 상태 문자열"""
        status = AdaptiveJitter.get_status(error_budget_remaining=0.1, current_load=0.5)

        assert status == "stressed"

    def test_get_status_normal(self):
        """보통 상태 문자열"""
        status = AdaptiveJitter.get_status()

        assert status == "normal"

    def test_jitter_values_are_random(self):
        """Jitter 값이 랜덤"""
        values = set()
        for _ in range(20):
            values.add(AdaptiveJitter.calculate())

        # 랜덤이므로 다양한 값이 생성되어야 함
        assert len(values) > 1


# =============================================================================
# AsyncHealingLogger Tests
# =============================================================================
class TestAsyncHealingLogger:
    """AsyncHealingLogger 단위 테스트"""

    def setup_method(self):
        """각 테스트 전 상태 초기화"""
        AsyncHealingLogger.reset()

    def teardown_method(self):
        """각 테스트 후 상태 초기화"""
        AsyncHealingLogger.reset()

    def test_configure_sets_callback(self):
        """configure()가 콜백 설정"""
        mock_callback = Mock()
        AsyncHealingLogger.configure(flush_callback=mock_callback)

        assert AsyncHealingLogger._flush_callback == mock_callback

    def test_log_increments_counter(self):
        """log()가 카운터 증가"""
        AsyncHealingLogger.log({"type": "test"})

        stats = AsyncHealingLogger.get_stats()
        assert stats["events_logged"] == 1

    def test_log_with_severity(self):
        """log()에 severity 적용"""
        AsyncHealingLogger.start()
        AsyncHealingLogger.log({"type": "test"}, EventSeverity.WARNING)
        time.sleep(0.1)

        # 큐에 이벤트가 추가됨
        assert (
            not AsyncHealingLogger._queue.empty()
            or AsyncHealingLogger.get_stats()["events_logged"] == 1
        )

    def test_critical_events_flush_immediately(self):
        """CRITICAL 이벤트는 즉시 플러시"""
        flushed_events = []
        mock_callback = Mock(side_effect=lambda events: flushed_events.extend(events))
        AsyncHealingLogger.configure(flush_callback=mock_callback)

        # CRITICAL 이벤트 (즉시 전송)
        AsyncHealingLogger.log({"type": "cb_open"}, EventSeverity.CRITICAL)

        # 즉시 전송이므로 잠시 대기
        time.sleep(0.2)

        assert len(flushed_events) == 1
        assert flushed_events[0]["severity"] == "CRITICAL"

    def test_start_and_stop(self):
        """start()와 stop() 동작"""
        AsyncHealingLogger.start()
        assert AsyncHealingLogger._running

        AsyncHealingLogger.stop()
        assert not AsyncHealingLogger._running

    def test_batch_flush(self):
        """배치 플러시 동작"""
        flushed_events = []
        mock_callback = Mock(side_effect=lambda events: flushed_events.extend(events))
        AsyncHealingLogger.configure(flush_callback=mock_callback)
        AsyncHealingLogger.start()

        # BATCH_SIZE 이상의 이벤트 추가
        batch_size = AsyncHealingLogger._get_batch_size()
        for i in range(batch_size + 1):
            AsyncHealingLogger.log({"type": "test", "index": i})

        # 배치 처리 대기
        time.sleep(0.5)

        # 배치로 플러시됨
        assert len(flushed_events) >= batch_size

    def test_manual_flush(self):
        """수동 플러시"""
        flushed_events = []
        mock_callback = Mock(side_effect=lambda events: flushed_events.extend(events))
        AsyncHealingLogger.configure(flush_callback=mock_callback)

        # start()를 호출해야 queue가 초기화됨
        AsyncHealingLogger.start()

        # 이벤트 추가
        AsyncHealingLogger.log({"type": "test1"})
        AsyncHealingLogger.log({"type": "test2"})

        # 수동 플러시
        AsyncHealingLogger.flush()

        # 워커가 처리할 시간 대기
        import time

        time.sleep(0.3)

        assert len(flushed_events) >= 1

        AsyncHealingLogger.stop(timeout=1.0)

    def test_get_stats(self):
        """통계 조회"""
        AsyncHealingLogger.log({"type": "test"})

        stats = AsyncHealingLogger.get_stats()

        assert "events_logged" in stats
        assert "events_flushed" in stats
        assert "immediate_flushes" in stats
        assert "batch_flushes" in stats
        assert "flush_errors" in stats

    def test_reset_stats(self):
        """통계 초기화"""
        AsyncHealingLogger.log({"type": "test"})
        AsyncHealingLogger.reset_stats()

        stats = AsyncHealingLogger.get_stats()
        assert stats["events_logged"] == 0

    def test_flush_error_handling(self):
        """플러시 오류 처리"""
        mock_callback = Mock(side_effect=Exception("Network error"))
        AsyncHealingLogger.configure(flush_callback=mock_callback)

        # CRITICAL 이벤트로 즉시 플러시 시도
        AsyncHealingLogger.log({"type": "error"}, EventSeverity.CRITICAL)

        time.sleep(0.2)

        stats = AsyncHealingLogger.get_stats()
        assert stats["flush_errors"] >= 1


# =============================================================================
# PortableHealthChecker Tests
# =============================================================================
class TestPortableHealthChecker:
    """PortableHealthChecker 단위 테스트"""

    def test_default_strategy_selection(self):
        """기본 전략 선택"""
        checker = PortableHealthChecker()

        # TTLCacheStrategy 사용
        assert checker.strategy_name == "TTLCacheStrategy"

    def test_force_ttl_cache_strategy(self):
        """TTL 캐시 전략 강제"""
        checker = PortableHealthChecker(force_strategy="ttl_cache")

        assert checker.strategy_name == "TTLCacheStrategy"

    def test_force_simple_socket_strategy(self):
        """소켓 전략 강제"""
        checker = PortableHealthChecker(force_strategy="simple_socket")

        assert checker.strategy_name == "SimpleSocketStrategy"

    def test_custom_callback(self):
        """커스텀 콜백 사용"""
        mock_callback = Mock(return_value=True)
        checker = PortableHealthChecker(check_callback=mock_callback)

        result = checker.is_healthy("test:8080")

        assert result is True
        mock_callback.assert_called_with("test:8080")

    def test_cache_hit(self):
        """캐시 히트"""
        call_count = 0

        def counting_callback(target):
            nonlocal call_count
            call_count += 1
            return True

        checker = PortableHealthChecker(check_callback=counting_callback, ttl=10)

        # 첫 번째 호출
        checker.is_healthy("test:8080")
        # 두 번째 호출 (캐시 히트)
        checker.is_healthy("test:8080")

        assert call_count == 1

    def test_invalidate(self):
        """캐시 무효화"""
        call_count = 0

        def counting_callback(target):
            nonlocal call_count
            call_count += 1
            return True

        checker = PortableHealthChecker(check_callback=counting_callback, ttl=10)

        checker.is_healthy("test:8080")
        checker.invalidate("test:8080")
        checker.is_healthy("test:8080")

        assert call_count == 2

    def test_invalidate_all(self):
        """전체 캐시 무효화"""
        call_count = 0

        def counting_callback(target):
            nonlocal call_count
            call_count += 1
            return True

        checker = PortableHealthChecker(check_callback=counting_callback, ttl=10)

        checker.is_healthy("test1:8080")
        checker.is_healthy("test2:8080")
        checker.invalidate_all()
        checker.is_healthy("test1:8080")
        checker.is_healthy("test2:8080")

        assert call_count == 4


class TestTTLCacheStrategy:
    """TTLCacheStrategy 단위 테스트"""

    def test_cache_miss_calls_callback(self):
        """캐시 미스 시 콜백 호출"""
        mock_callback = Mock(return_value=True)
        strategy = TTLCacheStrategy(check_callback=mock_callback)

        result = strategy.check("test:8080")

        assert result is True
        mock_callback.assert_called_once_with("test:8080")

    def test_cache_hit_skips_callback(self):
        """캐시 히트 시 콜백 스킵"""
        mock_callback = Mock(return_value=True)
        strategy = TTLCacheStrategy(check_callback=mock_callback, ttl=10)

        strategy.check("test:8080")
        strategy.check("test:8080")

        assert mock_callback.call_count == 1

    def test_ttl_expiry(self):
        """TTL 만료 후 다시 콜백 호출"""
        mock_callback = Mock(return_value=True)
        strategy = TTLCacheStrategy(check_callback=mock_callback, ttl=0.1)

        strategy.check("test:8080")
        time.sleep(0.2)
        strategy.check("test:8080")

        assert mock_callback.call_count == 2

    def test_callback_exception_returns_false(self):
        """콜백 예외 시 False 반환"""
        mock_callback = Mock(side_effect=Exception("Error"))
        strategy = TTLCacheStrategy(check_callback=mock_callback)

        result = strategy.check("test:8080")

        assert result is False

    def test_no_callback_returns_true(self):
        """콜백 없을 때 True 반환"""
        strategy = TTLCacheStrategy()

        result = strategy.check("test:8080")

        assert result is True


class TestSimpleSocketStrategy:
    """SimpleSocketStrategy 단위 테스트"""

    def test_invalid_target_format(self):
        """잘못된 타겟 형식"""
        strategy = SimpleSocketStrategy(timeout=0.1)

        result = strategy.check("invalid-format")

        assert result is False

    def test_connection_failure(self):
        """연결 실패 시 False 반환"""
        strategy = SimpleSocketStrategy(timeout=0.1)

        # 존재하지 않는 호스트
        result = strategy.check("nonexistent-host:12345")

        assert result is False

    def test_get_name(self):
        """전략 이름 반환"""
        strategy = SimpleSocketStrategy()

        assert strategy.get_name() == "SimpleSocketStrategy"


# =============================================================================
# Integration Tests
# =============================================================================
class TestPlatinumSLAIntegration:
    """Platinum SLA 최적화 통합 테스트"""

    def setup_method(self):
        """각 테스트 전 상태 초기화"""
        CBStateCache.reset()
        DegradedModeHandler.reset()
        AsyncHealingLogger.reset()

    def teardown_method(self):
        """각 테스트 후 상태 초기화"""
        CBStateCache.reset()
        DegradedModeHandler.reset()
        AsyncHealingLogger.reset()

    def test_graceful_degradation_flow(self):
        """Graceful Degradation 플로우"""
        # 1. 사령탑 연결 실패 시뮬레이션
        mock_callback = Mock(side_effect=Exception("Connection failed"))
        CBStateCache.configure(fetch_callback=mock_callback)

        # 2. CB 상태 조회 (실패 → DegradedModeHandler 사용)
        state = CBStateCache.get_state("payment")

        # 3. Degraded 모드 진입 확인
        assert DegradedModeHandler.is_degraded()

        # 4. 기본 CB 설정 반환 확인
        assert state is not None
        assert state["failure_threshold"] == 3

    def test_cache_performance(self):
        """캐시 성능 검증"""
        mock_callback = Mock(return_value={"state": "closed"})
        CBStateCache.configure(fetch_callback=mock_callback)

        # 첫 번째 호출 (캐시 미스)
        start = time.time()
        CBStateCache.get_state("payment")
        first_call_time = time.time() - start

        # 두 번째 호출 (캐시 히트)
        start = time.time()
        CBStateCache.get_state("payment")
        second_call_time = time.time() - start

        # 캐시 히트가 더 빨라야 함 (또는 거의 같음 - Mock이므로)
        # 실제로는 네트워크 호출이 없으므로 훨씬 빠름
        assert second_call_time <= first_call_time + 0.001

    def test_async_logging_non_blocking(self):
        """비동기 로깅이 논블로킹"""

        # 느린 콜백 설정
        def slow_callback(events):
            time.sleep(0.5)

        AsyncHealingLogger.configure(flush_callback=slow_callback)
        AsyncHealingLogger.start()

        # 로깅 시간 측정 (논블로킹이어야 함)
        start = time.time()
        for _ in range(10):
            AsyncHealingLogger.log({"type": "test"})
        log_time = time.time() - start

        # 논블로킹이므로 0.1초 미만이어야 함
        assert log_time < 0.1

    def test_adaptive_jitter_reduces_thundering_herd(self):
        """Adaptive Jitter가 Thundering Herd 방지"""
        # 부하가 높은 상황
        high_load_jitters = [
            AdaptiveJitter.calculate(current_load=0.9) for _ in range(100)
        ]

        # 부하가 낮은 상황
        low_load_jitters = [
            AdaptiveJitter.calculate(error_budget_remaining=0.8, current_load=0.1)
            for _ in range(100)
        ]

        # 높은 부하에서 jitter가 더 커야 함
        avg_high = sum(high_load_jitters) / len(high_load_jitters)
        avg_low = sum(low_load_jitters) / len(low_load_jitters)

        assert avg_high > avg_low
