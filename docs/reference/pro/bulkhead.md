# baldur_pro.services.bulkhead — Bulkhead

Concurrency isolation primitives: `BulkheadPolicy`, the semaphore and
thread-pool bulkheads, and the `@bulkhead` decorator.

!!! info "🔒 PRO Feature — requires a baldur-pro license"
    These symbols ship in the `baldur-pro` distribution. PRO modules import
    normally — there is no `ImportError`. PRO features activate only when
    `baldur.init()` runs with a valid `BALDUR_LICENSE_KEY`; without it the system
    runs with OSS defaults and `register_pro_services()` logs
    `entitlement.pro_registration_skipped`.

::: baldur_pro.services.bulkhead
