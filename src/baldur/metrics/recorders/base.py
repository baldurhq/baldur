"""
Base metric recorder — shared utilities for all domain recorders.

Provides metric name prefixing, domain cardinality guard integration,
synthetic traffic label resolution, and value clamping.
"""

from __future__ import annotations

from baldur.metrics.safe_gauge import clamp_non_negative, clamp_percentage

__all__ = ["BaseMetricRecorder"]


class BaseMetricRecorder:
    """Base class for domain-specific metric recorders.

    Each subclass defines its own metrics in __init__ using
    get_or_create_* from metrics.registry.
    The baldur_ prefix is standard per D13.

    All implementations must be thread-safe for concurrent metric recording.
    """

    PREFIX = "baldur"

    def _resolve_domain(self, domain: str) -> str:
        """Apply domain cardinality guard."""
        from baldur.metrics.registry import resolve_domain_label

        return resolve_domain_label(domain)

    def _get_synthetic_label(self) -> str:
        """Return synthetic traffic label value (D14)."""
        from baldur.core.test_mode_context import TestModeContext

        return TestModeContext.get_synthetic_label_value()

    @staticmethod
    def _clamp_non_negative(value: float, metric_name: str = "") -> float:
        """Clamp value to >= 0."""
        return clamp_non_negative(value, metric_name)

    @staticmethod
    def _clamp_percentage(value: float, metric_name: str = "") -> float:
        """Clamp value to 0-100 range."""
        return clamp_percentage(value, metric_name)
