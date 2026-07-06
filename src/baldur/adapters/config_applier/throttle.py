"""
AdaptiveThrottle SLA 설정 전용 ConfigApplier.

ConfigApplier Protocol 구현체.
화이트리스트 기반으로 허용된 SLA 파라미터(sla_warning_ms, sla_critical_ms)만 조정하며,
model_copy() Atomic Swap으로 Thread-Safety를 보장한다.

비허용 파라미터(rate_limit_rps 등)는 No-op + 로그로 하위 호환성 유지.
"""

from typing import Any, cast

import structlog

from baldur.factory.registry import ProviderRegistry

logger = structlog.get_logger()


class ThrottleConfigApplier:
    """
    AdaptiveThrottle의 SLA 설정 전용 ConfigApplier.

    화이트리스트 기반으로 허용된 파라미터만 조정하며,
    model_copy() Atomic Swap으로 Thread-Safety를 보장한다.

    비허용 파라미터(rate_limit_rps 등)는 No-op + 로그로 하위 호환성 유지.
    """

    # 조정 허용 파라미터 → config 속성명 매핑
    PARAM_TO_CONFIG: dict[str, str] = {
        "throttle_sla_warning_ms": "sla_warning_ms",
        "throttle_sla_critical_ms": "sla_critical_ms",
    }

    # No-op 처리할 레거시 파라미터 (하위 호환)
    LEGACY_NOOP_PARAMS: set[str] = {"rate_limit_rps"}

    def get_current(self, parameter: str) -> float:
        """현재 값 조회."""
        # No-op 레거시 파라미터
        if parameter in self.LEGACY_NOOP_PARAMS:
            return 0.0

        config_attr = self.PARAM_TO_CONFIG.get(parameter)
        if config_attr is None:
            raise ValueError(
                f"Parameter '{parameter}' not supported by ThrottleConfigApplier. "
                f"Allowed: {set(self.PARAM_TO_CONFIG.keys())}"
            )

        throttle = ProviderRegistry.adaptive_throttle.safe_get()
        if throttle is None:
            raise RuntimeError(
                "ThrottleConfigApplier requires baldur_pro AdaptiveThrottle"
            )
        # PRO impl exposes `.config`; OSS Protocol intentionally omits it
        # (impl-specific introspection used by the config applier).
        return float(getattr(cast(Any, throttle).config, config_attr))

    def apply(self, parameter: str, value: float) -> bool:
        """
        설정 적용 — Atomic Swap 방식.

        Pydantic v2 model_copy(update=...)로 새 config 객체를 생성하고,
        throttle.config 참조를 한 번에 교체한다.
        Python GIL에 의해 참조 대입은 atomic이므로 _maybe_adjust_limit()
        실행 중에도 안전하다.
        """
        # No-op 레거시 파라미터 — 성공 반환 (하위 호환)
        if parameter in self.LEGACY_NOOP_PARAMS:
            logger.info(
                "throttle_config_applier.deprecated_no_op_use",
                config_parameter=parameter,
            )
            return True

        config_attr = self.PARAM_TO_CONFIG.get(parameter)
        if config_attr is None:
            return False

        throttle = ProviderRegistry.adaptive_throttle.safe_get()
        if throttle is None:
            raise RuntimeError(
                "ThrottleConfigApplier requires baldur_pro AdaptiveThrottle"
            )
        # PRO impl exposes `.config`; OSS Protocol omits it intentionally.
        throttle_any = cast(Any, throttle)
        old_config = throttle_any.config

        # Atomic Swap: model_copy()로 새 객체 생성 후 참조 교체
        new_config = old_config.model_copy(update={config_attr: int(value)})
        throttle_any.config = new_config  # GIL atomic reference swap

        logger.info(
            "throttle_config_applier.applied_config_swap",
            config_parameter=parameter,
            getattr=getattr(old_config, config_attr),
            int=int(value),
        )
        return True

    def rollback(self, parameter: str, value: float) -> bool:
        """롤백 적용 — apply()와 동일 로직."""
        return self.apply(parameter, value)
