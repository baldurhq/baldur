# Storage backends

> Baldur keeps its own state in one of three places — in-memory, Redis, or a
> SQL database. This page explains which to use, when, and how to switch, and
> clears up one common confusion first.

## Baldur's state vs. the database you protect

Two different "databases" show up around Baldur, and it is worth separating them
before anything else:

- **The dependency you protect.** Your app's Postgres, a payment API, a search
  cluster — the thing Baldur wraps with a circuit breaker, retry, or bulkhead.
  When the concept guides mention "a slow database," this is what they mean.
  Baldur never stores its own state here.
- **Baldur's own state store.** Where Baldur keeps *its* bookkeeping: circuit
  breaker counters, idempotency keys, rate-limit windows, the dead-letter queue,
  cached status snapshots. This is what the rest of this page is about — and what
  you pick when you set `BALDUR_REDIS_URL` or `BALDUR_SQL_DSN`.

## The three backends

### In-memory (the default)

Out of the box Baldur stores everything in process memory. No Redis, no
database, no environment variables — `pip install` → `@baldur.protected` →
working code. This is the whole [quickstart](../../getting-started/index.md) path.

Its one hard limit is that the store is **per process**. Run more than one worker
(`gunicorn --workers N`, `uvicorn --workers N`, several Celery workers) and each
gets its own copy: circuit breaker state, idempotency keys, and rate-limit
counters diverge silently across workers. That breaks **correctness**, not just
scale. The in-memory store also grows unbounded. Treat it as a single-process /
development backend.

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

Two of those consumers share *state* through Redis without sharing *decisions*,
and the difference matters when you plan around an incident. Each circuit
breaker still opens and closes from its own worker-local counts; Redis gives it
restart recovery and half-open coordination, not a fleet-wide trip. The
[Circuit Breaker guide](../oss/circuit-breaker.md) covers how one worker's OPEN
can reach the rest of the fleet (a PRO option). The kill switch reaches the
whole fleet only after you deliberately select its Redis state backend; by
default its state lives in a local file on each host, and the
[System Control guide](../oss/system-control.md) walks through that choice.

**High availability.** For a Redis Sentinel topology, use the `redis+sentinel://`
scheme with the master name and the sentinel hosts; credentials stay out of the
URL:

```bash
export BALDUR_REDIS_URL=redis+sentinel://mymaster@sentinel-a:26379,sentinel-b:26379/0
export BALDUR_REDIS_PASSWORD=<master-password>
export BALDUR_REDIS_SENTINEL_PASSWORD=<sentinel-node-password>   # if your sentinels require auth
```

Use `rediss://` / `rediss+sentinel://` for TLS. Sentinel is the recommended
topology at growth (PRO) scale; standalone Redis is fine for a single host.

While Redis is briefly unreachable, workers stop sharing state — how each feature
behaves during the outage (skip the tier, degrade, or fail closed) differs per
feature and is covered in the data-consistency runbook linked below.

### SQL / your relational database (advanced, optional)

Baldur can also persist a subset of its own repositories — circuit breaker state,
failed-operation records, incidents, forensics, and statistics — to a relational
database through the SQL adapter. It works with any DB-API 2.0 driver (Postgres,
MySQL, SQLite), selected by the DSN scheme:

```bash
pip install baldur-framework[postgres]
export BALDUR_SQL_DSN=postgresql://user:pass@host:5432/db
```

Reach for this when you want that history **durable and queryable in the database
you already operate** rather than in Redis. It is an advanced backend and is not
part of the v1.0 tested compatibility matrix; the default multi-worker path is
Redis, not SQL. Baldur is a resilience layer, not your system of record — it does
not move your application's data here.

## Which do I need?

| You are… | Backend | Set |
|----------|---------|-----|
| Trying Baldur, or running a single process | In-memory | *nothing — it is the default* |
| Running more than one worker or host | Redis | `BALDUR_REDIS_URL=redis://…` |
| Running Redis with high availability | Redis Sentinel | `BALDUR_REDIS_URL=redis+sentinel://…` |
| Wanting durable, queryable Baldur history in your RDBMS | SQL | `BALDUR_SQL_DSN=postgresql://…` |

## See also

- [Getting Started](../../getting-started/index.md) — every quickstart ends with the Redis production step
- [Environment Variables](../../reference/env-vars.md) — the `Storage` section lists `BALDUR_REDIS_URL`, `BALDUR_SQL_DSN`, and the per-feature Redis overrides
- [Data consistency boundaries runbook](https://github.com/baldurhq/baldur/blob/main/docs/runbooks/data-consistency-boundaries.md) — which data belongs in Baldur vs. an ACID database, and how each feature behaves when a backend is unavailable
