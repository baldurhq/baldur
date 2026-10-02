# baldur_pro.services.replay — Replay Queue

`ReplayQueueService`: an in-memory queue with token-bucket rate limiting and
`BackpressureStatus` / `RateLimitStatus` signals. Nothing in Baldur dequeues
from it; code that enqueues also dequeues and processes the entries.

Replaying stored failures is not PRO-only. The OSS `ReplayService`
([Service access](../services/access.md)) handles single-entry replay, batch
replay by failure type, and the automatic sweep that runs when a circuit
breaker recovers, and that replay does not go through this queue. See
[DLQ + Replay](../../concepts/foundations/dlq-replay.md) for the tier split.

!!! info "🔒 PRO Feature — requires a baldur-pro license"
    These symbols ship in the `baldur-pro` distribution. PRO modules import
    normally — there is no `ImportError`. PRO features activate only when
    `baldur.init()` runs with a valid `BALDUR_LICENSE_KEY`; without it the system
    runs with OSS defaults and `register_pro_services()` logs
    `entitlement.pro_registration_skipped`.

::: baldur_pro.services.replay
