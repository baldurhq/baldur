# MetricsAwareCacheAdapter — Metrics-Decorating Cache Backend

Wraps any `CacheProviderInterface` backend with Prometheus hit/miss counters,
leaving the wrapped adapter's behavior unchanged. Compose it over
`RedisCacheAdapter` or `InMemoryCacheAdapter`.

::: baldur.adapters.cache.MetricsAwareCacheAdapter
