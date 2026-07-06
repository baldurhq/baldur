"""
Backoff Calculator

Throttle-aware backoff calculation logic.

Provides ThrottleAwareBackoffCalculator which adjusts retry delays
based on system load state (AdaptiveThrottle, SLA warnings, Emergency levels).

For the BackoffStrategy interface (used by retry_with_backoff / RetryPolicy),
use ThrottleAwareBackoffStrategy from strategy_adapter.py.

For simple exponential/linear/constant backoff without throttle-awareness,
use core/backoff.py strategies (ExponentialBackoff, LinearBackoff, etc.).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from .global_state import GlobalThrottleStateManager
from .models import (
    SYSTEM_TIMEOUT_SECONDS,
    BackoffConfig,
    GlobalThrottleState,
    PushBasedThrottleStateCache,
    ThrottleState,
)

if TYPE_CHECKING:
    pass

logger = structlog.get_logger()


# =============================================================================
# Throttle-Aware Backoff Calculator
# =============================================================================


class ThrottleAwareBackoffCalculator:
    """
    AdaptiveThrottle 상태를 인식하는 Backoff 계산기.

    시스템 부하 상태에 따라 동적으로 재시도 간격을 조정합니다.
    Full Stop 시 즉시 DLQ 이동을 위한 신호를 반환합니다.

    For BackoffStrategy interface, use ThrottleAwareBackoffStrategy adapter
    from strategy_adapter.py.
    """

    # 상태별 Backoff 배율
    BACKOFF_MULTIPLIERS: dict[str, float] = {
        "normal": 1.0,
        "sla_warning": 1.5,
        "sla_critical": 2.0,
        "emergency_1_2": 2.5,
        "emergency_3": 4.0,
        "error_budget_critical": 3.0,
    }

    def __init__(
        self,
        config: BackoffConfig | None = None,
        throttle_getter: Callable[[], Any] | None = None,
        enable_push_cache: bool = True,
        use_global_state: bool = False,
        service_name: str = "default",
        error_budget_check_enabled: bool = True,
    ):
        """
        초기화.

        Args:
            config: Backoff 설정
            throttle_getter: AdaptiveThrottle 인스턴스 getter (DI용)
            enable_push_cache: EventBus 푸시 캐싱 활성화 여부
            use_global_state: Redis 기반 글로벌 상태 사용 여부
            service_name: 서비스명 (ThrottleRegistry 연동용)
            error_budget_check_enabled: Error Budget 체크 활성화 여부 (테스트용)
        """
        self.config = config or BackoffConfig.from_settings()
        self._throttle_getter = throttle_getter
        self._enable_push_cache = enable_push_cache
        self._use_global_state = use_global_state
        self._service_name = service_name
        self._error_budget_check_enabled = error_budget_check_enabled
        self._throttle_subscribed: bool = False

        # 푸시 기반 캐시 (기본 활성화)
        self._state_cache = PushBasedThrottleStateCache()
        if enable_push_cache:
            self._subscribe_throttle_events()

        # 글로벌 상태 관리자
        self._global_state_manager = (
            GlobalThrottleStateManager() if use_global_state else None
        )

    def calculate(self, attempt: int, with_jitter: bool = True) -> int:
        """Calculate exponential backoff delay for a given attempt.

        Args:
            attempt: The attempt number (1-based).
            with_jitter: Whether to apply jitter.

        Returns:
            Delay in seconds (integer).
        """
        if attempt < 1:
            return self.config.min_delay

        delay = self.config.base**attempt
        delay = min(delay, self.config.max_delay)

        if with_jitter and self.config.jitter_percent > 0:
            jitter_factor = self.config.jitter_percent / 100.0
            jitter = delay * jitter_factor * (random.random() * 2 - 1)
            delay = int(delay + jitter)

        return max(self.config.min_delay, delay)

    def get_delays_sequence(
        self, max_attempts: int, with_jitter: bool = False
    ) -> list[int]:
        """Get the sequence of delays for multiple attempts."""
        return [
            self.calculate(attempt, with_jitter)
            for attempt in range(1, max_attempts + 1)
        ]

    def _subscribe_throttle_events(self) -> None:
        """Throttle 상태 변경 이벤트 구독 (기본 활성화)."""
        if self._throttle_subscribed:
            return

        try:
            from baldur.services.event_bus import EventType, get_event_bus

            bus = get_event_bus()
            bus.subscribe(EventType.THROTTLE_LIMIT_CHANGED, self._on_throttle_changed)
            bus.subscribe(EventType.THROTTLE_SLA_WARNING, self._on_sla_warning)
            bus.subscribe(EventType.THROTTLE_SLA_CRITICAL, self._on_sla_critical)
            bus.subscribe(EventType.EMERGENCY_LEVEL_CHANGED, self._on_emergency_changed)

            self._throttle_subscribed = True
            logger.debug("throttle_aware_backoff.eventbus_subscription_enabled")
        except Exception as e:
            logger.warning(
                "throttle_aware_backoff.eventbus_subscription_failed",
                error=e,
            )
            self._enable_push_cache = False  # 폴백: 직접 조회 모드

    def close(self) -> None:
        """Unsubscribe all EventBus handlers and release resources.

        Idempotent: safe to call multiple times.
        """
        if not self._throttle_subscribed:
            return

        try:
            from baldur.services.event_bus import EventType, get_event_bus

            bus = get_event_bus()
            bus.unsubscribe(EventType.THROTTLE_LIMIT_CHANGED, self._on_throttle_changed)
            bus.unsubscribe(EventType.THROTTLE_SLA_WARNING, self._on_sla_warning)
            bus.unsubscribe(EventType.THROTTLE_SLA_CRITICAL, self._on_sla_critical)
            bus.unsubscribe(
                EventType.EMERGENCY_LEVEL_CHANGED, self._on_emergency_changed
            )
            self._throttle_subscribed = False
            logger.debug("throttle_aware_backoff.eventbus_unsubscribed")
        except ImportError:
            pass
        except Exception:
            self._throttle_subscribed = False

    def _on_throttle_changed(self, event: Any) -> None:
        """Throttle limit 변경 시 캐시 업데이트."""
        data = event.data if hasattr(event, "data") else event
        reason = data.get("reason", "")

        self._state_cache.last_updated = time.time()
        self._state_cache.full_stop_active = data.get("full_stop", False)

        if self._state_cache.full_stop_active:
            self._state_cache.multiplier = float("inf")
            self._state_cache.reason = "full_stop_active"
        elif "emergency" in reason:
            level = data.get("emergency_level", 1)
            self._state_cache.emergency_level = level
            self._state_cache.multiplier = 4.0 if level >= 3 else 2.5
            self._state_cache.reason = f"emergency_level_{level}"
        elif "sla_critical" in reason:
            self._state_cache.multiplier = 2.0
            self._state_cache.reason = "sla_critical"
        elif "sla_warning" in reason:
            self._state_cache.multiplier = 1.5
            self._state_cache.reason = "sla_warning"
        else:
            self._state_cache.multiplier = 1.0
            self._state_cache.reason = "normal"

    def _on_sla_warning(self, event: Any) -> None:
        """SLA Warning 이벤트 처리."""
        self._state_cache.last_updated = time.time()
        self._state_cache.multiplier = 1.5
        self._state_cache.reason = "sla_warning"

    def _on_sla_critical(self, event: Any) -> None:
        """SLA Critical 이벤트 처리."""
        self._state_cache.last_updated = time.time()
        self._state_cache.multiplier = 2.0
        self._state_cache.reason = "sla_critical"

    def _on_emergency_changed(self, event: Any) -> None:
        """
        Emergency level change event handler.

        GIL-safe: all updates are simple attribute assignments (no compound read-modify-write).
        If migrating to free-threaded Python (PEP 703), add threading.Lock here.
        """
        from baldur.utils.event_filters import should_handle_emergency_event

        if not should_handle_emergency_event(event):
            return

        data = event.data if hasattr(event, "data") else event
        try:
            from baldur.models.emergency import EmergencyLevel

            level = EmergencyLevel(data["level"]).severity
        except Exception:
            level = 0

        self._state_cache.last_updated = time.time()
        self._state_cache.emergency_level = level
        self._state_cache.full_stop_active = level >= 3
        self._state_cache.multiplier = (
            4.0 if level >= 3 else (2.5 if level > 0 else 1.0)
        )
        self._state_cache.reason = f"emergency_level_{level}" if level > 0 else "normal"

    def _get_throttle(self) -> Any | None:
        """서비스별 AdaptiveThrottle 인스턴스 획득 (Fail-Open)."""
        if self._throttle_getter:
            return self._throttle_getter()

        # 서비스별 Throttle 사용 시도
        if self._service_name != "default":
            try:
                from baldur_pro.services.throttle.registry import get_throttle_registry

                return get_throttle_registry().get_throttle(self._service_name)
            except Exception as e:
                logger.debug(
                    "throttle_aware_backoff.registry_lookup_failed",
                    error=e,
                )

        # Fallback: global singleton
        try:
            from baldur.factory.registry import ProviderRegistry

            return ProviderRegistry.adaptive_throttle.safe_get()
        except Exception:
            return None

    def _get_throttle_state(self) -> ThrottleState | None:
        """현재 Throttle 상태 스냅샷 획득."""
        throttle = self._get_throttle()
        if throttle is None:
            # Throttle absent — query Emergency Manager directly for partial state
            try:
                from baldur.factory.registry import ProviderRegistry

                manager = ProviderRegistry.emergency_manager.safe_get()
                if manager is not None:
                    level = manager.get_current_level().severity
                    if level > 0:
                        return ThrottleState(
                            current_limit=0,
                            initial_limit=0,
                            emergency_level=level,
                            full_stop_active=(level >= 3),
                        )
            except Exception:
                pass
            return None

        try:
            stats = throttle.get_stats()
            adaptive_stats = stats.get("adaptive", {})
            emergency_stats = stats.get("emergency", {})

            return ThrottleState(
                current_limit=stats.get("current_limit", 100),
                initial_limit=throttle.config.initial_limit,
                emergency_level=emergency_stats.get("level", 0),
                full_stop_active=emergency_stats.get("full_stop_active", False),
                sla_warning_active=adaptive_stats.get("sla_warnings", 0) > 0,
                sla_critical_active=adaptive_stats.get("sla_criticals", 0) > 0,
                recovery_dampening_active=stats.get("recovery", {}).get(
                    "dampening_active", False
                ),
                error_budget_reduction_active=getattr(
                    throttle, "_error_budget_limit_reduction_active", False
                ),
            )
        except Exception as e:
            logger.debug(
                "throttle_aware_backoff.failed",
                error=e,
            )
            return None

    def _get_throttle_state_cached(self) -> tuple[float, str]:
        """캐시된 상태 반환 (stale 시 직접 조회 폴백)."""
        if self._enable_push_cache and not self._state_cache.is_stale():
            return self._state_cache.multiplier, self._state_cache.reason

        # 폴백: 직접 조회
        state = self._get_throttle_state()
        if state is None:
            return 1.0, "throttle_unavailable"

        multiplier = self._calculate_multiplier(state)
        reason = self._determine_reason(state)
        return multiplier, reason

    def _check_error_budget_critical_or_warning(self) -> bool:
        """
        ErrorBudgetGate CRITICAL 또는 WARNING 상태 확인.

        차단 직전 단계에서도 재시도 빈도를 낮추는 Soft-Landing 전략.
        """
        if not self._error_budget_check_enabled:
            return False

        try:
            from baldur.factory.registry import ProviderRegistry

            try:
                from baldur_pro.services.error_budget_gate.gate import GateStatus
            except ImportError:
                GateStatus = None  # type: ignore[assignment,misc]

            gate = ProviderRegistry.error_budget_gate.safe_get()
            if gate is None:
                return False
            result = gate.check()

            # WARNING 또는 BLOCKED 상태면 배율 적용
            return result.status in (GateStatus.WARNING, GateStatus.BLOCKED)
        except ImportError:
            return False
        except Exception as e:
            logger.debug(
                "throttle_aware_backoff.errorbudgetgate_check_failed",
                error=e,
            )
            return False

    def _calculate_multiplier(self, state: ThrottleState) -> float:
        """상태 기반 Backoff 배율 계산."""
        # Full Stop: 최대 배율 (재시도 차단에 가까움)
        if state.full_stop_active:
            return float("inf")  # 무한대 → execute()에서 즉시 DLQ 이동

        # Error Budget Critical/Warning 우선 검사
        if self._check_error_budget_critical_or_warning():
            return self.BACKOFF_MULTIPLIERS["error_budget_critical"]

        # Emergency LEVEL_3
        if state.emergency_level >= 3:
            return self.BACKOFF_MULTIPLIERS["emergency_3"]

        # Emergency LEVEL_1~2
        if state.emergency_level > 0:
            return self.BACKOFF_MULTIPLIERS["emergency_1_2"]

        # Error Budget Reduction Active (별도 flag)
        if state.error_budget_reduction_active:
            return self.BACKOFF_MULTIPLIERS["error_budget_critical"]

        # SLA Critical
        if state.sla_critical_active:
            return self.BACKOFF_MULTIPLIERS["sla_critical"]

        # SLA Warning
        if state.sla_warning_active:
            return self.BACKOFF_MULTIPLIERS["sla_warning"]

        # 정상 상태
        return self.BACKOFF_MULTIPLIERS["normal"]

    def _determine_reason(self, state: ThrottleState) -> str:
        """상태에서 reason 문자열 결정."""
        if state.full_stop_active:
            return "full_stop_active"
        if state.emergency_level >= 3:
            return "emergency_level_3"
        if state.emergency_level > 0:
            return f"emergency_level_{state.emergency_level}"
        if state.error_budget_reduction_active:
            return "error_budget_critical"
        if state.sla_critical_active:
            return "sla_critical"
        if state.sla_warning_active:
            return "sla_warning"
        return "normal"

    def _calculate_global_multiplier(
        self, state: GlobalThrottleState
    ) -> tuple[float, str]:
        """글로벌 상태 기반 배율 계산."""
        # 클러스터 과반수가 SLA Critical이면 2.0x
        if state.cluster_sla_critical_count > state.reporting_pod_count / 2:
            return 2.0, "cluster_sla_critical"

        # 클러스터 평균 Emergency Level 기반
        if state.cluster_emergency_level >= 3:
            return 4.0, "cluster_emergency_level_3"
        if state.cluster_emergency_level > 0:
            return 2.5, f"cluster_emergency_level_{state.cluster_emergency_level}"

        return 1.0, "cluster_normal"

    def _get_effective_multiplier(self) -> tuple[float, str]:
        """로컬 또는 글로벌 상태 기반 배율 계산."""
        if self._use_global_state and self._global_state_manager:
            global_state = self._global_state_manager.get_global_state()
            if global_state:
                return self._calculate_global_multiplier(global_state)

        # 폴백: 로컬 상태
        return self._get_throttle_state_cached()

    def _record_backoff_metrics(
        self,
        domain: str,
        original_delay: int,
        adjusted_delay: int,
        multiplier: float,
        reason: str,
    ) -> None:
        """Prometheus 메트릭 기록."""
        try:
            from baldur.services.metrics.definitions import (
                retry_backoff_adjusted_seconds,
                retry_backoff_multiplier,
                retry_backoff_original_seconds,
                retry_throttle_full_stop_skips_total,
            )

            retry_backoff_multiplier.labels(domain=domain, reason=reason).observe(
                multiplier
            )
            retry_backoff_original_seconds.labels(domain=domain).observe(original_delay)
            retry_backoff_adjusted_seconds.labels(domain=domain).observe(adjusted_delay)

            if multiplier == float("inf"):
                retry_throttle_full_stop_skips_total.labels(domain=domain).inc()

        except ImportError:
            pass  # Fail-Open
        except Exception as e:
            logger.debug(
                "throttle_aware_backoff.metrics_recording_failed",
                error=e,
            )

    def calculate_with_throttle_context(
        self,
        attempt: int,
        with_jitter: bool = True,
    ) -> tuple[int, float, str]:
        """
        Throttle 상태를 고려한 Backoff 계산.

        Args:
            attempt: 재시도 횟수
            with_jitter: Jitter 적용 여부

        Returns:
            (adjusted_delay, multiplier, reason) 튜플.
            delay=-1은 Full Stop 즉시 DLQ 이동 신호.
        """
        base_delay = self.calculate(attempt, with_jitter)

        state = self._get_throttle_state()
        if state is None:
            return base_delay, 1.0, "throttle_unavailable"

        multiplier = self._calculate_multiplier(state)

        # Full Stop 시 무한대 → 특수 처리
        if multiplier == float("inf"):
            self._record_backoff_metrics(
                domain=self._service_name,
                original_delay=base_delay,
                adjusted_delay=-1,
                multiplier=multiplier,
                reason="full_stop_active",
            )
            return -1, float("inf"), "full_stop_active"

        adjusted_delay = int(base_delay * multiplier)

        # 시스템 타임아웃 기준 cap (임의적 2배 대신)
        if adjusted_delay > SYSTEM_TIMEOUT_SECONDS:
            logger.warning(
                "throttle_aware_backoff.delay_capped",
                computed_delay=base_delay * multiplier,
                SYSTEM_TIMEOUT_SECONDS=SYSTEM_TIMEOUT_SECONDS,
            )
            adjusted_delay = SYSTEM_TIMEOUT_SECONDS

        reason = self._determine_reason(state)

        # 메트릭 기록
        self._record_backoff_metrics(
            domain=self._service_name,
            original_delay=base_delay,
            adjusted_delay=adjusted_delay,
            multiplier=multiplier,
            reason=reason,
        )

        return adjusted_delay, multiplier, reason


# =============================================================================
# Singleton
# =============================================================================

import threading

_calculator: ThrottleAwareBackoffCalculator | None = None
_calculator_lock = threading.Lock()


def get_backoff_calculator(
    **kwargs: Any,
) -> ThrottleAwareBackoffCalculator:
    """Return the ThrottleAwareBackoffCalculator singleton.

    Args:
        **kwargs: Forwarded to ThrottleAwareBackoffCalculator.__init__
            on first creation only.
    """
    global _calculator
    if _calculator is None:
        with _calculator_lock:
            if _calculator is None:
                _calculator = ThrottleAwareBackoffCalculator(**kwargs)
    return _calculator


def reset_backoff_calculator() -> None:
    """Reset the singleton (calls close() before clearing)."""
    global _calculator
    with _calculator_lock:
        if _calculator is not None:
            _calculator.close()
        _calculator = None
