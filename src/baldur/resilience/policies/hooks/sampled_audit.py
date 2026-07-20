"""
Sampled Audit Hook -- sampling-based audit logging.

Extends AuditHook to record audit logs only at the configured rate.
sample_rate=1.0 records 100%, identical to AuditHook.

Used in the adaptive pipeline's minimal mode to cut audit cost while retaining
statistical observability.
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
    """Sampling-based audit logging hook.

    Records an audit log on every Nth request.
    sample_rate=1.0 records every request (identical to AuditHook);
    sample_rate=0.01 records 1 in 100.

    reject/failure are always recorded (excluded from sampling). Only
    successful requests are sampled.

    Thread-safe: the counter is protected by a threading.Lock.
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
        """Current sampling rate."""
        return self._sample_rate

    def _should_sample(self) -> bool:
        """Decide whether to sample this request."""
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
        """On success, record an audit log per the sampling rate."""
        if self._should_sample():
            super().on_success(policy_name, result, context=context)

    def on_failure(
        self,
        policy_name: str,
        error: Exception,
        attempt: int,
        context: PolicyContext | None = None,
    ) -> None:
        """Failures are always recorded."""
        super().on_failure(policy_name, error, attempt, context=context)

    def on_reject(
        self, guard_name: str, reason: str, context: PolicyContext | None = None
    ) -> None:
        """Rejections are always recorded."""
        super().on_reject(guard_name, reason, context=context)
