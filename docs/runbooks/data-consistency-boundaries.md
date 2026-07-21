# Data Consistency Boundaries Runbook

> **Purpose**: Define which data classes belong inside Baldur's resilience layer (Redis + WAL fallback) and which must stay in an ACID-strong store (PostgreSQL/Aurora/Spanner). Reading this once before adopting Baldur prevents the most common production data-loss class.
> **Audience**: Tech lead / SRE evaluating Baldur for a new service, or operating it at PRO tier (5~30 services, 5K RPS, 99.9% SLO).
> **Cadence**: One-time read at adoption + revisit during architecture review.

---

## TL;DR

Baldur is an **AP system** (Available + Partition-tolerant per the CAP theorem). When Redis is unreachable, the framework keeps responding by serving from each worker's local memory + WAL — but it does **not** preserve cross-worker consistency during that window. Five narrow classes of data loss / divergence are possible *during DEGRADED mode*. None of them ever delete WAL-persisted data, but two of them (counter undercount, last-write-wins generic set) can lose application-visible state on recovery, and one (cross-worker idempotency invisibility) can cause business-level double-actions.

**The single most important rule**: **Never store data inside Baldur's cache that you cannot tolerate losing in narrow cross-worker DEGRADED windows.** Money, billing meters, idempotency truth-of-record → ACID DB. Rate limits, CB state, DLQ buffers, hot caches → Baldur.

---

## Background — What DEGRADED Mode Means

`ResilientStorageBackend` (`src/baldur/adapters/resilient/backend.py:75`) operates in two modes:

| Mode | Read source | Write target | Cross-worker visibility |
|------|-------------|--------------|-------------------------|
| `REDIS` | Redis (shared) | Redis (immediate) | ✅ All workers see writes |
| `DEGRADED` | `self._memory` (local only) | WAL(disk fsync) + `self._memory` | ❌ Each worker isolated |

DEGRADED is entered automatically when Redis becomes unreachable (`backend.py:766-771`). The WAL-First Write Protocol (`backend.py:432-464`) guarantees that writes survive process crash — every entry is fsynced to disk before being acknowledged. On Redis recovery, the WAL is replayed back into Redis (`backend.py:805-849`) — issue 470 wired the automatic trigger that makes this happen without operator intervention.

**Issue 470 changed nothing about DEGRADED-mode data semantics.** It only ensured workers leave DEGRADED automatically when Redis recovers, instead of staying stuck in DEGRADED for the rest of the process lifetime.

---

## The Five DEGRADED-Window Trade-offs

These exist whenever Baldur is in DEGRADED mode, regardless of issue 470. They are the consequence of the CAP-theorem AP choice the framework made.

### 1. DLQ delivery latency — NOT data loss

DLQ entries written during DEGRADED land in WAL + memory. Peer worker DLQ consumers reading from Redis ZSET cannot see them until the writer's WAL is replayed on recovery.

- **What is lost**: nothing. WAL is durable.
- **What is delayed**: cross-worker DLQ drain by `(DEGRADED duration + recovery duration)`.
- **Real-world impact**: time-critical retries (e.g., "must retry within 30s or lose payment auth window") can miss their SLO.

### 2. Circuit-breaker cross-worker coordination — temporary loss of cluster-wide CB

When workers are DEGRADED, each worker's local CB sees only its own request stream. Worker A may see the downstream failing and OPEN locally, while worker B (still in REDIS mode, observing a stale Redis CB state) keeps sending traffic.

- **What is lost**: the property that 5 workers cooperatively decide CB OPEN within ~1 second based on combined statistics.
- **What survives**: local per-worker CB still protects each worker's own traffic.
- **Mitigation built in**: `_sync_memory_to_redis` (`backend.py:880-893`) runs `cb:*` keys through the drift reconciler ("Most Restrictive Wins") on recovery, so the merged state after recovery is the strictest of all workers' views.

### 3. Counter undercount — REAL data loss

The most subtle case. `incr` in DEGRADED is documented as "best effort, not truly atomic" (`backend.py:756`):

```python
with self._lock:
    current = self._memory.get(key, 0)
    new_value = int(current) + 1
    self._memory[key] = new_value
```

On recovery, `_sync_memory_to_redis` (`backend.py:898`) writes via `set()` not `incrby()`:

```python
self._redis.set(full_key, value)  # overwrites — not additive
```

**Loss scenario**:
- Worker A increments counter `meter:user:42` from 50 → 70 during DEGRADED (memory[counter]=70)
- Worker B increments the same counter from 50 → 60 (memory[counter]=60)
- Redis recovers. A syncs first → `Redis[counter]=70`. B syncs next → `Redis[counter]=60`.
- The +20 worth of A's increments is **permanently lost**.

**This is real data loss**, and WAL replay does not fix it (replay also uses set-semantics, not incrby).

**Implication**: never use Baldur counters for billing meters, quota truth-of-record, or anything that requires SUM consistency. Use a database `INCREMENT` for those. Baldur counters are for rate-limit hints and observability where small drift is acceptable.

### 4. Idempotency-key invisibility — business-level double-action risk

An idempotency key written by worker A in DEGRADED is invisible to worker B until WAL replay completes. If a client retries and the load balancer routes the retry to worker B, B sees no key in Redis and processes the request as a fresh action.

- **What is lost**: no Baldur-level data. The idempotency key itself is durable in WAL.
- **What is corrupted**: business state, e.g., a payment processed twice.

**Implication**: a Redis-only idempotency check is **insufficient** for money-equivalent operations. The truth-of-record for idempotency on critical paths must be a database `UNIQUE` constraint. Stripe's published architecture works exactly this way — Redis is the fast path, PostgreSQL is the gate.

### 5. Stale / divergent reads across workers

Worker A writes `set("user:123:profile", new_value)` in DEGRADED → only A sees `new_value`. The user's next request, routed by the LB to worker B, returns Redis's old value or None.

- **What is lost**: the user's perceived "save → see the new value" guarantee.
- **What survives**: the data itself (in A's WAL), which converges on recovery.

**Implication**: do not put authoritative "current value" data in Baldur if your UX depends on read-your-writes consistency across the LB. Sticky sessions or DB-backed reads are required for that.

---

## Request-Path Liveness Under Total Redis Loss (the other axis)

The five trade-offs above are the **data** axis — what happens to *values* during a Redis blip. There is a second, independent axis: **request latency / worker liveness** when Redis is *completely* unreachable with no failover possible (a Sentinel quorum loss, or standalone Redis simply down). On this axis Baldur has **two distinct tracks**, and they behave differently:

| Track | Backing | Behavior when Redis is unreachable |
|-------|---------|------------------------------------|
| CB state, DLQ buffer, generic cache | `ResilientStorageBackend` (`src/baldur/adapters/resilient/backend.py`) — a **sticky** mode flag + periodic recovery probe + WAL | Degrades **responsively**: after the mode flips to DEGRADED, reads/writes serve from local memory + WAL. No per-request Redis wait. |
| Idempotency gate (`setnx`) | the **raw** cache adapter from the registry (`src/baldur/core/idempotency_gate.py` → `ProviderRegistry.get_cache()` → Redis) — **not** the resilient backend | **Blocks per request**: each `setnx` hits the dead Redis and waits out the client's `socket_timeout` (× retries), request after request, for the whole outage. |

### Why the idempotency gate blocks instead of degrading — it is fail-closed by design

This is deliberate, and it is the same principle as the data rule above ("money + idempotency truth → strong consistency"). Degrading idempotency to each worker's local memory during a Redis outage would silently drop the **cross-worker** dedup guarantee — two workers could each accept the "same" charge and double-act. So the idempotency path refuses to degrade: in production without an explicit escape hatch it **fail-closes** (it will not process a money-equivalent operation without the cross-worker guarantee) rather than fail-open into an unsafe per-worker dedup. Under a total Redis loss that fail-closed posture manifests as a **block** — the request waits for Redis to come back rather than proceeding unsafely.

### What an operator sees

- **In-flight requests on Redis-touching paths stall** for the outage duration and then mostly complete successfully once Redis returns. A small number may hard-fail (client-visible, retriable) — there is **no silent data loss**.
- The **vast majority** of requests (everything before and after the fault) is unaffected — only the requests in-flight during the outage stall. Recovery is bounded by **Redis returning**, not by any client timeout.
- The blast radius is bounded by **in-flight concurrency**: under closed-loop / bounded load only the active requests stall. Under **open-loop high traffic**, stalling requests can accumulate and exhaust the worker pool → whole-service unavailability. That boundary (and its graceful upgrade — a circuit breaker on Baldur's own Redis path so the gate fail-closes *fast* with a 503 instead of stalling) is tracked in the maintainer scale-boundary log as **SB-038**.

### The one knob that moves this today

`BALDUR_REDIS_RETRY_ON_TIMEOUT` (default `true`) is the lever, **not** `BALDUR_REDIS_SOCKET_TIMEOUT`:

- With retry **on** (default), each request re-tries the socket timeout through the whole outage, so the worker is never freed until Redis returns — the stall runs the full outage. Raising `socket_timeout` barely changes this.
- With retry **off** (`BALDUR_REDIS_RETRY_ON_TIMEOUT=false`), each Redis op fast-fails at ~`socket_timeout` with no retry, so the worker is freed sooner and the app stays responsive for non-Redis paths — **at the cost of turning a delayed-success into a fast client-visible failure** (the client must retry). This is the operator's *stall-vs-fast-fail* lever until SB-038's Redis-path circuit breaker lands.

### Operator response during a total-Redis-loss incident

1. **Confirm the regime.** A normal Sentinel *failover* recovers in seconds (the replica is promoted). A *total* loss — Sentinel quorum broken (≥2 of 3 sentinels also down) or standalone Redis down — has **no promotion** and stalls until Redis/quorum is restored. Check `SENTINEL ckquorum <master>` from a surviving sentinel; `NOQUORUM` confirms total loss.
2. **Expect the stall.** In-flight requests on idempotency-gated paths will stall for the outage. No framework action frees them faster than restoring Redis — this is the fail-closed guarantee working as designed.
3. **Restore Redis / repair Sentinel quorum.** Requests auto-recover on Redis return; no operator action inside Baldur is required.
4. **If worker-pool exhaustion under load is the failure mode**, set `BALDUR_REDIS_RETRY_ON_TIMEOUT=false` to bound per-request cost — accept fast-fail 503s in place of delayed successes for the outage window.

### Prevention

Total Redis loss frequency is **topology-dominated**, not a Redis property. The single most effective prevention is the same infra hardening as the data axis: run Redis in HA (Sentinel/Cluster) **and** spread the sentinels across ≥3 independent failure domains with anti-affinity from the data nodes, so no single host/rack/AZ failure takes out both the master and a sentinel majority. A quorum needs ≥2 independent domains to fail at once — that is what makes it rare when the topology is right, and as common as a single-host outage when it is not.

---

## Decision Flowchart — Where Should This Data Live?

```
Is the data tolerated to disappear briefly across workers
during a Redis blip?
│
├─ YES → Baldur OK
│        Examples: rate-limit counters, CB state, DLQ buffer,
│        hot cache, ephemeral session state, observability counters
│
└─ NO  → Does losing this data cause user-visible business harm?
         │
         ├─ YES (money, orders, identity, quota truth-of-record)
         │     → ACID database is the truth-of-record.
         │       Baldur may sit in front as a fast cache,
         │       but the database write must succeed before
         │       acknowledging the user.
         │       Examples: payments, account balance, order status,
         │       idempotency-key truth-of-record.
         │
         └─ NO (analytics, logs, derived metrics, A/B telemetry)
               → Baldur OK; treat brief loss as monitoring-tier noise.
```

**Quick gut-check questions**:
1. If this counter were 5% off after a Redis incident, would Finance care? → ACID DB.
2. If this key disappeared for 30 seconds during a cross-worker write, would a user double-pay? → ACID DB.
3. If this state were visible to only 4 of 5 workers for 30 seconds, would the system still be correct? → Baldur OK.

---

## Frequency Estimates by Tier

| Tier | Redis topology | Expected blip frequency | Expected DEGRADED window per blip | Cumulative DEGRADED time/year |
|------|---------------|-------------------------|----------------------------------|-------------------------------|
| OSS (single Redis instance, dev/prod parity) | Standalone | Monthly hiccups | 30s ~ 5min | 0.05% ~ 0.5% |
| PRO (Redis Sentinel, well-tuned) | Sentinel HA | Quarterly | 5s ~ 30s | < 0.01% |
| PRO (Redis Cluster, multi-AZ ElastiCache) | Cluster, multi-AZ | Yearly | < 30s | < 0.001% |

**For PRO operators, well-tuned Redis HA pushes the realistic data-loss exposure to 1~2 events per year, each affecting a sub-30-second window.** If your data-class assignments per the flowchart above are correct, this is comfortably inside a 99.9% SLO.

---

## Why "Just Use Strong Consistency Everywhere" Doesn't Work

A common reaction is "let's just use a CP system and avoid these trade-offs entirely." This is what big tech does for *some* data — but never for *all*. The reason:

- **CAP theorem (Brewer 2000)**: when a network partition exists, you can have C or A, not both. Partitions WILL happen — TCP RSTs, switch failures, AZ outages.
- **FLP impossibility (1985)**: deterministic consensus in an asynchronous network is impossible if any node may fail. All distributed consensus is probabilistic / timing-dependent.

Big-tech architectures concretely:

| Company | Strong-consistency layer | Eventually-consistent layer | What lives in each |
|---------|--------------------------|------------------------------|--------------------|
| Google | Spanner (CP, TrueTime + Paxos) | Bigtable (AP), Memcache | Money/identity → Spanner. Search index, ad serving counters → Bigtable/Memcache |
| Amazon | DynamoDB (strong-read mode), Aurora | DynamoDB (default eventual), ElastiCache | Orders/payments → Aurora. Cart cache, session, rate limit → DynamoDB-eventual / ElastiCache |
| Meta | TAO (writes), MySQL | Memcache, async replicas | Friends graph mutations → TAO/MySQL. Read serving → memcache; documented logic-error class accepted |
| Netflix | Cassandra (with quorum) | Cassandra (without quorum), EVCache | Account state → quorum reads. Recommendations → eventual |
| Stripe | PostgreSQL (with `UNIQUE` constraints) | Redis | Money + idempotency truth → PG. Rate limit, fast idempotency lookup → Redis |

**The pattern is universal**: critical truth-of-record in an ACID DB; everything else in a fast cache that explicitly trades consistency for availability. **Baldur's role is the second layer.** It is not a replacement for the first.

Documented incidents at the largest scale: AWS S3 (2017 partial data loss in `us-east-1`), Google Cloud (Korea region permanent deletion 2024), Meta (6-hour BGP outage 2021), Cloudflare (multiple DNS outages). The CAP/FLP price is paid even at trillion-dollar engineering investment levels — the price tag just buys lower frequency, not zero.

---

## PRO Operator Mitigation Checklist

Before going to production with Baldur at PRO tier, verify each item.

### Infrastructure (highest ROI — collapses DEGRADED frequency)

- [ ] Redis is run in **HA mode** — at minimum Sentinel, ideally Cluster or managed ElastiCache HA. **A single Redis instance in production is a hard fail.**
- [ ] Redis HA failover time has been measured under load (target: < 30s). Run a kill-test in staging.
- [ ] Multiple AZs / availability zones for Redis replicas (when applicable).
- [ ] `BALDUR_RESILIENT_STORAGE_RECOVERY_PROBE_INTERVAL` left at default 5s, or shortened, so workers leave DEGRADED quickly after Redis recovery.

### Data classification (the most important architectural step)

- [ ] All money-equivalent data (payments, balances, orders, quotas, billing meters) lives in an ACID database, not in Baldur.
- [ ] Idempotency keys for critical paths use a database `UNIQUE` constraint as truth-of-record. Baldur idempotency cache is the fast path only.
- [ ] Counters that need SUM consistency (anything where 5% drift would cause a financial / SLA dispute) live in a database, not in Baldur's `incr`.
- [ ] Generic `set/get` data put into Baldur is tolerant of cross-worker eventual consistency. If a UX flow relies on read-your-writes across workers, sticky sessions are configured at the LB, OR the data is read from the database not Baldur.

### Observability (so DEGRADED periods are not invisible)

- [ ] Alert on `resilient_storage.degraded_mode_entered` and `resilient_storage.degraded_mode_fallback` (CRITICAL level). Page on-call.
- [ ] Alert on `resilient_storage.recovery_failed` repeats (WARNING). Page if N consecutive within 5 min.
- [ ] Dashboard: % time in DEGRADED per worker, recovery attempts/min, WAL backlog size, recovery duration P99.
- [ ] Operations runbook for "Redis is flapping" reaches the on-call within 5 min.

### Backup / recovery

- [ ] WAL directory `wal_dir` is mounted on durable disk (not tmpfs / ephemeral container disk). Production deployments fail-fast at startup if WAL init fails (`bootstrap.py:803-809`).
- [ ] WAL volume has > 7 days of free space at peak ingest rate (`max_files × max_file_size_mb` × safety factor).
- [ ] Database backups exist for the truth-of-record tier. Baldur cache is reconstructible — the database is not.

---

## Incident Response — When DEGRADED Mode Triggers in Production

### 1. Confirm the trigger source

- Check Redis health from outside the application — `redis-cli PING` from a bastion. If Redis is genuinely down, this is an infra incident; jump to step 3.
- If Redis is up, the issue may be network reachability from the application pods. Check the pod's egress / DNS / SecurityGroup state.

### 2. Estimate blast radius

- How many workers are in DEGRADED? Query the metric or grep recent logs for `resilient_storage.degraded_mode_fallback`.
- What data classes are being written into the workers right now? If money / idempotency truth flows through Baldur (it shouldn't — see checklist above), elevate to SEV-1 immediately.

### 3. Restore Redis

- For managed Redis (ElastiCache, Memorystore): trigger or wait for failover. Restoration usually < 30s.
- For self-managed Redis Sentinel: confirm Sentinel sees the failure and is electing a new master. If quorum is broken, manual master promotion may be needed.
- For self-managed without HA: this is the moment to add HA. Restoration of a single instance can take minutes.

### 4. Verify recovery

- After Redis is reachable, workers should leave DEGRADED within `recovery_probe_interval + jitter` seconds (default ~5–10s, see issue 470 for the dispatch logic).
- Check `resilient_storage.recovery_succeeded` events in logs.
- Check that WAL backlog has drained — `_recovered_entries` metric returning to zero.

### 5. Audit the DEGRADED window for data divergence

For windows longer than ~30 seconds at PRO 5K+ RPS:

- Pull `incr` calls that occurred during DEGRADED — if any of them are billing-relevant, reconcile against the database truth-of-record.
- Pull idempotency-key writes during DEGRADED — if any client retried during the window, verify business-level uniqueness via the database constraint, not Baldur's cache.
- Pull `set` calls where two workers wrote the same key during DEGRADED. The losing-worker's value is gone; if that matters, manually reconcile.

The audit query target depends on your application; the pattern is "trace what was written through Baldur during the DEGRADED window and verify the truth-of-record was unaffected."

### 6. Post-incident

- File a postmortem if the window exceeded the SLO error budget.
- If counter undercount or double-action was observed, revisit data classification — that data did not belong in Baldur.
- If Redis flapping was the trigger, investigate Redis health (memory pressure, connection limits, eviction policy) before declaring resolved.

---

## Cross-references

- `src/baldur/adapters/resilient/backend.py` — `ResilientStorageBackend` source; wires the automatic DEGRADED → REDIS recovery loop.
- `src/baldur/audit/wal/` — Write-Ahead Log providing degraded-mode durability.
- CLAUDE.md § Scale Baseline by Tier — what PRO tier specifically means.
- Brewer, Eric. *CAP Twelve Years Later: How the "Rules" Have Changed*. IEEE Computer, 2012. (background on the AP/CP choice)
- Fischer, Lynch, Paterson. *Impossibility of Distributed Consensus with One Faulty Process*. JACM, 1985. (the FLP impossibility result)
