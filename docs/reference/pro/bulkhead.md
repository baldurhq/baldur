# baldur_pro.services.bulkhead — Bulkhead

Concurrency isolation primitives: `BulkheadPolicy`, the semaphore and
thread-pool bulkheads, and the `@bulkhead` decorator.

Bulkhead compartments are not PRO-only. The semaphore compartments, the
registry, the `@bulkhead` decorator, and the metrics all ship in the OSS core
([OSS bulkhead reference](../services/bulkhead.md)). This page documents the PRO
package, which adds thread-pool isolation: dedicated worker pools with
execution-timeout containment and a graceful-shutdown drain. See
[Bulkhead](../../concepts/foundations/bulkhead.md) for the tier split.

!!! info "🔒 PRO Feature — requires a baldur-pro license"
    These symbols ship in the `baldur-pro` distribution. PRO modules import
    normally — there is no `ImportError`. PRO features activate only when
    `baldur.init()` runs with a valid `BALDUR_LICENSE_KEY`; without it the system
    runs with OSS defaults and `register_pro_services()` logs
    `entitlement.pro_registration_skipped`.

::: baldur_pro.services.bulkhead
