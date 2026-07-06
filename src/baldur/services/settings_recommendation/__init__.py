"""Settings Recommendation Service — orchestrates ML + rule-based recommendations.

Public API:
    - RecommendationItem, RecommendationPlan: data models
    - RecommendationSource, RecommendationStatus: enums
    - WorkloadProfile, WORKLOAD_PRESETS: workload presets
    - apply_workload_profile: preset application
    - get_settings_recommendation_service/reset_*: singleton access

Heavy modules (service.py, pipeline.py) use PEP 562 lazy import.
Light modules (models.py, presets.py) are imported directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from baldur.services.settings_recommendation.models import (
    RecommendationItem,
    RecommendationPlan,
    RecommendationSource,
    RecommendationStatus,
)
from baldur.services.settings_recommendation.presets import (
    WORKLOAD_PRESETS,
    WorkloadProfile,
    apply_workload_profile,
)

if TYPE_CHECKING:
    from baldur.services.settings_recommendation.service import (
        SettingsRecommendationService,
        get_settings_recommendation_service,
        reset_settings_recommendation_service,
    )

__all__ = [
    "RecommendationItem",
    "RecommendationPlan",
    "RecommendationSource",
    "RecommendationStatus",
    "WorkloadProfile",
    "WORKLOAD_PRESETS",
    "apply_workload_profile",
    "get_settings_recommendation_service",
    "reset_settings_recommendation_service",
]

# PEP 562 lazy imports for heavy modules
_LAZY_IMPORTS = {
    "SettingsRecommendationService": "baldur.services.settings_recommendation.service",
    "get_settings_recommendation_service": "baldur.services.settings_recommendation.service",
    "reset_settings_recommendation_service": "baldur.services.settings_recommendation.service",
    "RecommendationPipeline": "baldur.services.settings_recommendation.pipeline",
    "ClusterMetricsCollector": "baldur.services.settings_recommendation.metrics_collector",
}


def __getattr__(name: str):
    module_path = _LAZY_IMPORTS.get(name)
    if module_path:
        import importlib

        module = importlib.import_module(module_path)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
