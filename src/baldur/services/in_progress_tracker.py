"""
In-Progress Operation Tracker.

Tracks operations in progress when configuration is being applied,
so that graceful configuration changes can be applied safely.

Usage:
    tracker = get_in_progress_tracker()
    count = tracker.count_in_progress("runtime_config")
"""

from __future__ import annotations

import threading
from collections import defaultdict

import structlog

logger = structlog.get_logger()


class InProgressTracker:
    """
    Tracker for operations in progress.

    Tracks the count of currently-in-progress operations per config_type.
    Used by ConfigApplyService when applying graceful configuration changes.
    """

    _instance: InProgressTracker | None = None
    _initialized: bool = False

    def __new__(cls) -> InProgressTracker:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._lock = threading.Lock()
        self._counters: dict[str, int] = defaultdict(int)

    def count_in_progress(self, config_type: str) -> int:
        """
        Return the count of in-progress operations for the given config_type.

        Args:
            config_type: Configuration type (e.g., "runtime_config", "feature_flag")

        Returns:
            Number of in-progress operations
        """
        with self._lock:
            return self._counters.get(config_type, 0)

    def increment(self, config_type: str) -> int:
        """Increment the in-progress count by 1."""
        with self._lock:
            self._counters[config_type] += 1
            return self._counters[config_type]

    def decrement(self, config_type: str) -> int:
        """Decrement the in-progress count by 1."""
        with self._lock:
            self._counters[config_type] = max(0, self._counters[config_type] - 1)
            return self._counters[config_type]

    def reset(self, config_type: str | None = None) -> None:
        """Reset the counter."""
        with self._lock:
            if config_type is None:
                self._counters.clear()
            else:
                self._counters.pop(config_type, None)


# =============================================================================
# Factory
# =============================================================================

from baldur.utils.singleton import make_singleton_factory


def _cleanup_in_progress_tracker(tracker: InProgressTracker) -> None:
    """Cleanup: clear class-level singleton."""
    InProgressTracker._instance = None


get_in_progress_tracker, configure_in_progress_tracker, reset_in_progress_tracker = (
    make_singleton_factory(
        "in_progress_tracker",
        InProgressTracker,
        cleanup_fn=_cleanup_in_progress_tracker,
    )
)

__all__ = [
    "InProgressTracker",
    "get_in_progress_tracker",
    "configure_in_progress_tracker",
    "reset_in_progress_tracker",
]
