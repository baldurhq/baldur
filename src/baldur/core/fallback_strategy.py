"""
Fallback Strategy for Partial Partitions

Provides graceful degradation strategies when connections fail:
- Cache miss → DB fallback
- External API down → cached/default response
- Message queue down → sync processing
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar

import structlog

from .connection_health import PartitionState

logger = structlog.get_logger()

T = TypeVar("T")


class FallbackMode(str, Enum):
    """Fallback behavior modes"""

    FAIL_FAST = "fail_fast"  # 즉시 실패
    USE_CACHE = "use_cache"  # 캐시된 값 사용
    USE_DEFAULT = "use_default"  # 기본값 사용
    DEGRADE_GRACEFULLY = "degrade"  # 기능 축소
    RETRY_ALTERNATIVE = "retry_alt"  # 대체 경로 시도
    HEDGE = "hedge"  # 병렬 헷징으로 인한 대체 응답


@dataclass
class FallbackResult(Generic[T]):
    """Result of a fallback operation"""

    value: T | None
    used_fallback: bool
    fallback_mode: FallbackMode | None = None
    original_error: str | None = None

    @property
    def success(self) -> bool:
        """True if we have a value (either primary or fallback)"""
        return self.value is not None or (
            self.used_fallback and self.fallback_mode != FallbackMode.FAIL_FAST
        )


class FallbackStrategy(ABC):
    """Abstract fallback strategy"""

    @abstractmethod
    def execute(
        self,
        primary_fn: Callable[[], T],
        fallback_fn: Callable[[], T] | None = None,
        default_value: T | None = None,
    ) -> FallbackResult[T]:
        """Execute with fallback"""
        pass


class SimpleFallback(FallbackStrategy):
    """Simple fallback that tries fallback_fn then default_value."""

    def execute(
        self,
        primary_fn: Callable[[], T],
        fallback_fn: Callable[[], T] | None = None,
        default_value: T | None = None,
    ) -> FallbackResult[T]:
        try:
            result = primary_fn()
            return FallbackResult(value=result, used_fallback=False)
        except Exception as e:
            logger.warning(
                "primary.function_failed",
                error=e,
            )

            # Try explicit fallback
            if fallback_fn:
                try:
                    result = fallback_fn()
                    return FallbackResult(
                        value=result,
                        used_fallback=True,
                        fallback_mode=FallbackMode.RETRY_ALTERNATIVE,
                        original_error=str(e),
                    )
                except Exception as fallback_e:
                    logger.warning(
                        "fallback.function_also_failed",
                        fallback_e=fallback_e,
                    )

            # Use default value
            if default_value is not None:
                return FallbackResult(
                    value=default_value,
                    used_fallback=True,
                    fallback_mode=FallbackMode.USE_DEFAULT,
                    original_error=str(e),
                )

            # All failed
            return FallbackResult(
                value=None,
                used_fallback=True,
                fallback_mode=FallbackMode.FAIL_FAST,
                original_error=str(e),
            )


class PartitionAwareFallback(FallbackStrategy):
    """
    Fallback strategy aware of partition state.
    Automatically selects fallback based on which connections are available.
    """

    def __init__(
        self,
        partition_state: PartitionState,
        cache_fallback: Callable[[], Any] | None = None,
        db_fallback: Callable[[], Any] | None = None,
    ):
        """
        Initialize partition-aware fallback.

        Args:
            partition_state: Current partition state
            cache_fallback: Function to get data from cache
            db_fallback: Function to get data from database
        """
        self._partition_state = partition_state
        self._cache_fallback = cache_fallback
        self._db_fallback = db_fallback

    def execute(
        self,
        primary_fn: Callable[[], T],
        fallback_fn: Callable[[], T] | None = None,
        default_value: T | None = None,
    ) -> FallbackResult[T]:
        try:
            result = primary_fn()
            return FallbackResult(value=result, used_fallback=False)
        except Exception as e:
            logger.warning(
                "primary.operation_failed",
                error=e,
            )
            return self._handle_failure(e, fallback_fn, default_value)

    def _handle_failure(
        self,
        error: Exception,
        fallback_fn: Callable[[], T] | None,
        default_value: T | None,
    ) -> FallbackResult[T]:
        # 1. 명시적 fallback 함수가 있으면 시도
        if fallback_fn:
            try:
                result = fallback_fn()
                return FallbackResult(
                    value=result,
                    used_fallback=True,
                    fallback_mode=FallbackMode.RETRY_ALTERNATIVE,
                    original_error=str(error),
                )
            except Exception as e:
                logger.warning(
                    "explicit.fallback_failed",
                    error=e,
                )

        # 2. 캐시 사용 불가 + DB 가용 → DB fallback
        if (
            not self._partition_state.cache_available
            and self._partition_state.db_available
        ) and self._db_fallback:
            try:
                result = self._db_fallback()
                logger.info("fallback_strategy.db_fallback_used")
                return FallbackResult(
                    value=result,
                    used_fallback=True,
                    fallback_mode=FallbackMode.DEGRADE_GRACEFULLY,
                    original_error=str(error),
                )
            except Exception as e:
                logger.warning(
                    "db.fallback_failed",
                    error=e,
                )

        # 3. DB 사용 불가 + 캐시 가용 → 캐시 fallback
        if (
            not self._partition_state.db_available
            and self._partition_state.cache_available
        ) and self._cache_fallback:
            try:
                result = self._cache_fallback()
                logger.info("fallback_strategy.cache_fallback_used")
                return FallbackResult(
                    value=result,
                    used_fallback=True,
                    fallback_mode=FallbackMode.USE_CACHE,
                    original_error=str(error),
                )
            except Exception as e:
                logger.warning(
                    "cache.fallback_failed",
                    error=e,
                )

        # 4. 기본값 반환
        if default_value is not None:
            logger.info("fallback_strategy.default_value_used")
            return FallbackResult(
                value=default_value,
                used_fallback=True,
                fallback_mode=FallbackMode.USE_DEFAULT,
                original_error=str(error),
            )

        # 5. 모든 fallback 실패
        logger.error(
            "all.fallback_strategies_failed",
            error=error,
        )
        return FallbackResult(
            value=None,
            used_fallback=True,
            fallback_mode=FallbackMode.FAIL_FAST,
            original_error=str(error),
        )

    def update_partition_state(self, new_state: PartitionState) -> None:
        """Update the partition state for dynamic adjustment."""
        self._partition_state = new_state


class CacheFirstFallback(FallbackStrategy, Generic[T]):
    """
    Fallback strategy that tries cache first, then DB.
    Useful for read-heavy operations.
    """

    def __init__(
        self,
        cache_fn: Callable[[], T],
        db_fn: Callable[[], T],
        update_cache_fn: Callable[[T], None] | None = None,
    ):
        """
        Initialize cache-first fallback.

        Args:
            cache_fn: Function to get from cache
            db_fn: Function to get from database
            update_cache_fn: Optional function to update cache after DB read
        """
        self._cache_fn = cache_fn
        self._db_fn = db_fn
        self._update_cache_fn = update_cache_fn

    def execute(  # type: ignore[override]
        self,
        primary_fn: Callable[[], T] | None = None,
        fallback_fn: Callable[[], T] | None = None,
        default_value: T | None = None,
    ) -> FallbackResult[T]:
        # CacheFirstFallback intentionally widens primary_fn to Optional: the
        # strategy ignores it (cache_fn always runs first by design). The
        # `type: ignore[override]` documents this contravariance vs the
        # FallbackStrategy ABC; SimpleFallback / PartitionAwareFallback still
        # require primary_fn so the ABC stays tight for them.
        # Try cache first
        try:
            result = self._cache_fn()
            if result is not None:
                return FallbackResult(value=result, used_fallback=False)
        except Exception as e:
            logger.debug(
                "cache.lookup_failed",
                error=e,
            )

        # Cache miss or error, try DB
        try:
            result = self._db_fn()
            if result is not None and self._update_cache_fn:
                try:
                    self._update_cache_fn(result)
                except Exception as e:
                    logger.warning(
                        "fallback_strategy.cache_update_failed",
                        error=e,
                    )

            return FallbackResult(
                value=result,
                used_fallback=True,
                fallback_mode=FallbackMode.DEGRADE_GRACEFULLY,
            )
        except Exception as e:
            logger.warning(
                "db.lookup_failed",
                error=e,
            )

        # Both failed
        if default_value is not None:
            return FallbackResult(
                value=default_value,
                used_fallback=True,
                fallback_mode=FallbackMode.USE_DEFAULT,
            )

        return FallbackResult(
            value=None,
            used_fallback=True,
            fallback_mode=FallbackMode.FAIL_FAST,
        )
