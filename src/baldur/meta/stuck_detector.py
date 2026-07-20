"""
Stuck Detector - zero-variance based stuck detection.

When a metric's variance stays near zero while the error rate is high, the
system is considered logically stuck.

Zero-variance stuck condition:
- A metric X holds σ²(X) ≈ 0 over a period (no movement)
- and the error rate is at or above the threshold

Examples:
- DLQ pending_count pinned at 1000 while processing keeps failing
- A circuit breaker held in the OPEN state
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()


@dataclass
class MetricSample:
    """A metric sample."""

    value: float
    """Metric value."""

    timestamp: float
    """Collection time (Unix timestamp)."""

    error: bool = False
    """Whether this sample was in an error state."""


@dataclass
class MetricWindow:
    """
    Sliding-window metric container.

    Retains the most recent N samples and computes variance and error rate.
    """

    samples: deque[MetricSample] = field(default_factory=deque)
    """Sample queue."""

    max_size: int = 20
    """Maximum number of samples."""

    def add(self, value: float, error: bool = False) -> None:
        """
        Add a sample.

        Args:
            value: metric value
            error: whether the sample is in an error state
        """
        self.samples.append(
            MetricSample(value=value, timestamp=time.time(), error=error)
        )
        while len(self.samples) > self.max_size:
            self.samples.popleft()

    def variance(self) -> float:
        """
        Compute the variance.

        Returns:
            Variance of the samples (infinity when there are too few)
        """
        if len(self.samples) < 2:
            return float("inf")  # too few samples: return infinity

        values = [s.value for s in self.samples]
        n = len(values)
        mean = sum(values) / n
        return sum((x - mean) ** 2 for x in values) / n

    def error_rate(self) -> float:
        """
        Compute the error rate.

        Returns:
            Fraction of error samples (0.0 ~ 1.0)
        """
        if not self.samples:
            return 0.0
        error_count = sum(1 for s in self.samples if s.error)
        return error_count / len(self.samples)

    def mean(self) -> float:
        """
        Compute the mean.

        Returns:
            Mean of the samples (0 when there are none)
        """
        if not self.samples:
            return 0.0
        return sum(s.value for s in self.samples) / len(self.samples)

    def is_stuck(
        self,
        variance_threshold: float = 0.001,
        error_rate_threshold: float = 0.5,
    ) -> bool:
        """
        Decide whether the window is stuck.

        Condition: variance ≈ 0 AND error rate > threshold

        Args:
            variance_threshold: variance threshold (default 0.001)
            error_rate_threshold: error-rate threshold (default 50%)

        Returns:
            Whether the window is stuck
        """
        if len(self.samples) < 5:
            return False  # minimum sample count not reached

        var = self.variance()
        err_rate = self.error_rate()

        # Very low variance with a high error rate means stuck
        return var < variance_threshold and err_rate > error_rate_threshold

    def clear(self) -> None:
        """Clear the samples."""
        self.samples.clear()


@dataclass
class StuckDetectionResult:
    """Stuck detection result."""

    component: str
    """Component name."""

    is_stuck: bool
    """Whether the component is stuck."""

    variance: float
    """Current variance."""

    error_rate: float
    """Current error rate."""

    sample_count: int
    """Number of samples."""

    duration_seconds: float
    """Elapsed time since the first sample."""

    mean_value: float = 0.0
    """Mean value."""

    details: dict[str, Any] = field(default_factory=dict)
    """Additional details."""


class StuckDetector:
    """
    Stuck detector.

    Tracks each component's metric and detects the zero-variance state.

    Example:
        detector = StuckDetector()

        # Record metrics (periodically)
        detector.record("dlq", pending_count=100, error=False)
        detector.record("dlq", pending_count=100, error=True)

        # Check for stuck
        result = detector.check("dlq")
        if result.is_stuck:
            trigger_recovery()
    """

    def __init__(
        self,
        window_size: int = 20,
        variance_threshold: float = 0.001,
        error_rate_threshold: float = 0.5,
    ):
        """
        Initialize.

        Args:
            window_size: sample window size
            variance_threshold: variance threshold for the stuck verdict
            error_rate_threshold: error-rate threshold for the stuck verdict
        """
        self._window_size = window_size
        self._variance_threshold = variance_threshold
        self._error_rate_threshold = error_rate_threshold

        self._windows: dict[str, MetricWindow] = {}
        self._first_sample_time: dict[str, float] = {}
        self._lock = threading.RLock()

    def record(
        self,
        component: str,
        value: float,
        error: bool = False,
    ) -> None:
        """
        Record a metric.

        Args:
            component: component name
            value: metric value (e.g. pending_count, queue_size)
            error: whether the sample is in an error state
        """
        with self._lock:
            if component not in self._windows:
                self._windows[component] = MetricWindow(
                    samples=deque(maxlen=self._window_size),
                    max_size=self._window_size,
                )
                self._first_sample_time[component] = time.time()

            self._windows[component].add(value=value, error=error)

    def check(self, component: str) -> StuckDetectionResult:
        """
        Check whether a component is stuck.

        Args:
            component: component name

        Returns:
            StuckDetectionResult
        """
        with self._lock:
            if component not in self._windows:
                return StuckDetectionResult(
                    component=component,
                    is_stuck=False,
                    variance=float("inf"),
                    error_rate=0.0,
                    sample_count=0,
                    duration_seconds=0.0,
                    mean_value=0.0,
                )

            window = self._windows[component]
            first_time = self._first_sample_time.get(component, time.time())
            duration = time.time() - first_time

            variance_stuck = window.is_stuck(
                variance_threshold=self._variance_threshold,
                error_rate_threshold=self._error_rate_threshold,
            )

            # Time-based stuck detection: component stuck if duration exceeds
            # stuck_threshold_seconds regardless of variance
            time_stuck = False
            try:
                from baldur.meta.config import get_meta_watchdog_settings

                threshold = get_meta_watchdog_settings().stuck_threshold_seconds
                time_stuck = (
                    duration >= threshold
                    and window.error_rate() >= self._error_rate_threshold
                )
            except Exception:
                pass

            is_stuck = variance_stuck or time_stuck

            return StuckDetectionResult(
                component=component,
                is_stuck=is_stuck,
                variance=window.variance(),
                error_rate=window.error_rate(),
                sample_count=len(window.samples),
                duration_seconds=duration,
                mean_value=window.mean(),
                details={
                    "variance_threshold": self._variance_threshold,
                    "error_rate_threshold": self._error_rate_threshold,
                },
            )

    def check_all(self) -> dict[str, StuckDetectionResult]:
        """
        Check every component for stuck.

        Returns:
            Stuck detection result per component
        """
        with self._lock:
            return {comp: self.check(comp) for comp in self._windows}

    def get_stuck_components(self) -> list[str]:
        """
        Return the components currently stuck.

        Returns:
            Names of the stuck components
        """
        results = self.check_all()
        return [comp for comp, result in results.items() if result.is_stuck]

    def clear(self, component: str | None = None) -> None:
        """
        Clear recorded metrics.

        Args:
            component: clear only this component (all components when None)
        """
        with self._lock:
            if component:
                self._windows.pop(component, None)
                self._first_sample_time.pop(component, None)
            else:
                self._windows.clear()
                self._first_sample_time.clear()

    def get_component_names(self) -> list[str]:
        """
        Return the registered component names.

        Returns:
            Component names
        """
        with self._lock:
            return list(self._windows.keys())


# =============================================================================
# Singleton
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

get_stuck_detector, configure_stuck_detector, reset_stuck_detector = (
    make_singleton_factory("stuck_detector", StuckDetector)
)
