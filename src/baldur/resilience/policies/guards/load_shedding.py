"""
LoadSheddingGuard — 우선순위 기반 Load Shedding Guard.

TrafficGate의 Load Shedding 체크(CascadeLoadShedding.should_accept)를
PolicyComposer Guard로 분리한다.

ThrottlePolicy는 요청의 priority 정보를 알 필요가 없다.
우선순위 기반 거부는 이 Guard가 context.extra["priority"]에서 읽어 판단한다.

Fail-Open 원칙:
    CascadeLoadShedding import/호출 실패 시 통과 허용.

사용 예시::

    from baldur.resilience.policies.guards.load_shedding import (
        LoadSheddingGuard,
    )
    policy.add_guard(LoadSheddingGuard())
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.interfaces.resilience_policy import (
    GuardResult,
    PolicyContext,
)

logger = structlog.get_logger()


class LoadSheddingGuard:
    """
    우선순위 기반 Load Shedding Guard.

    CascadeLoadShedding.should_accept(priority=priority)를 래핑하여
    현재 shedding 레벨에서 해당 우선순위 요청의 허용 여부를 판정한다.

    context.extra["priority"]에서 요청 우선순위를 읽는다.
    context=None 또는 priority 미지정 시 통과 허용.
    """

    def __init__(self, load_shedding: Any | None = None) -> None:
        """
        초기화.

        Args:
            load_shedding: CascadeLoadShedding 인스턴스.
                None이면 lazy import로 글로벌 인스턴스를 획득한다.
        """
        self._load_shedding = load_shedding
        self._initialized = load_shedding is not None

    @property
    def name(self) -> str:
        """Guard 식별자."""
        return "load_shedding"

    def check(self, context: PolicyContext | None = None) -> GuardResult:
        """
        우선순위 기반 Load Shedding 체크.

        context.extra["priority"]에서 요청 우선순위를 읽는다.
        context=None이면 전역 체크 (priority=0, 통과 허용).

        Returns:
            GuardResult(allowed=True) — 통과
            GuardResult(allowed=False, reason=...) — shedding 거부
        """
        shedding = self._get_load_shedding()
        if shedding is None:
            return GuardResult(allowed=True)

        priority = 0
        if context and context.extra:
            priority = context.extra.get("priority", 0)

        try:
            result = shedding.should_accept(priority=priority)
            if isinstance(result, dict) and not result.get("accepted", True):
                return GuardResult(
                    allowed=False,
                    reason=f"load_shedding_rejected:priority={priority}",
                    metadata={"priority": priority},
                )
        except Exception as e:
            logger.warning(
                "guard.check_failed_fail_open",
                guard_name="load_shedding",
                check="should_accept",
                error=str(e),
                exc_info=True,
            )

        return GuardResult(allowed=True)

    def _get_load_shedding(self) -> Any | None:
        """CascadeLoadShedding 인스턴스 획득 (lazy import, Fail-Open)."""
        if self._initialized:
            return self._load_shedding

        try:
            from baldur.audit.cascade_load_shedding import (
                get_cascade_load_shedding,
            )

            self._load_shedding = get_cascade_load_shedding()
            self._initialized = True
            return self._load_shedding
        except ImportError:
            logger.debug(
                "guard.dependency_missing",
                guard_name="load_shedding",
                dependency="baldur.audit.cascade_load_shedding",
            )
            self._initialized = True
            return None
        except Exception as e:
            logger.warning(
                "guard.check_failed_fail_open",
                guard_name="load_shedding",
                check="controller_init",
                error=str(e),
                exc_info=True,
            )
            self._initialized = True
            return None
