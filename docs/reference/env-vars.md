# Public Environment Variables (Operator-Tunable Allowlist)

Operators may set these env vars in production. Everything else with a
`BALDUR_*` prefix is advanced / internal and subject to change in v1.x.

The full settings inventory is internal; operator-tunable
promotion happens via dedicated proposals in later releases.

!!! info "`(PRO)` marker"
    Entries tagged `(PRO)` require the `baldur_pro` package — the backing
    service ships only in `baldur_pro`, so without it the knob is a silent
    no-op.

## Resilience core

```bash
BALDUR_CB_FAILURE_THRESHOLD=5           # consecutive failures that trip the breaker
BALDUR_CB_FAILURE_RATE_THRESHOLD=50.0   # failure % over the recent-call window that also trips it; 0 disables the rate trigger
BALDUR_CB_SLIDING_WINDOW_SIZE=100       # recent calls the failure rate is measured over, per worker process
BALDUR_CB_MINIMUM_CALLS=10              # calls the window needs before the rate is trusted; gates the rate trigger only
BALDUR_CB_RECOVERY_TIMEOUT=60
BALDUR_CB_HALF_OPEN_MAX_CALLS=3
BALDUR_CB_MANUAL_OVERRIDE_TTL_MINUTES=90  # minutes a manual override (Block / Allow / Override) lasts when the operator sets no lifetime of its own; 1-1440
BALDUR_CB_CLUSTER_STATE_PROPAGATION_ENABLED=false  # set true on every worker: a booting worker reads shared breaker state on a local miss, and with PRO + the Redis event bus a peer's OPEN/CLOSED is applied here (a peer's CLOSED overrides a manual block held on this worker)
BALDUR_RETRY_MAX_ATTEMPTS=3
BALDUR_RETRY_BACKOFF_STRATEGY=exponential  # exponential | linear | constant | decorrelated_jitter
BALDUR_RETRY_BASE_DELAY=1.0
BALDUR_RETRY_MAX_ELAPSED=30.0  # total wall-clock retry budget (s); unset = no budget. Distinct from the per-sleep max_delay cap.
BALDUR_IDEMPOTENCY_ENABLED=true
BALDUR_IDEMPOTENCY_DEFAULT_CACHE_TTL=60
BALDUR_IDEMPOTENCY_GATE_MEMORY_TTL_SECONDS=1800
BALDUR_PROTECT_DEFAULT_TIMEOUT_SECONDS=30  # unset (default) = no Baldur-level wall-clock bound on protect(); set to restore a global outer net. Per-call timeout= always wins
```

## DLQ

Dead-letter capture ships in the OSS core: a failed operation is recorded with
the context needed to replay it, size limits plus the overflow strategy bound
the queue, and the non-blocking outbox keeps capture off the request hot path.

```bash
BALDUR_DLQ_ENABLED=true
BALDUR_DLQ_MAX_SIZE=100000
BALDUR_DLQ_OUTBOX_ENABLED=true
```

## Replay automation

Automatic replay on circuit-breaker recovery. `ON_RECOVERY_ENABLED` is on by
default; setting it to `false` disables the on-recovery dispatch and, with it,
the per-recovery WARNING about a missing replay worker.
`SERVICE_FAILURE_TYPE_MAP` maps each recovered service to the failure types
whose captured entries it is responsible for — an empty mapping leaves the loop unable
to select entries on recovery (surfaced as a blocked-with-signal event, not a silent
no-op). See [DLQ + Replay → Closing the loop](../concepts/foundations/dlq-replay.md) for the
full set of prerequisites.

```bash
BALDUR_REPLAY_AUTOMATION_ON_RECOVERY_ENABLED=true
BALDUR_REPLAY_AUTOMATION_ON_RECOVERY_MAX_ITEMS=100
# JSON object: {"service_name": ["FAILURE_TYPE", ...]}
BALDUR_REPLAY_AUTOMATION_SERVICE_FAILURE_TYPE_MAP='{"payment_api": ["TIMEOUT", "CONNECTION_ERROR"]}'
```

## Audit

```bash
BALDUR_AUDIT_ENABLED=true
BALDUR_AUDIT_DISTRIBUTED_HASH_CHAIN=true   # set true on every pod of a multi-host deployment
BALDUR_AUDIT_BUFFER_REDIS_ENABLED=true     # set-to-enable: Redis staging buffer for audit records
```

An active PRO entitlement switches the audit subsystem on at startup and selects
the hash-chain backend, so a PRO install needs neither variable. Setting
`BALDUR_AUDIT_ENABLED` yourself always wins — `false` keeps audit off on an
entitled install, `true` switches it on without one.

`BALDUR_AUDIT_DISTRIBUTED_HASH_CHAIN` (default `false`) moves hash-chain
sequencing from a per-host file lock to Redis. A deployment where two or more
hosts write one trail (K8s with 2+ pods) must set it `true` on every pod: file
locks do not span hosts, so without it each host chains its entries
independently. `BALDUR_AUDIT_BUFFER_REDIS_ENABLED` (default `false`) stages
audit records in Redis and drains them to the terminal store in batches; it is
subordinate to the master switch, so it only takes effect while audit is
enabled. Both resolve their Redis connection through `BALDUR_REDIS_URL` (see
Storage below).

## License (entitlement)

```bash
BALDUR_LICENSE_KEY=<base64>
BALDUR_LICENSE_FILE=/etc/baldur/license
```

## Secrets (production boot gate)

```bash
BALDUR_SECRETS_AUDIT_SIGNING_KEY=<high-entropy-string>
BALDUR_SECRETS_ENCRYPTION_KEY=<fernet-key>
```

Both are CRITICAL secrets: with `BALDUR_ENVIRONMENT=production` set, boot
aborts (a `ConfigurationError` out of `baldur.init()`) when either is missing. The
gate runs before the audit switch is read, so it applies to every production
deployment whether or not audit is enabled. Outside production both may stay
unset — the zero-config development boot.

`BALDUR_SECRETS_AUDIT_SIGNING_KEY` keys the audit hash chain: each entry's
fingerprint becomes an HMAC-SHA256 keyed by this secret, so an actor who can
rewrite the stored files still cannot recompute a chain that passes
verification. `BALDUR_SECRETS_ENCRYPTION_KEY` encrypts the recoverable
(forensic-level) masked values; when it is unset, that masking degrades to a
non-recoverable form. Key generation, rotation, and the full
CRITICAL/IMPORTANT/OPTIONAL classification live in the secure-deployment
runbook shipped in the repository's `docs/runbooks/` directory.

## Storage

```bash
BALDUR_REDIS_URL=redis://localhost:6379
BALDUR_REDIS_PASSWORD=<secret>            # Redis instance / Sentinel master password
BALDUR_REDIS_SENTINEL_PASSWORD=<secret>   # Sentinel-node password (separate from master)
BALDUR_REDIS_USERNAME=<acl-user>          # Redis 6.0+ ACL username
BALDUR_REDIS_PROBE_CONNECT_TIMEOUT=0.5    # connect budget for the admission probe
BALDUR_REDIS_SOCKET_TIMEOUT=5.0           # per-operation socket timeout (seconds) on the data path
BALDUR_REDIS_RETRY_ON_TIMEOUT=true        # retry timed-out Redis operations instead of failing fast
BALDUR_RESILIENT_STORAGE_RECOVERY_PROBE_INTERVAL=5.0  # cooldown between degraded-mode recovery probes
BALDUR_SQL_DSN=postgresql://user:pass@host:5432/db
```

`BALDUR_SQL_DSN` is the canonical full-connection input. The discrete
`BALDUR_POSTGRES_HOST`, `BALDUR_POSTGRES_PORT`, `BALDUR_POSTGRES_DATABASE`, and
`BALDUR_POSTGRES_USER` vars are a postgres-only fallback, used only when
`BALDUR_SQL_DSN` is unset; they carry no password, so prefer the DSN for
authenticated connections.

`BALDUR_REDIS_URL` is the canonical Redis routing input for the cache, circuit
breaker, DLQ, audit-flush, resilient storage, and tiered-LOCAL. A per-feature
override (`BALDUR_RESILIENT_STORAGE_REDIS_URL`, `BALDUR_TIERED_REDIS_LOCAL_URL`,
`AUDIT_HASH_CHAIN_REDIS_URL`) wins where set; otherwise the consumer falls back
to `BALDUR_REDIS_URL`.

Redis credentials are configured **separately** from `BALDUR_REDIS_URL` and are
never embedded in it — keeping passwords out of the URL avoids leaking them into
logs, stack traces, and APM. `BALDUR_REDIS_PASSWORD` authenticates the Redis
instance (the master, under Sentinel); `BALDUR_REDIS_SENTINEL_PASSWORD`
authenticates the Sentinel nodes themselves when they require auth separate from
the master; `BALDUR_REDIS_USERNAME` supplies a Redis 6.0+ ACL username. Set only
the ones your deployment needs, and use the `rediss://` scheme for TLS
(standalone only — the Sentinel scheme does not currently support TLS).

`BALDUR_REDIS_PROBE_CONNECT_TIMEOUT` (default `0.5`) bounds only the first
connect that decides whether a Redis is reachable, before Baldur builds the
long-lived client for that lane. The data-path budgets
(`BALDUR_REDIS_SOCKET_TIMEOUT`, `BALDUR_REDIS_SOCKET_CONNECT_TIMEOUT`) are
unaffected by it. Raise it when a healthy Redis needs longer than half a second
to accept a connection — a cross-region or heavily loaded instance — because
otherwise that lane falls back as if the Redis were down. The rate-limit lane's
probe-failure warning names this variable for exactly that reason.

`BALDUR_REDIS_RETRY_ON_TIMEOUT` (default `true`) is the stall-vs-fast-fail lever
during a total Redis outage — one where no failover can promote a replica. With
retry on, an in-flight request on a Redis-touching path re-tries through the
outage and usually completes once Redis returns; the worker stays occupied for
the duration. With retry off, each Redis operation fails after roughly
`BALDUR_REDIS_SOCKET_TIMEOUT` (default `5.0` seconds) and the worker is freed —
at the cost of a client-visible, retriable error in place of a delayed success.
Flip it to `false` only when stalled requests threaten to exhaust the worker
pool under sustained load. `BALDUR_RESILIENT_STORAGE_RECOVERY_PROBE_INTERVAL`
(default `5.0`) sets the cooldown between degraded-mode recovery probes; leave
it at the default or shorten it so workers leave degraded mode quickly after
Redis recovers. The operational context for all three — what degrades, what
stalls, and the incident-response sequence — is the data-consistency-boundaries
runbook shipped in the repository's `docs/runbooks/` directory.

The RQ queue adapter is **not** yet routed through `BALDUR_REDIS_URL` and still
reads only a bare, non-prefixed `REDIS_URL`. On that path, clear any leftover bare
`REDIS_URL` so it cannot route the queue to a different Redis than your
`BALDUR_REDIS_URL`. The core Redis client's environment fallback prefers
`BALDUR_REDIS_URL` and reads a bare `REDIS_URL` only as a last-resort fallback
when the prefixed variable is unset, so a stray bare `REDIS_URL` can no longer
misroute it.

**Behavioral change (v1.x):** the audit-flush tasks and distributed hash
chain previously read a bare, non-prefixed `REDIS_URL` env var with a
hardcoded `redis://localhost:6379` default. They now resolve through
`BALDUR_REDIS_URL`. A deployment that set only the
undocumented bare `REDIS_URL` (and not `BALDUR_REDIS_URL`) must switch to
`BALDUR_REDIS_URL`. This is not an automated rename
(`scripts/migrate_baldur_env_vars.py` covers only `BALDUR_*`-prefixed keys).

## Health check

Readiness probes every configured database under a bounded budget. A database
that *refuses* connections always fails readiness. This variable decides the
other case: a database that accepts the connection but never answers, and so
exceeds the probe budget.

`not_ready` (the default) depools the pod, fast and honestly — the same outcome
a hung probe reaches today through the orchestrator's own probe timeout, but
decided by Baldur and visible in the response body. Choose `ready` when every
pod shares one database: there, depooling on a database stall takes the whole
service out of rotation at once, and staying in rotation degraded is the better
failure mode. Either way the affected alias is reported as `timed_out` in the
readiness body, so the stall is never silent.

```bash
BALDUR_HEALTH_CHECK_READINESS_TIMEOUT_FAIL_DIRECTION=not_ready
```

## Event logging (runtime level adjustment)

The global log level is read from `BALDUR_LOG_LEVEL` (default `WARNING`;
standard Python `logging` level names, e.g. `DEBUG`, `INFO`). It is a direct
environment read applied once when logging is configured — set it before the
process starts. `BALDUR_LOG_LEVEL=DEBUG` is the diagnostic switch the
troubleshooting page relies on (e.g. to surface the `protect.composer_built`
zone-composition event).

The four event families below have their own runtime-adjustable overrides:

```bash
BALDUR_EVENT_LOGGING_DLQ_LOG_LEVEL=INFO
BALDUR_EVENT_LOGGING_CB_LOG_LEVEL=WARNING
BALDUR_EVENT_LOGGING_REPLAY_LOG_LEVEL=INFO
BALDUR_EVENT_LOGGING_SLA_LOG_LEVEL=WARNING
```

## Admin server

Destructive admin operations (reset a breaker, purge the queue, flip the kill
switch) are refused with `403` until the server is explicitly unlocked — a
second gate on top of authentication, fail-closed by default. The unlock is
deliberate friction: a console left open in a browser tab cannot force
production. The admin server itself binds to localhost out of the box; its
other knobs stay advanced/internal for now.

```bash
BALDUR_ADMIN_UNLOCK=1   # set-to-enable: allow ADMIN-level (destructive) operations
```

## Scheduled jobs

Comma-separated names of the default scheduled jobs to skip at registration —
the targeted form of `BALDUR_SCHEDULER_AUTOSTART=0`, which stops all of them.
Valid names: `daily_report`, `sla_drift`, `cb_recovery`, `cb_override_expiry`,
`archive_old_dlq_entries`, `cleanup_expired_config`, `config_apply`,
`scan_zombie_rollouts`, `auto_promote_eligible`, `collect_canary_metrics`. An
unrecognised name logs a warning and is otherwise ignored.

Scope: the **in-process scheduler** only. On a Celery deployment the same jobs
also run off beat lanes this variable does not reach, controlled by
`configure_baldur_celery(include_*)` instead. The exceptions are `config_apply`
and the canary watchdog jobs, whose beat lanes honour this list as well.

```bash
BALDUR_SCHEDULER_DISABLED_JOBS=config_apply
```

## Canary watchdog (PRO)

The canary watchdog supervises live canary rollouts. It runs on any install
where the PRO distribution is present and the entitlement verdict is ACTIVE —
on Celery off the beat lane, elsewhere off the in-process scheduler — and the
worker or app process must have called `baldur.init()`, which is what registers
the canary rollout service the jobs need.

Its non-mutating work is always on: it renews each live rollout's config-type
lock (without which the lock lapses at its TTL and a second rollout can be
created for the same config type), alerts on rollouts that have stalled, and
collects rollout metrics. The two **mutating** actions are opt-in and off by
default, so activating the lane changes nothing on its own:

```bash
BALDUR_CANARY_WATCHDOG_ENABLE_AUTO_PROMOTE=true
BALDUR_CANARY_WATCHDOG_ENABLE_AUTO_ROLLBACK=true
```

`ENABLE_AUTO_PROMOTE` promotes a stage once its `duration_minutes` observation
window has elapsed and the pre-promote checks pass. `ENABLE_AUTO_ROLLBACK`
rolls back rollouts that the watchdog has judged **stalled** and that have been
stuck longer than `BALDUR_CANARY_WATCHDOG_AUTO_ROLLBACK_AFTER_MINUTES`. Note
the two conditions compose: a `CANARY`-state rollout is judged stalled only
after twice its current stage's `duration_minutes`, so a long stage waits for
the stall verdict first — the rollback timer is not measured from stage entry.

Before you enable the lane, review the rollouts that are already stalled — the
meta-watchdog's `canary_rollout` probe lists them under `rollout_ids`, as does
`get_active_rollouts()` — and resolve them. The first scan after activation
alerts once per still-stalled rollout, and those alerts draw on the same
notification budget as everything else.

## Runtime config delivery (PRO)

How often each process re-reads the stored configuration of every domain wired
for runtime pickup. This value *is* the convergence bound the config API
reports back to you: a change stored just after one read reaches that process's
consumers by the next one. Raising it widens the window in which a fleet can
serve two different configurations for one service; `0` turns the poll off, and
the domain then reports itself as stored-only instead of claiming a bound it
cannot keep. Like every other variable here, a change takes effect at the next
process start.

```bash
BALDUR_RUNTIME_CONFIG_WATCH_INTERVAL_SECONDS=30
```

## Circuit Breaker Slack push (OSS)

Set a Slack incoming-webhook URL and Baldur posts a message when a circuit
breaker opens or recovers. This is the one external notification the OSS tier
sends on its own; with the URL unset the open/close events are logged but
nothing is posted. The variable sits under the `META_WATCHDOG` namespace, but on
OSS only the circuit-breaker push reads it (the autonomous escalation paging
below is PRO). A set URL posts for real from any process that handles these
events, including local development, so leave it unset locally to avoid posting
to shared channels.

```bash
BALDUR_META_WATCHDOG_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

## Meta-Watchdog (self-monitoring, PRO)

Autonomous self-monitoring of Baldur's own healing subsystems. On detection of a
stuck/dead subsystem it pages a human through Slack or PagerDuty and stops
there — what ships enabled is detect-and-escalate, which takes no recovery
action of its own. Default-on under PRO — set
`BALDUR_META_WATCHDOG_ENABLED=false` to silence. Escalation pages deliver to
the same `BALDUR_META_WATCHDOG_SLACK_WEBHOOK_URL` documented in the
circuit-breaker push section above.

```bash
BALDUR_META_WATCHDOG_ENABLED=true
BALDUR_META_WATCHDOG_ESCALATION_ENABLED=true
BALDUR_META_WATCHDOG_PROBE_INTERVAL_SECONDS=30
BALDUR_META_WATCHDOG_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
BALDUR_META_WATCHDOG_PAGERDUTY_ROUTING_KEY=<pd-key>
BALDUR_META_WATCHDOG_RECOVERY_ENABLED=false  # opt-in autonomous repair: one bounded attempt per failing component before anyone is paged — read the two cautions below before setting it true
```

Recovery is implemented and ships switched off. Weigh two things before you turn
it on. The single flag covers both the in-process repairs and the ones that
restart shared infrastructure — the Redis and DLQ-worker workloads — so there is
no way to take the low-blast-radius half alone. And its circuit-breaker repair
force-closes the breakers it finds open without asking whether an operator put
them there (see [Circuit Breaker](../concepts/oss/circuit-breaker.md)), so a
manual block does not survive it. The per-component graduation criteria — which
repair exists for each component, its risk, and the evidence to look for first —
are in `docs/runbooks/meta-watchdog-escalation-response.md`.

Escalation only reaches you while the process is alive to send it. The outbound
liveness beacon covers the other case: set `BEACON_URL` and the watchdog loop
GETs it once per completed probe pass, so an external dead-man's-switch service
pages on the *absence* of pings when the process crashes, is OOM-killed or
hangs. Unset is the off switch (there is no separate enable flag). `FAIL_URL` is
optional and only routes UNHEALTHY passes elsewhere — silence is never used to
signal degradation, and with it unset an UNHEALTHY pass still pings `BEACON_URL`.
`TIMEOUT_SECONDS` (1–10) is the socket budget of the beacon's own sender thread
and bounds nothing on the watchdog loop. Setup, provider choice and grace-period
sizing: `docs/runbooks/meta-watchdog-escalation-response.md`.

```bash
BALDUR_META_WATCHDOG_BEACON_URL=https://<dms-provider>/ping/<check-id>
BALDUR_META_WATCHDOG_BEACON_FAIL_URL=https://<dms-provider>/ping/<check-id>/fail
BALDUR_META_WATCHDOG_BEACON_TIMEOUT_SECONDS=5
```

## Metrics source (canary live evaluation)

Connects Baldur to a Prometheus (or PromQL-compatible) metrics backend so the
canary live-evaluation gate can compare canary vs. stable traffic over the
evaluation window. Leave `BALDUR_PROMETHEUS_URL` unset and nothing is wired —
behavior is unchanged. Set it and `baldur.init()` registers the provider
automatically (an unset URL is the off switch — there is no separate enable
flag). `HEADERS` carries auth/tenancy credentials and is never logged.
`METRIC_NAMING` selects the query templates: `baldur` targets the built-in
`baldur_http_*` RED metrics, `otel` targets the OpenTelemetry HTTP-server
semantic-convention metrics. In a multi-service cluster set
`EXTRA_LABEL_SELECTORS` so queries are scoped to the target service instead of
aggregating the whole Prometheus. The remaining overrides let you point at a
third-party exporter's metric/label names.

```bash
BALDUR_PROMETHEUS_URL=http://prometheus:9090
BALDUR_PROMETHEUS_HEADERS='{"Authorization": "Bearer <token>", "X-Scope-OrgID": "tenant-a"}'
BALDUR_PROMETHEUS_TLS_VERIFY=true
BALDUR_PROMETHEUS_TLS_CA_CERT=/etc/ssl/certs/prometheus-ca.pem
BALDUR_PROMETHEUS_TIMEOUT_SECONDS=5.0
BALDUR_PROMETHEUS_RETRY_TOTAL=1
BALDUR_PROMETHEUS_RETRY_BACKOFF_FACTOR=0.5
BALDUR_PROMETHEUS_METRIC_NAMING=baldur
BALDUR_PROMETHEUS_EXTRA_LABEL_SELECTORS='{"namespace": "prod"}'
BALDUR_PROMETHEUS_SERVICE_LABEL=
BALDUR_PROMETHEUS_REQUESTS_TOTAL_METRIC=
BALDUR_PROMETHEUS_DURATION_HISTOGRAM_METRIC=
BALDUR_PROMETHEUS_STATUS_CODE_LABEL=
BALDUR_PROMETHEUS_ERROR_STATUS_REGEX=5..
```
