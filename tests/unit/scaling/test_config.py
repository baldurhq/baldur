"""
Unit tests for BackpressureSettings and related config.

테스트 항목:
- BackpressureLevel enum 값
- BackpressureStrategy enum 값
- BackpressureSettings 기본값
- get_level_for_queue_size 메서드
- get_rate_multiplier 메서드
- LEVEL_RATE_MULTIPLIERS 상수
"""

import pytest

from baldur.scaling.config import (
    LEVEL_RATE_MULTIPLIERS,
    BackpressureLevel,
    BackpressureSettings,
    BackpressureStrategy,
    get_backpressure_settings,
    reset_backpressure_settings,
)


class TestBackpressureLevel:
    """BackpressureLevel enum 테스트."""

    def test_level_values(self):
        """레벨 값 확인."""
        assert BackpressureLevel.NONE.value == "none"
        assert BackpressureLevel.LOW.value == "low"
        assert BackpressureLevel.MEDIUM.value == "medium"
        assert BackpressureLevel.HIGH.value == "high"
        assert BackpressureLevel.CRITICAL.value == "critical"

    def test_all_levels_defined(self):
        """모든 레벨이 정의되어 있는지 확인."""
        levels = list(BackpressureLevel)
        assert len(levels) == 5


class TestBackpressureStrategy:
    """BackpressureStrategy enum 테스트."""

    def test_strategy_values(self):
        """전략 값 확인."""
        assert BackpressureStrategy.DROP_OLDEST.value == "drop_oldest"
        assert BackpressureStrategy.DROP_NEWEST.value == "drop_newest"
        assert BackpressureStrategy.REJECT.value == "reject"
        assert BackpressureStrategy.THROTTLE.value == "throttle"
        assert BackpressureStrategy.QUEUE.value == "queue"

    def test_all_strategies_defined(self):
        """모든 전략이 정의되어 있는지 확인."""
        strategies = list(BackpressureStrategy)
        assert len(strategies) == 5


class TestLevelRateMultipliers:
    """LEVEL_RATE_MULTIPLIERS 상수 테스트."""

    def test_all_levels_have_multipliers(self):
        """모든 레벨에 배율이 정의되어 있는지 확인."""
        for level in BackpressureLevel:
            assert level in LEVEL_RATE_MULTIPLIERS

    def test_multiplier_values(self):
        """배율 값 확인."""
        assert LEVEL_RATE_MULTIPLIERS[BackpressureLevel.NONE] == 1.0
        assert LEVEL_RATE_MULTIPLIERS[BackpressureLevel.LOW] == 1.0
        assert LEVEL_RATE_MULTIPLIERS[BackpressureLevel.MEDIUM] == 0.9
        assert LEVEL_RATE_MULTIPLIERS[BackpressureLevel.HIGH] == 0.8
        assert LEVEL_RATE_MULTIPLIERS[BackpressureLevel.CRITICAL] == 0.5

    def test_multipliers_are_decreasing(self):
        """레벨이 올라갈수록 배율이 감소하는지 확인."""
        assert (
            LEVEL_RATE_MULTIPLIERS[BackpressureLevel.NONE]
            >= LEVEL_RATE_MULTIPLIERS[BackpressureLevel.LOW]
        )
        assert (
            LEVEL_RATE_MULTIPLIERS[BackpressureLevel.LOW]
            >= LEVEL_RATE_MULTIPLIERS[BackpressureLevel.MEDIUM]
        )
        assert (
            LEVEL_RATE_MULTIPLIERS[BackpressureLevel.MEDIUM]
            >= LEVEL_RATE_MULTIPLIERS[BackpressureLevel.HIGH]
        )
        assert (
            LEVEL_RATE_MULTIPLIERS[BackpressureLevel.HIGH]
            >= LEVEL_RATE_MULTIPLIERS[BackpressureLevel.CRITICAL]
        )


class TestBackpressureSettings:
    """BackpressureSettings 테스트."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """각 테스트 전후로 설정 캐시 리셋."""
        reset_backpressure_settings()
        yield
        reset_backpressure_settings()

    def test_default_values(self):
        """기본값 확인 (v1.1 deferred per impl 527 — all enabled sub-flags False)."""
        settings = BackpressureSettings()

        assert settings.backpressure_enabled is False
        assert settings.default_strategy == BackpressureStrategy.THROTTLE
        assert settings.queue_low_threshold == 100
        assert settings.queue_medium_threshold == 500
        assert settings.queue_high_threshold == 1000
        assert settings.queue_critical_threshold == 5000
        assert settings.max_rate_per_second == 1000.0
        assert settings.min_rate_per_second == 10.0
        assert settings.rate_increase_factor == 1.1
        assert settings.rate_adjust_interval_seconds == 5.0
        assert settings.queue_size_cache_ttl_seconds == 2.0
        assert settings.metrics_enabled is False
        assert settings.metrics_prefix == "baldur_"
        assert settings.hpa_enabled is False
        assert settings.hpa_target_queue_depth == 100
        assert settings.graceful_degradation_enabled is False
        assert settings.reject_retry_after_seconds == 5

    def test_get_level_for_queue_size_none(self):
        """큐 크기가 낮을 때 NONE 레벨 반환."""
        settings = BackpressureSettings()
        assert settings.get_level_for_queue_size(0) == BackpressureLevel.NONE
        assert settings.get_level_for_queue_size(50) == BackpressureLevel.NONE
        assert settings.get_level_for_queue_size(99) == BackpressureLevel.NONE

    def test_get_level_for_queue_size_low(self):
        """큐 크기가 LOW 임계치 이상일 때."""
        settings = BackpressureSettings()
        assert settings.get_level_for_queue_size(100) == BackpressureLevel.LOW
        assert settings.get_level_for_queue_size(200) == BackpressureLevel.LOW
        assert settings.get_level_for_queue_size(499) == BackpressureLevel.LOW

    def test_get_level_for_queue_size_medium(self):
        """큐 크기가 MEDIUM 임계치 이상일 때."""
        settings = BackpressureSettings()
        assert settings.get_level_for_queue_size(500) == BackpressureLevel.MEDIUM
        assert settings.get_level_for_queue_size(700) == BackpressureLevel.MEDIUM
        assert settings.get_level_for_queue_size(999) == BackpressureLevel.MEDIUM

    def test_get_level_for_queue_size_high(self):
        """큐 크기가 HIGH 임계치 이상일 때."""
        settings = BackpressureSettings()
        assert settings.get_level_for_queue_size(1000) == BackpressureLevel.HIGH
        assert settings.get_level_for_queue_size(2000) == BackpressureLevel.HIGH
        assert settings.get_level_for_queue_size(4999) == BackpressureLevel.HIGH

    def test_get_level_for_queue_size_critical(self):
        """큐 크기가 CRITICAL 임계치 이상일 때."""
        settings = BackpressureSettings()
        assert settings.get_level_for_queue_size(5000) == BackpressureLevel.CRITICAL
        assert settings.get_level_for_queue_size(10000) == BackpressureLevel.CRITICAL

    def test_get_rate_multiplier(self):
        """레벨별 배율 반환 테스트."""
        settings = BackpressureSettings()

        assert settings.get_rate_multiplier(BackpressureLevel.NONE) == 1.0
        assert settings.get_rate_multiplier(BackpressureLevel.LOW) == 1.0
        assert settings.get_rate_multiplier(BackpressureLevel.MEDIUM) == 0.9
        assert settings.get_rate_multiplier(BackpressureLevel.HIGH) == 0.8
        assert settings.get_rate_multiplier(BackpressureLevel.CRITICAL) == 0.5


class TestGetBackpressureSettings:
    """get_backpressure_settings 싱글톤 테스트."""

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """각 테스트 전후로 설정 캐시 리셋."""
        reset_backpressure_settings()
        yield
        reset_backpressure_settings()

    def test_singleton_returns_same_instance(self):
        """싱글톤이 동일한 인스턴스를 반환하는지 확인."""
        settings1 = get_backpressure_settings()
        settings2 = get_backpressure_settings()
        assert settings1 is settings2

    def test_reset_clears_cache(self):
        """리셋 후 새 인스턴스가 생성되는지 확인."""
        settings1 = get_backpressure_settings()
        reset_backpressure_settings()
        settings2 = get_backpressure_settings()
        # lru_cache 리셋 후 새 인스턴스 생성
        assert settings1 is not settings2
