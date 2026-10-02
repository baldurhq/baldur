# Storage backends

> Baldur keeps its own state in three kinds of store — in-memory, Redis, and a
> SQL database. This page explains which to use, when, and how to switch, and
> clears up one common confusion first.

## Baldur's state vs. the database you protect

Two different "databases" show up around Baldur, and it is worth separating them
before anything else:

- **The dependency you protect.** Your app's Postgres, a payment API, a search
  cluster — the thing Baldur wraps with a circuit breaker, retry, or bulkhead.
  When the concept guides mention "a slow database," this is what they mean.
  Baldur stores its own state here only if this is also the database you hand it:
  through `BALDUR_SQL_DSN`, or on Django through your `DATABASES` setting, which
  Baldur uses by default (see the SQL section below).
- **Baldur's own state store.** Where Baldur keeps *its* bookkeeping: circuit
  breaker counters, idempotency keys, rate-limit windows, the dead-letter queue,
  cached status snapshots. This is what the rest of this page is about — and what
  you pick when you set `BALDUR_REDIS_URL` or `BALDUR_SQL_DSN`.

## The three backends

### In-memory (the default)

Out of the box Baldur keeps its state in process memory. Two things also go to local
disk: the kill switch, which it records in a small file, and a write-ahead log of circuit
breaker state changes (under `/var/log/baldur/wal`, or a per-user directory when that is
not writable; `BALDUR_RESILIENT_STORAGE_WAL_DIR` moves it), which a restart without Redis
does not read back. There is nothing to install or configure beyond the package itself;
this is the whole [quickstart](../../getting-started/index.md) path.

Its one hard limit is that the store is **per process**. Run more than one worker
(`gunicorn --workers N`, `uvicorn --workers N`, several Celery workers) and each
gets its own copy: circuit breaker state, idempotency keys, and rate-limit
counters diverge silently across workers. That breaks **correctness**, not just
scale. The store is also volatile: a process restart clears it, so breaker state,
idempotency keys, and dead letters start from zero. Treat it as a development
backend. With `BALDUR_ENVIRONMENT=production` set, `baldur.init()` raises
`ConfigurationError` until `BALDUR_REDIS_URL` is set and the `redis` extra is installed,
even for a single process; `BALDUR_TEST_MODE=true` accepts a memory-only process
deliberately.

### Redis (shared across workers)

The moment you run more than one worker or host, point Baldur at Redis so its
bookkeeping lives in one store that every process shares. It is a single
variable, no code change:

```bash
pip install baldur-framework[redis]
export BALDUR_REDIS_URL=redis://localhost:6379/0
```

That one URL is the canonical routing input for Baldur's Redis consumers:
circuit breaker state, idempotency keys, rate-limit windows, the dead-letter
queue, the shared cache tier, and the system-control kill switch. A duplicate
idempotency key is now rejected fleet-wide instead of per worker, and every
worker's breaker state and dead letters land in the same store.

One of those consumers shares *state* through Redis without sharing *decisions*,
and the difference matters when you plan around an incident. Each circuit
breaker still opens and closes from its own worker-local counts; Redis gives it
restart recovery and half-open coordination, not a fleet-wide trip. The
[Circuit Breaker guide](../oss/circuit-breaker.md) covers how one worker's OPEN
can reach the rest of the fleet (a PRO option). The kill switch does reach the
whole fleet: with `BALDUR_REDIS_URL` set its state lives in Redis unless you
choose the file store, and every process re-reads it within about five seconds.
The [System Control guide](../oss/system-control.md) covers what a flip reaches
and what happens while the store is down.

**High availability.** For a Redis Sentinel topology, use the `redis+sentinel://`
scheme with the master name and the sentinel hosts; credentials stay out of the
URL:

```bash
export BALDUR_REDIS_URL=redis+sentinel://mymaster@sentinel-a:26379,sentinel-b:26379/0
export BALDUR_REDIS_PASSWORD=<master-password>
export BALDUR_REDIS_SENTINEL_PASSWORD=<sentinel-node-password>   # if your sentinels require auth
```

Use `rediss://` for TLS to a standalone Redis; the Sentinel scheme does not
currently support TLS. Async calls that carry an idempotency key (`@aprotected`, or
`@protected` on an `async def`) cannot use the Sentinel scheme yet: each one raises
`ValueError` before it runs, while synchronous keyed calls and async calls without a key
are unaffected. Sentinel support is part of the open-source core and is the recommended
topology for a growth-stage fleet; standalone Redis is fine for a single host.

While Redis is briefly unreachable, workers stop sharing state, and what each
feature does then (degrade or fail closed) differs: the data-consistency runbook
linked below covers the dead-letter queue, circuit breakers, counters and
idempotency keys.

### SQL / your relational database (advanced)

Baldur can also keep its **incident history** in a relational database through the SQL
adapter: security incidents, plus postmortems and recovery-session archives, which
are recorded only with PRO active. The event journal
lands there too when Redis is not configured. Postgres, MySQL, and SQLite are supported,
selected by the DSN scheme:

```bash
pip install baldur-framework[postgres]
export BALDUR_SQL_DSN=postgresql://user:pass@host:5432/db
```

Baldur opens the connection from the DSN itself, through psycopg2 for Postgres (the
`postgres` extra installs it), mysql-connector-python for MySQL, or Python's built-in
sqlite3. It opens a new connection for every operation, with no pool, and says so once at
startup in a `sql.default_factory_no_pool` line at INFO. That suits the incident history's
write rate. Where the connection count matters — a dead-letter queue on SQL under a
failure storm, a busy database — register a pooled provider under the name `sql` before
`baldur.init()`, for example
`ProviderRegistry.failed_op_repo.register("sql", lambda: SQLFailedOperationRepository(engine.raw_connection))`;
Baldur never replaces a registration you made (the
[SQL adapter reference](../../reference/adapters/sql.md) lists the callables the
repositories accept). The store must still select `sql`: the incident history does when
`BALDUR_SQL_DSN` is set; the dead-letter queue prefers Redis whenever `BALDUR_REDIS_URL` is
set, so there it needs `BALDUR_DLQ_BACKEND=sql`.

At startup Baldur builds the SQL stores it selected, so a missing driver or a PostgreSQL
DSN that does not parse stops a production boot with a message naming `BALDUR_SQL_DSN`,
and warns elsewhere. It does not connect: a DSN that parses but names the wrong host or
password boots, and the first write to that store fails.

Reach for this when you want that history **durable and queryable in the database
you already operate** rather than in Redis. With PRO active, production requires such a
home, because PRO writes its incident records there: with
`BALDUR_ENVIRONMENT=production`, `baldur.init()` refuses to start until `BALDUR_SQL_DSN` is
set. A Django app can skip the DSN, because with it unset Baldur keeps security incidents
and postmortems in your `DATABASES`. Without PRO nothing writes this history on its own,
so production does not require it: outside Django, security incidents your app records
through the security API stay in each process's memory, announced at startup at INFO,
until you set `BALDUR_SQL_DSN`.

**The dead-letter queue can live here too.** A parked call is business data, an order that
did not go through, so it is the one live store worth keeping in a database you already
back up. Select it explicitly, or let Baldur pick:

```bash
export BALDUR_DLQ_BACKEND=sql     # explicit
```

Left unset, Baldur picks the first backend the environment offers: Redis when
`BALDUR_REDIS_URL` is set, otherwise SQL when a DSN is configured, otherwise memory. So a
deployment with Redis keeps its dead letters in Redis, and one with only a database keeps
them there instead of losing them at the next restart. If the chosen backend cannot be
constructed (the driver is not installed, say), Baldur says so at startup rather than
quietly falling back: in production it refuses to boot, elsewhere it warns and falls back
to memory. The check proves the driver loads and, for PostgreSQL, that the DSN parses —
not that the database answers: a capture that cannot reach the database is written to a
local fallback file instead, and the failed write is logged at ERROR. Nothing moves that
record back into the queue once the database answers; recovering it from that host's disk is a
manual step.

Be precise about what this makes durable: the **store**. By default capture is
asynchronous (the entry is buffered in-process and written a moment later), so a process
killed inside that window can still lose the most recent entries. Set
`BALDUR_DLQ_OUTBOX_ENABLED=false` when you need each capture written before the protected
call returns.

The other **live** stores — circuit breaker state, idempotency keys, and
rate-limit windows — do not move to SQL. They are high-frequency coordination
state and belong in memory or Redis. Baldur is a resilience layer, not your
system of record — it does not move your application's data here. The SQL
backend is advanced and is not part of the tested compatibility matrix; the
default multi-worker path is Redis, not SQL.

## Which do I need?

| You are… | Backend | Set |
|----------|---------|-----|
| Trying Baldur, or running a single process outside production | In-memory | *nothing — it is the default* |
| Running more than one worker or host | Redis | `BALDUR_REDIS_URL=redis://…` |
| Running Redis with high availability | Redis Sentinel | `BALDUR_REDIS_URL=redis+sentinel://…` |
| Running with `BALDUR_ENVIRONMENT=production` | Redis | `BALDUR_REDIS_URL=…` |
| Running with `BALDUR_ENVIRONMENT=production` and PRO active | Redis, plus SQL unless you run Django | `BALDUR_REDIS_URL=…` + `BALDUR_SQL_DSN=…` |
| Wanting durable, queryable incident history in your RDBMS | SQL | `BALDUR_SQL_DSN=postgresql://…` |
| Wanting parked calls in the database you already back up | SQL | `BALDUR_SQL_DSN=…` + `BALDUR_DLQ_BACKEND=sql` |

## See also

- [Getting Started](../../getting-started/index.md) — every quickstart ends with the Redis production step
- [Environment Variables](../../reference/env-vars.md) — the `Storage` section lists `BALDUR_REDIS_URL`, `BALDUR_SQL_DSN`, `BALDUR_DLQ_BACKEND`, and the per-feature Redis overrides
- [Data consistency boundaries runbook](https://github.com/baldurhq/baldur/blob/main/docs/runbooks/data-consistency-boundaries.md) — which data belongs in Baldur vs. an ACID database, and what a Redis outage does to the dead-letter queue, breakers, counters and idempotency keys
