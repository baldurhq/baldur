"""
Adaptive Retry Budget

Manages retry budget ratios to prevent Self-DDoS.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# =============================================================================
# Adaptive Retry Budget
# =============================================================================


@dataclass
class AdaptiveRetryBudget:
    """
    Adaptive retry budget manager.

    Manages the retry-to-total request ratio to prevent Self-DDoS.
    The budget is cut dynamically according to the throttle state.
    """

    max_retry_ratio: float = 0.10  # Default 10%
    current_retry_count: int = 0
    current_total_count: int = 0
    window_seconds: int = 60
    _window_start: float = field(default_factory=time.time)

    # Throttle-linked dynamic cut ratios
    THROTTLE_BUDGET_RATIOS: dict[str, float] = field(
        default_factory=lambda: {
            "normal": 0.10,  # 10%
            "sla_warning": 0.07,  # 7%
            "sla_critical": 0.05,  # 5%
            "emergency_level_1": 0.03,  # 3%
            "emergency_level_2": 0.03,  # 3%
            "emergency_1_2": 0.03,  # 3%
            "emergency_level_3": 0.01,  # 1%
            "emergency_3": 0.01,  # 1%
            "full_stop": 0.0,  # 0% (no retries)
            "full_stop_active": 0.0,  # 0% (no retries)
        }
    )

    def should_allow_retry(self) -> bool:
        """Check whether a retry is allowed."""
        self._maybe_reset_window()

        if self.current_total_count == 0:
            return True

        current_ratio = self.current_retry_count / self.current_total_count
        return current_ratio < self.max_retry_ratio

    def record_request(self, is_retry: bool = False) -> None:
        """Record a request."""
        self._maybe_reset_window()
        self.current_total_count += 1
        if is_retry:
            self.current_retry_count += 1

    def _maybe_reset_window(self) -> None:
        """Reset once the window has elapsed."""
        now = time.time()
        if now - self._window_start > self.window_seconds:
            self.current_retry_count = 0
            self.current_total_count = 0
            self._window_start = now

    def adjust_budget_for_throttle_state(self, throttle_reason: str) -> None:
        """Adjust the budget dynamically based on the throttle state."""
        if throttle_reason in self.THROTTLE_BUDGET_RATIOS:
            self.max_retry_ratio = self.THROTTLE_BUDGET_RATIOS[throttle_reason]
        else:
            # Unknown state — be conservative at 5%
            self.max_retry_ratio = 0.05

    def get_stats(self) -> dict:
        """Current state statistics."""
        return {
            "max_retry_ratio": self.max_retry_ratio,
            "current_retry_count": self.current_retry_count,
            "current_total_count": self.current_total_count,
            "current_ratio": (
                self.current_retry_count / self.current_total_count
                if self.current_total_count > 0
                else 0.0
            ),
            "budget_remaining": max(
                0,
                int(self.current_total_count * self.max_retry_ratio)
                - self.current_retry_count,
            ),
        }
