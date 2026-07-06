"""
Rate-aware Backpressure Controller.

동적으로 처리율을 조절하여 과부하를 방지합니다.
AIMD (Additive Increase, Multiplicative Decrease) 패턴 적용.

주의:
    이 모듈은 동기(Threading) 환경용입니다.
    asyncio 환경에서는 이벤트 루프를 블로킹할 수 있습니다.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from baldur.scaling.config import (
    BackpressureLevel,
    BackpressureSettings,
    BackpressureStrategy,
    get_backpressure_settings,
)

if TYPE_CHECKING:
    from baldur.meta.daemon_worker import (  # noqa: F401
        DaemonWorkerHandle,
    )
    from baldur.scaling.metrics import BackpressureMetrics

logger = structlog.get_logger()


# Priority별 토큰 비율 임계치 (Watermark).
# 현재 토큰 잔량 비율이 이 값 미만이면 해당 priority의 요청을 거부한다.
# 하위 호환: 기존 import 유지. 동적 변경은 BackpressureSettings 필드를 통해 수행.
# should_process() 내부에서는 settings에서 매번 읽어 동적 변경을 반영한다.
PRIORITY_WATERMARKS: dict[str, float] = {
    "critical": 0.0,
    "standard": 0.3,
    "non_essential": 0.6,
}


@dataclass
class RateControllerState:
    """Rate Controller 현재 상태."""

    current_rate: float
    """현재 처리율 (항목/초)."""

    target_rate: float
    """목표 처리율."""

    level: BackpressureLevel
    """Backpressure 레벨."""

    queue_size: int
    """현재 큐 크기."""

    processed_count: int
    """처리된 항목 수."""

    dropped_count: int
    """버려진 항목 수."""

    dropped_by_tier: dict[str, int] | None = None
    """Tier별 거부 항목 수 (critical / standard / non_essential)."""

    processed_by_tier: dict[str, int] | None = None
    """Tier별 처리 항목 수 (critical / standard / non_essential)."""


class TokenBucket:
    """
    Token Bucket 알고리즘 구현.

    Rate Limit을 구현하기 위한 토큰 버킷.
    토큰이 일정 속도로 충전되고, 요청 시 토큰을 소비합니다.
    """

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
    ):
        """
        Args:
            rate: 초당 토큰 생성율
            capacity: 최대 토큰 수 (None이면 rate와 동일)
        """
        self._rate = rate
        self._capacity = capacity or rate
        self._tokens = self._capacity
        self._last_update = time.time()
        self._lock = threading.Lock()

    def set_rate(self, rate: float) -> None:
        """Rate 변경."""
        with self._lock:
            self._rate = rate

    def get_rate(self) -> float:
        """현재 Rate 반환."""
        with self._lock:
            return self._rate

    def consume(self, tokens: int = 1) -> bool:
        """
        토큰 소비 시도.

        Args:
            tokens: 소비할 토큰 수

        Returns:
            소비 성공 여부
        """
        with self._lock:
            now = time.time()
            elapsed = now - self._last_update
            self._last_update = now

            # 토큰 충전 (시간 경과에 따라)
            self._tokens = min(
                self._capacity,
                self._tokens + elapsed * self._rate,
            )

            # 토큰 소비 시도
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def get_token_ratio(self) -> float:
        """현재 토큰 잔량 비율 반환 (0.0 ~ 1.0).

        충전량을 반영하되 _last_update는 갱신하지 않는다 (읽기 전용).
        """
        with self._lock:
            now = time.time()
            elapsed = now - self._last_update
            current = min(self._capacity, self._tokens + elapsed * self._rate)
            return current / self._capacity if self._capacity > 0 else 0.0

    def wait_for_token(self, timeout: float = 1.0) -> bool:
        """
        토큰을 대기하며 획득 시도.

        주의:
            time.sleep()을 사용하므로 asyncio 환경에서는
            이벤트 루프를 블로킹합니다.

        Args:
            timeout: 최대 대기 시간 (초)

        Returns:
            토큰 획득 성공 여부
        """
        start = time.time()
        while time.time() - start < timeout:
            if self.consume():
                return True
            time.sleep(0.01)
        return False


# Starvation Relief 설정 상수
STARVATION_RELIEF_SECONDS = 300.0
"""연속 거부 시간(초) 초과 시 watermark 완화. 기본 5분."""

STARVATION_RELIEF_WATERMARK = 0.3
"""완화 시 적용할 watermark (standard tier와 동일 수준)."""


class RateController:
    """
    Rate-aware Backpressure Controller.

    기능:
    - 큐 크기 기반 Backpressure 레벨 계산
    - 동적 Rate 조절 (AIMD 패턴)
    - 전략 기반 처리 (Throttle, Drop, Reject)

    Usage:
        controller = RateController()
        controller.start()

        if controller.should_process():
            process_item()
        else:
            # Backpressure 활성화됨
            pass

        controller.stop()
    """

    def __init__(
        self,
        settings: BackpressureSettings | None = None,
        queue_size_provider: Callable[[], int] | None = None,
        metrics: BackpressureMetrics | None = None,
    ):
        """
        Args:
            settings: Backpressure 설정
            queue_size_provider: 큐 크기 제공 함수
            metrics: Prometheus per-tier 메트릭 인스턴스 (None이면 Prometheus 미발행)
        """
        self._settings = settings or get_backpressure_settings()
        self._queue_size_provider = queue_size_provider or (lambda: 0)
        self._metrics = metrics

        self._lock = threading.RLock()
        self._current_rate = self._settings.max_rate_per_second
        self._level = BackpressureLevel.NONE
        self._token_bucket = TokenBucket(self._current_rate)

        # 통계
        self._processed_count = 0
        self._dropped_count = 0
        self._dropped_by_tier: dict[str, int] = {
            "critical": 0,
            "standard": 0,
            "non_essential": 0,
        }
        self._processed_by_tier: dict[str, int] = {
            "critical": 0,
            "standard": 0,
            "non_essential": 0,
        }

        # 백그라운드 조절 스레드
        self._running = False
        self._worker: threading.Thread | None = None
        self._handle: DaemonWorkerHandle | None = None  # impl 489 D9

        # Redis sync tracking (for multi-process LS stats)
        self._last_flushed_processed = 0
        self._last_flushed_dropped = 0
        self._last_flushed_by_tier: dict[str, int] = {}

        # Starvation Relief: tier별 마지막 허용 시각 (monotonic)
        self._tier_last_allowed: dict[str, float] = {
            "critical": time.monotonic(),
            "standard": time.monotonic(),
            "non_essential": time.monotonic(),
        }

        # External level from Throttle SLA (PX4 bridge)
        self._external_level: BackpressureLevel = BackpressureLevel.NONE
        self._external_level_until: float = 0.0

        # EventBus subscription tracking
        self._sla_subscribed: bool = False

    def get_state(self) -> RateControllerState:
        """현재 상태 반환."""
        with self._lock:
            return RateControllerState(
                current_rate=self._current_rate,
                target_rate=self._settings.max_rate_per_second,
                level=self._level,
                queue_size=self._queue_size_provider(),
                processed_count=self._processed_count,
                dropped_count=self._dropped_count,
                dropped_by_tier=dict(self._dropped_by_tier),
                processed_by_tier=dict(self._processed_by_tier),
            )

    def should_process(self, priority: str = "standard") -> bool:  # noqa: C901, PLR0912
        """
        처리 여부 결정 (priority 기반 Watermark 적용).

        현재 토큰 잔량 비율이 priority별 watermark 미만이면
        토큰 소비를 시도하지 않고 즉시 거부한다.
        이를 통해 토큰이 적을 때 critical 요청을 우선 보호한다.

        Args:
            priority: 요청 priority tier.
                "critical" | "standard" | "non_essential".
                기본값 "standard"는 기존 호출부와 하위 호환.

        Returns:
            True면 처리, False면 Backpressure로 거부
        """
        if not self._settings.backpressure_enabled:
            return True

        # Watermark 확인: settings에서 동적으로 읽어 런타임 변경 반영
        watermarks = self._settings.get_priority_watermarks()
        watermark = watermarks.get(priority, 0.3)
        token_ratio = self._token_bucket.get_token_ratio()

        if token_ratio < watermark:
            # Starvation Relief: N분간 연속 거부된 tier는 watermark 임시 완화
            relief_applied = False
            if priority in self._tier_last_allowed:
                elapsed = time.monotonic() - self._tier_last_allowed[priority]
                if (
                    elapsed > STARVATION_RELIEF_SECONDS
                    and self._check_starvation_relief_allowed()
                ):
                    watermark = min(watermark, STARVATION_RELIEF_WATERMARK)
                    logger.warning(
                        "rate_controller.starvation_relief_applied",
                        tier=priority,
                        elapsed_seconds=elapsed,
                        relaxed_watermark=watermark,
                    )
                    relief_applied = True

            if (not relief_applied or token_ratio < watermark) and not relief_applied:
                self._record_drop(priority)
                logger.debug(
                    "rate_controller.request_rejected",
                    tier=priority,
                    reason="watermark_exceeded",
                    token_ratio=token_ratio,
                    watermark=watermark,
                )
                return False

        # Token Bucket에서 토큰 소비 시도 (단일 버킷)
        if self._token_bucket.consume():
            self._record_process(priority)
            return True

        # 토큰 부족 시 전략에 따른 처리
        logger.debug(
            "rate_controller.request_rejected",
            tier=priority,
            reason="token_exhausted",
            token_ratio=token_ratio,
        )
        strategy = self._settings.default_strategy

        if strategy == BackpressureStrategy.REJECT:
            self._record_drop(priority)
            return False

        if strategy == BackpressureStrategy.THROTTLE:
            # 잠시 대기 후 재시도
            if self._token_bucket.wait_for_token(timeout=0.1):
                self._record_process(priority)
                return True
            self._record_drop(priority)
            return False

        if strategy == BackpressureStrategy.DROP_OLDEST:
            # DROP_OLDEST는 호출자가 처리
            return True

        if strategy == BackpressureStrategy.QUEUE:
            # QUEUE는 호출자가 처리
            return True

        return True

    def _record_drop(self, priority: str) -> None:
        """Record a dropped request: increment counters and fire metric.

        Counter and metric only — caller is responsible for logging because
        the log reason ("watermark_exceeded" vs "token_exhausted") and the
        accompanying extras differ per call site.
        """
        with self._lock:
            self._dropped_count += 1
            if priority in self._dropped_by_tier:
                self._dropped_by_tier[priority] += 1
        if self._metrics is not None:
            self._metrics.inc_dropped_by_tier(priority)

    def _record_process(self, priority: str) -> None:
        """Record a processed request: increment counters, update last_allowed, fire metric."""
        with self._lock:
            self._processed_count += 1
            if priority in self._processed_by_tier:
                self._processed_by_tier[priority] += 1
            self._tier_last_allowed[priority] = time.monotonic()
        if self._metrics is not None:
            self._metrics.inc_processed_by_tier(priority)

    def _check_starvation_relief_allowed(self) -> bool:
        """Starvation Relief 활성화 전 시스템 안정성 확인.

        RecoveryGate와 동일한 기준(CPU < 80%, error_rate < 5%)을 사용하여
        과부하 상태에서 Relief가 트래픽을 증가시키는 것을 방지한다.

        Returns:
            True면 Relief 허용, False면 차단
        """
        try:
            from baldur_pro.services.emergency_mode.recovery_gate import (
                RecoveryGate,
            )

            gate = RecoveryGate()
            allowed, reason = gate.check_recovery_allowed()
            if not allowed:
                logger.info(
                    "rate_controller.starvation_relief_blocked",
                    reason=reason,
                )
            return allowed
        except Exception:
            # RecoveryGate 사용 불가 시 안전하게 Relief 차단
            return False

    def _get_resource_pressure_multiplier(self) -> float:
        """CPU 사용률 기반 Rate 감쇠 배율.

        SystemMetricsCache에서 캐시된 CPU 사용률을 읽어
        임계치에 따라 Rate 배율을 결정한다.
        캐시 읽기는 ~0ms (Lock-free, GIL atomic 참조 교체).

        Returns:
            1.0 (정상), 0.5 (CPU >= high_threshold), 0.1 (CPU >= critical_threshold)
        """
        try:
            from baldur.services.system_metrics_cache import (
                get_cached_cpu_percent,
            )

            cpu = get_cached_cpu_percent()
            if cpu >= self._settings.resource_cpu_critical_threshold:
                return 0.1
            if cpu >= self._settings.resource_cpu_high_threshold:
                return 0.5
        except Exception:
            pass
        return 1.0

    def _adjust_rate(self) -> None:
        """
        Rate 조절 (AIMD 패턴).

        - 과부하 시: 레벨별 차등 감소 (Multiplicative Decrease)
        - 정상화 시: 점진적 증가 (Additive Increase)
        """
        queue_size = self._queue_size_provider()
        queue_level = self._settings.get_level_for_queue_size(queue_size)

        # Expire external level TTL
        with self._lock:
            if (
                self._external_level != BackpressureLevel.NONE
                and time.time() > self._external_level_until
            ):
                self._external_level = BackpressureLevel.NONE

            # max(queue, external) — conservative policy
            new_level = max(queue_level, self._external_level)
            self._level = new_level

        # AIMD 패턴: 레벨별 Rate 배율 적용
        if new_level == BackpressureLevel.NONE:
            # 정상: 점진적 증가 (Additive Increase)
            new_rate = self._current_rate * self._settings.rate_increase_factor
        else:
            # 과부하: 레벨별 차등 감소 (Multiplicative Decrease)
            multiplier = self._settings.get_rate_multiplier(new_level)
            new_rate = self._settings.max_rate_per_second * multiplier

        # CPU 사용률 기반 추가 감쇠 적용
        resource_multiplier = self._get_resource_pressure_multiplier()
        new_rate *= resource_multiplier

        # 범위 제한
        new_rate = max(
            self._settings.min_rate_per_second,
            min(self._settings.max_rate_per_second, new_rate),
        )

        with self._lock:
            if new_rate != self._current_rate:
                self._current_rate = new_rate
                self._token_bucket.set_rate(new_rate)
                logger.info(
                    "rate_controller.rate_adjusted",
                    new_rate=new_rate,
                    new_level=new_level.value,
                    queue_size=queue_size,
                    rate_multiplier=self._settings.get_rate_multiplier(new_level),
                )

    def _flush_to_redis(self) -> None:
        """Flush counter deltas to Redis for multi-process aggregation."""
        try:
            from datetime import timedelta

            from baldur.factory import ProviderRegistry
            from baldur.utils.time import utc_now

            cache = ProviderRegistry.get_cache()
            date_key = utc_now().strftime("%Y-%m-%d")
            with self._lock:
                d_processed = self._processed_count - self._last_flushed_processed
                d_dropped = self._dropped_count - self._last_flushed_dropped
                d_by_tier = {
                    t: self._dropped_by_tier[t] - self._last_flushed_by_tier.get(t, 0)
                    for t in self._dropped_by_tier
                }
                self._last_flushed_processed = self._processed_count
                self._last_flushed_dropped = self._dropped_count
                self._last_flushed_by_tier = dict(self._dropped_by_tier)
            prefix = f"baldur:rate_controller:{date_key}"
            ttl = timedelta(hours=48)
            if d_processed > 0:
                cache.incr(f"{prefix}:processed", d_processed)
                cache.expire(f"{prefix}:processed", ttl)
            if d_dropped > 0:
                cache.incr(f"{prefix}:dropped", d_dropped)
                cache.expire(f"{prefix}:dropped", ttl)
            for tier, delta in d_by_tier.items():
                if delta > 0:
                    cache.incr(f"{prefix}:dropped:{tier}", delta)
                    cache.expire(f"{prefix}:dropped:{tier}", ttl)
        except Exception:
            pass  # fail-open — Redis down does not affect rate limiting

    def _run_loop(self) -> None:
        """Background rate adjustment loop."""
        while self._running:
            iter_start = time.monotonic()
            try:
                self._adjust_rate()
            except Exception as e:
                logger.exception(
                    "rate_controller.adjust_error",
                    error=e,
                )

            if self._settings.redis_sync_enabled:
                self._flush_to_redis()

            if self._handle is not None:
                self._handle.observe_iteration(time.monotonic() - iter_start)
                self._handle.heartbeat()

            time.sleep(self._settings.rate_adjust_interval_seconds)

    def _run_loop_with_crash_capture(self) -> None:
        try:
            self._run_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def start(self) -> None:
        """Rate 조절 시작."""
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        if not self._settings.backpressure_enabled:
            logger.info("rate_controller.disabled")
            return

        if self._running:
            return

        self._subscribe_throttle_sla_events()

        self._running = True
        self._spawn_worker_thread()
        assert self._worker is not None  # populated by _spawn_worker_thread
        self._handle = DaemonWorkerHandle(
            thread=self._worker,
            tick_interval_seconds=self._settings.rate_adjust_interval_seconds,
            restart_callback=self._spawn_worker_thread,
        )
        register_daemon_worker("RateController", self._handle)
        logger.info("rate_controller.started")

    def _spawn_worker_thread(self) -> None:
        """Construct + start a fresh rate-adjust thread (impl 489 D9)."""
        self._worker = threading.Thread(
            target=self._run_loop_with_crash_capture,
            name="RateController",
            daemon=True,
        )
        self._worker.start()
        if self._handle is not None:
            self._handle.thread = self._worker

    def _subscribe_throttle_sla_events(self) -> None:
        """Subscribe to THROTTLE_SLA_CRITICAL for external level bridge (Fail-Open).

        Skipped when backpressure_enabled=False — no need for external signals
        when backpressure is disabled.
        """
        if not self._settings.backpressure_enabled:
            return

        if self._sla_subscribed:
            return

        try:
            from baldur.services.event_bus import EventType, get_event_bus

            bus = get_event_bus()
            bus.subscribe(
                EventType.THROTTLE_SLA_CRITICAL,
                self._handle_throttle_sla_critical,
            )
            self._sla_subscribed = True
            logger.info("rate_controller.subscribed_throttle_sla_events")
        except ImportError:
            logger.debug("rate_controller.eventbus_unavailable")
        except Exception as e:
            logger.warning(
                "rate_controller.subscribe_throttle_sla_failed",
                error=e,
            )

    def _handle_throttle_sla_critical(self, event) -> None:
        """Set external backpressure level on Throttle SLA critical event.

        Fixed at BackpressureLevel.HIGH — Throttle's reduction_percent is always
        30% hardcoded, so dynamic mapping would be dead code. Each event reception
        renews the TTL (lease pattern).
        """
        with self._lock:
            self._external_level = BackpressureLevel.HIGH
            self._external_level_until = (
                time.time() + self._settings.external_level_ttl_seconds
            )

    def stop(self) -> None:
        """Rate 조절 중지 및 EventBus 구독 해제."""
        # Unsubscribe EventBus handlers first
        if self._sla_subscribed:
            try:
                from baldur.services.event_bus import EventType, get_event_bus

                bus = get_event_bus()
                bus.unsubscribe(
                    EventType.THROTTLE_SLA_CRITICAL,
                    self._handle_throttle_sla_critical,
                )
                self._sla_subscribed = False
                logger.debug("rate_controller.unsubscribed_throttle_sla_events")
            except ImportError:
                pass
            except Exception:
                self._sla_subscribed = False

        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker
        from baldur.settings.thread_management import (
            get_thread_management_settings,
        )

        if self._handle is not None:
            self._handle.is_stopping = True
        self._running = False
        timeout = get_thread_management_settings().join_timeout
        if self._worker:
            self._worker.join(timeout=timeout)
        unregister_daemon_worker("RateController")
        if self._worker is not None and self._worker.is_alive():
            logger.critical(
                "daemon_worker.stop_join_timeout",
                worker_name="RateController",
                join_timeout_seconds=timeout,
            )
        logger.info("rate_controller.stopped")


# =============================================================================
# Singleton
# =============================================================================

from baldur.utils.singleton import CLEANUP_STOP, make_singleton_factory


def _create_rate_controller() -> RateController:
    from baldur.scaling.metrics import get_backpressure_metrics

    return RateController(metrics=get_backpressure_metrics())


get_rate_controller, configure_rate_controller, reset_rate_controller = (
    make_singleton_factory(
        "rate_controller", _create_rate_controller, cleanup_fn=CLEANUP_STOP
    )
)
