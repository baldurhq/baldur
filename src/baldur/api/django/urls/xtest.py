"""X-Test-Mode URL patterns (chaos proof + integration testing endpoints).

Always available but protected by the ``X-Test-Mode`` request header plus
environment checks (DEBUG / ENABLE_XTEST). Per-view auth gates the actual
behavior — this module only wires the routes.
"""

from __future__ import annotations

from django.urls import path

from baldur.api.django.views.xtest import (
    BackoffPreviewView,
    BlastRadiusTestView,
    CBStatusDetailView,
    CheckDuplicateView,
    ClearKeysView,
    DLQXTestStatusView,
    FastFailTestView,
    ForceStatusView,
    FullSnapshotView,
    GenerateKeyView,
    HealingTimelineView,
    IdempotencyStatusView,
    InjectCBFailureView,
    InjectDLQEntryView,
    InjectErrorBudgetView,
    MultiServiceBlastRadiusView,
    RateLimitClientView,
    RateLimitConfigXTestView,
    RateLimitHistoryView,
    RateLimitResetView,
    RateLimitStatusView,
    RecordHealingEventView,
    RegisterKeyView,
    ReplayBatchView,
    ReplaySingleView,
    ReplayStatusView,
    ResetCBView,
    ResetDLQXTestView,
    ResetView,
    RetryRateLimitStatusView,
    RetrySimulateView,
    RunScenarioView,
    ScenarioStatusView,
    SwitchToAutoModeView,
    SystemSnapshotView,
    ThrottleCBOpenSimulationView,
    ThrottleEmergencySimulationView,
    ThrottleRTTDelayInjectionView,
    ThrottleXTestResetView,
    ThrottleXTestStatusView,
    TriggerCBRecoveryView,
    TriggerReplayOnCBCloseView,
    TryRecoveryTransitionView,
    XTestRetryConfigView,
)

urlpatterns = [
    # Chaos Monkey API (requires X-Test-Mode: chaos-monkey header)
    path(
        "xtest/inject-cb-failure/",
        InjectCBFailureView.as_view(),
        name="xtest-inject-cb-failure",
    ),
    path("xtest/reset-cb/", ResetCBView.as_view(), name="xtest-reset-cb"),
    path("xtest/cb-status/", CBStatusDetailView.as_view(), name="xtest-cb-status"),
    path(
        "xtest/switch-to-auto/",
        SwitchToAutoModeView.as_view(),
        name="xtest-switch-to-auto",
    ),
    path(
        "xtest/try-recovery-transition/",
        TryRecoveryTransitionView.as_view(),
        name="xtest-try-recovery-transition",
    ),
    path(
        "xtest/inject-error-budget/",
        InjectErrorBudgetView.as_view(),
        name="xtest-inject-error-budget",
    ),
    path("xtest/snapshot/", SystemSnapshotView.as_view(), name="xtest-snapshot"),
    path(
        "xtest/fast-fail-test/", FastFailTestView.as_view(), name="xtest-fast-fail-test"
    ),
    path(
        "xtest/trigger-cb-recovery/",
        TriggerCBRecoveryView.as_view(),
        name="xtest-trigger-cb-recovery",
    ),
    # Stage 51: Observability & Blast Radius
    path(
        "xtest/healing-timeline/",
        HealingTimelineView.as_view(),
        name="xtest-healing-timeline",
    ),
    path(
        "xtest/blast-radius-test/",
        BlastRadiusTestView.as_view(),
        name="xtest-blast-radius-test",
    ),
    path(
        "xtest/record-healing-event/",
        RecordHealingEventView.as_view(),
        name="xtest-record-healing-event",
    ),
    path(
        "xtest/multi-blast-radius/",
        MultiServiceBlastRadiusView.as_view(),
        name="xtest-multi-blast-radius",
    ),
    # DLQ X-Test
    path("xtest/dlq/inject/", InjectDLQEntryView.as_view(), name="xtest-dlq-inject"),
    path("xtest/dlq/status/", DLQXTestStatusView.as_view(), name="xtest-dlq-status"),
    path(
        "xtest/dlq/force-status/",
        ForceStatusView.as_view(),
        name="xtest-dlq-force-status",
    ),
    path("xtest/dlq/reset/", ResetDLQXTestView.as_view(), name="xtest-dlq-reset"),
    # Replay X-Test
    path(
        "xtest/replay/single/", ReplaySingleView.as_view(), name="xtest-replay-single"
    ),
    path("xtest/replay/batch/", ReplayBatchView.as_view(), name="xtest-replay-batch"),
    path(
        "xtest/replay/trigger-on-cb-close/",
        TriggerReplayOnCBCloseView.as_view(),
        name="xtest-replay-trigger-cb-close",
    ),
    path(
        "xtest/replay/status/", ReplayStatusView.as_view(), name="xtest-replay-status"
    ),
    # Retry X-Test
    path(
        "xtest/retry/backoff-preview/",
        BackoffPreviewView.as_view(),
        name="xtest-retry-backoff-preview",
    ),
    path(
        "xtest/retry/simulate/",
        RetrySimulateView.as_view(),
        name="xtest-retry-simulate",
    ),
    path(
        "xtest/retry/rate-limit-status/",
        RetryRateLimitStatusView.as_view(),
        name="xtest-retry-rate-limit-status",
    ),
    path(
        "xtest/retry/config/", XTestRetryConfigView.as_view(), name="xtest-retry-config"
    ),
    # Rate Limit X-Test
    path(
        "xtest/rate-limit/status/",
        RateLimitStatusView.as_view(),
        name="xtest-rate-limit-status",
    ),
    path(
        "xtest/rate-limit/client/",
        RateLimitClientView.as_view(),
        name="xtest-rate-limit-client",
    ),
    path(
        "xtest/rate-limit/history/",
        RateLimitHistoryView.as_view(),
        name="xtest-rate-limit-history",
    ),
    path(
        "xtest/rate-limit/config/",
        RateLimitConfigXTestView.as_view(),
        name="xtest-rate-limit-config",
    ),
    path(
        "xtest/rate-limit/reset/",
        RateLimitResetView.as_view(),
        name="xtest-rate-limit-reset",
    ),
    # Idempotency X-Test
    path(
        "xtest/idempotency/generate-key/",
        GenerateKeyView.as_view(),
        name="xtest-idempotency-generate-key",
    ),
    path(
        "xtest/idempotency/check-duplicate/",
        CheckDuplicateView.as_view(),
        name="xtest-idempotency-check-duplicate",
    ),
    path(
        "xtest/idempotency/status/",
        IdempotencyStatusView.as_view(),
        name="xtest-idempotency-status",
    ),
    path(
        "xtest/idempotency/register/",
        RegisterKeyView.as_view(),
        name="xtest-idempotency-register",
    ),
    path(
        "xtest/idempotency/clear/",
        ClearKeysView.as_view(),
        name="xtest-idempotency-clear",
    ),
    # Integration X-Test (component integration tests)
    path(
        "xtest/integration/run-scenario/",
        RunScenarioView.as_view(),
        name="xtest-integration-run-scenario",
    ),
    path(
        "xtest/integration/scenario/<str:scenario_id>/",
        ScenarioStatusView.as_view(),
        name="xtest-integration-scenario-status",
    ),
    path(
        "xtest/integration/full-snapshot/",
        FullSnapshotView.as_view(),
        name="xtest-integration-full-snapshot",
    ),
    path(
        "xtest/integration/reset/", ResetView.as_view(), name="xtest-integration-reset"
    ),
    # Throttle X-Test
    path(
        "xtest/throttle/simulate-emergency/",
        ThrottleEmergencySimulationView.as_view(),
        name="xtest-throttle-simulate-emergency",
    ),
    path(
        "xtest/throttle/simulate-cb-open/",
        ThrottleCBOpenSimulationView.as_view(),
        name="xtest-throttle-simulate-cb-open",
    ),
    path(
        "xtest/throttle/inject-rtt-delay/",
        ThrottleRTTDelayInjectionView.as_view(),
        name="xtest-throttle-inject-rtt-delay",
    ),
    path(
        "xtest/throttle/status/",
        ThrottleXTestStatusView.as_view(),
        name="xtest-throttle-status",
    ),
    path(
        "xtest/throttle/reset/",
        ThrottleXTestResetView.as_view(),
        name="xtest-throttle-reset",
    ),
]
