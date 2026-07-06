"""
X-Test-Mode (Chaos Monkey) Control Views Package

Rate Limiter(L1)를 우회하여 L2/L3 동작을 직접 관찰하기 위한 테스트 전용 API.

Security:
- X-Test-Mode: chaos-monkey 헤더 필수
- DEBUG 또는 CHAOS_ENABLED 환경 변수 필요
- production 환경에서는 완전 차단

Regional Scope (리전 경계 강제):
- GLOBAL scope API는 X-Region 헤더 필수
- X-Region 값이 현재 클러스터 리전(BALDUR_NAMESPACE_REGION)과 일치해야 허용
- 리전 불일치 시 403 Forbidden (cross_region_xtest_denied)

GLOBAL Scope API (X-Region 헤더 필수):
- xtest/emergency/global/* : 전역 Emergency 상태 변경
- xtest/isolation/region/* : 리전 격리 조작
- xtest/governance/global/* : 전역 거버넌스 설정

LOCAL Scope API (X-Region 헤더 불필요):
- 그 외 모든 X-Test API (DLQ, CB, Replay 등)

Endpoints:
- POST /api/baldur/xtest/inject-cb-failure/ - CB 장애 주입
- POST /api/baldur/xtest/reset-cb/ - CB 상태 초기화
- GET  /api/baldur/xtest/cb-status/ - CB 상태 확인 (상세)
- POST /api/baldur/xtest/inject-error-budget/ - Error Budget 차감
- GET  /api/baldur/xtest/snapshot/ - 시스템 스냅샷
- GET  /api/baldur/xtest/fast-fail-test/ - Fast Fail 검증
- POST /api/baldur/xtest/trigger-cb-recovery/ - CB 복구 트리거

Stage 51 Observability:
- GET  /api/baldur/xtest/healing-timeline/ - 힐링 타임라인
- POST /api/baldur/xtest/blast-radius-test/ - Blast Radius 테스트
- POST /api/baldur/xtest/multi-blast-radius/ - 다중 서비스 격리 매트릭스
- POST /api/baldur/xtest/generate-postmortem/ - Post-mortem 생성
- POST /api/baldur/xtest/record-healing-event/ - 힐링 이벤트 기록
- GET  /api/baldur/xtest/healing-incidents/ - 인시던트 목록

DLQ Test Endpoints:
- POST /api/baldur/xtest/dlq/inject/ - DLQ 테스트 항목 생성
- GET  /api/baldur/xtest/dlq/status/ - DLQ 현황 조회
- POST /api/baldur/xtest/dlq/force-status/ - DLQ 상태 강제 변경
- POST /api/baldur/xtest/dlq/reset/ - X-Test-Mode 생성 항목 초기화
"""

# Base utilities and Regional Scope constants
# Incident functions from postmortem_store
from baldur.dlq.helpers import (
    add_healing_incident,
    get_healing_incidents,
    get_healing_incidents_count,
)

from .base import (
    GLOBAL_SCOPE_ENDPOINT_PATTERNS,
    XTestModeMixin,
    add_healing_event,
    collect_system_snapshot,
    get_healing_events,
    get_healing_events_count,
)

# Circuit Breaker views
from .circuit_breaker import (
    CBStatusDetailView,
    FastFailTestView,
    InjectCBFailureView,
    ResetCBView,
    SwitchToAutoModeView,  # New! For releasing manually_controlled state
    TriggerCBRecoveryView,
    TryRecoveryTransitionView,  # Domain-free OPEN → HALF_OPEN transition
)

# DLQ X-Test views
from .dlq import (
    DLQXTestStatusView,
    ForceStatusView,
    InjectDLQEntryView,
    ResetDLQXTestView,
)

# Error Budget views
from .error_budget import (
    InjectErrorBudgetView,
)

# Idempotency X-Test views
from .idempotency import (
    CheckDuplicateView,
    ClearKeysView,
    GenerateKeyView,
    IdempotencyStatusView,
    RegisterKeyView,
)

# Integration X-Test views
from .integration import (
    FullSnapshotView,
    ResetView,
    RunScenarioView,
    ScenarioStatusView,
)

# Observability views (Stage 51)
from .observability import (
    BlastRadiusTestView,
    HealingTimelineView,
    MultiServiceBlastRadiusView,
    RecordHealingEventView,
)

# Rate Limit X-Test views
from .rate_limit import (
    RateLimitClientView,
    RateLimitConfigXTestView,
    RateLimitHistoryView,
    RateLimitResetView,
    RateLimitStatusView,
)

# Replay X-Test views
from .replay import (
    ReplayBatchView,
    ReplaySingleView,
    ReplayStatusView,
    TriggerReplayOnCBCloseView,
)

# Retry X-Test views
from .retry import (
    BackoffPreviewView,
    RetryRateLimitStatusView,
    RetrySimulateView,
    XTestRetryConfigView,
)

# Integration Scenario utilities
from .scenarios import (
    SCENARIO_REGISTRY,
    IntegrationScenario,
    ScenarioResult,
    ScenarioStatus,
    get_scenario_class,
    list_available_scenarios,
)

# Snapshot views
from .snapshot import (
    SystemSnapshotView,
)

# Throttle Simulation X-Test views
from .throttle_simulation import (
    ThrottleCBOpenSimulationView,
    ThrottleEmergencySimulationView,
    ThrottleRTTDelayInjectionView,
)
from .throttle_simulation import (
    ThrottleResetView as ThrottleXTestResetView,
)
from .throttle_simulation import (
    ThrottleStatusView as ThrottleXTestStatusView,
)

__all__ = [
    # Base utilities
    "XTestModeMixin",
    "GLOBAL_SCOPE_ENDPOINT_PATTERNS",
    "collect_system_snapshot",
    "add_healing_event",
    "add_healing_incident",
    "get_healing_events",
    "get_healing_events_count",
    "get_healing_incidents",
    "get_healing_incidents_count",
    # Circuit Breaker views
    "InjectCBFailureView",
    "ResetCBView",
    "CBStatusDetailView",
    "FastFailTestView",
    "TriggerCBRecoveryView",
    "TryRecoveryTransitionView",  # Domain-free OPEN → HALF_OPEN
    "SwitchToAutoModeView",  # New!
    # Error Budget views
    "InjectErrorBudgetView",
    # Snapshot views
    "SystemSnapshotView",
    # Observability views (Stage 51)
    "HealingTimelineView",
    "BlastRadiusTestView",
    "MultiServiceBlastRadiusView",
    "RecordHealingEventView",
    # DLQ X-Test views
    "InjectDLQEntryView",
    "DLQXTestStatusView",
    "ForceStatusView",
    "ResetDLQXTestView",
    # Replay X-Test views
    "ReplaySingleView",
    "ReplayBatchView",
    "TriggerReplayOnCBCloseView",
    "ReplayStatusView",
    # Retry X-Test views
    "BackoffPreviewView",
    "RetrySimulateView",
    "RetryRateLimitStatusView",
    "XTestRetryConfigView",
    # Rate Limit X-Test views
    "RateLimitStatusView",
    "RateLimitClientView",
    "RateLimitHistoryView",
    "RateLimitConfigXTestView",
    "RateLimitResetView",
    # Idempotency X-Test views
    "GenerateKeyView",
    "CheckDuplicateView",
    "IdempotencyStatusView",
    "RegisterKeyView",
    "ClearKeysView",
    # Integration X-Test views
    "RunScenarioView",
    "ScenarioStatusView",
    "FullSnapshotView",
    "ResetView",
    # Integration Scenario utilities
    "SCENARIO_REGISTRY",
    "IntegrationScenario",
    "ScenarioResult",
    "ScenarioStatus",
    "get_scenario_class",
    "list_available_scenarios",
    # Throttle Simulation X-Test views
    "ThrottleEmergencySimulationView",
    "ThrottleCBOpenSimulationView",
    "ThrottleRTTDelayInjectionView",
    "ThrottleXTestStatusView",
    "ThrottleXTestResetView",
]
