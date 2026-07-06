# baldur.interfaces — Singleton Protocols & PRO-Boundary Markers

Protocol markers for the singletons resolved through `ProviderRegistry` — the
governance and admin-identity contracts, plus the PRO-boundary service
protocols (DLQ, emergency, error budget, bulkhead, canary, chaos, throttle, and
others). OSS code consumes these via the registry; PRO ships the
implementations.

## Governance & admin identity

::: baldur.interfaces.GovernanceChecker

::: baldur.interfaces.NoOpGovernanceChecker

::: baldur.interfaces.AdminIdentityResolver

::: baldur.interfaces.AdminPrincipal

## Service singleton protocols

::: baldur.interfaces.BlastRadiusManager

::: baldur.interfaces.Bulkhead

::: baldur.interfaces.BulkheadRegistry

::: baldur.interfaces.CanaryRollout

::: baldur.interfaces.CanaryRolloutService

::: baldur.interfaces.ChaosScheduler

::: baldur.interfaces.ReportGenerator

::: baldur.interfaces.SafetyGuard

::: baldur.interfaces.DLQRepository

::: baldur.interfaces.DLQService

::: baldur.interfaces.EmergencyManager

::: baldur.interfaces.ErrorBudgetGate

::: baldur.interfaces.ErrorBudgetService

::: baldur.interfaces.LearningServiceProtocol

::: baldur.interfaces.SelfhealerWatchdog

::: baldur.interfaces.UnifiedNotificationManager
