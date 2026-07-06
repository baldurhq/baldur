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
BALDUR_CB_FAILURE_THRESHOLD=5
BALDUR_CB_RECOVERY_TIMEOUT=60
BALDUR_CB_HALF_OPEN_MAX_CALLS=3
BALDUR_RETRY_MAX_ATTEMPTS=3
BALDUR_RETRY_BASE_DELAY=1.0
BALDUR_IDEMPOTENCY_ENABLED=true
BALDUR_IDEMPOTENCY_DEFAULT_CACHE_TTL=60
BALDUR_IDEMPOTENCY_GATE_MEMORY_TTL_SECONDS=1800
```

## DLQ (PRO)

Requires the `baldur_pro` package — the DLQ store ships only in `baldur_pro`.
Without it these knobs are a no-op: the OSS path is a no-op recorder that
discards the operation instead of capturing it for replay. The failure still
raises to the caller — only the durable, replayable record is missing. The
first time `protect(dlq=True)` is wired without a store, the facade logs a
one-time `protect.dlq_requested_without_backing` warning so the missing
backing is visible rather than silent.

```bash
BALDUR_DLQ_ENABLED=true
BALDUR_DLQ_MAX_SIZE=100000
BALDUR_DLQ_OUTBOX_ENABLED=true
```

## Replay automation (PRO)

Automatic replay on circuit-breaker recovery. `ON_RECOVERY_ENABLED` is on by
default; `SERVICE_FAILURE_TYPE_MAP` maps each recovered service to the failure types
whose captured entries it is responsible for — an empty mapping leaves the loop unable
to select entries on recovery (surfaced as a blocked-with-signal event, not a silent
no-op). See [DLQ + Replay → Closing the loop](../concepts/pro/dlq-replay.md) for the
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
BALDUR_LEADER_ELECTION_REDIS_URL=...  # (PRO — Redis leader elector ships in baldur_pro)
BALDUR_SQL_DSN=postgresql://user:pass@host:5432/db
```

`BALDUR_SQL_DSN` is the canonical full-connection input. The discrete
`BALDUR_POSTGRES_HOST`, `BALDUR_POSTGRES_PORT`, `BALDUR_POSTGRES_DATABASE`, and
`BALDUR_POSTGRES_USER` vars are a postgres-only fallback, used only when
`BALDUR_SQL_DSN` is unset; they carry no password, so prefer the DSN for
authenticated connections.

`BALDUR_REDIS_URL` is the canonical Redis routing input for the cache, circuit
breaker, DLQ, leader elector, audit-flush, resilient storage, tiered-LOCAL, and
the Redis quorum witness. A per-feature override (`BALDUR_LEADER_ELECTION_REDIS_URL`,
`BALDUR_RESILIENT_STORAGE_REDIS_URL`, `BALDUR_TIERED_REDIS_LOCAL_URL`,
`BALDUR_MULTIREGION_QUORUM_REDIS_URL`, `AUDIT_HASH_CHAIN_REDIS_URL`) wins where
set; otherwise the consumer falls back to `BALDUR_REDIS_URL`.
`BALDUR_LEADER_ELECTION_REDIS_URL` is **(PRO)** — the Redis leader elector
ships only in `baldur_pro`, so this override is a no-op in an OSS install.

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
of a stuck/dead subsystem it pages a human (PagerDuty/Slack); it does not
self-recover (autonomous recovery is deferred). Default-on under PRO — set
`BALDUR_META_WATCHDOG_ENABLED=false` to silence.

```bash
BALDUR_META_WATCHDOG_ENABLED=true
BALDUR_META_WATCHDOG_ESCALATION_ENABLED=true
BALDUR_META_WATCHDOG_PROBE_INTERVAL_SECONDS=30
BALDUR_META_WATCHDOG_PAGERDUTY_ROUTING_KEY=<pd-key>
```
