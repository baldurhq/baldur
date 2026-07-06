"""
큐 크기 제공자 (캐싱 포함).

Redis 큐 조회 시 네트워크 지연이 RateController 병목이 되는 것을 방지.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import structlog

from baldur.scaling.config import (
    BackpressureSettings,
    get_backpressure_settings,
)

logger = structlog.get_logger()


class CachedQueueSizeProvider:
    """
    큐 크기 캐싱 Provider.

    Redis 등 외부 큐 조회 시 빈번한 네트워크 호출을 방지합니다.
    TTL 기반 캐싱으로 조회 빈도를 제한합니다.

    Usage:
        def get_redis_queue_size() -> int:
            return redis.llen("my_queue")

        provider = CachedQueueSizeProvider(get_redis_queue_size, cache_ttl=2.0)

        # 2초 내 재호출 시 캐시된 값 반환
        size = provider()
    """

    def __init__(
        self,
        provider: Callable[[], int],
        cache_ttl: float | None = None,
        settings: BackpressureSettings | None = None,
    ):
        """
        Args:
            provider: 실제 큐 크기 조회 함수
            cache_ttl: 캐시 TTL (초). None이면 설정에서 로드.
            settings: Backpressure 설정
        """
        self._provider = provider
        self._settings = settings or get_backpressure_settings()
        self._cache_ttl = cache_ttl or self._settings.queue_size_cache_ttl_seconds

        self._cached_value = 0
        self._last_fetch_time = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> int:
        """
        큐 크기 반환 (캐시 적용).

        TTL 내 재호출 시 캐시된 값 반환.
        TTL 초과 시 실제 조회 후 캐시 갱신.

        Returns:
            큐 크기
        """
        now = time.time()

        with self._lock:
            if now - self._last_fetch_time > self._cache_ttl:
                try:
                    self._cached_value = self._provider()
                    self._last_fetch_time = now
                except Exception as e:
                    logger.warning(
                        "cached_queue_size_provider.fetch_failed_using_cached",
                        error=e,
                    )
                    # 실패 시 기존 캐시 값 유지

            return self._cached_value

    def invalidate(self) -> None:
        """캐시 무효화. 다음 호출 시 즉시 조회."""
        with self._lock:
            self._last_fetch_time = 0.0

    def get_cache_info(self) -> dict:
        """
        캐시 정보 반환.

        Returns:
            캐시 상태 정보
        """
        with self._lock:
            return {
                "cached_value": self._cached_value,
                "last_fetch_time": self._last_fetch_time,
                "cache_ttl": self._cache_ttl,
                "age_seconds": time.time() - self._last_fetch_time,
            }
