# baldur.services — Bulkhead

Per-domain concurrency isolation: the bulkhead base class and its semaphore
implementations, the per-domain registry, the `@bulkhead` decorator, policy
adapters for composition, and the rejection/timeout exceptions.

## Core & implementations

::: baldur.services.bulkhead.Bulkhead

::: baldur.services.bulkhead.SemaphoreBulkhead

::: baldur.services.bulkhead.AsyncSemaphoreBulkhead

::: baldur.services.bulkhead.BulkheadState

::: baldur.services.bulkhead.BulkheadType

## Registry

::: baldur.services.bulkhead.BulkheadRegistry

::: baldur.services.bulkhead.get_bulkhead_registry

::: baldur.services.bulkhead.reset_bulkhead_registry

## Decorator & policy

::: baldur.services.bulkhead.bulkhead

::: baldur.services.bulkhead.bulkhead_for_database

::: baldur.services.bulkhead.bulkhead_for_cache

::: baldur.services.bulkhead.BulkheadPolicy

::: baldur.services.bulkhead.AsyncBulkheadPolicy

::: baldur.services.bulkhead.bulkhead_policy

::: baldur.services.bulkhead.async_bulkhead_policy

## Metrics

::: baldur.services.bulkhead.BulkheadMetricsUpdater

::: baldur.services.bulkhead.get_metrics_updater

::: baldur.services.bulkhead.start_metrics_updater

::: baldur.services.bulkhead.stop_metrics_updater

::: baldur.services.bulkhead.update_bulkhead_metrics

::: baldur.services.bulkhead.increment_rejected_count

## Exceptions

::: baldur.services.bulkhead.BulkheadError

::: baldur.services.bulkhead.BulkheadFullError

::: baldur.services.bulkhead.BulkheadTimeoutError

::: baldur.services.bulkhead.BulkheadNotFoundError
