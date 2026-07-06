"""
BackpressureGuard — RateController 기반 Backpressure Guard.

TrafficGate의 3번째 구성요소인 RateController를
PolicyComposer Guard로 분리한다.

ThrottlePolicy(SlidingWindowThrottle)와는 알고리즘/목적이 완전히 다르다:
    - SlidingWindowThrottle: 외부 API 호출 제한 (서비스 보호), Sliding Window
    - RateController: 내부 큐 과부하 방지 (backpressure), Token Bucket + AIMD

Fail-Open 원칙:
    RateController 미설치/호출 실패 시 통과 허용.

사용 예시::

    from baldur.resilience.policies.guards.backpressure import (
        BackpressureGuard,
    )
    policy.add_guard(BackpressureGuard())
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.interfaces.resilience_policy import (
    GuardResult,
    PolicyContext,
)

logger = structlog.get_logger()


class BackpressureGuard:
    """
    RateController 기반 Backpressure Guard.

    Token Bucket + AIMD 패턴의 큐 기반 backpressure를 Guard로 래핑.
    RateController.should_process()가 False이면 거부한다.

    ThrottlePolicy와 독립적이며, PolicyComposer에서 add_guard()로 등록한다.
    """

    def __init__(self, rate_controller: Any | None = None) -> None:
        """
        초기화.

        Args:
            rate_controller: RateController 인스턴스.
                None이면 lazy import로 글로벌 인스턴스를 획득한다.
        """
        self._controller = rate_controller
        self._initialized = rate_controller is not None

    @property
    def name(self) -> str:
        """Guard 식별자."""
        return "backpressure"

    def check(self, context: PolicyContext | None = None) -> GuardResult:
        """
        Backpressure 체크.

        RateController.should_process()가 False이면
        큐 과부하로 인한 거부를 반환한다.

        Returns:
            GuardResult(allowed=True) — 처리 허용
            GuardResult(allowed=False, reason=...) — backpressure 거부
        """
        controller = self._get_rate_controller()
        if controller is None:
            return GuardResult(allowed=True)

        try:
            if not controller.should_process():
                state = controller.get_state()
                level_value = (
                    state.level.value
                    if hasattr(state.level, "value")
                    else str(state.level)
                )
                return GuardResult(
                    allowed=False,
                    reason=f"backpressure:level={level_value}",
                    metadata={
                        "backpressure_level": level_value,
                        "queue_size": state.queue_size,
                        "current_rate": state.current_rate,
                    },
                )
        except Exception as e:
            logger.warning(
                "guard.check_failed_fail_open",
                guard_name="backpressure",
                check="should_process",
                error=str(e),
                exc_info=True,
            )

        return GuardResult(allowed=True)

    def _get_rate_controller(self) -> Any | None:
        """RateController 인스턴스 획득 (lazy import, Fail-Open)."""
        if self._initialized:
            return self._controller

        try:
            from baldur.scaling.rate_controller import get_rate_controller

            self._controller = get_rate_controller()
            self._initialized = True
            return self._controller
        except ImportError:
            logger.debug(
                "guard.dependency_missing",
                guard_name="backpressure",
                dependency="baldur.scaling.rate_controller",
            )
            self._initialized = True
            return None
        except Exception as e:
            logger.warning(
                "guard.check_failed_fail_open",
                guard_name="backpressure",
                check="controller_init",
                error=str(e),
                exc_info=True,
            )
            self._initialized = True
            return None
