"""Recommendation pipeline — pure data transformation, no I/O.

Four steps: analyze_rules → analyze_ml → propagate → merge.
All external I/O (metrics collection, constraint validation, shadow evaluation)
is handled by SettingsRecommendationService.
"""

from __future__ import annotations

import logging
from statistics import mean
from typing import TYPE_CHECKING, Any

from baldur.core.decision_engine import AdjustmentPriority
from baldur.services.settings_recommendation.models import (
    RecommendationItem,
    RecommendationSource,
)

if TYPE_CHECKING:
    from baldur.core.decision_engine import DecisionEngine
    from baldur.core.settings_dependency import SettingsDependencyGraph

logger = logging.getLogger(__name__)

__all__ = ["RecommendationPipeline"]

# Metric → parameter mapping derived from DecisionEngine.DEFAULT_RULES
METRIC_TO_PARAM: dict[str, str] = {
    "p99_latency_ms": "timeout_ms",
    "retry_exhausted_rate": "retry_count",
    "error_rate": "circuit_breaker_threshold",
    "retry_collision_rate": "jitter_range",
    "throttle_rate": "rate_limit_rps",
}

# Metrics where increase = degradation (lower is better)
LOWER_IS_BETTER: frozenset[str] = frozenset(
    {
        "error_rate",
        "retry_exhaustion_rate",
        "throttle_rate",
        "p99_latency_ms",
        "cb_open_ratio",
    }
)

# SLI metric name → SLO metric name mapping
_SLI_TO_METRIC: dict[str, str] = {
    "error_rate": "error_rate",
    "latency_p99": "p99_latency_ms",
    "availability": "error_rate",  # availability = 1 - error_rate
}


class RecommendationPipeline:
    """Step-by-step pipeline executor (pure data transformation, no I/O)."""

    def __init__(
        self,
        decision_engine: DecisionEngine | None = None,
        dependency_graph: SettingsDependencyGraph | None = None,
        mode: str = "rule_based",
        min_confidence: float = 0.7,
        max_changes_per_cycle: int = 5,
    ) -> None:
        self._decision_engine = decision_engine
        self._dependency_graph = dependency_graph
        self._mode = mode
        self._min_confidence = min_confidence
        self._max_changes = max_changes_per_cycle

    def step_analyze_rules(
        self,
        metrics: dict[str, float],
        prediction_context: dict[str, Any] | None = None,
    ) -> list[RecommendationItem]:
        """Step 1: Rule-based analysis via DecisionEngine."""
        if self._decision_engine is None:
            return []

        try:
            decisions = self._decision_engine.analyze(
                metrics,
                prediction_context=prediction_context,
            )
        except Exception:
            logger.warning("recommendation.rule_analysis_failed", exc_info=True)
            return []

        items: list[RecommendationItem] = []
        for d in decisions:
            items.append(
                RecommendationItem(
                    parameter=d.parameter,
                    current_value=d.current_value,
                    recommended_value=d.suggested_value,
                    source=RecommendationSource.RULE_BASED,
                    confidence=d.confidence,
                    expected_improvement=abs(d.suggested_value - d.current_value)
                    / max(d.current_value, 1e-10),
                    reason=d.reason,
                    priority=d.priority,
                    metric_evidence=d.metric_snapshot,
                )
            )
        return items

    def step_analyze_ml(
        self,
        metrics: dict[str, float],
        ml_context: dict[str, Any],
    ) -> tuple[list[RecommendationItem], dict[str, Any]]:
        """Step 2: ML-based analysis (if ready).

        Returns:
            (items, anomaly_context): recommendation items + anomaly metadata.
        """
        items: list[RecommendationItem] = []
        anomaly_context: dict[str, Any] = {}

        if self._mode == "rule_based":
            return items, anomaly_context

        bounds = ml_context.get("bounds", {})
        current_values = ml_context.get("current_values", {})
        objective_metrics = ml_context.get("objective_metrics", [])

        # --- BayesianOptimizer (ML_OPTIMIZATION) ---
        items.extend(
            self._analyze_optimizer(
                metrics, ml_context, bounds, current_values, objective_metrics
            )
        )

        # --- IsolationForest (anomaly context for confidence boost) ---
        anomaly_context = self._analyze_anomaly(metrics)

        # --- ARIMA/HoltLinear Forecaster (ML_FORECAST) ---
        items.extend(
            self._analyze_forecast(metrics, ml_context, bounds, current_values)
        )

        return items, anomaly_context

    def _analyze_optimizer(
        self,
        metrics: dict[str, float],
        ml_context: dict[str, Any],
        bounds: dict[str, tuple[float, float]],
        current_values: dict[str, float],
        objective_metrics: list[str],
    ) -> list[RecommendationItem]:
        """Generate ML_OPTIMIZATION items from BayesianOptimizer."""
        items: list[RecommendationItem] = []
        try:
            from baldur.factory.strategies import get_best_optimizer

            optimizer = get_best_optimizer()
            if optimizer is None:
                return items

            tunable_params = ml_context.get("tunable_parameters", sorted(bounds.keys()))
            history = ml_context.get("history", [])

            suggestions = optimizer.suggest_batch(
                parameters=tunable_params,
                current_values=current_values,
                bounds=bounds,
                history=history,
                objective_metrics=objective_metrics,
            )

            # Compute avg bound span for sigma normalization
            spans = [hi - lo for lo, hi in bounds.values()]
            avg_span = mean(spans) if spans else 1.0

            # suggest_batch returns (suggested_val, joint_ei, sigma) triples
            # at runtime; the optimizer Protocol declares a looser shape.
            for param, triple in suggestions.items():
                suggested_val, joint_ei, sigma = triple  # type: ignore[misc]
                if param not in current_values:
                    continue
                norm_sigma = sigma / avg_span if avg_span > 0 else 1.0
                confidence = max(0.0, 1.0 - min(1.0, norm_sigma))

                if confidence < self._min_confidence:
                    continue

                items.append(
                    RecommendationItem(
                        parameter=param,
                        current_value=current_values[param],
                        recommended_value=suggested_val,
                        source=RecommendationSource.ML_OPTIMIZATION,
                        confidence=confidence,
                        expected_improvement=joint_ei,
                        reason=f"Bayesian optimization EI={joint_ei:.4f}, σ={sigma:.4f}",
                        priority=AdjustmentPriority.MEDIUM,
                        metric_evidence={
                            "joint_ei": joint_ei,
                            "gp_sigma": sigma,
                            **{m: metrics.get(m, 0.0) for m in objective_metrics},
                        },
                    )
                )
        except Exception:
            logger.warning("recommendation.optimizer_analysis_failed", exc_info=True)
        return items

    def _analyze_anomaly(self, metrics: dict[str, float]) -> dict[str, Any]:
        """Detect anomalies for confidence boosting (no direct items)."""
        anomaly_context: dict[str, Any] = {}
        try:
            from baldur.factory.strategies import get_best_anomaly_detector
            from baldur.utils.time import utc_now

            detector = get_best_anomaly_detector()
            if detector is None:
                return anomaly_context

            detected: dict[str, dict[str, Any]] = {}
            for metric_name, value in metrics.items():
                if not isinstance(value, (int, float)):
                    continue
                try:
                    is_anomalous, score = detector.detect(value)
                    if is_anomalous:
                        # score is in [-1, 0]; confidence = abs(score)
                        detected[metric_name] = {
                            "score": score,
                            "confidence": min(1.0, abs(score)),
                            "value": value,
                        }
                except Exception:
                    continue

            if detected:
                anomaly_context = {
                    "detected": detected,
                    "timestamp": utc_now().isoformat(),
                }
        except Exception:
            logger.debug("recommendation.anomaly_detection_failed", exc_info=True)
        return anomaly_context

    def _analyze_forecast(  # noqa: C901
        self,
        metrics: dict[str, float],
        ml_context: dict[str, Any],
        bounds: dict[str, tuple[float, float]],
        current_values: dict[str, float],
    ) -> list[RecommendationItem]:
        """Generate ML_FORECAST items from ARIMA/HoltLinear forecaster."""
        items: list[RecommendationItem] = []
        try:
            from baldur.factory.strategies import get_best_forecaster

            forecaster = get_best_forecaster()
            if forecaster is None:
                return items

            objective_metrics = ml_context.get("objective_metrics", [])
            prediction_steps = ml_context.get("prediction_steps", 3)

            for metric_name in objective_metrics:
                try:
                    predicted = forecaster.predict(steps_ahead=prediction_steps)
                    confidence = forecaster.get_confidence()
                    if predicted is None or confidence < self._min_confidence:
                        continue

                    current_metric = metrics.get(metric_name, 0)
                    trigger_reason = None

                    # Phase 1: SLO-based trigger
                    slo = self._find_slo_for_metric(metric_name)
                    if slo and self._will_violate_slo(slo, predicted):
                        trigger_reason = (
                            f"SLO violation predicted: {metric_name}={predicted:.4f} "
                            f"(target={slo.target})"
                        )

                    # Phase 2: Degradation fallback
                    if trigger_reason is None and abs(current_metric) > 1e-10:
                        degradation = abs(predicted - current_metric) / abs(
                            current_metric
                        )
                        is_worsening = self._is_metric_worsening(
                            metric_name, current_metric, predicted
                        )
                        if is_worsening and confidence * degradation > 0.15:
                            trigger_reason = f"Predicted {degradation:.0%} degradation in {metric_name}"

                    if trigger_reason and metric_name in METRIC_TO_PARAM:
                        param = METRIC_TO_PARAM[metric_name]
                        current_param = current_values.get(param, 0.0)
                        if current_param == 0.0:
                            continue
                        intensity = min(
                            0.15,
                            abs(predicted - current_metric)
                            / max(abs(current_metric), 1e-10),
                        )
                        items.append(
                            RecommendationItem(
                                parameter=param,
                                current_value=current_param,
                                recommended_value=current_param * (1 + intensity),
                                source=RecommendationSource.ML_FORECAST,
                                confidence=confidence,
                                expected_improvement=intensity,
                                reason=trigger_reason,
                                priority=AdjustmentPriority.MEDIUM,
                                metric_evidence={
                                    "metric": metric_name,
                                    "current": current_metric,
                                    "predicted": predicted,
                                    "slo_target": slo.target if slo else None,
                                },
                            )
                        )
                except Exception:
                    logger.debug(
                        "recommendation.forecast_metric_failed",
                        extra={"metric": metric_name},
                        exc_info=True,
                    )
        except Exception:
            logger.warning("recommendation.forecast_analysis_failed", exc_info=True)
        return items

    def step_propagate(
        self,
        items: list[RecommendationItem],
        current_values: dict[str, float],
    ) -> list[RecommendationItem]:
        """Step 3: Dependency propagation via DependencyGraph."""
        if not self._dependency_graph or not items:
            return items

        cascade_items: list[RecommendationItem] = []
        for item in items:
            try:
                current_val = float(current_values.get(item.parameter, 0.0))
                changes: dict[str, tuple[float, float]] = {
                    item.parameter: (current_val, float(item.recommended_value))
                }
                propagated = self._dependency_graph.propagate(changes, current_values)
                for param, new_val in propagated.items():
                    if param == item.parameter:
                        continue
                    current = current_values.get(param, 0.0)
                    if current == 0.0:
                        continue
                    cascade_items.append(
                        RecommendationItem(
                            parameter=param,
                            current_value=current,
                            recommended_value=new_val,
                            source=RecommendationSource.DEPENDENCY_CASCADE,
                            confidence=item.confidence * 0.8,
                            expected_improvement=abs(new_val - current)
                            / max(current, 1e-10),
                            reason=f"Cascade from {item.parameter} change",
                            priority=AdjustmentPriority.LOW,
                            is_cascade=True,
                        )
                    )
            except Exception:
                logger.debug(
                    "recommendation.cascade_propagated",
                    extra={"parameter": item.parameter},
                    exc_info=True,
                )

        logger.debug(
            "recommendation.cascade_propagated",
            extra={"original": len(items), "cascade": len(cascade_items)},
        )
        return items + cascade_items

    def step_merge(  # noqa: C901
        self,
        items: list[RecommendationItem],
        anomaly_context: dict[str, Any] | None = None,
    ) -> list[RecommendationItem]:
        """Step 4: Merge, deduplicate, apply anomaly boost, sort.

        Rules:
        - Same parameter from multiple sources → keep highest confidence
        - Conflicting directions → ML over rule-based in ml_primary mode
        - Apply anomaly confidence boost
        - Apply max_changes_per_cycle limit
        - Sort by priority (CRITICAL first)
        """
        if not items:
            return items

        # Anomaly confidence boost
        if anomaly_context and anomaly_context.get("detected"):
            for item in items:
                for metric, info in anomaly_context["detected"].items():
                    if metric in item.metric_evidence:
                        boost = info["confidence"] * 0.15
                        item.confidence = min(1.0, item.confidence + boost)
                        item.metric_evidence["anomaly_detected"] = metric

        # Deduplicate: keep highest confidence per parameter
        best_per_param: dict[str, RecommendationItem] = {}
        for item in items:
            existing = best_per_param.get(item.parameter)
            if existing is None:
                best_per_param[item.parameter] = item
            elif (
                self._mode == "ml_primary"
                and item.source != RecommendationSource.RULE_BASED
            ):
                # ML items win in ml_primary mode
                if (
                    existing.source == RecommendationSource.RULE_BASED
                    or item.confidence > existing.confidence
                ):
                    best_per_param[item.parameter] = item
            elif item.confidence > existing.confidence:
                best_per_param[item.parameter] = item

        merged = list(best_per_param.values())

        # Sort by priority (CRITICAL > HIGH > MEDIUM > LOW)
        priority_order = {
            AdjustmentPriority.CRITICAL: 4,
            AdjustmentPriority.HIGH: 3,
            AdjustmentPriority.MEDIUM: 2,
            AdjustmentPriority.LOW: 1,
        }
        merged.sort(key=lambda i: priority_order.get(i.priority, 0), reverse=True)

        # Limit changes per cycle
        merged = merged[: self._max_changes]

        logger.debug(
            "recommendation.items_merged",
            extra={"input": len(items), "output": len(merged)},
        )
        return merged

    def run(
        self,
        metrics: dict[str, float],
        ml_context: dict[str, Any],
        prediction_context: dict[str, Any] | None = None,
    ) -> tuple[list[RecommendationItem], dict[str, Any]]:
        """Execute all 4 steps in sequence.

        Returns:
            (items, anomaly_context): final recommendation items + anomaly metadata.
        """
        # Step 1: Rule-based
        rule_items = self.step_analyze_rules(metrics, prediction_context)

        # Step 2: ML-based
        ml_items, anomaly_context = self.step_analyze_ml(metrics, ml_context)

        all_items = rule_items + ml_items

        # Step 3: Dependency propagation
        current_values = ml_context.get("current_values", {})
        all_items = self.step_propagate(all_items, current_values)

        # Step 4: Merge and deduplicate
        final_items = self.step_merge(all_items, anomaly_context)

        return final_items, anomaly_context

    # --- Helpers ---

    @staticmethod
    def _find_slo_for_metric(metric_name: str) -> Any:
        """Find SLO definition for a given metric name."""
        try:
            from baldur.slo import SLI

            # SLOManager was retired; this lookup is best-effort and
            # currently has no central enumerator. Return None so callers
            # fall through to the no-SLO branch.
            _ = SLI  # placeholder until a per-tenant SLO registry exists
        except Exception:
            pass
        return None

    @staticmethod
    def _will_violate_slo(slo: Any, predicted: float) -> bool:
        """Check if predicted value would violate SLO target."""
        try:
            from baldur.slo import SLI

            if slo.sli in (SLI.ERROR_RATE,):
                # Lower is better — violation if predicted exceeds (1 - target)
                return predicted > (1 - slo.target)
            # Higher is better — violation if predicted falls below target
            return predicted < slo.target
        except Exception:
            return False

    @staticmethod
    def _is_metric_worsening(
        metric_name: str,
        current: float,
        predicted: float,
    ) -> bool:
        """Check if the predicted value represents degradation."""
        if metric_name in LOWER_IS_BETTER:
            return predicted > current  # Increase = worse
        return predicted < current  # Decrease = worse
