# baldur.interfaces — Singleton Protocols & PRO-Boundary Markers

Protocol markers for the singletons resolved through `ProviderRegistry` — the
governance and admin-identity contracts, plus the PRO-boundary service
protocols (DLQ, emergency, bulkhead, canary, throttle, and others). OSS code
consumes these via the registry; PRO ships the implementations.

## Governance & admin identity

::: baldur.interfaces.GovernanceChecker

::: baldur.interfaces.NoOpGovernanceChecker

::: baldur.interfaces.AdminIdentityResolver

::: baldur.interfaces.AdminPrincipal

## Service singleton protocols

::: baldur.interfaces.Bulkhead

::: baldur.interfaces.BulkheadRegistry

::: baldur.interfaces.CanaryRollout

::: baldur.interfaces.CanaryRolloutService

::: baldur.interfaces.DLQRepository

::: baldur.interfaces.DLQService

::: baldur.interfaces.EmergencyManager

::: baldur.interfaces.SelfhealerWatchdog

::: baldur.interfaces.UnifiedNotificationManager
