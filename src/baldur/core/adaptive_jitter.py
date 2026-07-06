# packages/baldur-python/src/baldur/core/adaptive_jitter.py
"""
지능형 Jitter 계산기 (Platinum SLA 최적화)

시스템 상태에 따라 Jitter 범위를 동적으로 조절
P99 안정화 및 불필요한 지연 제거

설정값은 JitterSettings를 통해 환경변수로 오버라이드 가능:
- BALDUR_JITTER_ERROR_BUDGET_DANGER_THRESHOLD
- BALDUR_JITTER_ERROR_BUDGET_SAFE_THRESHOLD
- BALDUR_JITTER_LOAD_HIGH_THRESHOLD
- BALDUR_JITTER_LOAD_LOW_THRESHOLD
"""

import random

from baldur.settings.jitter import get_jitter_settings

__all__ = ["AdaptiveJitter"]


class AdaptiveJitter:
    """
    지능형 Jitter 계산기

    시스템 상태에 따라 Jitter 범위를 동적으로 조절:
    - 여유로운 상황: 최소 지터 (빠른 복구)
    - 위험 상황: 최대 지터 (Thundering Herd 방지)

    Usage:
        # 상태 정보 없이 (기본 범위)
        jitter = AdaptiveJitter.calculate()

        # 상태 정보와 함께
        jitter = AdaptiveJitter.calculate(
            error_budget_remaining=0.3,  # 30% 남음
            current_load=0.7             # 70% 부하
        )

        # 밀리초 단위
        jitter_ms = AdaptiveJitter.calculate_ms()
    """

    # Jitter 범위 설정 (초)
    JITTER_MIN_RELAXED: tuple[float, float] = (0, 0.05)  # 여유: 0~50ms
    JITTER_MIN_NORMAL: tuple[float, float] = (0.03, 0.1)  # 보통: 30~100ms
    JITTER_MIN_STRESSED: tuple[float, float] = (0.1, 0.3)  # 위험: 100~300ms

    @classmethod
    def _get_error_budget_danger_threshold(cls) -> float:
        """에러 버짓 위험 임계값 (20% 이하 → 위험)"""
        return get_jitter_settings().error_budget_danger_threshold

    @classmethod
    def _get_error_budget_safe_threshold(cls) -> float:
        """에러 버짓 안전 임계값 (50% 이상 → 여유)"""
        return get_jitter_settings().error_budget_safe_threshold

    @classmethod
    def _get_load_high_threshold(cls) -> float:
        """고부하 임계값 (80% 이상 → 위험)"""
        return get_jitter_settings().load_high_threshold

    @classmethod
    def _get_load_low_threshold(cls) -> float:
        """저부하 임계값 (30% 이하 → 여유)"""
        return get_jitter_settings().load_low_threshold

    @classmethod
    def calculate(
        cls,
        error_budget_remaining: float | None = None,
        current_load: float | None = None,
    ) -> float:
        """
        상황에 맞는 Jitter 값 계산

        Args:
            error_budget_remaining: 남은 에러 버짓 비율 (0.0 ~ 1.0)
            current_load: 현재 시스템 부하 (0.0 ~ 1.0)

        Returns:
            적용할 Jitter 값 (초)
        """
        jitter_range = cls.get_jitter_range(error_budget_remaining, current_load)
        return random.uniform(*jitter_range)

    @classmethod
    def calculate_ms(
        cls,
        error_budget_remaining: float | None = None,
        current_load: float | None = None,
    ) -> int:
        """밀리초 단위로 반환"""
        return int(cls.calculate(error_budget_remaining, current_load) * 1000)

    @classmethod
    def get_jitter_range(
        cls,
        error_budget_remaining: float | None = None,
        current_load: float | None = None,
    ) -> tuple[float, float]:
        """
        상황에 맞는 Jitter 범위 결정

        Args:
            error_budget_remaining: 남은 에러 버짓 비율 (0.0 ~ 1.0)
            current_load: 현재 시스템 부하 (0.0 ~ 1.0)

        Returns:
            (min_jitter, max_jitter) 튜플 (초)
        """
        # 정보가 없으면 보통 범위 사용
        if error_budget_remaining is None and current_load is None:
            return cls.JITTER_MIN_NORMAL

        # 위험 상황 판단 (settings에서 임계값 조회)
        is_budget_danger = (
            error_budget_remaining is not None
            and error_budget_remaining < cls._get_error_budget_danger_threshold()
        )
        is_load_high = (
            current_load is not None and current_load > cls._get_load_high_threshold()
        )

        # 여유 상황 판단 (settings에서 임계값 조회)
        is_budget_safe = (
            error_budget_remaining is not None
            and error_budget_remaining > cls._get_error_budget_safe_threshold()
        )
        is_load_low = (
            current_load is not None and current_load < cls._get_load_low_threshold()
        )

        # 위험: 최대 지터
        if is_budget_danger or is_load_high:
            return cls.JITTER_MIN_STRESSED

        # 여유: 최소 지터
        if is_budget_safe and is_load_low:
            return cls.JITTER_MIN_RELAXED

        # 보통: 중간 지터
        return cls.JITTER_MIN_NORMAL

    @classmethod
    def get_status(
        cls,
        error_budget_remaining: float | None = None,
        current_load: float | None = None,
    ) -> str:
        """
        현재 상태 문자열 반환

        Returns:
            'relaxed', 'normal', or 'stressed'
        """
        jitter_range = cls.get_jitter_range(error_budget_remaining, current_load)

        if jitter_range == cls.JITTER_MIN_RELAXED:
            return "relaxed"
        if jitter_range == cls.JITTER_MIN_STRESSED:
            return "stressed"
        return "normal"
