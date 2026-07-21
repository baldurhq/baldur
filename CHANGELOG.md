# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This changelog covers the open-source core (`baldur-framework`). PRO release
notes are published separately at <https://baldur.sh/concepts/pro/release-notes/>.

## [Unreleased]

### Fixed

- Durability directories fall back to a writable location when the shipped default is not.
- A directory you set explicitly fails loud: `ConfigurationError` names it and its env var.
- `ResilientStorageBackend` warns instead of logging an ERROR traceback on a non-root install.
- Production boot still requires the resilient-storage WAL on its configured directory.
- Break-glass: set `BALDUR_RESILIENT_STORAGE_WAL_DIR` to any writable path to boot anyway.
- `schedule_retention_cleanup()` reads `BALDUR_AUDIT_WAL_DIR` first, warning on the legacy name.
- `BALDUR_CONFIG` and `BALDUR_DOTENV` no longer warn as unknown environment variables.

### Added

- Daily report records the on-recovery replay sweep, so its "Auto-replay" line renders on OSS.
- `baldur.utils.fs.resolve_writable_dir` — canonical writable-directory resolver.
- Startup report gains `storage_dirs`: which durability directories resolved, and which fell back.
- `WALConfig.wal_dir_operator_set` / `create_wal(wal_dir_operator_set=...)` — mark chosen dirs.
- `ResilientStorageBackend.get_stats()` gains `wal_on_fallback_dir`.

### Changed

- Digest sections `dlq`, `automated_actions`, `auto_replay` are labeled OSS, not PRO.
- Daily-report `failed_ops_without_dlq` → `dlq_captured_without_adaptive_replay`. **Breaking**
- `wait_if_needed(key, max_wait=...)` bounds outbound 429 cooldown waits — past the bound it defers.
- A provider `Retry-After` is honored up to `BALDUR_RATE_LIMIT_BACKOFF_RETRY_AFTER_CEILING` (1h).
- The 429 escalation ladder is seeded from `default_retry_after`, not the `Retry-After` header.
- `rate_limit_cooldown_seconds` can now record honored cooldowns above `max_delay`.
- Outbound 429s are coordinated by default on the sync retry stage when the call names a `domain`.
- `BALDUR_RATE_LIMIT_BACKOFF_COORDINATION_ENABLED=false` or `rate_limit_aware=False` opts out.
- `@retry`/`standard_pipeline`/`ha_pipeline` without `domain` stay uncoordinated, with a WARNING.
- `RetryPolicyConfig` gains `rate_limit_aware`/`rate_limit_key`; both are inert on async surfaces.

### Removed

- `baldur.services.RetryConfig` and `from_retry_config` — use `RetryPolicyConfig`. **Breaking**
- Daily-report `approval_expired_count` — no producer, always 0. **Breaking**
- `MerkleSpotChecker` — only its never-scheduled callers used it. **Breaking**
- `create_pydantic_serializer`, `PydanticSerializerMixin` + helpers — unwired. **Breaking**
- Dead `BALDUR_AUDIT_INTEGRITY_*` knobs: merkle, verification, lock-timeout, check-interval.
- Cascade Warm/Cold retention tiers — never delivered; only the event TTL ships. **Breaking**
- `CascadeRetentionConfig`, `get_cascade_retention_config` — unused. **Breaking**
- Cascade event archive repositories (interface, memory/sql/django adapters). **Breaking**
- `CascadeEventData`, `TriggerType`, `CascadeEventArchive` model + its table. **Breaking**
- `CELERY_BEAT_SCHEDULE` — `configure_baldur_celery(app)` replaces 2 of its 5 entries. **Breaking**
- `CHAOS_SCHEDULER_BEAT_SCHEDULE` — unread duplicate of the lane getter. **Breaking**

### Fixed

- Jittered `ExponentialBackoff`/`LinearBackoff` delays no longer exceed `max_delay`.
- A provider `Retry-After` is no longer undercut by jitter into an early retry.
- A rate-limit coordinator or storage fault degrades to a logged no-op, not a changed outcome.
- An exception's string `retry_after` is coerced, so a 429 no longer installs no cooldown.
- `configure_baldur_celery(app)` raised `TypeError` on every call and registered nothing.
- PRO-only DLQ maintenance no longer schedules without PRO — three tasks failed on cadence.
- Stale REPLAYING entries now release back to PENDING without PRO, instead of stranding.
- Cleanup-lane approval-expiry and WAL-gauge entries are PRO-gated; the WAL task failed hourly.
- The X-Test-Mode snapshot error no longer echoes raw exception text into the response body.
- Compressed DLQ entries now age ACTIVE→STALE→ARCHIVED on a daily schedule (was never run).
- Compressed-entry sweep reads the oldest page, not the newest — it was a no-op above ~3/day.
- SQL DLQ adapter stamps `stale_at`/`archived_at`, so STALE→ARCHIVED can fire on SQL backends.
- SQL adapters read timestamps back as UTC-aware; MySQL returned naive ones and broke compares.
- Compressed-entry sweep no longer re-reads entries it already transitioned on Redis.
- Daily-report Auto-Processing counts (archived/expired/purged) now reflect real cleanup work.
- Replay-driven DLQ resolutions now count in the digest and decrement the pending gauge.
- Redis DLQ archive/purge counts no longer include writes that changed nothing.
- Shadow-PRO insight no longer claims failed operations had no DLQ; OSS captures them.
- SLA drift check no longer crashes every run on non-Django hosts (QuerySet-only `.count()`).
- `dlq_outbox_current_size` gauge now reports the outbox queue depth (was never set).
- `overflow_strategy` help text now matches OSS synchronous eviction (background worker is PRO).

## [1.2.0] - 2026-07-17

### Added

- OSS DLQ durably captures + replays failed ops, incl. auto-replay on CB recovery (no PRO).
- OSS DLQ read UI + REST: list/detail/facets/stats + single-entry retry/resolve/force-redrive.
- Bulkhead primitives (semaphore/async, registry, `@bulkhead`, policy, metrics) are now core.
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
- Provider factories resolving their own slot now raise `RuntimeError` instead of deadlocking.
- Retry `outcome="exhausted"` now excludes non-retryable/budget/deadline aborts.
- `baldur.resilience.policies.__all__` drops PRO-backed policy names; they stay importable.
- `protect()` fallback runs after retry; timeout/CB-open covered, CB still trips. **Breaking**

### Removed

- Circuit-breaker canary-recovery cluster and its public API. **Breaking**
- `StaleCacheStore` moved to `baldur.core.stale_cache`. **Breaking**
- `IPCStateCache.stats` — use `get_stats()`. **Breaking**
- `baldur.core.timezone` — use `baldur.utils.time.utc_now`. **Breaking**
- `baldur.settings.audit_settings` alias — use `from baldur.settings import audit`. **Breaking**
- `POOL_CB_*` pool circuit-breaker env vars — use `BALDUR_POOL_CB_*`. **Breaking**
- `BALDUR_DLQ_RESOLVE_BATCH_CHUNK_SIZE` — the setting was never read. **Breaking**

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
