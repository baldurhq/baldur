# baldur.interfaces — Cache, Locking & Rate Limiting

The cache-provider interface with its distributed-lock primitive and lock
exceptions, plus the rate-limit storage contract and the adaptive-throttle
marker.

## Cache & locking

::: baldur.interfaces.CacheProviderInterface

::: baldur.interfaces.AsyncCacheProviderInterface

::: baldur.interfaces.DistributedLock

::: baldur.interfaces.LockAcquisitionError

::: baldur.interfaces.LockNotOwnedError

::: baldur.interfaces.generate_lock_owner_id

## Rate limiting & throttle

::: baldur.interfaces.RateLimitStorageType

::: baldur.interfaces.RateLimitState

::: baldur.interfaces.RateLimitStorageInterface

::: baldur.interfaces.RateLimitStorageError

::: baldur.interfaces.RateLimitStorageUnavailableError

::: baldur.interfaces.AdaptiveThrottle
