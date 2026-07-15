# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This changelog covers the open-source core (`baldur-framework`). PRO release
notes are published separately at <https://baldur.sh/concepts/pro/release-notes/>.

## [Unreleased]

### Added

- OSS DLQ now durably captures + replays failed operations (`@protected(dlq=True)`; no PRO required).
- Remote-Prometheus time-series metrics provider (`BALDUR_PROMETHEUS_URL`).
- `config_shadow` time-series metrics-provider DI seam.
- Result-predicate retry (`retry_on_result`) and a `BALDUR_RETRY_MAX_ELAPSED` wall-clock budget.
- `protect()` fallback callables may take the triggering error: `fallback(error)`.

### Changed

- Outbound 429-backoff env vars move to `BALDUR_RATE_LIMIT_BACKOFF_*`. **Breaking**
- `ServiceConfig` is now immutable. **Breaking**
- `TTLCacheBase.get_stats()` returns a locked snapshot, not the live object.
- Circuit-breaker state values are now lowercase. **Breaking**
- Admin config-write endpoints reject unknown fields with `400`. **Breaking**
- `import baldur` is now lightweight — hot-path barrels load lazily (251→8 modules).
- Retry `outcome="exhausted"` now excludes non-retryable/budget/deadline aborts.
- `protect()` fallback runs after retry; timeout/CB-open covered, CB still trips. **Breaking**

### Removed

- Circuit-breaker canary-recovery cluster and its public API. **Breaking**
- `StaleCacheStore` moved to `baldur.core.stale_cache`. **Breaking**
- `IPCStateCache.stats` — use `get_stats()`. **Breaking**
- `baldur.core.timezone` — use `baldur.utils.time.utc_now`. **Breaking**
- `baldur.settings.audit_settings` alias — use `from baldur.settings import audit`. **Breaking**
- `POOL_CB_*` pool circuit-breaker env vars — use `BALDUR_POOL_CB_*`. **Breaking**

### Security

- Config serializers no longer leak non-validation exception messages.
- WAL crash-recovery caps oversized record length prefixes (OOM guard).

### Fixed

- `protect_with_meta().attempts` reflects the real retry count with a fallback set (was `1`).
- A protected builtin `TimeoutError` is no longer misreported as a policy timeout.
- Circuit-breaker `503` now sends an accurate `Retry-After` (was hardcoded).
- Real client IP resolves behind `X-Real-IP`-only proxies.
- Internal retry backoff is now jittered; RQ honors `retry_jitter`.
- Sync `RedisCacheAdapter` honors `BALDUR_REDIS_*` socket/retry settings.
- First retry waits `base_delay`, not `base_delay / multiplier`.
- Audit buffer reports its true dropped-entry count in `get_stats()`.
- In-memory circuit-breaker rate-limit tracker no longer grows unbounded.
- Capacity-reservation safety valve now engages when enabled.
- Audit and incident-duration parsers skip malformed persisted input.
- Notifying-task alert cooldowns no longer shorten each other across subclasses.
- Rate-limit debounce state is now bounded (was an unbounded per-key map).

## [1.1.0] - 2026-07-07

### Added

- Async `aprotect()` / `@aprotected` now apply the circuit breaker and retry.
- `@circuit_breaker` is now async-safe.
- `@retry` — one retry decorator for sync and async functions.
- `aprotect(retry=…)` now works via the async tenacity bridge.
- `protect()` metrics now carry a `mode` label (`sync` / `async`).
- New `baldur_idempotency_gate_takeover_total` metric.

### Changed

- `@aprotected` / `aprotect` now apply the circuit breaker by default. **Breaking**

### Removed

- Inert `BALDUR_SECURITY_*` rate-limit / failed-login env vars. **Breaking**
- `@with_retry` / `@retried_async` — use `@retry`. **Breaking**
- Inert `BALDUR_SCALING_LOAD_SHEDDING_ENABLED` env var.
- Inert `TrafficGate(settings=...)` parameter. **Breaking**
- `BALDUR_API_RATE_LIMIT_*` — use `BALDUR_RATE_LIMIT_*`. **Breaking**
- Unused `TLSResilientClient` / `SimpleTLSResilientClient`. **Breaking**
- Unused `KafkaProducerProtocol` / `KafkaConsumerProtocol`. **Breaking**
- Unused `baldur.interfaces.runbook` type markers. **Breaking**

### Fixed

- Web Console Meta-Watchdog panel no longer errors right after startup.
- Emergency-mode auto-expiry and governance metric refresh now run out-of-box.
- Control-API rate limit now enforces its per-minute cap.
- Control-API `429` responses now include `X-RateLimit-Limit`.
- Scheduled maintenance jobs now run out-of-box on a single host.
- FastAPI and Flask apps no longer import the Django integration eagerly.
- Building a PRO preset without a license now raises a clear tier error.
- Async `aprotect()` deduplication is now awaitable-native (no loop stall).
- Idempotency no longer double-executes on the retry path.
- Idempotency now honors `fail_open_on_cache_error` during a cache outage.

### Security

- Pool circuit-breaker `503` no longer leaks raw database error text.

## [1.0.0] - 2026-06-23

This is the inaugural release. The changelog begins at v1.0; pre-release internal
changes are intentionally omitted.

### Added

- Circuit Breaker — stop calling a failing dependency and auto-probe for recovery.
- Retry — re-run a failed operation with growing backoff.
- Idempotency — block duplicate runs of must-happen-once operations.
- Graceful Shutdown — drain in-flight requests before the process exits.
- Health Check — ready-made liveness and readiness endpoints.
- System Control — runtime kill switch plus an observe-only dry-run mode.
- Web Console — zero-config browser UI for self-healing state and controls.
- Metrics — auto-recorded Prometheus metrics with a cardinality guard.
- Dashboard — one-call snapshot of the full self-healing picture.
- Precomputed Cache — serve Baldur's status endpoints from a warmed cache.
