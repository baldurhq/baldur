"""
EventBus Hook — EventBus CONFIG_UPDATED 이벤트를 Policy 설정 갱신에 중개.

PolicyComposer와 함께 사용하여 런타임 설정 변경을 Policy에 전파한다.
EventBus가 없는 환경에서는 아무 동작도 하지 않는다 (Fail-Open).

현재 HedgingConfigUpdateHook이 동일 기능을 수행하며,
이 모듈은 범용 EventBus Hook 확장점으로 배치한다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.resilience_policy import PolicyResult

if TYPE_CHECKING:
    from baldur.interfaces.resilience_policy import PolicyContext

logger = structlog.get_logger()


class EventBusHook:
    """EventBus 이벤트를 PolicyComposer Hook으로 관찰하는 범용 Hook.

    파이프라인 성공/실패/거부 이벤트를 EventBus로 발행한다.
    EventBus가 없는 환경에서는 아무 동작도 하지 않는다.
    """

    def __init__(self, event_prefix: str = "policy_pipeline") -> None:
        """
        Args:
            event_prefix: EventBus 이벤트 키 접두사.
        """
        self._event_prefix = event_prefix
        self._bus: Any = None
        self._initialized = False

    def _ensure_bus(self) -> bool:
        """EventBus lazy 초기화. 사용 불가 시 False 반환."""
        if self._initialized:
            return self._bus is not None

        self._initialized = True
        try:
            from baldur.services.event_bus import get_event_bus

            self._bus = get_event_bus()
            return True
        except ImportError:
            logger.debug("event_bus.not_available")
            return False
        except Exception as e:
            logger.warning(
                "eventbus.initialization_failed",
                error=e,
            )
            return False

    def on_execute(
        self, policy_name: str, attempt: int, context: PolicyContext | None = None
    ) -> None:
        """실행 시작."""

    def on_success(
        self,
        policy_name: str,
        result: PolicyResult,
        context: PolicyContext | None = None,
    ) -> None:
        """파이프라인 성공 시 EventBus에 이벤트 발행."""
        if not self._ensure_bus():
            return

        try:
            self._bus.publish(
                f"{self._event_prefix}.success",
                {
                    "policies": result.executed_policies,
                    "attempts": result.total_attempts,
                    "duration_ms": result.total_duration_ms,
                },
            )
        except Exception as e:
            logger.debug(
                "eventbus.publish_failed_fail",
                error=e,
            )

    def on_failure(
        self,
        policy_name: str,
        error: Exception,
        attempt: int,
        context: PolicyContext | None = None,
    ) -> None:
        """파이프라인 실패 시 EventBus에 이벤트 발행."""
        if not self._ensure_bus():
            return

        try:
            self._bus.publish(
                f"{self._event_prefix}.failure",
                {
                    "error_type": type(error).__name__,
                    "error_message": str(error)[:500],
                    "attempts": attempt,
                },
            )
        except Exception as e:
            logger.debug(
                "eventbus.publish_failed_fail",
                error=e,
            )

    def on_retry(
        self,
        policy_name: str,
        attempt: int,
        delay: float,
        context: PolicyContext | None = None,
    ) -> None:
        """재시도 — Composer 레벨에서는 미사용."""

    def on_reject(
        self, guard_name: str, reason: str, context: PolicyContext | None = None
    ) -> None:
        """파이프라인 거부 시 EventBus에 이벤트 발행."""
        if not self._ensure_bus():
            return

        try:
            self._bus.publish(
                f"{self._event_prefix}.rejected",
                {
                    "guard": guard_name,
                    "reason": reason,
                },
            )
        except Exception as e:
            logger.debug(
                "eventbus.publish_failed_fail",
                error=e,
            )
