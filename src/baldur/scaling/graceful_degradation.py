"""
Graceful Degradation - 단계별 기능 축소.

과부하 시 비필수 기능부터 순차적으로 비활성화합니다.
Backpressure 레벨에 따라 자동으로 기능을 활성화/비활성화합니다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum

import structlog

from baldur.scaling.config import (
    BackpressureLevel,
    BackpressureSettings,
    get_backpressure_settings,
)

logger = structlog.get_logger()


class FeaturePriority(IntEnum):
    """
    기능 우선순위.

    값이 낮을수록 높은 우선순위 (비활성화 대상에서 제외됨).
    """

    CRITICAL = 0  # 항상 유지 (핵심 기능)
    HIGH = 1  # 높음 (DLQ 처리 등)
    MEDIUM = 2  # 중간 (알림)
    LOW = 3  # 낮음 (로깅, 통계)
    OPTIONAL = 4  # 선택 (디버그, 추적)


@dataclass
class Feature:
    """기능 정의."""

    name: str
    """기능 이름 (고유 식별자)."""

    priority: FeaturePriority
    """우선순위."""

    enabled: bool = True
    """현재 활성화 상태."""

    on_disable: Callable[[], None] | None = None
    """비활성화 시 호출될 콜백."""

    on_enable: Callable[[], None] | None = None
    """활성화 시 호출될 콜백."""


class GracefulDegradation:
    """
    Graceful Degradation Manager.

    Backpressure 레벨에 따라 기능 활성화/비활성화.

    레벨별 동작:
    - NONE: 모든 기능 활성화
    - LOW: OPTIONAL 비활성화
    - MEDIUM: LOW 이하 비활성화
    - HIGH: MEDIUM 이하 비활성화
    - CRITICAL: CRITICAL만 유지

    Usage:
        degradation = GracefulDegradation()

        # 기능 등록
        degradation.register_feature(Feature(
            name="detailed_logging",
            priority=FeaturePriority.OPTIONAL,
        ))

        # 레벨 업데이트
        degradation.update_level(BackpressureLevel.HIGH)

        # 기능 사용 가능 여부 확인
        if degradation.is_enabled("detailed_logging"):
            log_details()
    """

    # 레벨별 활성화 우선순위 임계치
    # 해당 레벨에서는 이 우선순위 이하(값이 큰 것)는 비활성화
    LEVEL_THRESHOLDS: dict[BackpressureLevel, FeaturePriority] = {
        BackpressureLevel.NONE: FeaturePriority.OPTIONAL,  # 모두 유지
        BackpressureLevel.LOW: FeaturePriority.LOW,  # OPTIONAL 비활성화
        BackpressureLevel.MEDIUM: FeaturePriority.MEDIUM,  # LOW 이하 비활성화
        BackpressureLevel.HIGH: FeaturePriority.HIGH,  # MEDIUM 이하 비활성화
        BackpressureLevel.CRITICAL: FeaturePriority.CRITICAL,  # CRITICAL만 유지
    }

    def __init__(
        self,
        settings: BackpressureSettings | None = None,
    ):
        """
        Args:
            settings: Backpressure 설정
        """
        self._settings = settings or get_backpressure_settings()
        self._features: dict[str, Feature] = {}
        self._current_level = BackpressureLevel.NONE

    def register_feature(self, feature: Feature) -> None:
        """
        기능 등록.

        Args:
            feature: 등록할 기능
        """
        self._features[feature.name] = feature
        logger.debug(
            "graceful_degradation.feature_registered",
            feature=feature.name,
        )

    def unregister_feature(self, name: str) -> None:
        """
        기능 등록 해제.

        Args:
            name: 기능 이름
        """
        if name in self._features:
            del self._features[name]

    def is_enabled(self, name: str) -> bool:
        """
        기능 활성화 여부 확인.

        Args:
            name: 기능 이름

        Returns:
            활성화 여부 (등록되지 않은 기능은 True 반환)
        """
        if not self._settings.graceful_degradation_enabled:
            return True

        feature = self._features.get(name)
        if feature is None:
            return True

        return feature.enabled

    def update_level(self, level: BackpressureLevel) -> None:
        """
        Backpressure 레벨 업데이트.

        레벨에 따라 기능 활성화/비활성화.

        Args:
            level: 새 Backpressure 레벨
        """
        if not self._settings.graceful_degradation_enabled:
            return

        if level == self._current_level:
            return

        old_level = self._current_level
        self._current_level = level

        threshold = self.LEVEL_THRESHOLDS.get(level, FeaturePriority.OPTIONAL)

        for feature in self._features.values():
            # 우선순위 값이 임계치 이하면 활성화 (값이 작을수록 높은 우선순위)
            should_enable = feature.priority.value <= threshold.value

            if should_enable and not feature.enabled:
                feature.enabled = True
                if feature.on_enable:
                    try:
                        feature.on_enable()
                    except Exception as e:
                        logger.exception(
                            "graceful_degradation.error",
                            error=e,
                        )
                logger.info(
                    "graceful_degradation.enabled",
                    feature=feature.name,
                )

            elif not should_enable and feature.enabled:
                feature.enabled = False
                if feature.on_disable:
                    try:
                        feature.on_disable()
                    except Exception as e:
                        logger.exception(
                            "graceful_degradation.error",
                            error=e,
                        )
                logger.info(
                    "graceful_degradation.disabled",
                    feature=feature.name,
                )

        logger.info(
            "graceful_degradation.level_changed",
            old_level=old_level.value,
            degradation_level=level.value,
        )

    def get_enabled_features(self) -> list[str]:
        """
        활성화된 기능 목록 반환.

        Returns:
            활성화된 기능 이름 목록
        """
        return [name for name, feature in self._features.items() if feature.enabled]

    def get_disabled_features(self) -> list[str]:
        """
        비활성화된 기능 목록 반환.

        Returns:
            비활성화된 기능 이름 목록
        """
        return [name for name, feature in self._features.items() if not feature.enabled]

    def get_current_level(self) -> BackpressureLevel:
        """현재 레벨 반환."""
        return self._current_level


# =============================================================================
# Singleton
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

get_graceful_degradation, configure_graceful_degradation, reset_graceful_degradation = (
    make_singleton_factory("graceful_degradation", GracefulDegradation)
)
