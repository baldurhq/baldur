# baldur_pro.services.replay — Replay Queue

Backpressure-aware replay of stored failures: `ReplayQueueService` with its
`BackpressureStatus` and `RateLimitStatus` signals.

Replaying stored failures is not PRO-only. The OSS `ReplayService`
([Service access](../services/access.md)) handles single-entry replay, batch
replay by failure type, and the automatic sweep that runs when a circuit
breaker recovers. This page documents the PRO replay queue layered on top, which paces
replay at scale with backpressure and rate-limit signals. See
[DLQ + Replay](../../concepts/foundations/dlq-replay.md) for the tier split.

!!! info "🔒 PRO Feature — requires a baldur-pro license"
    These symbols ship in the `baldur-pro` distribution. PRO modules import
    normally — there is no `ImportError`. PRO features activate only when
    `baldur.init()` runs with a valid `BALDUR_LICENSE_KEY`; without it the system
    runs with OSS defaults and `register_pro_services()` logs
    `entitlement.pro_registration_skipped`.

::: baldur_pro.services.replay
