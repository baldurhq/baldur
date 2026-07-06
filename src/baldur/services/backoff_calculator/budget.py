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
    적응형 재시도 예산 관리자.

    전체 요청 대비 재시도 비율을 관리하여 Self-DDoS를 방지합니다.
    Throttle 상태에 따라 예산이 동적으로 삭감됩니다.
    """

    max_retry_ratio: float = 0.10  # 기본 10%
    current_retry_count: int = 0
    current_total_count: int = 0
    window_seconds: int = 60
    _window_start: float = field(default_factory=time.time)

    # Throttle 연동 동적 삭감 비율
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
            "full_stop": 0.0,  # 0% (재시도 금지)
            "full_stop_active": 0.0,  # 0% (재시도 금지)
        }
    )

    def should_allow_retry(self) -> bool:
        """재시도 허용 여부 확인."""
        self._maybe_reset_window()

        if self.current_total_count == 0:
            return True

        current_ratio = self.current_retry_count / self.current_total_count
        return current_ratio < self.max_retry_ratio

    def record_request(self, is_retry: bool = False) -> None:
        """요청 기록."""
        self._maybe_reset_window()
        self.current_total_count += 1
        if is_retry:
            self.current_retry_count += 1

    def _maybe_reset_window(self) -> None:
        """윈도우 초과 시 리셋."""
        now = time.time()
        if now - self._window_start > self.window_seconds:
            self.current_retry_count = 0
            self.current_total_count = 0
            self._window_start = now

    def adjust_budget_for_throttle_state(self, throttle_reason: str) -> None:
        """Throttle 상태에 따라 예산 동적 조정."""
        if throttle_reason in self.THROTTLE_BUDGET_RATIOS:
            self.max_retry_ratio = self.THROTTLE_BUDGET_RATIOS[throttle_reason]
        else:
            # 알 수 없는 상태면 보수적으로 5%
            self.max_retry_ratio = 0.05

    def get_stats(self) -> dict:
        """현재 상태 통계."""
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
