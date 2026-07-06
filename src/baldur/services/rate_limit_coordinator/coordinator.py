"""
Rate Limit Coordinator - Core Coordinator

Central coordinator for distributed rate limit management.
Prevents Self-DDoS by coordinating retry behavior across all workers.

Key Features:
    - Global cooldown on 429 responses
    - Exponential backoff with jitter
    - Distributed state via pluggable storage
    - 100% coverage with database fallback

Design Philosophy:
    "어떤 고객 환경이든 100% Self-DDoS 차단"
    - Redis 있으면 사용 (최고 성능)
    - 없으면 Database 사용 (100% 호환)
    - DB도 없으면 InMemory (단일 프로세스)
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

import structlog

from baldur.adapters.rate_limit import get_rate_limit_storage
from baldur.interfaces.rate_limit_storage import (
    RateLimitState,
    RateLimitStorageInterface,
)

from .helpers import (
    _default_get_retry_after,
    _default_is_429,
    _emit_rate_limit_event,
    _record_rate_limit_metrics,
)
from .models import RateLimitCoordinatorConfig, RateLimitResult

logger = structlog.get_logger()

T = TypeVar("T")


class RateLimitCoordinator:
    """
    Coordinates rate limiting across distributed workers.

    Prevents Self-DDoS by:
    1. Detecting 429 responses
    2. Setting global cooldown (shared across all workers)
    3. Making all workers wait before retrying
    4. Using exponential backoff with jitter

    Usage:
        coordinator = RateLimitCoordinator()

        # Before making request
        coordinator.wait_if_needed("payment_api")

        # After receiving 429
        coordinator.on_rate_limited(
            key="payment_api",
            retry_after=response.headers.get("Retry-After"),
        )

        # After successful request
        coordinator.on_success("payment_api")

    With decorator:
        @coordinator.rate_limit_aware("payment_api")
        def call_external_api():
            return requests.post(...)
    """

    _instance: RateLimitCoordinator | None = None
    _instance_lock = threading.Lock()

    def __init__(
        self,
        storage: RateLimitStorageInterface | None = None,
        config: RateLimitCoordinatorConfig | None = None,
    ) -> None:
        """
        Initialize rate limit coordinator.

        Args:
            storage: Rate limit storage backend (auto-detected if None)
            config: Rate limit configuration
        """
        self._storage = storage or get_rate_limit_storage()
        self._config = config or RateLimitCoordinatorConfig.from_settings()
        self._local_lock = threading.Lock()

        # EventBus 디바운싱 상태 (동일 key에 대한 이벤트 중복 발행 방지)
        self._last_event_emit_times: dict[str, float] = {}
        self._debounce_lock = threading.Lock()

        # Canary 상태 추적 (Cooldown 직후 첫 요청 정찰 모드)
        self._canary_in_progress: dict[str, bool] = {}
        self._canary_lock = threading.Lock()

        # Cooldown 종료 이벤트 타이머 추적 (취소용)
        self._cooldown_timers: dict[str, threading.Timer] = {}
        self._timer_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> RateLimitCoordinator:
        """Get singleton instance."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance for test isolation.

        Cancels all pending cooldown Timer threads before clearing instance.
        """
        with cls._instance_lock:
            instance = cls._instance
            if instance is not None:
                with instance._timer_lock:
                    for timer in instance._cooldown_timers.values():
                        timer.cancel()
                    instance._cooldown_timers.clear()
            cls._instance = None

    @property
    def storage_type(self) -> str:
        """Get the type of storage backend being used."""
        return self._storage.storage_type.value

    # =========================================================================
    # EventBus Debouncing Methods
    # =========================================================================

    def _should_emit_event(self, key: str) -> bool:
        """
        디바운싱 확인 - 동일 key에 대해 윈도우 내 중복 이벤트 방지.

        Args:
            key: Rate limit key

        Returns:
            이벤트 발행 여부
        """
        now = time.time()

        with self._debounce_lock:
            last_time = self._last_event_emit_times.get(key, 0)

            if now - last_time < self._config.debounce_window_seconds:
                logger.debug(
                    "rate_limit_coordinator.debounced_event_last_emit",
                    rate_limit_key=key,
                    time_since_last_request=now - last_time,
                )
                return False

            self._last_event_emit_times[key] = now
            return True

    # =========================================================================
    # Cooldown End Event Scheduling
    # =========================================================================

    def _schedule_cooldown_end_event(self, key: str, cooldown_until: float) -> None:
        """
        Cooldown 종료 시점에 RATE_LIMIT_COOLDOWN_END 이벤트 예약.

        Threading Timer 사용으로 비동기 발행.

        Args:
            key: Rate limit key
            cooldown_until: Cooldown 종료 시각 (Unix timestamp)
        """
        delay = cooldown_until - time.time()
        if delay <= 0:
            return

        def emit_cooldown_end() -> None:
            _emit_rate_limit_event(
                "RATE_LIMIT_COOLDOWN_END",
                {
                    "key": key,
                    "cooldown_ended_at": time.time(),
                },
                priority_name="NORMAL",
            )
            logger.info(
                "rate_limit_coordinator.cooldown_ended",
                rate_limit_key=key,
            )

            # 타이머 정리
            with self._timer_lock:
                self._cooldown_timers.pop(key, None)

        # 기존 타이머 취소
        with self._timer_lock:
            existing_timer = self._cooldown_timers.get(key)
            if existing_timer:
                existing_timer.cancel()

            timer = threading.Timer(delay, emit_cooldown_end)
            timer.daemon = True
            timer.start()
            self._cooldown_timers[key] = timer

    # =========================================================================
    # Canary Request Methods
    # =========================================================================

    def _check_canary_mode(self, key: str, state: RateLimitState) -> bool:
        """
        Canary 모드 확인 - Cooldown 종료 직후 첫 요청인지 확인.

        Args:
            key: Rate limit key
            state: 현재 Rate limit 상태

        Returns:
            is_canary 여부
        """
        if state.consecutive_429s == 0:
            return False

        with self._canary_lock:
            if key not in self._canary_in_progress:
                self._canary_in_progress[key] = True
                logger.info(
                    "rate_limit_coordinator.canary_request_mode",
                    rate_limit_key=key,
                )
                return True

        return False

    def _clear_canary_state(self, key: str) -> None:
        """Canary 상태 해제."""
        with self._canary_lock:
            if key in self._canary_in_progress:
                del self._canary_in_progress[key]
                logger.debug(
                    "rate_limit_coordinator.canary_state_cleared",
                    rate_limit_key=key,
                )

    def get_state(self, key: str) -> RateLimitState:
        """Get current rate limit state for a key."""
        return self._storage.get_state(key)

    def wait_if_needed(self, key: str) -> RateLimitResult:
        """
        Wait if currently in cooldown period.

        Call this BEFORE making an external request.
        Cooldown 종료 직후 첫 요청은 is_canary=True로 표시.

        Args:
            key: Rate limit key (e.g., "payment_api", "external_service")

        Returns:
            RateLimitResult with wait information and canary mode flag
        """
        state = self._storage.get_state(key)

        if state.is_in_cooldown:
            wait_time = state.remaining_cooldown

            logger.info(
                "rate_limit_coordinator.waiting",
                wait_time=wait_time,
                key=key,
                state=state.consecutive_429s,
            )

            time.sleep(wait_time)

            return RateLimitResult(
                waited=True,
                wait_time=wait_time,
                was_rate_limited=True,
                consecutive_429s=state.consecutive_429s,
                is_canary=False,
            )

        # Cooldown 종료 직후 - Canary 모드 확인
        is_canary = self._check_canary_mode(key, state)

        return RateLimitResult(
            waited=False,
            wait_time=0.0,
            was_rate_limited=state.consecutive_429s > 0,
            consecutive_429s=state.consecutive_429s,
            is_canary=is_canary,
        )

    def on_rate_limited(
        self,
        key: str,
        retry_after: float | None = None,
        status_code: int = 429,
    ) -> float:
        """
        Handle a rate limit (429) response.

        Call this when you receive a 429 response.
        Sets a global cooldown for all workers and emits events.

        Args:
            key: Rate limit key
            retry_after: Retry-After header value (seconds)
            status_code: HTTP status code (for logging)

        Returns:
            Calculated cooldown duration in seconds
        """
        consecutive = self._storage.increment_consecutive_429s(key)

        # Calculate backoff with exponential increase
        if retry_after is not None and retry_after > 0:
            base_delay = retry_after
        else:
            base_delay = self._config.default_retry_after

        # Exponential backoff: base * (multiplier ^ consecutive)
        delay = base_delay * (self._config.backoff_multiplier ** (consecutive - 1))
        delay = min(delay, self._config.max_delay)

        # Add jitter to prevent thundering herd
        jitter_range = delay * (self._config.jitter_percent / 100.0)
        jitter = random.uniform(-jitter_range, jitter_range)
        delay = max(0.1, delay + jitter)

        # Set global cooldown
        cooldown_until = time.time() + delay
        self._storage.set_cooldown(key, cooldown_until)

        # EventBus 연동 (디바운싱 적용)
        if self._should_emit_event(key):
            _emit_rate_limit_event(
                "RATE_LIMIT_429",
                {
                    "key": key,
                    "status_code": status_code,
                    "retry_after_header": retry_after,
                    "calculated_delay": delay,
                    "consecutive_429s": consecutive,
                    "cooldown_until": cooldown_until,
                },
                priority_name="HIGH",
            )

            # Prometheus 메트릭 기록
            _record_rate_limit_metrics(
                key=key,
                status_code=status_code,
                cooldown_seconds=delay,
                consecutive_429s=consecutive,
            )

            # Cooldown 종료 이벤트 스케줄링
            self._schedule_cooldown_end_event(key, cooldown_until)

        # 317: Kafka 분산 채널을 통해 클러스터 전체에 429 이벤트 전파
        self._broadcast_to_cluster(key, consecutive, cooldown_until, delay)

        logger.warning(
            "rate_limit_coordinator.rate_limited",
            key=key,
            status_code=status_code,
            consecutive=consecutive,
            delay=delay,
        )

        return delay

    def _broadcast_to_cluster(
        self,
        key: str,
        consecutive_429s: int,
        cooldown_until: float,
        calculated_delay: float,
    ) -> None:
        """317: Kafka 분산 채널로 429 이벤트 비동기 전파 (Fail-Open)."""
        try:
            from baldur.services.rate_limit.distributed_channel import (
                get_distributed_rate_limit_channel,
            )

            channel = get_distributed_rate_limit_channel()
            channel.broadcast_rate_limit_429(
                key=key,
                consecutive_429s=consecutive_429s,
                cooldown_until=cooldown_until,
                calculated_delay=calculated_delay,
            )
        except Exception as e:
            logger.debug(
                "rate_limit_coordinator.broadcast_skipped",
                error=e,
            )

    def on_success(self, key: str) -> None:
        """
        Handle a successful response.

        Call this after a successful request to clear canary state
        and reset consecutive 429 counter.

        Args:
            key: Rate limit key
        """
        # Canary 상태 해제
        self._clear_canary_state(key)

        state = self._storage.get_state(key)

        if state.consecutive_429s > 0:
            # Gradual reduction instead of immediate reset
            # Prevents immediate flood after recovery
            self._storage.reset_consecutive_429s(key)

            logger.debug(
                "rate_limit_coordinator.success_reset_consecutive_counter",
                key=key,
            )

    def clear(self, key: str) -> None:
        """Clear all rate limit state for a key."""
        self._storage.clear(key)
        logger.info(
            "rate_limit_coordinator.cleared_state",
            key=key,
        )

    def rate_limit_aware(
        self,
        key: str,
        is_429: Callable[[Any], bool] | None = None,
        get_retry_after: Callable[[Any], float | None] | None = None,
    ) -> Callable[[Callable[..., T]], Callable[..., T]]:
        """
        Decorator to make a function rate-limit aware.

        Args:
            key: Rate limit key
            is_429: Function to detect if response is 429 (default: check status_code)
            get_retry_after: Function to extract Retry-After from response

        Returns:
            Decorated function

        Example:
            @coordinator.rate_limit_aware("payment_api")
            def call_payment_api():
                return requests.post(...)

            @coordinator.rate_limit_aware(
                "external_api",
                is_429=lambda r: r.status_code == 429,
                get_retry_after=lambda r: float(r.headers.get("Retry-After", 5)),
            )
            def call_external_api():
                return requests.get(...)
        """

        def decorator(func: Callable[..., T]) -> Callable[..., T]:
            def wrapper(*args: Any, **kwargs: Any) -> T:
                # Wait if in cooldown
                self.wait_if_needed(key)

                result = func(*args, **kwargs)

                # Check if rate limited
                _is_429 = is_429 or _default_is_429
                _get_retry_after = get_retry_after or _default_get_retry_after

                if _is_429(result):
                    retry_after = _get_retry_after(result)
                    self.on_rate_limited(key, retry_after)
                else:
                    self.on_success(key)

                return result

            return wrapper

        return decorator


# Convenience function
def get_rate_limit_coordinator() -> RateLimitCoordinator:
    """Get the global rate limit coordinator instance."""
    return RateLimitCoordinator.get_instance()
