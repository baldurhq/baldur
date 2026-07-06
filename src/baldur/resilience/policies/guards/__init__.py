"""
Policy Guards — 사전 검증 모듈.

PolicyComposer 파이프라인 실행 전 전역/티어별 조건을 검증하는
Guard 구현체를 제공한다.

- KillSwitchGuard: 시스템 전역 활성/비활성 체크
- ErrorBudgetGuard: 에러 버짓 잔여량 체크
- ThrottleGovernanceGuard: Kill Switch/Emergency/ErrorBudget/BreakGlass 통합
- FullStopGuard: Emergency LEVEL_3 + DB CB OPEN + Budget 소진 3중 조건
- LoadSheddingGuard: 우선순위 기반 Load Shedding
- BackpressureGuard: RateController 기반 큐 과부하 방지
"""

from baldur.resilience.policies.guards.backpressure import BackpressureGuard
from baldur.resilience.policies.guards.error_budget import ErrorBudgetGuard
from baldur.resilience.policies.guards.full_stop import (
    FullStopGuard,
    create_default_full_stop_guard,
)
from baldur.resilience.policies.guards.governance import ThrottleGovernanceGuard
from baldur.resilience.policies.guards.kill_switch import KillSwitchGuard
from baldur.resilience.policies.guards.load_shedding import LoadSheddingGuard

__all__ = [
    "BackpressureGuard",
    "ErrorBudgetGuard",
    "FullStopGuard",
    "KillSwitchGuard",
    "LoadSheddingGuard",
    "ThrottleGovernanceGuard",
    "create_default_full_stop_guard",
]
