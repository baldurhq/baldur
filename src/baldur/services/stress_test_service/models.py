"""
Stress Test Service - Data Models.

스트레스 테스트 결과를 표현하는 데이터 클래스 모음.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from baldur.core.serializable import SerializableMixin

# =============================================================================
# Data Classes for Stress Test Results
# =============================================================================


@dataclass
class StressTestResult(SerializableMixin):
    """스트레스 테스트 결과 데이터 클래스."""

    status: str
    elapsed_seconds: float = 0.0
    message: str = ""
    error: str | None = None
    error_type: str | None = None
    extra: dict = field(default_factory=dict)

    def _post_serialize(self, data: dict[str, Any]) -> dict[str, Any]:
        """Round elapsed_seconds, pop falsy optional fields, merge extra dict."""
        data["elapsed_seconds"] = round(data["elapsed_seconds"], 2)
        if not data.get("message"):
            data.pop("message", None)
        if not data.get("error"):
            data.pop("error", None)
        if not data.get("error_type"):
            data.pop("error_type", None)
        # Merge extra dict contents into top level and remove the extra key
        extra = data.pop("extra", {})
        if extra:
            data.update(extra)
        return super()._post_serialize(data)


@dataclass
class PoolStatusResult(SerializableMixin):
    """커넥션 풀 상태 결과."""

    status: str
    sqlalchemy_pool: dict = field(default_factory=dict)
    pg_stats: dict = field(default_factory=dict)
    connection_usable: bool = True
    use_connection_pool: bool = False
    error: str | None = None
    error_type: str | None = None

    def _post_serialize(self, data: dict[str, Any]) -> dict[str, Any]:
        """Pop falsy optional fields."""
        if not data.get("error"):
            data.pop("error", None)
        if not data.get("error_type"):
            data.pop("error_type", None)
        return super()._post_serialize(data)


@dataclass
class LockContentionResult(SerializableMixin):
    """락 경합 테스트 결과."""

    status: str
    lock_id: int
    duration_seconds: float
    total_attempts: int = 0
    success_count: int = 0
    fail_count: int = 0
    success_rate_percent: float = 0.0
    avg_wait_ms: float = 0.0
    lock_hold_ms: int = 0
    error: str | None = None

    def _post_serialize(self, data: dict[str, Any]) -> dict[str, Any]:
        """Round duration, include detail fields only when completed, pop falsy error."""
        data["duration_seconds"] = round(data["duration_seconds"], 2)
        if self.status != "completed":
            for key in (
                "total_attempts",
                "success_count",
                "fail_count",
                "success_rate_percent",
                "avg_wait_ms",
                "lock_hold_ms",
            ):
                data.pop(key, None)
        if not data.get("error"):
            data.pop("error", None)
        return super()._post_serialize(data)


@dataclass
class BurstFailureResult(SerializableMixin):
    """Burst 장애 테스트 결과."""

    status: str
    lock_id: int
    lock_timeout_ms: int
    burst_duration_seconds: float
    total_attempts: int = 0
    timeout_count: int = 0
    success_count: int = 0
    deadlock_count: int = 0
    failure_rate_percent: float = 0.0
    message: str = ""
    error: str | None = None

    def _post_serialize(self, data: dict[str, Any]) -> dict[str, Any]:
        """Round burst_duration_seconds, pop falsy optional fields."""
        data["burst_duration_seconds"] = round(data["burst_duration_seconds"], 2)
        if not data.get("message"):
            data.pop("message", None)
        if not data.get("error"):
            data.pop("error", None)
        return super()._post_serialize(data)
