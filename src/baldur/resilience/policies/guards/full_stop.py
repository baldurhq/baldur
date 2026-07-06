"""
FullStopGuard — Emergency LEVEL_3 + DB CB OPEN + Error Budget 소진 3중 조건 Guard.

AdaptiveThrottle.check_full_stop_conditions()에서 하드코딩된
CircuitBreakerService, ErrorBudgetService, EmergencyMode 직접 참조를
생성자 주입 기반 Guard로 분리한다.

3중 조건이 모두 충족되어야 거부되므로 단일 조건 실패 시에도
다른 Guard(ThrottleGovernanceGuard 등)가 개별적으로 차단할 수 있다.

Fail-Open 원칙:
    각 provider가 import/호출 실패 시 조건 미충족으로 간주 (통과 허용).
    create_default_full_stop_guard() 팩토리 함수로 기본 provider를 자동 구성.

사용 예시::

    from baldur.resilience.policies.guards.full_stop import (
        create_default_full_stop_guard,
    )
    policy.add_guard(create_default_full_stop_guard())
"""

from __future__ import annotations

from collections.abc import Callable

import structlog

from baldur.interfaces.resilience_policy import (
    GuardResult,
    PolicyContext,
)

logger = structlog.get_logger()


class FullStopGuard:
    """
    Full Stop 3중 조건 Guard.

    3개 조건이 모두 참일 때만 거부한다:
    1. Emergency LEVEL_3 이상
    2. 핵심 DB Circuit Breaker OPEN
    3. Error Budget 완전 소진 (0% 이하)

    각 조건은 Callable provider로 주입받으며,
    create_default_full_stop_guard()에서 lazy import 기반으로 자동 구성된다.
    """

    def __init__(
        self,
        emergency_provider: Callable[[], int],
        cb_state_provider: Callable[[str], str],
        budget_provider: Callable[[], float],
    ) -> None:
        """
        생성자 주입으로 외부 시스템 의존성을 Callable로 추상화.

        Args:
            emergency_provider: Emergency Level 반환 (0=NORMAL, 3=CRITICAL)
            cb_state_provider: 서비스명 → CB 상태 ("open"/"closed") 반환
            budget_provider: Error Budget 잔여 퍼센트 반환
        """
        self._get_emergency_level = emergency_provider
        self._get_cb_state = cb_state_provider
        self._get_budget_remaining = budget_provider

    @property
    def name(self) -> str:
        """Guard 식별자."""
        return "full_stop"

    def check(self, context: PolicyContext | None = None) -> GuardResult:
        """
        Full Stop 3중 조건 체크.

        3개 조건이 모두 충족될 때만 거부한다.
        개별 조건 실패 시 통과 (다른 Guard가 개별 차단 담당).

        Returns:
            GuardResult(allowed=False) — 3중 조건 모두 충족 시
            GuardResult(allowed=True) — 그 외
        """
        is_level_3 = self._get_emergency_level() >= 3
        db_cb_open = self._get_cb_state("database") == "open"
        budget_exhausted = self._get_budget_remaining() <= 0

        if is_level_3 and db_cb_open and budget_exhausted:
            return GuardResult(
                allowed=False,
                reason="full_stop:LEVEL_3+DB_CB_OPEN+BUDGET_EXHAUSTED",
                metadata={
                    "emergency_level": self._get_emergency_level(),
                    "db_cb_state": "open",
                    "budget_remaining": self._get_budget_remaining(),
                },
            )

        return GuardResult(allowed=True)


def create_default_full_stop_guard() -> FullStopGuard:  # noqa: C901
    """
    기본 FullStopGuard 생성.

    CircuitBreakerService, ErrorBudgetService, EmergencyMode를
    lazy import하여 provider를 자동 구성한다.
    각 provider가 import 실패 시 Fail-Open (조건 미충족 = 통과 허용).

    Returns:
        FullStopGuard 인스턴스
    """

    def _get_emergency_level() -> int:
        """Emergency Level lookup (fail-open: 0=NORMAL)."""
        try:
            from baldur.factory.registry import ProviderRegistry

            manager = ProviderRegistry.emergency_manager.safe_get()
            if manager is None:
                return 0
            return manager.get_current_level().severity
        except Exception:
            return 0

    def _get_cb_state(service: str) -> str:
        """핵심 DB Circuit Breaker 상태 조회 (Fail-Open → "closed")."""
        try:
            from baldur.services.circuit_breaker import (
                get_circuit_breaker_service,
            )

            cb_service = get_circuit_breaker_service()
            db_services = [
                "database",
                "db",
                "postgres",
                "mysql",
                "redis",
                "mongodb",
            ]
            for db_name in db_services:
                try:
                    state = cb_service.get_state(db_name)
                    if state == "open":
                        return "open"
                except Exception:
                    pass
            return "closed"
        except ImportError:
            return "closed"
        except Exception:
            return "closed"

    def _get_budget_remaining() -> float:
        """Error budget remaining percent (fail-open: 100.0)."""
        try:
            from baldur.factory.registry import ProviderRegistry

            service = ProviderRegistry.error_budget_service.safe_get()
            if service is None:
                return 100.0
            return service.get_budget_status().budget_remaining_percent
        except Exception:
            return 100.0

    return FullStopGuard(
        emergency_provider=_get_emergency_level,
        cb_state_provider=_get_cb_state,
        budget_provider=_get_budget_remaining,
    )
