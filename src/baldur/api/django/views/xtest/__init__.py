"""
X-Test-Mode (Chaos Monkey) Control Views Package

Test-only API that bypasses the Rate Limiter (L1) to observe L2/L3 behavior
directly.

Security:
- X-Test-Mode: chaos-monkey header required
- DEBUG or the CHAOS_ENABLED environment variable required
- Fully blocked in production environments

Regional Scope (region boundary enforcement):
- GLOBAL scope APIs require the X-Region header
- The X-Region value must match the current cluster region
  (BALDUR_NAMESPACE_REGION) to be allowed
- On region mismatch, 403 Forbidden (cross_region_xtest_denied)

GLOBAL Scope APIs (X-Region header required):
- xtest/emergency/global/* : global Emergency state changes
- xtest/isolation/region/* : region isolation operations
- xtest/governance/global/* : global governance settings

LOCAL Scope APIs (X-Region header not required):
- All other X-Test APIs (DLQ, CB, Replay, etc.)

Endpoints:
- POST /api/baldur/xtest/inject-cb-failure/ - inject a CB failure
- POST /api/baldur/xtest/reset-cb/ - reset CB state
- GET  /api/baldur/xtest/cb-status/ - check CB state (detailed)
- POST /api/baldur/xtest/inject-error-budget/ - consume error budget
- GET  /api/baldur/xtest/snapshot/ - system snapshot
- GET  /api/baldur/xtest/fast-fail-test/ - fast-fail verification
- POST /api/baldur/xtest/trigger-cb-recovery/ - trigger CB recovery

Stage 51 Observability:
- GET  /api/baldur/xtest/healing-timeline/ - healing timeline
- POST /api/baldur/xtest/blast-radius-test/ - blast radius test
- POST /api/baldur/xtest/multi-blast-radius/ - multi-service isolation matrix
- POST /api/baldur/xtest/generate-postmortem/ - generate a post-mortem
- POST /api/baldur/xtest/record-healing-event/ - record a healing event
- GET  /api/baldur/xtest/healing-incidents/ - incident list

DLQ Test Endpoints:
- POST /api/baldur/xtest/dlq/inject/ - create DLQ test entries
- GET  /api/baldur/xtest/dlq/status/ - query DLQ status
- POST /api/baldur/xtest/dlq/force-status/ - force a DLQ status change
- POST /api/baldur/xtest/dlq/reset/ - reset entries created by X-Test-Mode
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
