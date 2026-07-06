"""
Safety Bounds - 자율 조정 안전 한계

자율 조정이 위험한 범위로 벗어나지 않도록 보호합니다.

핵심 기능:
- 파라미터별 min/max 범위 검증
- 한 번에 변경 가능한 최대 비율 제한
- 런타임 한계 업데이트 (관리자 전용)

설정값은 SafetyBoundsSettings를 통해 환경변수로 오버라이드 가능:
- BALDUR_SAFETY_BOUNDS_TIMEOUT_MS_MIN / MAX / MAX_CHANGE
- BALDUR_SAFETY_BOUNDS_RETRY_COUNT_MIN / MAX / MAX_CHANGE
- 기타 파라미터...
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

import structlog

from baldur.settings.safety_bounds import get_safety_bounds_settings

logger = structlog.get_logger()


@dataclass
class ParameterBound:
    """파라미터 한계"""

    min_value: float
    max_value: float
    max_change_per_cycle: float  # 한 번에 변경 가능한 최대 비율 (0.3 = 30%)

    def validate(self) -> bool:
        """한계 설정 유효성 검사"""
        if self.min_value > self.max_value:
            return False
        return not (self.max_change_per_cycle <= 0 or self.max_change_per_cycle > 1)


class SafetyBounds:
    """
    안전 한계 관리

    자율 조정의 범위를 제한하여 시스템 안정성 보호

    보호 메커니즘:
    1. 절대 범위: min_value ~ max_value
    2. 변경폭 제한: 한 번에 max_change_per_cycle % 이내
    3. 알 수 없는 파라미터 거부
    """

    @classmethod
    def _get_default_bounds(cls) -> dict[str, ParameterBound]:
        """
        SafetyBoundsSettings에서 기본 한계 설정 로드.

        환경변수로 오버라이드 가능한 파라미터별 한계값 반환.
        """
        settings = get_safety_bounds_settings()
        return {
            "timeout_ms": ParameterBound(
                min_value=settings.timeout_ms_min,
                max_value=settings.timeout_ms_max,
                max_change_per_cycle=settings.timeout_ms_max_change,
            ),
            "retry_count": ParameterBound(
                min_value=settings.retry_count_min,
                max_value=settings.retry_count_max,
                max_change_per_cycle=settings.retry_count_max_change,
            ),
            "circuit_breaker_threshold": ParameterBound(
                min_value=settings.circuit_breaker_threshold_min,
                max_value=settings.circuit_breaker_threshold_max,
                max_change_per_cycle=settings.circuit_breaker_threshold_max_change,
            ),
            "jitter_range": ParameterBound(
                min_value=settings.jitter_range_min,
                max_value=settings.jitter_range_max,
                max_change_per_cycle=settings.jitter_range_max_change,
            ),
            "rate_limit_rps": ParameterBound(
                min_value=settings.rate_limit_rps_min,
                max_value=settings.rate_limit_rps_max,
                max_change_per_cycle=settings.rate_limit_rps_max_change,
            ),
            "throttle_sla_warning_ms": ParameterBound(
                min_value=settings.throttle_sla_warning_ms_min,
                max_value=settings.throttle_sla_warning_ms_max,
                max_change_per_cycle=settings.throttle_sla_warning_ms_max_change,
            ),
            "throttle_sla_critical_ms": ParameterBound(
                min_value=settings.throttle_sla_critical_ms_min,
                max_value=settings.throttle_sla_critical_ms_max,
                max_change_per_cycle=settings.throttle_sla_critical_ms_max_change,
            ),
            "backoff_base_ms": ParameterBound(
                min_value=settings.backoff_base_ms_min,
                max_value=settings.backoff_base_ms_max,
                max_change_per_cycle=settings.backoff_base_ms_max_change,
            ),
            "backoff_max_ms": ParameterBound(
                min_value=settings.backoff_max_ms_min,
                max_value=settings.backoff_max_ms_max,
                max_change_per_cycle=settings.backoff_max_ms_max_change,
            ),
            "connection_pool_size": ParameterBound(
                min_value=settings.connection_pool_size_min,
                max_value=settings.connection_pool_size_max,
                max_change_per_cycle=settings.connection_pool_size_max_change,
            ),
        }

    def __init__(
        self,
        custom_bounds: dict[str, dict[str, float]] | None = None,
        strict_mode: bool = True,
    ):
        """
        Args:
            custom_bounds: 커스텀 한계 설정
            strict_mode: True면 알 수 없는 파라미터 거부
        """
        self._lock = RLock()
        self.strict_mode = strict_mode

        # 기본 한계 복사 (settings에서 로드)
        default_bounds = self._get_default_bounds()
        self.bounds: dict[str, ParameterBound] = {
            k: ParameterBound(
                min_value=v.min_value,
                max_value=v.max_value,
                max_change_per_cycle=v.max_change_per_cycle,
            )
            for k, v in default_bounds.items()
        }

        # 커스텀 한계 적용
        if custom_bounds:
            for param, config in custom_bounds.items():
                self.update_bounds(param, config)

        logger.info(
            "safety_bounds.initialized_parameters",
            bounds_count=len(self.bounds),
        )

    def is_within_bounds(
        self,
        parameter: str,
        new_value: float,
        current_value: float | None = None,
    ) -> bool:
        """
        값이 안전 한계 내인지 확인

        Args:
            parameter: 파라미터 이름
            new_value: 새로운 값
            current_value: 현재 값 (변경폭 검증용)

        Returns:
            안전 한계 내이면 True
        """
        with self._lock:
            bound = self.bounds.get(parameter)

            if bound is None:
                if self.strict_mode:
                    logger.warning(
                        "safety_bounds.unknown_parameter_rejected",
                        safety_parameter=parameter,
                    )
                    return False
                logger.debug(
                    "safety_bounds.unknown_parameter_allowed_non",
                    safety_parameter=parameter,
                )
                return True

            # 범위 검증
            if new_value < bound.min_value:
                logger.warning(
                    "safety_bounds.below_minimum",
                    safety_parameter=parameter,
                    new_value=new_value,
                    bound=bound.min_value,
                )
                return False

            if new_value > bound.max_value:
                logger.warning(
                    "safety_bounds.above_maximum",
                    safety_parameter=parameter,
                    new_value=new_value,
                    bound=bound.max_value,
                )
                return False

            # 변경폭 검증
            if current_value is not None and current_value > 0:
                change_ratio = abs(new_value - current_value) / current_value
                if change_ratio > bound.max_change_per_cycle:
                    logger.warning(
                        "safety_bounds.change_ratio_exceeds_limit",
                        safety_parameter=parameter,
                        change_ratio=change_ratio,
                        bound=bound.max_change_per_cycle,
                    )
                    return False

            return True

    def clamp_to_bounds(
        self,
        parameter: str,
        value: float,
        current_value: float | None = None,
    ) -> float:
        """
        값을 안전 한계 내로 제한

        Args:
            parameter: 파라미터 이름
            value: 원하는 값
            current_value: 현재 값 (변경폭 제한용)

        Returns:
            안전 한계 내로 조정된 값
        """
        with self._lock:
            bound = self.bounds.get(parameter)

            if bound is None:
                return value

            # 절대 범위 적용
            clamped = max(bound.min_value, min(value, bound.max_value))

            # 변경폭 제한 적용
            if current_value is not None and current_value > 0:
                max_change = current_value * bound.max_change_per_cycle
                if abs(clamped - current_value) > max_change:
                    # 변경 방향 유지하면서 폭만 제한
                    if clamped > current_value:
                        clamped = current_value + max_change
                    else:
                        clamped = current_value - max_change

            return clamped

    def update_bounds(
        self,
        parameter: str,
        config: dict[str, float],
    ) -> bool:
        """
        런타임에 한계 업데이트 (관리자 전용)

        Args:
            parameter: 파라미터 이름
            config: {"min_value": x, "max_value": y, "max_change_per_cycle": z}

        Returns:
            성공 여부
        """
        with self._lock:
            try:
                new_bound = ParameterBound(
                    min_value=config.get("min_value", 0),
                    max_value=config.get("max_value", float("inf")),
                    max_change_per_cycle=config.get("max_change_per_cycle", 0.3),
                )

                if not new_bound.validate():
                    logger.error(
                        "safety_bounds.invalid_bound_config",
                        safety_parameter=parameter,
                    )
                    return False

                self.bounds[parameter] = new_bound
                logger.info(
                    "safety_bounds.updated_bounds",
                    safety_parameter=parameter,
                    new_bound=new_bound.min_value,
                    max_value=new_bound.max_value,
                    max_change_per_cycle=new_bound.max_change_per_cycle,
                )
                return True
            except Exception as e:
                logger.exception(
                    "safety_bounds.update_bounds_failed",
                    error=e,
                )
                return False

    def remove_bounds(self, parameter: str) -> bool:
        """한계 제거"""
        with self._lock:
            if parameter in self.bounds:
                del self.bounds[parameter]
                logger.info(
                    "safety_bounds.removed_bounds",
                    safety_parameter=parameter,
                )
                return True
            return False

    def get_bounds(self, parameter: str) -> dict[str, float] | None:
        """특정 파라미터의 한계 조회"""
        with self._lock:
            bound = self.bounds.get(parameter)
            if bound is None:
                return None
            return {
                "min_value": bound.min_value,
                "max_value": bound.max_value,
                "max_change_per_cycle": bound.max_change_per_cycle,
            }

    def get_all_bounds(self) -> dict[str, dict[str, float]]:
        """모든 한계 조회"""
        with self._lock:
            return {
                param: {
                    "min_value": bound.min_value,
                    "max_value": bound.max_value,
                    "max_change_per_cycle": bound.max_change_per_cycle,
                }
                for param, bound in self.bounds.items()
            }

    def check_all(
        self,
        values: dict[str, float],
        current_values: dict[str, float] | None = None,
    ) -> dict[str, bool]:
        """여러 값을 한 번에 검증"""
        results = {}
        for param, value in values.items():
            current = current_values.get(param) if current_values else None
            results[param] = self.is_within_bounds(param, value, current)
        return results

    def reset_to_defaults(self) -> None:
        """기본 한계로 리셋 (settings에서 다시 로드)"""
        with self._lock:
            default_bounds = self._get_default_bounds()
            self.bounds = {
                k: ParameterBound(
                    min_value=v.min_value,
                    max_value=v.max_value,
                    max_change_per_cycle=v.max_change_per_cycle,
                )
                for k, v in default_bounds.items()
            }
            logger.info("safety_bounds.reset_defaults")


__all__ = [
    "SafetyBounds",
    "ParameterBound",
]
