"""
Sampled Audit Hook -- 샘플링 기반 감사 로깅.

AuditHook을 확장하여 설정된 비율로만 감사 로그를 기록한다.
sample_rate=1.0이면 AuditHook과 동일하게 100% 기록한다.

적응형 파이프라인(adaptive_pipeline)의 minimal 모드에서
감사 비용을 줄이면서도 통계적 관찰을 유지하기 위해 사용한다.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import structlog

from baldur.interfaces.resilience_policy import PolicyResult
from baldur.resilience.policies.hooks.audit import AuditHook

if TYPE_CHECKING:
    from baldur.interfaces.resilience_policy import PolicyContext

logger = structlog.get_logger()


class SampledAuditHook(AuditHook):
    """샘플링 기반 감사 로깅 훅.

    N번째 요청마다 감사 로그를 기록한다.
    sample_rate=1.0이면 모든 요청을 기록하고 (AuditHook과 동일),
    sample_rate=0.01이면 100번 중 1번만 기록한다.

    reject/failure는 항상 기록한다 (샘플링 대상에서 제외).
    성공 요청만 샘플링 대상이다.

    Thread-safe: 카운터는 threading.Lock으로 보호된다.
    """

    def __init__(self, sample_rate: float = 1.0) -> None:
        if not 0.0 <= sample_rate <= 1.0:
            raise ValueError(
                f"sample_rate must be between 0.0 and 1.0, got {sample_rate}"
            )
        self._sample_rate = sample_rate
        self._interval = max(1, int(1 / sample_rate)) if sample_rate > 0.0 else 0
        self._counter = 0
        self._lock = threading.Lock()

    @property
    def sample_rate(self) -> float:
        """현재 샘플링 비율."""
        return self._sample_rate

    def _should_sample(self) -> bool:
        """이번 요청을 샘플링할지 결정."""
        if self._sample_rate >= 1.0:
            return True
        if self._sample_rate <= 0.0:
            return False
        with self._lock:
            self._counter += 1
            return (self._counter % self._interval) == 0

    def on_success(
        self,
        policy_name: str,
        result: PolicyResult,
        context: PolicyContext | None = None,
    ) -> None:
        """성공 시 샘플링 비율에 따라 감사 로그 기록."""
        if self._should_sample():
            super().on_success(policy_name, result, context=context)

    def on_failure(
        self,
        policy_name: str,
        error: Exception,
        attempt: int,
        context: PolicyContext | None = None,
    ) -> None:
        """실패는 항상 기록한다."""
        super().on_failure(policy_name, error, attempt, context=context)

    def on_reject(
        self, guard_name: str, reason: str, context: PolicyContext | None = None
    ) -> None:
        """거부는 항상 기록한다."""
        super().on_reject(guard_name, reason, context=context)
