# baldur_pro.services.dlq — Dead-Letter Queue

Durable capture and replay of failed operations: the `DLQService`, the
`store_to_dlq` entry point, and the DLQ domain models.

The dead-letter queue itself is not PRO-only. A plain `pip install baldur-framework`
captures failed operations, browses them, and retries, resolves, or
force-redrives a single entry. This page documents the PRO layer on top of that
core, which adds the operate-at-scale surface: batch replay, compressed-summary
overflow, a disk-durable outbox, and archive/purge retention. See
[DLQ + Replay](../../concepts/foundations/dlq-replay.md) for the tier split and
[`@dlq_protect`](../decorators.md) for the OSS entry point.

!!! info "🔒 PRO Feature — requires a baldur-pro license"
    These symbols ship in the `baldur-pro` distribution. PRO modules import
    normally — there is no `ImportError`. PRO features activate only when
    `baldur.init()` runs with a valid `BALDUR_LICENSE_KEY`; without it the system
    runs with OSS defaults and `register_pro_services()` logs
    `entitlement.pro_registration_skipped`.

::: baldur_pro.services.dlq
