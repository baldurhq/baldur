# baldur_pro.services.dlq — Dead-Letter Queue

Durable capture and replay of failed operations: the `DLQService`, the
`store_to_dlq` entry point, and the DLQ domain models.

!!! info "🔒 PRO Feature — requires a baldur-pro license"
    These symbols ship in the `baldur-pro` distribution. PRO modules import
    normally — there is no `ImportError`. PRO features activate only when
    `baldur.init()` runs with a valid `BALDUR_LICENSE_KEY`; without it the system
    runs with OSS defaults and `register_pro_services()` logs
    `entitlement.pro_registration_skipped`.

::: baldur_pro.services.dlq
