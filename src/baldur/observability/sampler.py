"""
Emergency Level Adaptive Sampler for OpenTelemetry.

Dynamically adjusts sampling ratio based on system emergency level.
Samples 100% during emergencies.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from opentelemetry.context import Context
    from opentelemetry.sdk.trace.sampling import SamplingResult
    from opentelemetry.trace import Link, SpanKind
    from opentelemetry.util.types import Attributes

logger = structlog.get_logger()


# Sampling ratios per emergency escalation level. NORMAL (level 0) is
# deliberately absent so `_get_current_ratio` falls through to `base_ratio`
# (honoring OTEL_TRACES_SAMPLER_ARG); only escalation levels override it.
EMERGENCY_LEVEL_SAMPLING_RATIOS: dict[int, float] = {
    1: 0.10,  # LEVEL_1: 10% sampling
    2: 1.00,  # LEVEL_2: 100% sampling
    3: 1.00,  # LEVEL_3: 100% sampling
}


def _get_current_emergency_level() -> int:
    """
    Get the current emergency level from the emergency mode service.

    Returns:
        int: Emergency level (0-3). Defaults to 0 (NORMAL) if unavailable.
    """
    try:
        from baldur.factory.registry import ProviderRegistry

        manager = ProviderRegistry.emergency_manager.safe_get()
        if manager is None:
            return 0
        return manager.get_current_level().severity
    except Exception:
        # Default to NORMAL if emergency mode is unavailable
        return 0


class EmergencyLevelAdaptiveSampler:
    """
    Adaptive sampler that adjusts ratio based on EmergencyLevel.

    Sampling behavior:
    - NORMAL (level 0): base_ratio (default 1%)
    - LEVEL_1: 10%
    - LEVEL_2+: 100% (full sampling during emergencies)

    This sampler implements the OpenTelemetry Sampler interface.
    """

    def __init__(
        self,
        base_ratio: float = 0.01,
    ) -> None:
        """
        Initialize the adaptive sampler.

        Args:
            base_ratio: Base sampling ratio during normal operation (0.0-1.0)
        """
        self._base_ratio = max(0.0, min(1.0, base_ratio))

        logger.debug(
            "otel_sampler.initialized",
            base_ratio_percent=self._base_ratio * 100,
        )

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes | None = None,
        links: Sequence[Link] | None = None,
    ) -> SamplingResult:
        """
        Determine whether a span should be sampled.

        Implements OpenTelemetry Sampler interface.

        Args:
            parent_context: Parent span context
            trace_id: 128-bit trace identifier
            name: Span name
            kind: Span kind (optional)
            attributes: Span attributes (optional)
            links: Span links (optional)

        Returns:
            SamplingResult indicating whether to sample
        """
        from opentelemetry.sdk.trace.sampling import Decision, SamplingResult
        from opentelemetry.trace import get_current_span

        # Check parent sampling decision (parent-based behavior)
        if parent_context is not None:
            parent_span = get_current_span(parent_context)
            if parent_span is not None:
                parent_span_context = parent_span.get_span_context()
                if (
                    parent_span_context is not None
                    and parent_span_context.is_valid
                    and parent_span_context.trace_flags.sampled
                ):
                    # Parent is sampled, sample this span too
                    return SamplingResult(
                        decision=Decision.RECORD_AND_SAMPLE,
                        attributes=attributes,
                    )

        # Get current sampling ratio based on emergency level
        ratio = self._get_current_ratio()

        # Make sampling decision based on trace_id for consistency
        # (same trace_id always produces same decision)
        threshold = int(ratio * (2**64))
        if trace_id & 0xFFFFFFFFFFFFFFFF < threshold:
            return SamplingResult(
                decision=Decision.RECORD_AND_SAMPLE,
                attributes=attributes,
            )

        return SamplingResult(
            decision=Decision.DROP,
            attributes=attributes,
        )

    def _get_current_ratio(self) -> float:
        """
        Get the current sampling ratio based on emergency level.

        Returns:
            float: Sampling ratio (0.0-1.0)
        """
        level = _get_current_emergency_level()
        return EMERGENCY_LEVEL_SAMPLING_RATIOS.get(level, self._base_ratio)

    def get_description(self) -> str:
        """
        Get a human-readable description of this sampler.

        Returns:
            str: Description including current ratio
        """
        ratio = self._get_current_ratio()
        level = _get_current_emergency_level()
        return (
            f"EmergencyLevelAdaptiveSampler("
            f"level={level}, ratio={ratio:.2%}, "
            f"base_ratio={self._base_ratio:.2%})"
        )


class StaticRatioSampler:
    """
    Simple static ratio sampler for testing without EmergencyLevel dependency.

    Samples traces at a fixed ratio.
    """

    def __init__(self, ratio: float = 0.01) -> None:
        """
        Initialize with a fixed sampling ratio.

        Args:
            ratio: Sampling ratio (0.0-1.0)
        """
        self._ratio = max(0.0, min(1.0, ratio))

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes | None = None,
        links: Sequence[Link] | None = None,
    ) -> SamplingResult:
        """Determine whether to sample based on fixed ratio."""
        from opentelemetry.sdk.trace.sampling import Decision, SamplingResult

        threshold = int(self._ratio * (2**64))
        if trace_id & 0xFFFFFFFFFFFFFFFF < threshold:
            return SamplingResult(
                decision=Decision.RECORD_AND_SAMPLE,
                attributes=attributes,
            )

        return SamplingResult(
            decision=Decision.DROP,
            attributes=attributes,
        )

    def get_description(self) -> str:
        """Get description of this sampler."""
        return f"StaticRatioSampler(ratio={self._ratio:.2%})"
