# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This changelog covers the open-source core (`baldur-framework`). PRO release
notes are published separately at <https://baldur.sh/concepts/pro/release-notes/>.

## [Unreleased]

### Fixed

- Pulling the kill switch now stops governed automation on the next check, not up to 30s later.
- `/system/disable/` publishes the flip, so the throttle and auto-tuning react to it at last.
- A kill switch flipped on one server is now seen by the others within one cache window.
- A state write that fails no longer lets a later refresh quietly re-enable the system.
- `/system/status/` reports `persist_dirty` when this node's state has not reached the backend.
- An install without the optional Kafka adapter no longer logs a Kafka error per critical event.
- An install without the PRO throttle no longer logs a warning on every kill-switch flip.

## [1.9.0] - 2026-08-31

### Added

- The Celery adapter calls `baldur.init()` in each worker itself, as the other adapters do.
- `setup_baldur_signals()` or `configure_baldur_celery()` is now the whole of a Celery setup.
- A Celery worker reads `BALDUR_REDIS_URL` and runs background maintenance with no extra receiver.
- A prefork worker starts them per pool child, so a recycled child comes back with its own.
- A production Celery worker whose config `baldur.init()` rejects now exits before it forks.
- `BALDUR_DLQ_BACKEND` keeps the dead-letter queue in your own database (`memory`/`redis`/`sql`).
- Unset, it picks Redis, else a configured SQL DSN, else memory — parked calls survive a restart.
- A dead-letter backend that cannot be built now fails the production boot, not the first capture.
- The startup report names the dead-letter backend, so the configured store is visible at INFO.

### Changed

- **Breaking**: a production boot missing a critical secret raises `ConfigurationError` now.
- **Breaking**: `SQLCircuitBreakerStateRepository` removed — breaker state stays in memory or Redis.
- **Breaking**: `ProviderRegistry.set_defaults(repo=…)` removed — set each registry's default.
- Dead-letter overflow eviction now targets the entries the size cap counts, so it shrinks them.
- A closed, healthy circuit breaker no longer writes its state on every recorded success.
- Circuit-breaker writes to Redis now track state transitions rather than request volume.
- A worker's success no longer resets a peer worker's open circuit or failure count in Redis.
- A circuit-breaker row for a service that never trips now ages out of the daily stale-key sweep.

### Fixed

- Backend-override env values are case-insensitive — `BALDUR_EVENT_JOURNAL_BACKEND=SQL` selects sql.
- A zero-config process no longer prints `resilience.bypass_hooks_skipped` to stdout on import.
- A wired boot no longer warns `init_not_called_get_cache` from inside `baldur.init()` itself.
- A Celery task failure the DLQ rejected (disabled, overflow) is no longer logged as stored.
- A forked worker (`gunicorn --preload`) now drains its own DLQ outbox instead of losing entries.
- Its writer dies at the fork, so async stores reported success into a buffer nothing consumed.
- Each worker restarts its own writer and leaves the parent's queued entries to the parent.
- A zero-config run under failing traffic no longer warns about a Redis nobody configured.
- Four circuit-breaker write paths dialed the default address directly, past the storage backend.
- A store someone named keeps every warning; only a never-reached default address is skipped.

## [1.8.0] - 2026-08-26

### Fixed

- A circuit breaker that trips no longer has its own failure records erase the trip from Redis.
- The trip is one atomic store write now, so a worker no longer readmits traffic it should reject.
- One open event and one error-budget charge per trip, instead of one per worker that tripped.
- An operator's force-open or force-close is no longer overwritten by a worker's state mirror.
- A trip is declined while an operator's override is in force, and logged rather than swallowed.
- A closed circuit-breaker row no longer keeps the timestamp of the open it left.
- `baldur_circuit_breaker_trip_degraded_mode_total` counts trips served locally on a store failure.
- Drift reconciliation now propagates a local half-open winner to the shared store.
- It previously copied the losing shared state back, undoing the resolution it had just made.
- A shared row an operator pinned is exempt: the pinned state is copied to the local row instead.
- A half-open circuit breaker whose trial window is missing no longer rejects every call forever.
- Its first trial call now starts the window, so the breaker recovers instead of locking on Redis.
- A half-open window rebuilt from a snapshot is no longer misread as stalled during a Redis outage.
- It admitted one extra round of trial calls before stamping a window and repairing itself.
- A circuit breaker the shared store reports closed no longer stays rejected on one worker.
- That worker discarded the answer on every request until a restart; it now converges to it.
- Where the shared store lost the row instead, the worker restores it rather than admitting.
- `baldur_circuit_breaker_reject_path_convergence_total` counts each outcome, by service.
- An idle audit-enabled process no longer appends a WAL entry about its own WAL once a second.
- Those entries reached the audit trail too, so the compliance ledger filled with self-reference.
- The audit sync worker no longer re-reads the whole retained backlog to deliver one batch of it.
- Its read is capped at `BALDUR_AUDIT_SYNC_BATCH_SIZE`; the lag gauge still reports the backlog.
- A WAL checksum mismatch is now reported on best-effort reads too, not only on strict ones.
- A raising `on_corruption` hook no longer silently truncates the recovery read it fires in.
- A circuit-breaker force run from the CLI is recorded as an operator action, not an automatic one.
- A manual circuit-breaker reset is recorded as an operator action too, not an automatic close.
- Every `baldur` command that changes state carries a `user@host` identity into the audit trail.
- An audit row's timestamp is the time of the audited event, so `query(start_time=)` filters on it.
- It was previously the append time, which for buffered events lagged by a whole batch window.
- Rows recovered from the write-ahead log carry a real action and target type, not raw event names.
- A group-committed WAL entry now reaches the audit destination within one drain cycle.

### Removed

- **Breaking**: the `WAL_RECOVERED` audit event; `baldur_wal_entries_recovered_total` counts it.
- **Breaking**: `WAL_ROTATED` / `WAL_CORRUPTION_DETECTED` no longer reach the trail via the WAL.
- Both still reach a wired `audit_adapter`, and log `wal.file_rotated` / `wal.corruption_detected`.

## [1.7.0] - 2026-08-21

### Added

- Django 6.1 joins the supported and CI-tested matrix (Python 3.12/3.13 cells).
- Runnable self-healing demo: `python -m baldur.scripts.demo_self_healing` (no infra needed).
- `python -m baldur.scripts.measure_footprint` reports what Baldur costs the process it runs in.
- It prints RSS, thread and CPU deltas per startup stage, plus the posture and host they came from.
- `BALDUR_RATE_LIMIT_REDIS_RECOVERY_PROBE_INTERVAL_SECONDS` (default 30) paces the recovery probe.
- `BALDUR_SCHEDULER_DISABLED_JOBS` switches off named default jobs without stopping the scheduler.
- It governs the in-process scheduler; `config_apply` and the canary jobs honour it in beat too.
- The canary watchdog now runs on PRO-entitled installs, renewing rollout locks and alerting stalls.
- It also runs off the in-process scheduler, so a non-Celery deployment gets the same three jobs.
- `BALDUR_CANARY_WATCHDOG_ENABLE_AUTO_PROMOTE` and `..._ENABLE_AUTO_ROLLBACK` opt into its actions.
- `audit_backend_wired` reads 0 when audit is on but every record would reach the no-op adapter.

### Changed

- **Breaking**: the `config_apply` job and its beat lane need an active entitlement, not just PRO.
- Migration: an installed-but-unlicensed deployment stops applying DELAYED/GRACEFUL config changes.
- It could not create those changes either, and any pending ones still expire on their own schedule.
- Idle cost drops only with PRO installed but not entitled; a licensed install is unchanged.
- A Redis outage starting after resolution now keeps the outbound 429 cooldown, per worker.
- `baldur_ratelimit_fallback_active` reads 1 there; leaving it needs a passing write probe.
- **Breaking**: `extend_cooldown` / `increment_consecutive_429s` degrade there instead of raising.
- **Breaking**: `BALDUR_CANARY_WATCHDOG_ENABLE_AUTO_PROMOTE` / `..._ROLLBACK` default `false`.
- Migration: set both to `true` to keep a hand-wired watchdog promoting and rolling back.
- `promote()` takes `expected_stage_index`; the canary lane needs a matching `baldur-pro`.
- **Breaking**: `CircuitBreakerConfig()` defaults `enabled=True`, matching `BALDUR_CB_ENABLED`.
- Migration: pass `CircuitBreakerConfig(enabled=False)` to keep a directly built config off.
- The published API reference rendered `False`; a config from settings was always enabled.

### Fixed

- An OSS-only Celery deployment no longer schedules the config-apply task it can never run.
- That lane logged a `blocked` warning and wrote an audit row every 30s on every such install.
- A supervised canary rollout's config-type lock no longer lapses at its TTL while the rollout runs.
- `baldur_canary_governance_blocked_total` now records; its only emitter passed the wrong labels.
- Audit records waiting in the write-ahead log are no longer deleted when no real backend is wired.
- The sync worker counted the no-op adapter as a delivery target, so it reported them delivered.
- `BALDUR_AUDIT_DISTRIBUTED_HASH_CHAIN=true` now reaches Redis; it silently ran a local chain.
- The hash-chain audit directory falls back to a writable path instead of failing to construct.

### Removed

- **Breaking**: `baldur_ratelimit_state_drift_total` and `baldur_ratelimit_reconciliation_total`.
- Neither ever emitted a sample; `baldur_ratelimit_fallback_active` reports the degraded window.
- **Breaking**: `BALDUR_CANARY_WATCHDOG_SLACK_CHANNEL`; it never reached a delivery target.
- Set the watchdog's channel via `BALDUR_CHANNEL_ROUTING_CATEGORY_SLACK_TARGETS` (`operations`).

## [1.6.0] - 2026-08-13

### Changed

- Rate-limit coordination and CB L2 boot hydration skip Redis when nobody configured one.
- Migration: set `BALDUR_REDIS_URL` to keep using an unconfigured local Redis for either lane.
- That posture is `BALDUR_ENVIRONMENT` != `production`, the default when the variable is unset.
- Redis admission probes in audit, air-gap, metrics and rate-limit now bound the connect phase.
- A connect that times out is retried once; a refusal is immediate. Data-path timeouts unchanged.
- RQ broker connections take timeouts and credentials from the adapter, never `BALDUR_REDIS_*`.

### Fixed

- A first protected call no longer stalls for seconds with redis-py installed and no server.
- The RQ broker client had no socket timeout at all; a black-holed host blocked ~21 s.

## [1.5.0] - 2026-08-13

### Added

- `baldur.runtime_posture` (INFO, once per process) — storage, metrics and statistics backends.
- `baldur.posture` logger carries an INFO floor, so that line survives the default WARNING root.

### Changed

- A zero-config first run emits no WARNING-or-above baldur line, on every documented path.
- `meta_watchdog.enabled_but_unregistered` is silent on an install that never set the flag itself.
- `null_statistics_repository.no_adapter_registered` → DEBUG; the posture line reports it instead.
- `baldur.init()` configures logging first, so its own DEBUG/INFO output is filtered from line one.
- **Breaking**: `storage.writable_dir_probe_failed` → INFO `storage.writable_dir_fallback`.
- **Breaking**: `outside.recommended_range` → `leader_election.renew_interval_outside_range`.
- That renewal-cadence line is DEBUG unless you set `BALDUR_LEADER_ELECTION_RENEW_INTERVAL_SECONDS`.
- `resilient_storage.degraded_mode_entered` is DEBUG when no Redis was configured, non-production.
- `resilient_storage.lazy_redis_probe_failed` / `shadow_log.sync_failed`: same demotion.
- `redis_factory.connection_failed` drops to DEBUG for a probe against a URL nobody configured.
- `resilient_storage.degraded_mode_fallback` (losing a live Redis) stays CRITICAL in every posture.
- `baldur.registry_memory_fallback` → INFO; production still raises `ConfigurationError` first.
- Secret-validation reports are INFO/DEBUG outside production; the production abort is unchanged.
- `baldur.init_not_called_get_*` are WARNING only when Redis is configured, DEBUG otherwise.
- `database_rate_limit_storage.database_unavailable` → DEBUG `..._not_configured` without a factory.
- `redis_rate_limit_storage.redis_unavailable` is DEBUG when no Redis was configured.
- Missing `prometheus` extra: recording is a silent no-op instead of a warning per protected call.
- **Breaking**: `metrics.prometheus_unavailable` and `metrics.up_gauge_registration_failed` removed.
- `prometheus.unavailable` → INFO; `metrics.protect_recorder_unavailable_sticky` → DEBUG.
- `on_rate_limited` returns the cooldown now in force for the key, not the delay this call computed.
- `baldur_rate_limit_cooldown_seconds` buckets now reach 3600 s, so honored cooldowns leave `+Inf`.

### Fixed

- `BALDUR_RESILIENT_STORAGE_REDIS_URL` now wins over `BALDUR_REDIS_URL`, which overrode it before.
- Zero-config startup no longer logs repeated circuit-breaker warmup errors when Redis is absent.
- "Is a Redis configured?" no longer imports Django unless `DJANGO_SETTINGS_MODULE` is set.
- `GET /error-budget/status/` answers 200 `unavailable` without PRO installed, instead of a 500.
- Pool status omits `pg_stats` on a non-PostgreSQL backend instead of failing the whole payload.
- The error-budget and pool-status cache jobs no longer log a traceback on every refresh pass.
- 429 cooldowns are monotonic per key; a shorter concurrent write no longer cuts an honored one.
- A debounce-suppressed 429 moves the all-clear it extended, instead of leaving it at the old time.
- `RATE_LIMIT_COOLDOWN_END` carries `cooldown_until`, the expiry it was announced for.
- Exponential backoff no longer overflows past ~1024 attempts, which had silently dropped cooldowns.
- `on_rate_limited` accepts a raw `Retry-After` string; the documented direct-drive form raised.
- `Retry-After` in HTTP-date form is honored, instead of falling back to the backoff ladder.
- An infinite `Retry-After` reads as absent instead of clamping to the caller's maximum wait.

## [1.4.0] - 2026-08-11

### Added

- `BALDUR_CB_MANUAL_OVERRIDE_TTL_MINUTES` (`90`, range 1-1440) — default manual-override lifetime.
- A force-close (Allow / Override) now expires too, restoring automatic protection on its own.
- Control responses report `effective_until` read back from storage, so it matches the real expiry.
- `BALDUR_METRICS_COLLECTION_INTERVAL_SECONDS` (`60`, range 5-3600) — gauge collection cadence.
- `BaldurMetricCollectionStale` alert rule; `BaldurMetricCollectionAbsent` ships commented out.
- `GET /healing/summary` (viewer, read-only) — this process's replay/page counts and p95 latencies.
- The console ledger reports `replayed` and `humans paged`, and System rows carry a p95 token.
- `EscalationResult.delivered_externally` — whether a channel that leaves the host accepted a page.
- Config responses carry `runtime_apply`: whether a stored change reaches running processes.
- `BALDUR_RUNTIME_CONFIG_WATCH_INTERVAL_SECONDS` (`30`, range 0-3600) — config delivery cadence.
- Stored circuit-breaker config now reaches already-running processes; `0` disables the delivery.
- `baldur_runtime_config_installed_fingerprint{config_type}` — equal across converged workers.
- `baldur.gunicorn_hooks_installed` (INFO) — confirms the hooks are wired, not just warns when not.

### Changed

- Admin console redesigned: 3-tier triage layout with a healing-ledger hero (crosshair, no deps).
- Console + landing state palette refreshed (two-hue red/jade); Schibsted Grotesk embedded.
- **Breaking**: `ttl_minutes` of `0` or below is rejected (`TTL_OUT_OF_RANGE`) instead of stored.
- **Breaking**: `atomic_force_open` / `atomic_force_close` take `ttl_minutes: int | None`.
- **Breaking**: the Django admin's `manually_controlled` is read-only — use `reset_selected`.
- The override-expiry sweep clears the manual flag only; it no longer writes circuit state.
- A manual block or allow is enforced per process — an already-running peer worker won't see it.
- The DLQ backlog alerts and dashboard panel read the O(1) pending total, not the per-domain sum.
- **Breaking**: those alerts lost their `domain` label — re-key per-domain routing and silences.
- **Breaking**: `BALDUR_SYNC_ON_STARTUP` / `BALDUR_SYNC_JITTER_MAX` dropped with the gauge hydrator.
- **Breaking**: `retry_success_rate` in `GET /api/baldur/metrics/` may be null (was always `100.0`).
- A `monitored_services` breaker that never tripped gets no series until its state is recorded.
- `baldur_replay_attempts_total` now covers console retries, force-redrives and PRO batch replays.
- `baldur_replay_duration_seconds` covers those replays too; it was replay-service-only before.
- **Migration**: `baldur_replay_outcomes_total{outcome="failure"}` alerts fire on failed retries.
- `baldur_dlq_replay_duration_seconds` no longer times gate refusals, so its p95 rises to the truth.
- **Breaking**: `baldur.services.config` and `BALDUR_PROPAGATION_*` removed — use `RedisEventBus`.
- **Breaking**: `failure_rate_5m` in `GET /api/baldur/metrics/` may be null (was always `0.0`).
- **Breaking**: `last_5m_failure_rate` may be null; `circuit_state` may be null (was `closed`).
- A service with an observed call but no registered domain now gets a row in that payload.
- **Breaking**: `baldur.server` removed — use `baldur.adapters.gunicorn`'s three worker hooks.
- **Breaking**: WAL `max_files` is now a per-process cap, so a directory holds workers × that many.
- Orphan WAL absorption is scheduled as the drain loop's first action, not done on the boot path.

### Removed

- **Breaking**: `AsyncHealingLogger.configure_wal()` and `WALPolicy` are gone; no replacement.
- **Breaking**: `AsyncHealingLogger.get_stats()` dropped `wal_writes`; indexing it now raises.
- Both described a WAL-first write path that had no caller and never executed.

### Fixed

- Per-service `failure_rate_5m` was a literal; a real 5-minute windowed rate now backs it.
- `last_5m_failure_rate` was the DLQ backlog share and `last_5m_request_count` its all-time total.
- `circuit_state` read a repository no breaker writes to, so it said `closed` about open breakers.
- A breaker name with a dot, a hyphen or a capital missed the DLQ and state joins on its row.
- Reset pinned a manual circuit-breaker override instead of clearing it, so it never reopened.
- A stored out-of-range circuit-breaker value could disable protection; it is now clamped.
- A manual block admitted every request: control and traffic read different breaker stores.
- Recording an outcome from a request admitted before a block could reopen the blocked breaker.
- A block placed elsewhere was lost on process start; it now loads with the rest of the state.
- A drift repair could overwrite a live block in the shared store with an unblocked row.
- An automatic 429-cascade force-open overrode an operator's block or allow; it now yields to it.
- With `BALDUR_NAMESPACE_NAMESPACE_ENABLED=true`, every breaker keyspace scan matched nothing.
- The block lifetime typed in the console was discarded — every block lasted the global default.
- Manual overrides never expired without Celery; the inline scheduler now runs the sweep.
- DLQ and circuit-breaker gauges froze at startup; every serving process now refreshes them.
- A failed DLQ statistics read no longer zeroes every pending gauge mid-incident; values hold.
- DLQ status gauges read `0` on the memory and SQL backends regardless of what was stored.
- The retry success-rate gauge and payload field reported a fabricated 100%; both are now absent.
- `total_dlq_pending` and the daily report's DLQ line read `0` when the breakdown was unavailable.
- **Migration**: a copied `BaldurServiceDead` rule needs `{component="error_budget"}` to stay true.
- A failed config-store read replaced a process's live configuration with factory defaults.
- Two near-simultaneous config edits could leave the older one in force until the next write.
- A first read of the shared breaker configuration could deadlock against a concurrent edit.
- The `stored_only` detail said running processes keep the old value; it now states only the bound.
- With `BALDUR_EVENT_BUS_BACKEND=redis`, no default event handler was registered in any process.
- CB notifications, snapshots and post-mortems never fired on those installs; now they do.
- Under `gunicorn --preload` no worker received cross-process events; each worker now subscribes.
- Workers also reused the master's sender id, so a sibling's events looked self-sent and dropped.
- A forked child's shutdown could unsubscribe the parent from every channel, deafening it for good.
- Event-handler dispatch stalled silently in any forked child; the inherited pool is now rebuilt.
- A worker recycle ran no audit shutdown: the WAL was left unclosed and no checkpoint saved.
- Under `gunicorn --preload`, audit events emitted in a worker reached a consumer killed by fork.
- Non-CRITICAL audit events logged in such a worker were dropped; the pipeline is now revived.
- `AsyncHealingLogger.flush()` waited 5 s and flushed nothing when its consumer thread was dead.
- A worker with no live audit threads reported the pipeline as running; both reads now check it.
- A swallowed audit-startup failure was recorded as success, so no later call ever retried it.
- Orphan-WAL absorption read a *live* peer's file and re-delivered entries it had already sent.
- WAL retention, the disk purge and startup cleanup could unlink a living peer's open file.
- A forked worker inherited a disk-full latch and silently dropped every WAL write for its life.
- A no-op audit destination counted as delivery and consumed the one-shot orphan absorb.
- The orphan absorb ran on the boot path; a slow one could exceed gunicorn's worker-boot timeout.

## [1.3.2] - 2026-07-31

### Fixed

- The system-metrics cache no longer samples psutil twice per interval.
- Starting it armed two refresh chains, and `stop()` could only ever reach one of them.
- A failed metrics tick no longer stops the sampling for the life of the process.

## [1.3.1] - 2026-07-30

### Fixed

- A failed precomputed-cache refresh no longer stops the worker for the life of the process.
- The pass reads circuit-breaker state, which can raise while Redis is down — exactly when it ran.
- Recovery needed a restart; the endpoints kept serving whatever the last good pass had computed.

## [1.3.0] - 2026-07-30

### Added

- `baldur_retry_attempts_started_total{is_retry}` counts attempts at start, not on resolution.
- `RetryPressureHigh` reads its retry share, so a storm shows while it is still in flight.
- Before, sequences asleep in backoff or a 429 cooldown moved the alert only once they resolved.
- `BALDUR_HEALTH_CHECK_READINESS_TIMEOUT_FAIL_DIRECTION` — depool on a hung DB, or stay pooled.
- Readiness reports a database that stopped answering as `timed_out`, distinct from `not_ready`.
- Readiness verdicts are cached briefly, so probe cadence no longer scales query load per pod.
- Daily report records the on-recovery replay sweep, so its "Auto-replay" line renders on OSS.
- `baldur.utils.fs.resolve_writable_dir` — canonical writable-directory resolver.
- Startup report gains `storage_dirs`: which durability directories resolved, and which fell back.
- `WALConfig.wal_dir_operator_set` / `.wal_dir_env_var` — mark a chosen dir and its override.
- `ResilientStorageBackend.get_stats()` gains `wal_on_fallback_dir`.
- `BALDUR_CB_FAILURE_RATE_THRESHOLD` (`50.0`) — failure % over the window that opens the circuit.
- `BALDUR_CB_SLIDING_WINDOW_SIZE` (`100`) — recent calls the rate is measured over, per worker.
- `BALDUR_CB_MINIMUM_CALLS` (`10`) — calls needed before the rate is trusted; gates rate only.
- `CircuitBreakerService.get_window_evidence(name)` returns the window's `(failures, total)`.
- `CIRCUIT_BREAKER_OPENED` carries the window failure/total counts and the consecutive count.
- `BALDUR_DLQ_COMPRESS_SUMMARY_SCAN_CAP` (`5000`) — cap above which the compressed summary windows.
- Compressed summary flags `summary_truncated` when it windows to the newest `…SCAN_CAP` entries.
- `baldur dlq migrate-compressed` converts an existing install's compressed index in one pass.
- Without it the lifecycle sweep migrates automatically, taking effect after two daily runs.
- Re-run it any time filtered compressed listings come up short after a restore or rollback.
- `ResilientStorageBackend.degrade_count` — times the backend left Redis mode.
- `baldur_rate_limit_wait_seconds` — cooldown wait imposed on a caller, by coordination key.
- `baldur_rate_limit_deferrals_total` — wait-or-defer decisions that deferred, by key.
- `baldur_task_retries_total` — one increment per Celery `task_retry` signal, by domain.
- Sample alert `RateLimitStorageDegraded` — 429s arriving with no cooldown recorded for the key.
- `baldur_task_attempts_distribution` / `baldur_task_outcomes_total` — task-layer resolutions.
- Sample alert `TaskFailureRateHigh` — the task-queue counterpart of the protected-call rule.
- Shipped Grafana boards gain rate-limit panels; task-layer panels join the operations board.
- The demo stack drives a `/rate-limited/` endpoint, so the rate limit panels start populated.
- Startup report gains `effective_retry_backoff`: the ladder the retry stage actually builds.
- `RetrySettings` warns when `base_delay` exceeds `max_delay`, which starts the ladder saturated.
- `ConstantBackoff(max_delay=...)` caps the constant delay; unset keeps today's uncapped behavior.
- `baldur_up` — label-less exporter liveness marker, exported by every Baldur process.
- Sample alert `BaldurMetricsAbsent` — no `baldur_up` sample in 5m, so nothing is reporting.

### Changed

- Application domains now register themselves, so their metric labels stop collapsing to one series.
- A protect retry stage, `@domain_tag`, a DLQ store and the Celery sites each claim their domain.
- Registration is lazy: a domain leaves `OTHER_DOMAIN` when its declaring module or call first runs.
- Domain label values are canonical, lower-cased: `My-Service` becomes `my_service`.
- Auto-registered domains share `max_registered_domains` with your own `register_domain()` calls.
- A DLQ domain the validator rejects is stored canonicalized when canonicalization fixes it.
- `on_domain_rejected` now fires only when canonicalization cannot fix the input.
- `retry_backoff_*` and `dlq_outbox_processing_delay_seconds` now carry the capped domain label.
- `baldur_dlq_evicted_total` labels an unattributed eviction `OTHER_DOMAIN`, not the empty string.
- Meta-Watchdog probes now run concurrently under a per-pass wall-clock budget.
- A probe still running at the budget reports `UNKNOWN`; the pass no longer waits for it.
- The budget is derived from `probe_interval_seconds` and the daemon-worker staleness multiplier.
- `POST /meta-watchdog/force-check` answers `409` with the last snapshot while a check runs.
- Before, it queued on the check lock, so a poller could starve the watchdog's own loop.
- `ProbeResult` gains `observed`: `False` marks a result no probe actually produced.
- `dlq_id` is typed `str` across the public surface: it is an opaque token, never parse it.
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
- The circuit breaker trips at exactly `failure_threshold` (5) consecutive failures. **Breaking**
- Rate evidence is per worker process, so workers under skewed load trip independently.
- `baldur_retry_outcomes_total{outcome="retry"}` → `baldur_task_retries_total`. **Breaking**
- Celery terminals → `baldur_task_outcomes_total`/`baldur_task_attempts_distribution`. **Breaking**
- The retry series and its two sample alerts are now protected-call SLIs, task queue excluded.
- Retry jitter defaults to +/-20% (was 25%), matching `BALDUR_BACKOFF_EXPONENTIAL_JITTER_FACTOR`.
- `RetryPolicyConfig` defaults are now 1 s base / 60 s cap; pipeline presets shorten to match.
- `record_retry_attempt` → `record_retry_resolution`; it always fired per resolution. **Breaking**
- Sample alert `BaldurMetricsDown` joins on `baldur_up`, so it fires on any framework.
- Before, it selected `up{job="django"}` and could never fire on Flask, FastAPI or the CLI.
- Re-merge these two rules if you customized a copy of `examples/monitoring/prometheus-alerts.yml`.

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
- `InMemoryCircuitBreakerStateRepository(sliding_window_size=)` — moved to the service. **Breaking**
- `LayeredCircuitBreakerStateRepository(sliding_window_size=)` — same removal. **Breaking**
- `AuditWatchdog` + exports — nothing ever started it; no in-tree replacement. **Breaking**
- It pushed an outbound dead-man's-switch ping; use an external uptime monitor instead.
- `BALDUR_AUDIT_WATCHDOG_*` — never had any effect. **Breaking**
- `BALDUR_BACKOFF_LEGACY_*` — use `BALDUR_RETRY_BASE_DELAY` for the first wait. **Breaking**
- `retry_attempts_total` + `baldur.core.retry_hooks`. **Breaking**
- Use `baldur_retry_attempts_started_total`; the removed histogram never had a live writer.
- Routes `xtest/retry/backoff-preview/` + `simulate/` — read `effective_retry_backoff`. **Breaking**
- Both rendered a curve no retry path produced; the startup-report entry reports the real one.

### Fixed

- An unreachable Redis no longer stalls the event-bus connect or the watchdog probe for seconds.
- Both give up after `BALDUR_REDIS_PROBE_CONNECT_TIMEOUT` (0.5s), not the data-path timeout.
- The Redis metric-source adapter builds its client through the shared connection factory.
- Before, its check ping carried no connect timeout and hung on the OS TCP timeout.
- It now reads the Redis URL through settings too, so credentials and Sentinel URLs apply.
- `start_sync_worker()` absorbs a crashed worker's orphaned audit WAL entries before draining.
- Before, only the internal start path did, so the public helper stranded those entries forever.
- The escalation channel self-test now closes the PagerDuty incident it opens.
- If that close fails, the self-test result names the cause and says to close it manually.
- Durability directories fall back to a writable location when the shipped default is not.
- A directory you set explicitly fails loud: `ConfigurationError` names it and its env var.
- `ResilientStorageBackend` warns instead of logging an ERROR traceback on a non-root install.
- Production boot still requires the resilient-storage WAL on its configured directory.
- Break-glass: set `BALDUR_RESILIENT_STORAGE_WAL_DIR` to any writable path to boot anyway.
- `schedule_retention_cleanup()` reads `BALDUR_AUDIT_WAL_DIR` first, warning on the legacy name.
- `BALDUR_CONFIG` and `BALDUR_DOTENV` no longer warn as unknown environment variables.
- Readiness answers within a bounded time: a database that hangs no longer hangs the probe.
- A health provider that raises logs at WARNING with the traceback at DEBUG, no longer ERROR.
- The circuit breaker's failure rate is a real rate: successful calls now count in it too.
- A service failing 30% of calls trips the breaker; before, only near-100% failure did.
- `minimum_calls` no longer raises the consecutive-failure trip point above `failure_threshold`.
- In-memory circuit-breaker counters reset like Redis/SQL: a success clears the failure count.
- `get_aggregate_failure_rate()` reports the mean error fraction, not a near-binary 0.0/1.0.
- It covers the service instance you call it on; `protect()`/`@circuit_breaker` each own one.
- Config-shadow CB simulation shares the live trip predicate, so it stops disagreeing.
- Shadow-testing one CB field completes the rest from the running config, not from stock defaults.
- Async retry exhaustion now emits `RETRY_EXHAUSTED` and records the Prometheus retry series.
- `BALDUR_RETRY_ENABLED=false` now stops async retries too: the function runs once, no retry.
- A compressed DLQ entry outside the newest 1000 now opens in its detail view, no longer a 404.
- The compressed-summary endpoint no longer costs one Redis round trip per entry ever compressed.
- Filtering compressed DLQ entries by status now reaches past the newest page.
- `has_more` on the compressed list is now exact under a status or domain filter.
- The compressed sweep no longer walks the archived tail; each lane reads its own status index.
- The compressed lifecycle sweep holds a distributed lock, so overlapping runs cannot skip an entry.
- Compressed `by_status` counts stay exact above `BALDUR_DLQ_COMPRESS_SUMMARY_SCAN_CAP`.
- A negative `limit` on the compressed list no longer reads the whole index; it clamps to 1.
- Sample alert `RetryRateHigh` measured call throughput; `RetryPressureHigh` measures retries.
- `ProtectedCallFailureRateHigh` replaces `RetrySuccessRateLow`, which printed a ratio as `%`.
- Both retry sample alerts exclude synthetic traffic and need ~10 samples before they can fire.
- `baldur_rate_limit_429_total` counts every 429; the event debounce no longer flattens it.
- It is recorded before the storage calls, so a storm stays countable during a Redis outage.
- Celery task terminals record their real attempt count in `baldur_task_attempts_distribution`.
- OSS retries now wait the `BALDUR_RETRY_BASE_DELAY` you set; every wait was ~4x longer before.
- `BALDUR_BACKOFF_EXPONENTIAL_MULTIPLIER` and `_JITTER_FACTOR` now reach the retry ladder.
- `BALDUR_RETRY_BACKOFF_STRATEGY` now picks the strategy; exponential ran whatever you set.
- A per-domain `retry.base_delay` override now takes effect; a bad value falls back with a WARNING.
- A `before` hook passed via `retrying_kwargs` now runs; the tenacity bridge dropped it silently.
- It is chained ahead of Baldur's own hook, the same way the `before=` constructor argument is.
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
- The compressed sweep no longer re-reads entries it transitioned earlier in the same run.
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
