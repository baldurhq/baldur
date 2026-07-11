"""
Config Shadow Evaluator — pre-simulation engine for configuration changes.

Replays past events to predict the effect of a configuration change.
"""

from __future__ import annotations

from baldur.services.config_shadow.metrics_provider import (
    get_metrics_provider,
    is_metrics_provider_registered,
    reset_metrics_provider,
    set_metrics_provider,
)
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
    "get_metrics_provider",
    "set_metrics_provider",
    "reset_metrics_provider",
    "is_metrics_provider_registered",
]
