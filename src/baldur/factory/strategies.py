"""
Strategy access helpers and auto-discover callbacks for strategy registries.

discover_* functions register available strategy implementations when
invoked. They serve as auto_discover callbacks for GenericProviderRegistry
instances on ProviderRegistry (D3: DCL variant unification).

get_best_* functions select the best available ML/statistical strategy
from the ProviderRegistry slots. They are OSS chassis code: the slots are
populated by the private-distribution bootstrap hook; when no provider is
registered (OSS-only install) the getters raise AdapterNotFoundError or
return None, and consumers fail open through their existing try/except
seams.
# Relocated from services/ml_models/__init__.py per 599 D9.

Note: Correlation/root-cause/graph-build strategies are typically registered
by their respective modules explicitly, so these callbacks are minimal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from baldur.interfaces.ml_strategy import (
        AnomalyDetectionStrategy,
        ClassificationStrategy,
        ForecastStrategy,
        OptimizationStrategy,
    )

__all__ = [
    "discover_correlation_strategies",
    "discover_root_cause_strategies",
    "discover_graph_build_strategies",
    "get_best_anomaly_detector",
    "get_best_classifier",
    "get_best_forecaster",
    "get_best_optimizer",
]


def discover_correlation_strategies() -> None:
    """Auto-discover correlation strategies (placeholder)."""
    # Correlation strategies are registered explicitly by consumers.
    # No default auto-discovery needed.


def discover_root_cause_strategies() -> None:
    """Auto-discover root cause strategies (placeholder)."""
    # Root cause strategies are registered explicitly by consumers.
    # No default auto-discovery needed.


def discover_graph_build_strategies() -> None:
    """Auto-discover graph build strategies (placeholder)."""
    # Graph build strategies are registered explicitly by consumers.
    # No default auto-discovery needed.


# ── ML strategy selection helpers ──


def get_best_anomaly_detector() -> AnomalyDetectionStrategy:
    """Get the best available anomaly detector.

    Priority:
    1. isolation_forest (if installed and is_ready())
    2. zscore (statistical fallback, registered by the private bootstrap hook)

    Note: ProviderRegistry import is inside the function to avoid
    circular dependency (this module is referenced by registry.py
    auto_discover lambdas). Same pattern as factory/repositories.py:29.

    Exception scope: Only catch AdapterNotFoundError (not registered)
    and ImportError (sklearn not installed) for the ML branch. Model
    internal errors (OOM, numpy, etc.) must propagate for operator
    visibility. With no provider registered at all (OSS-only install),
    the fallback .get() raises AdapterNotFoundError into the caller's
    fail-open seam.
    """
    from baldur.core.exceptions import AdapterNotFoundError
    from baldur.factory.registry import ProviderRegistry
    from baldur.interfaces import StrategyLifecycle

    registry = ProviderRegistry.anomaly_detection

    try:
        detector = registry.get("isolation_forest")
        if isinstance(detector, StrategyLifecycle) and detector.is_ready():
            return cast("AnomalyDetectionStrategy", detector)
    except (AdapterNotFoundError, ImportError):
        pass

    return cast("AnomalyDetectionStrategy", registry.get("zscore"))


def get_best_forecaster() -> ForecastStrategy:
    """Get the best available forecaster.

    Priority:
    1. arima (if installed and is_ready())
    2. holt_linear (statistical fallback, registered by the private bootstrap hook)
    """
    from baldur.core.exceptions import AdapterNotFoundError
    from baldur.factory.registry import ProviderRegistry
    from baldur.interfaces import StrategyLifecycle

    registry = ProviderRegistry.forecast

    try:
        forecaster = registry.get("arima")
        if isinstance(forecaster, StrategyLifecycle) and forecaster.is_ready():
            return cast("ForecastStrategy", forecaster)
    except (AdapterNotFoundError, ImportError):
        pass

    return cast("ForecastStrategy", registry.get("holt_linear"))


def get_best_classifier() -> ClassificationStrategy:
    """Get the best available classifier.

    Priority:
    1. gradient_boost (if installed and is_ready())
    2. spike_rules (statistical fallback, registered by the private bootstrap hook)
    """
    from baldur.core.exceptions import AdapterNotFoundError
    from baldur.factory.registry import ProviderRegistry
    from baldur.interfaces import StrategyLifecycle

    registry = ProviderRegistry.classification

    try:
        classifier = registry.get("gradient_boost")
        if isinstance(classifier, StrategyLifecycle) and classifier.is_ready():
            return cast("ClassificationStrategy", classifier)
    except (AdapterNotFoundError, ImportError):
        pass

    return cast("ClassificationStrategy", registry.get("spike_rules"))


def get_best_optimizer() -> OptimizationStrategy | None:
    """Get the best available optimizer.

    Priority:
    1. bayesian (if installed and is_ready())
    2. None (no statistical fallback — DecisionEngine is rule-based,
       not OptimizationStrategy-compatible. 374 handles this gap.)
    """
    from baldur.core.exceptions import AdapterNotFoundError
    from baldur.factory.registry import ProviderRegistry
    from baldur.interfaces import StrategyLifecycle

    registry = ProviderRegistry.optimization

    try:
        optimizer: OptimizationStrategy = registry.get("bayesian")
        if isinstance(optimizer, StrategyLifecycle) and optimizer.is_ready():
            return optimizer
    except (AdapterNotFoundError, ImportError):
        pass

    return None
