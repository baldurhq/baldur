"""Recommendation pipeline metric definitions and recording."""

from __future__ import annotations

from baldur.metrics.recorders.base import BaseMetricRecorder

__all__ = ["RecommendationMetricRecorder"]


class RecommendationMetricRecorder(BaseMetricRecorder):
    """Recommendation pipeline metrics (6 metrics)."""

    def __init__(self) -> None:
        from baldur.metrics.registry import (
            get_or_create_counter,
            get_or_create_gauge,
            get_or_create_histogram,
        )

        self._generated_total = get_or_create_counter(
            f"{self.PREFIX}_recommendation_generated_total",
            "Total recommendation plans generated",
            ["mode", "source"],
        )
        self._applied_total = get_or_create_counter(
            f"{self.PREFIX}_recommendation_applied_total",
            "Total recommendation plans applied",
            ["mode", "result"],
        )
        self._confidence = get_or_create_histogram(
            f"{self.PREFIX}_recommendation_confidence",
            "Confidence distribution of recommendations",
            ["source"],
        )
        self._pipeline_duration_seconds = get_or_create_histogram(
            f"{self.PREFIX}_recommendation_pipeline_duration_seconds",
            "Time to generate a recommendation plan",
            ["step"],
        )
        self._ml_model_ready = get_or_create_gauge(
            f"{self.PREFIX}_ml_model_ready",
            "Whether ML model is ready for inference",
            ["model_type", "model_name"],
        )
        self._ml_history_grouped_parameters = get_or_create_gauge(
            f"{self.PREFIX}_recommendation_ml_history_grouped_parameters",
            "Average number of parameters per ML history observation group",
            [],
        )

    def record_generated(self, mode: str, source: str) -> None:
        try:
            self._generated_total.labels(mode=mode, source=source).inc()
        except Exception:
            pass

    def record_applied(self, mode: str, result: str) -> None:
        try:
            self._applied_total.labels(mode=mode, result=result).inc()
        except Exception:
            pass

    def observe_confidence(self, source: str, value: float) -> None:
        try:
            self._confidence.labels(source=source).observe(value)
        except Exception:
            pass

    def observe_pipeline_duration(self, step: str, seconds: float) -> None:
        try:
            self._pipeline_duration_seconds.labels(step=step).observe(seconds)
        except Exception:
            pass

    def set_ml_model_ready(self, model_type: str, model_name: str, ready: bool) -> None:
        try:
            self._ml_model_ready.labels(
                model_type=model_type, model_name=model_name
            ).set(1.0 if ready else 0.0)
        except Exception:
            pass

    def set_grouped_parameters(self, avg_count: float) -> None:
        try:
            self._ml_history_grouped_parameters.labels().set(avg_count)
        except Exception:
            pass
