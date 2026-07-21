# Public Environment Variables (Operator-Tunable Allowlist)

Operators may set these env vars in production. Everything else with a
`BALDUR_*` prefix is advanced / internal and subject to change in v1.x.

The full settings inventory is internal to v1.0; operator-tunable
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
BALDUR_RETRY_MAX_ATTEMPTS=3
BALDUR_RETRY_BASE_DELAY=1.0
BALDUR_RETRY_MAX_ELAPSED=30.0  # total wall-clock retry budget (s); unset = no budget. Distinct from the per-sleep max_delay cap.
BALDUR_IDEMPOTENCY_ENABLED=true
BALDUR_IDEMPOTENCY_DEFAULT_CACHE_TTL=60
BALDUR_IDEMPOTENCY_GATE_MEMORY_TTL_SECONDS=1800
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
```

## License (entitlement)

```bash
BALDUR_LICENSE_KEY=<base64>
BALDUR_LICENSE_FILE=/etc/baldur/license
```

## Storage

```bash
BALDUR_REDIS_URL=redis://localhost:6379
BALDUR_REDIS_PASSWORD=<secret>            # Redis instance / Sentinel master password
BALDUR_REDIS_SENTINEL_PASSWORD=<secret>   # Sentinel-node password (separate from master)
BALDUR_REDIS_USERNAME=<acl-user>          # Redis 6.0+ ACL username
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
the ones your deployment needs, and use the `rediss://` / `rediss+sentinel://`
scheme for TLS.

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

```bash
BALDUR_EVENT_LOGGING_DLQ_LOG_LEVEL=INFO
BALDUR_EVENT_LOGGING_CB_LOG_LEVEL=WARNING
BALDUR_EVENT_LOGGING_REPLAY_LOG_LEVEL=INFO
BALDUR_EVENT_LOGGING_SLA_LOG_LEVEL=WARNING
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

Autonomous self-monitoring of Baldur's own healing subsystems. On detection
of a stuck/dead subsystem it pages a human through Slack or PagerDuty; it does not
self-recover (autonomous recovery is deferred). Default-on under PRO — set
`BALDUR_META_WATCHDOG_ENABLED=false` to silence. Escalation pages deliver to
the same `BALDUR_META_WATCHDOG_SLACK_WEBHOOK_URL` documented in the
circuit-breaker push section above.

```bash
BALDUR_META_WATCHDOG_ENABLED=true
BALDUR_META_WATCHDOG_ESCALATION_ENABLED=true
BALDUR_META_WATCHDOG_PROBE_INTERVAL_SECONDS=30
BALDUR_META_WATCHDOG_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
BALDUR_META_WATCHDOG_PAGERDUTY_ROUTING_KEY=<pd-key>
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
