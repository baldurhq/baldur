"""
Adaptive Replay Manager.

Dynamically adjusts replay batch size based on success rate.
Similar to Netflix Gradient algorithm for rate limiting,
but applied to DLQ replay batch sizing.

Algorithm:
- If failure rate >= threshold (20%): reduce batch size by 20%
- If 3 consecutive perfect batches: increase batch size by 5
- Bounded by min_items and max_items
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import structlog

logger = structlog.get_logger()


@dataclass
class AdaptiveReplayConfig:
    """
    Configuration for Adaptive Replay.

    Loaded from ReplayAutomationConfig via RuntimeConfigManager.
    """

    # Bounds
    min_items: int = 10
    max_items: int = 100
    initial_items: int = 50

    # Adjustment ratios
    decrease_ratio: float = 0.8  # Reduce by 20% on failure
    increase_step: int = 5  # Increase by 5 on success

    # Triggers
    failure_threshold: float = 0.2  # Reduce when failure rate >= 20%
    success_streak_required: int = 3  # Increase after 3 consecutive successes


@dataclass
class BatchHistoryEntry:
    """Single batch result history entry."""

    timestamp: float
    total: int
    success: int
    failures: int
    failure_rate: float
    max_items_at_time: int


class AdaptiveReplayManager:
    """
    Dynamically adjusts replay batch size based on success rate.

    Thread-safe singleton pattern for global use.

    Usage:
        manager = get_adaptive_replay_manager()

        # Get current recommended max_items
        max_items = manager.get_current_max_items()

        # After batch replay
        manager.record_batch_result(total=50, success=48, failures=2)

    Algorithm:
        - Failure rate >= 20%: reduce by 20%
        - 3 consecutive perfect batches: increase by 5
        - Always bounded by [min_items, max_items]
    """

    _instance: AdaptiveReplayManager | None = None
    _initialized: bool = False
    _lock = threading.Lock()

    def __new__(cls) -> AdaptiveReplayManager:
        """Thread-safe singleton creation."""
        if cls._instance is None:
            with cls._lock:
                # Double-checked locking
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialized = False
                    cls._instance = instance
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance for test isolation."""
        cls._instance = None

    def __init__(self) -> None:
        """Initialize manager (only once due to singleton)."""
        if self._initialized:
            return

        with self._lock:
            if self._initialized:
                return

            self._config = AdaptiveReplayConfig()
            self._current_items = self._config.initial_items
            self._success_streak = 0
            self._history: list[BatchHistoryEntry] = []
            self._state_lock = threading.RLock()
            self._initialized = True

            logger.info(
                "adaptive_replay.initialized",
                initial_items=self._config.initial_items,
                min_items=self._config.min_items,
                max_items=self._config.max_items,
            )

    def configure(self, config: AdaptiveReplayConfig) -> None:
        """
        Update configuration.

        Called when RuntimeConfig changes.
        Does not reset current_items - only updates bounds and thresholds.
        """
        with self._state_lock:
            old_config = self._config
            self._config = config

            # Clamp current_items to new bounds
            if self._current_items < config.min_items:
                self._current_items = config.min_items
            elif self._current_items > config.max_items:
                self._current_items = config.max_items

            logger.info(
                "adaptive_replay.config_updated",
                new_min_items=config.min_items,
                old_config=old_config.min_items,
                new_max_items=config.max_items,
                old_max_items=old_config.max_items,
                current_items=self._current_items,
            )

    def get_current_max_items(self) -> int:
        """
        Get current recommended max_items.

        Thread-safe read.
        """
        with self._state_lock:
            return self._current_items

    def record_batch_result(
        self,
        total: int,
        success: int,
        failures: int,
    ) -> None:
        """
        Record batch result and adjust max_items accordingly.

        Args:
            total: Total items processed
            success: Number of successful replays
            failures: Number of failed replays
        """
        if total == 0:
            logger.debug("adaptive_replay.empty_batch_skipping_adjustment")
            return

        failure_rate = failures / total

        with self._state_lock:
            # Record history entry
            entry = BatchHistoryEntry(
                timestamp=time.time(),
                total=total,
                success=success,
                failures=failures,
                failure_rate=failure_rate,
                max_items_at_time=self._current_items,
            )
            self._history.append(entry)

            # Keep only last 100 entries
            if len(self._history) > 100:
                self._history = self._history[-100:]

            # Adjust based on results
            self._adjust_max_items(failure_rate, failures)

    def _adjust_max_items(self, failure_rate: float, failures: int) -> None:
        """
        Internal method to adjust max_items based on failure rate.

        Called with _state_lock held.
        """
        old_items = self._current_items

        if failure_rate >= self._config.failure_threshold:
            # Too many failures → reduce batch size
            new_items = int(self._current_items * self._config.decrease_ratio)
            self._current_items = max(self._config.min_items, new_items)
            self._success_streak = 0

            logger.info(
                "adaptive_replay.high_failure_rate_reduced",
                failure_rate=failure_rate,
                old_items=old_items,
                current_items=self._current_items,
            )

        elif failures == 0:
            # Perfect batch → count toward increase
            self._success_streak += 1

            if self._success_streak >= self._config.success_streak_required:
                new_items = self._current_items + self._config.increase_step
                self._current_items = min(self._config.max_items, new_items)
                self._success_streak = 0

                logger.info(
                    "adaptive_replay.consecutive_successes_increased",
                    success_streak_required=self._config.success_streak_required,
                    old_items=old_items,
                    current_items=self._current_items,
                )
            else:
                logger.debug(
                    "adaptive_replay.perfect_batch_streak",
                    success_streak=self._success_streak,
                )
        else:
            # Some failures but below threshold
            self._success_streak = 0
            logger.debug(
                "adaptive_replay.partial_success_streak_reset",
                failure_rate=failure_rate,
                current_items=self._current_items,
            )

    def get_stats(self) -> dict:
        """
        Get current statistics.

        Returns:
            Dictionary with current state and recent history summary.
        """
        with self._state_lock:
            recent_count = len(self._history)
            avg_failure_rate = 0.0
            if recent_count > 0:
                avg_failure_rate = (
                    sum(h.failure_rate for h in self._history) / recent_count
                )

            return {
                "current_max_items": self._current_items,
                "success_streak": self._success_streak,
                "config": {
                    "min_items": self._config.min_items,
                    "max_items": self._config.max_items,
                    "initial_items": self._config.initial_items,
                    "failure_threshold": self._config.failure_threshold,
                    "decrease_ratio": self._config.decrease_ratio,
                    "increase_step": self._config.increase_step,
                    "success_streak_required": self._config.success_streak_required,
                },
                "history": {
                    "total_batches": recent_count,
                    "avg_failure_rate": avg_failure_rate,
                },
            }

    def reset(self) -> None:
        """
        Reset manager to initial state.

        Used for testing or manual reset.
        """
        with self._state_lock:
            self._current_items = self._config.initial_items
            self._success_streak = 0
            self._history.clear()

            logger.info(
                "adaptive_replay.reset_initial_state",
                current_items=self._current_items,
            )


# =============================================================================
# Module-level singleton accessor
# =============================================================================

from baldur.utils.singleton import make_singleton_factory


def _cleanup_adaptive_replay_manager(manager: AdaptiveReplayManager) -> None:
    """Cleanup: reset state and clear class-level singleton."""
    manager.reset()
    AdaptiveReplayManager._instance = None


(
    get_adaptive_replay_manager,
    configure_adaptive_replay_manager,
    reset_adaptive_replay_manager,
) = make_singleton_factory(
    "adaptive_replay_manager",
    AdaptiveReplayManager,
    cleanup_fn=_cleanup_adaptive_replay_manager,
)
