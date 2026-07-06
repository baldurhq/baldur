# InMemoryCacheAdapter — In-Memory Cache Backend

A `CacheProviderInterface` implementation for tests and single-process usage,
with no external Redis dependency. Locks are process-local.

::: baldur.adapters.cache.InMemoryCacheAdapter

## Async in-memory adapter

An `AsyncCacheProviderInterface` implementation backing the async idempotency
fallback path and serving as the async dedup test double (no Redis, no
`fakeredis`).

::: baldur.adapters.cache.AsyncInMemoryCacheAdapter
