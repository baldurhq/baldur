"""
Config Shadow Evaluator — 설정 변경 사전 시뮬레이션 엔진.

과거 이벤트를 리플레이하여 설정 변경 효과를 예측한다.
"""

from __future__ import annotations

from baldur.services.config_shadow.service import ShadowEvaluatorService
from baldur.utils.singleton import make_singleton_factory

(
    get_shadow_evaluator_service,
    configure_shadow_evaluator_service,
    reset_shadow_evaluator_service,
) = make_singleton_factory("shadow_evaluator_service", ShadowEvaluatorService)

__all__ = [
    "ShadowEvaluatorService",
    "get_shadow_evaluator_service",
    "configure_shadow_evaluator_service",
    "reset_shadow_evaluator_service",
]
