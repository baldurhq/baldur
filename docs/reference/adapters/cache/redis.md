# RedisCacheAdapter — Redis Cache Backend

The production-default `CacheProviderInterface` implementation. Backs
distributed locks with Redlock-style primitives so cache coordination is safe
across processes and hosts.

::: baldur.adapters.cache.RedisCacheAdapter

## Async Redis adapter

An `AsyncCacheProviderInterface` implementation over `redis.asyncio`, backing
the awaitable idempotency dedup gate so `aprotect(idempotency_key=…)` performs
its cross-worker dedup via native `await`. Shares key-prefix and serialization
with `RedisCacheAdapter`, so async and sync writes hit the same Redis keys.

::: baldur.adapters.cache.AsyncRedisCacheAdapter
