"""
Graceful Degradation Enums and Configurations.

Contains:
- DegradationLevel: Degradation level enum
- CircuitState: Circuit breaker states
- FallbackConfig: Configuration for fallback chain
- CircuitBreakerConfig: Configuration for circuit breaker
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from baldur.settings.graceful_degradation import GracefulDegradationSettings


class DegradationLevel(str, Enum):
    """
    Hash chain degradation levels.

    Determines available features at each level:
    - NORMAL: Full functionality with Redis
    - DEGRADED: Partial functionality with local fallback
    - EMERGENCY: Minimal functionality, memory-only
    - READONLY: No writes, only reads from cache
    """

    NORMAL = "normal"
    DEGRADED = "degraded"
    EMERGENCY = "emergency"
    READONLY = "readonly"


class CircuitState(str, Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class FallbackConfig:
    """Configuration for fallback chain."""

    redis_timeout_seconds: float = 5.0
    replica_timeout_seconds: float = 3.0
    local_file_path: Path | None = None
    memory_max_entries: int = 10000
    key_prefix: str = "baldur:"

    @classmethod
    def from_settings(
        cls,
        settings: GracefulDegradationSettings | None = None,
        **overrides,
    ) -> FallbackConfig:
        """
        Settings에서 FallbackConfig 인스턴스 생성.

        Args:
            settings: GracefulDegradationSettings 인스턴스 (없으면 싱글톤 사용)
            **overrides: 개별 필드 오버라이드

        Returns:
            FallbackConfig: Settings 기반 인스턴스
        """
        from baldur.settings.graceful_degradation import (
            get_graceful_degradation_settings,
        )

        s = settings or get_graceful_degradation_settings()
        return cls(
            redis_timeout_seconds=overrides.get(
                "redis_timeout_seconds", s.redis_timeout_seconds
            ),
            replica_timeout_seconds=overrides.get(
                "replica_timeout_seconds", s.replica_timeout_seconds
            ),
            local_file_path=overrides.get("local_file_path"),
            memory_max_entries=overrides.get(
                "memory_max_entries", s.memory_max_entries
            ),
            key_prefix=overrides.get("key_prefix", s.key_prefix),
        )


@dataclass
class HashChainCircuitBreakerConfig:
    """Configuration for hash chain circuit breaker."""

    failure_threshold: int = 5
    recovery_timeout_seconds: float = 30.0
    half_open_requests: int = 3
    success_threshold: int = 2

    @classmethod
    def from_settings(
        cls,
        settings: GracefulDegradationSettings | None = None,
        **overrides,
    ) -> HashChainCircuitBreakerConfig:
        """
        Settings에서 HashChainCircuitBreakerConfig 인스턴스 생성.

        Args:
            settings: GracefulDegradationSettings 인스턴스 (없으면 싱글톤 사용)
            **overrides: 개별 필드 오버라이드

        Returns:
            HashChainCircuitBreakerConfig: Settings 기반 인스턴스
        """
        from baldur.settings.graceful_degradation import (
            get_graceful_degradation_settings,
        )

        s = settings or get_graceful_degradation_settings()
        return cls(
            failure_threshold=overrides.get(
                "failure_threshold", s.cb_failure_threshold
            ),
            recovery_timeout_seconds=overrides.get(
                "recovery_timeout_seconds", s.cb_recovery_timeout_seconds
            ),
            half_open_requests=overrides.get(
                "half_open_requests", s.cb_half_open_requests
            ),
            success_threshold=overrides.get(
                "success_threshold", s.cb_success_threshold
            ),
        )


__all__ = [
    "DegradationLevel",
    "CircuitState",
    "FallbackConfig",
    "HashChainCircuitBreakerConfig",
]
