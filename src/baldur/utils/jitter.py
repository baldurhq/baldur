"""
Jitter Utilities for Thundering Herd Prevention.

Provides random delay mechanisms to prevent all instances from
hitting the database simultaneously during startup.

Note: This module was moved from metrics/jitter.py since jitter
utilities are general-purpose and not metrics-specific.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

import structlog

logger = structlog.get_logger()

P = ParamSpec("P")
R = TypeVar("R")


def with_jitter(
    max_delay_seconds: float | None = None,
    min_delay_seconds: float | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    동기화 함수에 무작위 지연을 추가하는 데코레이터.

    분산 환경에서 동시 시작되는 인스턴스들의 DB 쿼리를
    시간적으로 분산시켜 Thundering Herd를 방지합니다.

    Args:
        max_delay_seconds: 최대 지연 시간 (초). None이면 Settings에서 로드.
        min_delay_seconds: 최소 지연 시간 (초). None이면 Settings에서 로드.

    Example:
        >>> @with_jitter(max_delay_seconds=30.0)
        ... def sync_metrics():
        ...     # 0~30초 사이 무작위 지연 후 실행
        ...     return do_sync()

    환경별 권장 설정:
        - 단일 서버: 0초 (비활성화)
        - K8s 10 Pods: 30초
        - K8s 100+ Pods: 60초
    """
    # Settings에서 기본값 로드
    if max_delay_seconds is None or min_delay_seconds is None:
        from baldur.settings.jitter import get_jitter_settings

        settings = get_jitter_settings()
        if max_delay_seconds is None:
            max_delay_seconds = settings.max_delay_seconds
        if min_delay_seconds is None:
            min_delay_seconds = settings.min_delay_seconds

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            jitter = random.uniform(min_delay_seconds, max_delay_seconds)
            logger.debug(
                "jitter.sleeping_before",
                jitter=jitter,
                func=func.__name__,
            )
            time.sleep(jitter)
            return func(*args, **kwargs)

        @wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            jitter = random.uniform(min_delay_seconds, max_delay_seconds)
            logger.debug(
                "jitter.sleeping_before",
                jitter=jitter,
                func=func.__name__,
            )
            await asyncio.sleep(jitter)
            return await func(*args, **kwargs)  # type: ignore[misc,no-any-return]

        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        return sync_wrapper

    return decorator


def calculate_jitter(
    max_delay_seconds: float | None = None,
    min_delay_seconds: float | None = None,
) -> float:
    """
    Jitter 지연 시간을 계산합니다.

    데코레이터를 사용할 수 없는 경우 직접 호출하여 사용합니다.

    Args:
        max_delay_seconds: 최대 지연 시간 (초). None이면 Settings에서 로드.
        min_delay_seconds: 최소 지연 시간 (초). None이면 Settings에서 로드.

    Returns:
        계산된 지연 시간 (초)

    Example:
        >>> delay = calculate_jitter(max_delay_seconds=30.0)
        >>> time.sleep(delay)
        >>> do_sync()
    """
    if max_delay_seconds is None or min_delay_seconds is None:
        from baldur.settings.jitter import get_jitter_settings

        settings = get_jitter_settings()
        if max_delay_seconds is None:
            max_delay_seconds = settings.max_delay_seconds
        if min_delay_seconds is None:
            min_delay_seconds = settings.min_delay_seconds
    return random.uniform(min_delay_seconds, max_delay_seconds)


def sleep_with_jitter(
    max_delay_seconds: float | None = None,
    min_delay_seconds: float | None = None,
) -> float:
    """
    Jitter를 적용하여 동기적으로 대기합니다.

    Args:
        max_delay_seconds: 최대 지연 시간 (초). None이면 Settings에서 로드.
        min_delay_seconds: 최소 지연 시간 (초). None이면 Settings에서 로드.

    Returns:
        실제 대기한 시간 (초)

    Example:
        >>> waited = sleep_with_jitter(max_delay_seconds=30.0)
        >>> print(f"Waited {waited:.2f} seconds")
    """
    delay = calculate_jitter(max_delay_seconds, min_delay_seconds)
    time.sleep(delay)
    return delay


async def async_sleep_with_jitter(
    max_delay_seconds: float | None = None,
    min_delay_seconds: float | None = None,
) -> float:
    """
    Jitter를 적용하여 비동기적으로 대기합니다.

    Args:
        max_delay_seconds: 최대 지연 시간 (초). None이면 Settings에서 로드.
        min_delay_seconds: 최소 지연 시간 (초). None이면 Settings에서 로드.

    Returns:
        실제 대기한 시간 (초)

    Example:
        >>> waited = await async_sleep_with_jitter(max_delay_seconds=30.0)
        >>> print(f"Waited {waited:.2f} seconds")
    """
    delay = calculate_jitter(max_delay_seconds, min_delay_seconds)
    await asyncio.sleep(delay)
    return delay


class JitterConfig:
    """
    Jitter 설정 클래스.

    환경 변수 또는 직접 설정으로 Jitter를 구성합니다.
    """

    def __init__(
        self,
        enabled: bool = True,
        max_delay_seconds: float = 60.0,
        min_delay_seconds: float = 0.0,
    ):
        """
        Initialize JitterConfig.

        Args:
            enabled: Jitter 활성화 여부
            max_delay_seconds: 최대 지연 시간
            min_delay_seconds: 최소 지연 시간
        """
        self.enabled = enabled
        self.max_delay_seconds = max_delay_seconds
        self.min_delay_seconds = min_delay_seconds

    @classmethod
    def from_settings(cls, settings=None, **overrides) -> JitterConfig:
        """
        Settings 기반 인스턴스 생성.

        Args:
            settings: JitterSettings 인스턴스 (None이면 자동 로드)
            **overrides: 개별 필드 오버라이드

        Returns:
            JitterConfig: Settings 기반 인스턴스
        """
        from baldur.settings.jitter import get_jitter_settings

        s = settings or get_jitter_settings()
        return cls(
            enabled=overrides.get("enabled", s.enabled),
            max_delay_seconds=overrides.get("max_delay_seconds", s.max_delay_seconds),
            min_delay_seconds=overrides.get("min_delay_seconds", s.min_delay_seconds),
        )

    def get_delay(self) -> float:
        """Jitter 지연 시간을 반환합니다 (비활성화 시 0)."""
        if not self.enabled:
            return 0.0
        return calculate_jitter(self.max_delay_seconds, self.min_delay_seconds)

    def sleep(self) -> float:
        """Jitter를 적용하여 대기합니다."""
        delay = self.get_delay()
        if delay > 0:
            time.sleep(delay)
        return delay

    async def async_sleep(self) -> float:
        """Jitter를 적용하여 비동기적으로 대기합니다."""
        delay = self.get_delay()
        if delay > 0:
            await asyncio.sleep(delay)
        return delay


__all__ = [
    "with_jitter",
    "calculate_jitter",
    "sleep_with_jitter",
    "async_sleep_with_jitter",
    "JitterConfig",
]
