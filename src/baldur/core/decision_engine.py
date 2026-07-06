"""
Decision Engine - metric-based adjustment decisions

Analyzes real-time metrics and proposes parameter adjustments.

A Netflix Hystrix / Google Autopilot style autonomous adjustment engine

Settings can be overridden via env vars through DecisionEngineSettings:
- BALDUR_DECISION_ENGINE_MIN_CHANGE_RATIO
- BALDUR_DECISION_ENGINE_CONFIDENCE_* (confidence mapping)
- BALDUR_DECISION_ENGINE_STABILITY_* (stability factors)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol

import structlog

from baldur.settings.decision_engine import get_decision_engine_settings
from baldur.utils.time import utc_now

logger = structlog.get_logger()


class AdjustmentPriority(str, Enum):
    """Adjustment priority"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class AdjustmentDecision:
    """Adjustment decision"""

    parameter: str
    current_value: float
    suggested_value: float
    reason: str
    confidence: float  # 0.0 ~ 1.0
    priority: AdjustmentPriority = AdjustmentPriority.MEDIUM
    metric_snapshot: dict[str, float] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: utc_now())


@dataclass
class AdjustmentRule:
    """Adjustment rule"""

    parameter: str
    metric: str
    condition: Callable[
        [float, float], bool
    ]  # (current_value, metric_value) -> should_adjust
    adjustment: Callable[
        [float, float], float
    ]  # (current_value, metric_value) -> new_value
    reason: str
    priority: AdjustmentPriority = AdjustmentPriority.MEDIUM
    min_confidence: float = 0.5


class ConfigProvider(Protocol):
    """Config provider protocol"""

    def get(self, key: str, default: Any = None) -> Any:
        """Look up a config value"""
        ...


class DecisionEngine:
    """
    Adjustment decision engine

    Analyzes metric patterns and proposes parameter adjustments

    Default rules:
    - timeout_ms: raise if P99 latency is at or above 80% of the timeout
    - retry_count: increase if the retry exhaustion rate is 10% or more
    - circuit_breaker_threshold: raise if the error rate approaches the CB threshold
    - jitter_range: widen if the retry collision rate is high
    """

    # Default adjustment rules
    DEFAULT_RULES: list[AdjustmentRule] = [
        AdjustmentRule(
            parameter="timeout_ms",
            metric="p99_latency_ms",
            condition=lambda current, metric: metric > current * 0.8,
            adjustment=lambda current, metric: min(current * 1.2, 10000),
            reason="P99 latency at or above 80% of the timeout → raise the timeout",
            priority=AdjustmentPriority.MEDIUM,
        ),
        AdjustmentRule(
            parameter="retry_count",
            metric="retry_exhausted_rate",
            condition=lambda current, metric: metric > 0.1,  # >=10% exhausted
            adjustment=lambda current, metric: min(current + 1, 5),
            reason="Retry exhaustion rate 10% or more → increase the retry count",
            priority=AdjustmentPriority.MEDIUM,
        ),
        AdjustmentRule(
            parameter="circuit_breaker_threshold",
            metric="error_rate",
            condition=lambda current, metric: metric > current * 0.9,
            adjustment=lambda current, metric: min(current * 1.1, 0.8),
            reason="Error rate approaching the CB threshold → raise the threshold",
            priority=AdjustmentPriority.HIGH,
        ),
        AdjustmentRule(
            parameter="jitter_range",
            metric="retry_collision_rate",
            condition=lambda current, metric: metric > 0.05,  # collision 5% or more
            adjustment=lambda current, metric: current * 1.5,
            reason="High retry collision rate → widen the jitter range",
            priority=AdjustmentPriority.LOW,
        ),
        AdjustmentRule(
            parameter="rate_limit_rps",
            metric="throttle_rate",
            condition=lambda current, metric: (
                metric < 0.01 and current < 5000
            ),  # almost no throttling
            adjustment=lambda current, metric: current * 1.1,
            reason="Low throttling occurrence → Rate Limit can be raised",
            priority=AdjustmentPriority.LOW,
        ),
    ]

    @property
    def MIN_CHANGE_RATIO(self) -> float:
        """For a change to be meaningful, at least this ratio of change is required (default 5%)"""
        return get_decision_engine_settings().min_change_ratio

    def __init__(
        self,
        config_provider: ConfigProvider,
        custom_rules: list[AdjustmentRule] | None = None,
        enabled: bool = True,
    ):
        self.config_provider = config_provider
        self.rules = list(self.DEFAULT_RULES)
        if custom_rules:
            self.rules.extend(custom_rules)
        self.enabled = enabled

        # Analysis history
        self._history: list[dict[str, Any]] = []

        logger.info(
            "decision_engine.initialized_rules",
            rules_count=len(self.rules),
        )

    def analyze(
        self,
        metrics: dict[str, float],
        *,
        prediction_context: dict[str, Any] | None = None,
    ) -> list[AdjustmentDecision]:
        """
        Analyze metrics and make adjustment decisions

        Args:
            metrics: collected metrics (error_rate, p99_latency_ms, etc.)
            prediction_context: prediction context (PredictiveForecaster)
                - trend_slope: metric trend slope
                - prediction_confidence: prediction confidence

        Returns:
            List of adjustment decisions
        """
        if not self.enabled:
            return []

        decisions = []

        for rule in self.rules:
            decision = self._evaluate_rule(rule, metrics, prediction_context)
            if decision:
                decisions.append(decision)

        # Sort by priority
        decisions.sort(key=lambda d: self._priority_order(d.priority), reverse=True)

        # Save analysis history
        self._record_analysis(metrics, decisions)

        return decisions

    def _evaluate_rule(
        self,
        rule: AdjustmentRule,
        metrics: dict[str, float],
        prediction_context: dict[str, Any] | None = None,
    ) -> AdjustmentDecision | None:
        """Evaluate a single rule"""
        metric_value = metrics.get(rule.metric)

        if metric_value is None:
            return None

        try:
            current_value = self.config_provider.get(rule.parameter)
            if current_value is None:
                logger.debug(
                    "decision_engine.no_current_value",
                    rule=rule.parameter,
                )
                return None

            current_value = float(current_value)
        except (TypeError, ValueError) as e:
            logger.warning(
                "decision_engine.invalid_current_value",
                rule=rule.parameter,
                error=e,
            )
            return None

        # Evaluate the condition
        try:
            should_adjust = rule.condition(current_value, metric_value)
        except Exception as e:
            logger.warning(
                "decision_engine.condition_evaluation_failed",
                error=e,
            )
            return None

        if not should_adjust:
            return None

        # Compute the new value
        try:
            suggested_value = rule.adjustment(current_value, metric_value)
        except Exception as e:
            logger.warning(
                "decision_engine.adjustment_calculation_failed",
                error=e,
            )
            return None

        # Check whether the change is meaningful
        if current_value > 0:
            change_ratio = abs(suggested_value - current_value) / current_value
            if change_ratio < self.MIN_CHANGE_RATIO:
                return None

        # Compute confidence
        confidence = self._calculate_confidence(metrics, rule, prediction_context)

        if confidence < rule.min_confidence:
            logger.debug(
                "decision_engine.low_confidence",
                confidence=confidence,
                rule=rule.parameter,
            )
            return None

        return AdjustmentDecision(
            parameter=rule.parameter,
            current_value=current_value,
            suggested_value=suggested_value,
            reason=rule.reason,
            confidence=confidence,
            priority=rule.priority,
            metric_snapshot=metrics.copy(),
        )

    def _calculate_confidence(
        self,
        metrics: dict[str, float],
        rule: AdjustmentRule,
        prediction_context: dict[str, float] | None = None,
    ) -> float:
        """
        Compute confidence

        Considers sample count, metric variability, etc.
        Loads thresholds and factors from DecisionEngineSettings.

        Phase 3 (238_PREDICTIVE_ANOMALY_FORECASTER):
        If prediction_context is provided, reflect the prediction trend slope in the confidence.
        If the trend slope agrees with the rule direction, boost the confidence;
        if it is the opposite direction, lower the stability factor.

        Args:
            metrics: collected metrics
            rule: the rule being evaluated
            prediction_context: prediction context (optional)
                - "trend_slope": HoltLinear trend slope
                - "prediction_confidence": prediction confidence (0~1)
        """
        settings = get_decision_engine_settings()

        # Base confidence (sample-count based - looked up from settings)
        sample_count = metrics.get("sample_count", 10)
        sample_confidence = settings.get_sample_confidence(int(sample_count))

        # The lower the metric variability, the higher the confidence (looked up from settings)
        variance = metrics.get(f"{rule.metric}_variance", 0)
        mean = metrics.get(rule.metric, 1)
        if mean > 0 and variance > 0:
            cv = (variance**0.5) / mean  # Coefficient of variation
            stability_factor = settings.get_stability_factor(cv)
        else:
            # Keep the default if there is no variability information
            stability_factor = settings.stability_factor_stable

        # Phase 3: reflect the prediction context
        if prediction_context:
            trend_slope = prediction_context.get("trend_slope", 0.0)
            pred_confidence = prediction_context.get("prediction_confidence", 0.0)

            if pred_confidence > 0 and trend_slope != 0:
                # Adjust the stability factor by the trend slope and prediction confidence
                # A positive slope (upward trend) slightly boosts the stability_factor
                # Adjustment range: ±10% (proportional to pred_confidence)
                trend_boost = min(0.1, abs(trend_slope) * 0.01) * pred_confidence
                stability_factor = min(1.0, stability_factor + trend_boost)

        confidence = sample_confidence * stability_factor
        return min(1.0, max(0.0, confidence))

    def _priority_order(self, priority: AdjustmentPriority) -> int:
        """Numeric conversion for priority sorting"""
        order = {
            AdjustmentPriority.LOW: 1,
            AdjustmentPriority.MEDIUM: 2,
            AdjustmentPriority.HIGH: 3,
            AdjustmentPriority.CRITICAL: 4,
        }
        return order.get(priority, 0)

    def _record_analysis(
        self, metrics: dict[str, float], decisions: list[AdjustmentDecision]
    ):
        """Record analysis history"""
        self._history.append(
            {
                "timestamp": utc_now().isoformat(),
                "metrics": metrics,
                "decisions_count": len(decisions),
                "decisions": [
                    {
                        "parameter": d.parameter,
                        "current": d.current_value,
                        "suggested": d.suggested_value,
                        "confidence": d.confidence,
                    }
                    for d in decisions
                ],
            }
        )

        # Settings-based history limit (Phase 2: 238_PREDICTIVE_ANOMALY_FORECASTER)
        max_history = get_decision_engine_settings().max_history
        if len(self._history) > max_history:
            self._history = self._history[-max_history:]

    def add_rule(self, rule: AdjustmentRule) -> None:
        """Add a rule"""
        self.rules.append(rule)
        logger.info(
            "decision_engine.added_rule",
            rule=rule.parameter,
        )

    def remove_rule(self, parameter: str) -> bool:
        """Remove a rule"""
        original_count = len(self.rules)
        self.rules = [r for r in self.rules if r.parameter != parameter]
        removed = len(self.rules) < original_count
        if removed:
            logger.info(
                "decision_engine.removed_rule",
                decision_parameter=parameter,
            )
        return removed

    def get_rules(self) -> list[dict[str, Any]]:
        """Look up the rule list"""
        return [
            {
                "parameter": r.parameter,
                "metric": r.metric,
                "reason": r.reason,
                "priority": r.priority.value,
                "min_confidence": r.min_confidence,
            }
            for r in self.rules
        ]

    def get_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Look up the analysis history"""
        return self._history[-limit:]


__all__ = [
    "DecisionEngine",
    "AdjustmentDecision",
    "AdjustmentRule",
    "AdjustmentPriority",
]
