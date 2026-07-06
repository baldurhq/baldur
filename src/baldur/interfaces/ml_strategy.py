"""
ML Strategy Interfaces - foundation for AI/ML extensions.

Defines ML Strategy Protocols that can be shared across the system.
Provides extension points so that the built-in statistical implementations
(ZScore, IQR, Holt-Winters) can be swapped for AI/ML models.

Designed around Protocol (duck typing) so that:
    - The consumer can use any ML framework (scikit-learn, PyTorch, TensorFlow)
    - @runtime_checkable enables runtime type checks
    - Existing classes are compatible just by adding the methods (no inheritance required)

Usage:
    from baldur.interfaces.ml_strategy import (
        AnomalyDetectionStrategy,
        ForecastStrategy,
        ClassificationStrategy,
        BatchDetectable,
        BatchClassifiable,
        StrategyLifecycle,
    )

    # Check Protocol conformance
    if isinstance(my_detector, AnomalyDetectionStrategy):
        is_anomaly, score = my_detector.detect(value)

    # Check batch capability
    if isinstance(my_detector, BatchDetectable):
        results = my_detector.detect_batch(values)
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = [
    "AnomalyDetectionStrategy",
    "ForecastStrategy",
    "ClassificationStrategy",
    "OptimizationStrategy",
    "BatchDetectable",
    "BatchClassifiable",
    "StrategyLifecycle",
]

# =============================================================================
# AnomalyDetectionStrategy - anomaly detection strategy
# =============================================================================


@runtime_checkable
class AnomalyDetectionStrategy(Protocol):
    """Anomaly detection strategy - swappable between statistics / ML / deep learning.

    Built-in:
        - ZScoreDetector: Z-Score based
        - IQRDetector: IQR based

    Consumer extension examples:
        - IsolationForestDetector: scikit-learn Isolation Forest
        - AutoencoderDetector: PyTorch Autoencoder based
        - ProphetDetector: Facebook Prophet based seasonal-aware detection

    Used by:
        - PredictiveForecasterService (metric anomaly detection)
        - CoOccurrenceTracker (co-occurrence frequency anomaly detection)
        - CorruptionShield L3 (data anomaly detection)
    """

    def detect(
        self,
        value: float,
        context: dict[str, Any] | None = None,
    ) -> tuple[bool, float]:
        """Decide whether a single value is anomalous.

        Args:
            value: Value to check
            context: Multi-dimensional features to pass to the ML model (optional).
                Ignored by the built-in statistical strategies (ZScore, IQR).
                ML implementations extract the features they need from this dict.

        Returns:
            (is_anomalous, score): anomaly verdict and anomaly score.
            score does not need to be normalized -- strategies choose
            their own scale (ZScore, probability, etc.).
        """
        ...

    def update(
        self,
        value: float,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Add a training sample (online learning).

        Args:
            value: New observation
            context: Multi-dimensional features to pass to the ML model (optional)
        """
        ...

    def reset(self) -> None:
        """Reset learning state."""
        ...

    def get_feature_schema(self) -> dict[str, str] | None:
        """Return the context key/type schema the strategy expects.

        Returns:
            Shape like {"service_name": "str", "cpu_usage": "float", ...}.
            None when no schema applies (statistical strategy).
            Used by the orchestrator to validate inputs in advance.
        """
        ...


# =============================================================================
# ForecastStrategy - time-series forecasting strategy
# =============================================================================


@runtime_checkable
class ForecastStrategy(Protocol):
    """Time-series forecasting strategy.

    Built-in:
        - HoltLinearForecaster: double exponential smoothing

    Consumer extension examples:
        - ProphetForecaster: Facebook Prophet
        - LSTMForecaster: PyTorch LSTM
        - ARIMAForecaster: statsmodels ARIMA

    Used by:
        - PredictiveForecasterService (metric forecasting)
        - CoOccurrenceTracker (co-occurrence frequency trend)
    """

    def update(self, value: float) -> float:
        """Update the model with a new observation.

        Args:
            value: New observation

        Returns:
            Current level (smoothed value)
        """
        ...

    def predict(self, steps_ahead: int = 1) -> float | None:
        """Predict a future value.

        Args:
            steps_ahead: Number of future steps to predict

        Returns:
            Predicted value. None when there is insufficient data.
        """
        ...

    def get_confidence(self) -> float:
        """Confidence of the current model (0.0 to 1.0)."""
        ...


# =============================================================================
# ClassificationStrategy - classification strategy
# =============================================================================


@runtime_checkable
class ClassificationStrategy(Protocol):
    """Classification strategy - event / spike type classification.

    Built-in:
        - SpikeClassifier: rule-based classifier

    Consumer extension examples:
        - RandomForestClassifier: scikit-learn RF
        - XGBoostClassifier: XGBoost
        - NeuralClassifier: PyTorch NN

    Used by:
        - PredictiveForecasterService (spike classification)
        - CorrelationEngine (event pattern classification)
    """

    def classify(
        self,
        features: dict[str, float],
        context: dict[str, Any] | None = None,
    ) -> tuple[str, float]:
        """Feature vector -> class label + confidence.

        Args:
            features: Feature name -> value mapping
            context: Additional metadata to pass to the ML model (optional)

        Returns:
            (label, confidence): classification label and confidence (0.0 to 1.0)
        """
        ...


# =============================================================================
# BatchDetectable - batch anomaly detection marker Protocol
# =============================================================================


@runtime_checkable
class BatchDetectable(Protocol):
    """Batch anomaly detection marker.

    ML implementations that support tensor batch operations implement this.
    Statistical strategies (ZScore, IQR) do not need to implement this.

    Usage:
        - CorrelationEngineService (batch dispatch)
        - MicroBatchConsumer (batch dispatch)
    """

    def detect_batch(
        self,
        values: list[float],
        contexts: list[dict[str, Any] | None] | None = None,
    ) -> list[tuple[bool, float]]:
        """Batch anomaly detection.

        Args:
            values: Values to check.
            contexts: Per-value metadata (optional).

        Returns:
            List of (is_anomalous, score) tuples in input order.
        """
        ...

    def update_batch(self, values: list[float]) -> None:
        """Add batch training data."""
        ...


# =============================================================================
# BatchClassifiable - batch classification marker Protocol
# =============================================================================


@runtime_checkable
class BatchClassifiable(Protocol):
    """Batch classification marker.

    ML classification implementations that support vectorized batch
    operations implement this. Rule-based classifiers (SpikeClassifier)
    do not need to implement this.

    Usage:
        - CorrelationEngine (batch event classification)
    """

    def classify_batch(
        self,
        features_list: list[dict[str, float]],
        contexts: list[dict[str, Any]] | None = None,
    ) -> list[tuple[str, float]]:
        """Batch classification.

        Args:
            features_list: List of feature dicts.
            contexts: Per-item metadata (optional).

        Returns:
            List of (label, confidence) tuples in input order.
        """
        ...


# =============================================================================
# OptimizationStrategy - parameter optimization strategy
# =============================================================================


@runtime_checkable
class OptimizationStrategy(Protocol):
    """Parameter optimization strategy - settings value search.

    Default:
        - DecisionEngine rules (rule-based)

    ML implementations:
        - BayesianOptimizer: Gaussian Process + Expected Improvement
        - EvolutionaryOptimizer: CMA-ES (optional, future)

    Usage:
        - SettingsRecommendationService (optimal value search)
        - AutoTuningService (parameter suggestion)

    Note:
        DecisionEngine (rule-based) and OptimizationStrategy (ML-based) operate
        on fundamentally different paradigms. Their integration is handled by
        SettingsRecommendationService (374), NOT by an adapter in this package.
        373 provides the ML implementation; 374 orchestrates the pipeline.
    """

    def suggest(
        self,
        parameter: str,
        current_value: float,
        bounds: tuple[float, float],
        history: list[dict[str, Any]],
        objective_metric: str,
        minimize: bool = True,
    ) -> tuple[float, float]:
        """Suggest optimal value for a parameter.

        Args:
            parameter: Parameter name to optimize.
            current_value: Current parameter value.
            bounds: (min, max) allowed range.
            history: Past observations [{parameter: value, metric: value}, ...].
            objective_metric: Metric name to optimize (e.g., "p99_latency_ms").
            minimize: If True, lower metric = better (default). If False,
                higher metric = better (e.g., throughput RPS).

        Returns:
            (suggested_value, expected_improvement): Value and expected gain.
        """
        ...

    def suggest_batch(
        self,
        parameters: list[str],
        current_values: dict[str, float],
        bounds: dict[str, tuple[float, float]],
        history: list[dict[str, Any]],
        objective_metrics: list[str],
        minimize: bool = True,
    ) -> dict[str, tuple[float, float]]:
        """Suggest optimal values for multiple parameters simultaneously.

        Returns:
            {parameter: (suggested_value, expected_improvement)}
        """
        ...

    def update_observation(
        self,
        parameters: dict[str, float],
        metrics: dict[str, float],
    ) -> None:
        """Record an observation (parameter values -> metric outcomes).

        Used to update the internal model after applying a recommendation.
        """
        ...


# =============================================================================
# StrategyLifecycle - model loading / warmup + K8s Readiness integration
# =============================================================================


@runtime_checkable
class StrategyLifecycle(Protocol):
    """ML strategy lifecycle management - optional implementation.

    Lightweight statistical strategies (ZScore, IQR) do not need to implement this.
    Heavy ML models (PyTorch, XGBoost, LLM) implement this.

    Prior art:
        - ShutdownHandler ABC: on_shutdown_start(), on_drain_complete()
        - ProviderRegistry.health_check_all(): unified provider health checks
        - HoltLinearForecaster: predict() -> None when warmup_samples < 30
    """

    def initialize(self) -> None:
        """Load model weights from disk to memory (or GPU VRAM).

        Called during Orchestrator startup.
        """
        ...

    def warmup(self) -> None:
        """Run a first inference with dummy input.

        Purpose: JIT compilation (PyTorch), CUDA kernel caching, TensorRT
        engine building, etc.
        Called after initialize() and before the first real detect().
        """
        ...

    def is_ready(self) -> bool:
        """Whether the strategy is ready for inference.

        Wired up to the K8s Readiness Probe.
        Must return an O(1) cached boolean
        (constrained by readinessProbe.timeoutSeconds=3).
        """
        ...

    def teardown(self) -> None:
        """Release resources (GPU VRAM, temporary files).

        Called from GracefulShutdownCoordinator.initiate_shutdown().
        """
        ...
