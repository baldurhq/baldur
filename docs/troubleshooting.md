# Troubleshooting & FAQ

Answers to the issues new users and operators hit most often in the first weeks of
running Baldur. Each entry is a **bold symptom line** you can `Ctrl-F` for, followed by
the cause and the fix. Capabilities that need the PRO package are marked `(PRO)`; where a
keyword reads differently on OSS, the entry says so plainly rather than implying a
guarantee OSS does not provide.

!!! danger "Before you adopt — what to store *in* Baldur"

    Baldur is an **AP system** (Available + Partition-tolerant). When Redis is briefly
    unreachable, every worker keeps serving from its own local memory plus a
    write-ahead log — but it does **not** preserve cross-worker consistency during that
    window. So the data you put behind Baldur must tolerate brief, narrow divergence
    across workers. Decide this *before* you wire money-equivalent data through it: a
    user who only discovers the rule under the multi-worker symptom below has already
    lost data.

    | Keep the truth-of-record in an **ACID database** | Safe to keep in **Baldur** |
    |--------------------------------------------------|----------------------------|
    | Money, billing meters, account balances, order status | Rate-limit counters, circuit-breaker state |
    | Idempotency truth-of-record for money-equivalent paths | Dead-letter buffers, hot caches |
    | Anything where a 5% drift would start a finance/SLA dispute | Ephemeral session state, observability counters |

    Baldur may sit *in front* of the database as a fast cache, but the database write
    must succeed before you acknowledge the user. Rule of thumb: **if a 5% drift after a
    Redis blip would make Finance care, it belongs in a database, not in Baldur.**

---

## Symptoms

### Installation & startup

**`ModuleNotFoundError` for `django` / `fastapi` / `flask` / `redis` / `celery` / `prometheus`.**
*Cause:* the matching optional dependency (an "extra") was not installed. The Baldur core
ships framework-agnostic; integrations are opt-in extras.
*Fix:* install the extra you need (quote the brackets in `zsh` / `fish`):

```bash
pip install "baldur-framework[django]"      # or [fastapi], [flask], [redis], [celery], [prometheus]
pip install "baldur-framework[django,redis]"  # combine extras with commas
```

**A PRO feature does nothing — no error, no effect. `(PRO)`**
*Cause:* the `baldur-pro` package is not installed, or `BALDUR_LICENSE_KEY` /
`BALDUR_LICENSE_FILE` is unset or invalid. Entitlement is **fail-open and silent** by
design: an OSS install never breaks because a PRO knob was left set — the capability is
simply inert.
*Fix:* install the PRO package and supply the license:

```bash
pip install baldur-pro
export BALDUR_LICENSE_KEY=<your-key>     # or BALDUR_LICENSE_FILE=/etc/baldur/license
```

The Web Console panel for a PRO feature appears only once its backing service is
actually registered and running — so a present panel is the real "PRO is active" signal,
not a license file's claim.

**Cache / storage / audit / scheduler defaults behave as if unconfigured.**
*Cause:* the framework adapter started without running Baldur's centralized wiring. All
the cross-cutting defaults (cache + storage backend, audit pipeline, scheduler, the
built-in admin server) are set up by `baldur.init()`, called from each adapter's startup
path.
*Fix:* make sure the adapter is wired in — its startup path calls `baldur.init()` for
you. On Django, add `baldur.adapters.django` to `INSTALLED_APPS` (its app config calls
`baldur.init()` on startup); Flask wires it through the Baldur bootstrap, FastAPI through
the lifespan handler. If you run plain Python with no adapter, call `baldur.init()`
yourself at startup.

### Circuit breaker

**The circuit breaker won't open even though the dependency is clearly failing.**
*Cause:* the breaker requires a **minimum number of calls** in its window before it can
trip, so a barely-used endpoint can't flip on a single early failure. On a low-traffic
service you simply haven't crossed that floor yet.
*Fix:* this is working as designed — it prevents one failure on a quiet endpoint from
opening the breaker on noise. If you genuinely need it to react sooner, raise the
service's traffic in your reproduction, or take the dependency out of rotation manually
with `force_open_circuit`. `BALDUR_CB_FAILURE_THRESHOLD` (default `5`) controls how many
failures in the window trip it once the minimum-calls floor is met.

**The circuit breaker opens almost immediately under load.**
*Cause:* a burst of HTTP `429` (Too Many Requests) from the dependency is treated as a
failure signal and can trip the breaker fast — that is deliberate, so your app stops
amplifying an overload. An operator `force_open_circuit` does the same on purpose.
*Fix:* expected behavior during a rate-limit storm. Let the breaker shed load while the
dependency recovers; it will probe and close on its own. If the `429`s are your own
client over-calling, fix the call rate rather than the breaker.

**The circuit breaker won't close again after the dependency recovers.**
*Cause:* after it opens, the breaker stays OPEN for `BALDUR_CB_RECOVERY_TIMEOUT` (default
`60` seconds) before it lets any trial calls through, then admits only
`BALDUR_CB_HALF_OPEN_MAX_CALLS` (default `3`) probes. If a single probe fails it snaps
straight back to OPEN and the timer restarts.
*Fix:* wait out the recovery timeout and confirm the dependency truly succeeds on the
trial calls. If you know it has recovered, `force_close_circuit` returns it to normal
immediately; hand control back to automatic mode afterward.

**The same breaker behaves differently on each worker.** *(light — expected under a Redis blip)*
*Cause:* when workers lose their shared Redis (DEGRADED mode), each worker's breaker sees
only its own request stream, so one worker can be OPEN while another keeps sending
traffic. On recovery, Baldur reconciles the breaker keys with a **Most-Restrictive-Wins**
merge, so the post-recovery state is the strictest of all workers' views.
*Fix:* expected during a DEGRADED window; per-worker breakers still protect each worker's
own traffic, and cluster-wide agreement returns when Redis does. If divergence is
frequent, your Redis is flapping — see *Redis connection refused* below.

### Retry, timeout & fallback

**Retry, fallback, or DLQ capture isn't happening even though I used `@baldur.protected`.**
*Cause:* a bare `@baldur.protected("name")` gives you the **circuit breaker only**. Every
other pattern is opt-in, so Baldur never turns on machinery you didn't ask for.
*Fix:* opt the pieces in by keyword:

```python
@baldur.protected("charge-customer", retry=True, fallback=give_up, dlq=True, timeout=10)
def charge(order_id: str) -> dict:
    ...
```

| Pattern | On by default? | How to enable |
|---------|----------------|---------------|
| Circuit breaker | **Yes** | `circuit_breaker=False` to turn off |
| Retry with backoff | No | `retry=True` |
| Fallback | No | `fallback=<callable>` |
| Timeout (wall-clock) | No | `timeout=<seconds>` |
| Idempotency (dedup) | No | `idempotency_key=...` |
| Dead-letter queue `(PRO)` | No | `dlq=True` |

On `async def` callsites, circuit breaker and retry are not available — request them on an
async call and Baldur raises a clear error rather than pretending to protect it. Use
`@baldur.aprotected` for async-only callsites.

**A `protect()` / `@protected` call hangs and never returns.**
*Cause:* `protect()` adds **no default wall-clock timeout**. If the I/O client inside the
protected call has no timeout of its own, the call can block the worker indefinitely.
*Fix:* three resolution paths, in order of preference:

1. **Set the timeout at the I/O client** (the right layer — it surfaces a domain
   exception callers can branch on).
2. **Pass `timeout=N` per call** — `baldur.protect("name", fn, timeout=30)` — re-introduces
   a Baldur-level wall-clock bound for that one call.
3. **Restore a framework-wide default** — `BALDUR_PROTECT_DEFAULT_TIMEOUT_SECONDS=30` —
   recovers a global outer net (temporary mitigation; adds roughly 30–50 µs per call back).

The default I/O-client timeouts that most often cause this:

| Client | Default timeout | How to set one |
|--------|-----------------|----------------|
| `requests` | **none** — blocks forever (the most common offender) | `requests.get(url, timeout=(5, 30))` |
| `httpx` | 5 s if `timeout=` omitted; **none** if you pass `timeout=None` | `httpx.Client(timeout=httpx.Timeout(connect=5, read=30))` |
| `aiohttp` | 5 min total | `aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))` |
| `psycopg` | none at the client (server `statement_timeout` controls) | `psycopg.connect(..., options="-c statement_timeout=30000")` |
| `redis-py` | none at socket level | `redis.Redis(socket_timeout=5, socket_connect_timeout=5)` |
| `subprocess.run` | none | `subprocess.run([...], timeout=30)` |

To confirm a zone has no Baldur timeout policy, set `BALDUR_LOG_LEVEL=DEBUG` and look for
a `protect.composer_built` event: `timeout_seconds=None` and no `'timeout'` in its
`policies` list confirms the call has no Baldur-level wall-clock bound.

### Dead-letter queue (DLQ)

**(OSS) Work that failed permanently just disappears — nothing is captured for replay.**
*Cause:* on an OSS install, `dlq=True` has nowhere to write. The OSS path is a **no-op
recorder** — it silently discards the failed operation instead of capturing it. The
durable dead-letter store that actually parks failed work for replay ships in the PRO
package.
*Fix:* this is a tier boundary, not a bug. On OSS, an exhausted call still surfaces the
error to the caller (and runs your `fallback` if you set one) — the operation simply
isn't set aside. To capture and replay failed work, add PRO `(OSS no-op → PRO)`: the
**exact same** `dlq=True` code then records every failed operation durably across
restarts.

**(PRO) The DLQ keeps growing and never drains. `(PRO)`**
*Cause:* either the replay worker isn't running, or new failures are arriving faster than
they drain. The queue protects itself with an overall size cap (`BALDUR_DLQ_MAX_SIZE`,
default `100000`) and a per-domain cap, plus an overflow strategy.
*Fix:* confirm the replay worker is alive, then drain with a targeted or batch replay from
the Web Console DLQ panel or the REST API. The overflow strategy decides what gives when
the cap is hit — `drop_oldest` (the default) evicts the oldest entries, `reject` refuses
new ones (surfaced as a `503`), `compress_oldest` summarizes the oldest before evicting.

**(PRO) DLQ replay isn't running. `(PRO)`**
*Cause:* nothing has triggered a replay. Replay happens three ways: a **targeted** replay
of one entry/domain, a **batch** replay of a domain or failure type, or **automatic on
recovery** — when a dependency's circuit breaker closes again, Baldur sweeps that
dependency's queued failures.
*Fix:* trigger a targeted or batch replay manually, or rely on the automatic
circuit-breaker-close sweep. An entry that exhausts its replay budget converges to a
terminal **needs-review** state (it isn't retried forever or dropped); after you fix the
root cause, an operator can **force-redrive** it to grant a fresh budget. Check that the
replay worker process is live if neither manual nor automatic replay advances.

### Idempotency

**The same request is processed twice.**
*Cause:* the two arrivals didn't resolve to the **same** dedup key. Keys are namespaced by
domain *and* by operation: the facade's field-name form prefixes the protected service's
name, and `@idempotent` includes the decorated function's module-qualified name — so two
different functions, or a renamed/moved function, don't share dedup memory. During a Redis
blip (DEGRADED mode), a key written by one worker is also invisible to its peers until
recovery.
*Fix:* give both entry points the **same explicit `operation=` label** when they really
are one logical operation (an HTTP handler and a worker guarding the same charge), so a
rename can't reset dedup memory. For money-equivalent paths, pair Baldur's dedup with your
payment provider's own idempotency key (derived deterministically from the order ID), and
keep the truth-of-record in your database — Baldur's cross-worker dedup is the fast path,
not the gate (see the adoption callout at the top).

**A duplicate is blocked (or let through) when I didn't expect it.**
*Cause:* a key lives under **two independent clocks**. The *memory window*
(`BALDUR_IDEMPOTENCY_GATE_MEMORY_TTL_SECONDS`, default `1800` s) is how long a completed
operation keeps blocking duplicates; the *execution window* (`execution_ttl=` /
`idempotency_execution_ttl=`, default 30 min) is how long a running claim is honored before
a crashed attempt becomes retryable. The dedup posture is **fail-closed** by default: if
the store can't be reached, the call is blocked with an "unavailable" error rather than
risking a duplicate.
*Fix:* set the execution window to your operation's worst-case runtime (never to the dedup
horizon). If a momentary store blip is blocking calls and availability matters more than
the guarantee for that call, opt it (or the service) into **fail-open**. A duplicate raises
`IdempotencyDuplicateError` — catch it and treat it as "this work already happened"; Baldur
does not replay the original response.

### Alerts & notifications

**Slack / alerts never arrive.**
*Cause:* on OSS, notifications are **not sent to Slack** — the delivery transports ship
with PRO, so `notify()` and its variants are a no-op on an OSS install. With PRO active, a
notification can still be suppressed for a named reason: `disabled` (notifications off),
`rate_limited` (per-minute/per-hour budget spent), `cooldown` (a repeat of the same alert
within its window), or `log_only` (no channel resolved for that priority).
*Fix:* to receive delivered alerts at all, run PRO `(OSS no-op → PRO)`. With PRO, when an
alert is missing, check which suppression reason applies — a `MEDIUM`/`LOW` priority routes
to Slack only, `INFO` is logged unless a channel is configured, and a flood of identical
alerts collapses to one by cooldown (which spans every running instance, not just one
process).

**Too many alerts — the channel is unreadable. `(PRO)`**
*Cause:* the fatigue guards aren't tuned for your volume. Two independent guards apply: a
**rate limit** (per-minute and per-hour budgets) caps total volume, and a **cooldown**
de-duplicates repeats of the *same* alert by key.
*Fix:* these knobs ship with production-safe defaults and are advanced/internal for now
(not in the public env-var allowlist). The defaults already collapse a storm of identical
"circuit breaker still open" messages to one and hold across the whole fleet; if a single
source is noisy, its burst crossing a threshold escalates *its* priority — treat that as the
signal to fix the source.

### Redis & storage

**`ConnectionRefusedError` / Redis is unreachable.**
*Cause:* `BALDUR_REDIS_URL` points nowhere reachable, or Redis is down. With no reachable
Redis, Baldur falls back to a per-process in-memory store and enters DEGRADED mode.
*Fix:* set `BALDUR_REDIS_URL=redis://localhost:6379/0` (or your endpoint) and confirm Redis
is up (`redis-cli PING`). The in-memory fallback is **single-worker only** — it keeps
correctness for one process but diverges silently across workers (idempotency keys,
rate-limit counters, breaker state). Give Baldur a shared Redis before running more than one
worker.

**I set `REDIS_URL` but audit / hash-chain still connect to a different Redis.**
*Cause:* the audit-flush and hash-chain consumers resolve through `BALDUR_REDIS_URL`, not a
bare `REDIS_URL`, so setting only the unprefixed variable never reaches them. This is not an
automatic rename — the env-var migration helper only covers `BALDUR_*`-prefixed keys.
*Fix:* set `BALDUR_REDIS_URL` for the canonical consumers (cache, circuit breaker, DLQ,
audit-flush, resilient storage). A per-feature override (e.g.
`BALDUR_RESILIENT_STORAGE_REDIS_URL`) wins where set; otherwise the consumer falls back to
`BALDUR_REDIS_URL`. One sharp edge: the RQ queue adapter still reads only a bare,
unprefixed `REDIS_URL` and never `BALDUR_REDIS_URL`, so a stray `REDIS_URL` left in the
environment can pull the RQ path to a different Redis than your `BALDUR_REDIS_URL`. Set the
prefixed variable everywhere and clear any leftover bare `REDIS_URL` so the two can't
disagree. (The core Redis client's environment fallback prefers `BALDUR_REDIS_URL` and
honors a bare `REDIS_URL` only when the prefixed variable is unset, so it is no longer
misrouted by a stray bare var.)

**Multi-worker counters drift, idempotency goes invisible, or money looks inconsistent.**
*Cause:* during a DEGRADED window each worker is isolated, so counter increments can
undercount on recovery (the merge uses set-semantics, not additive `incrby`), an
idempotency key written on one worker is invisible to peers until replay, and a generic
`set` can be last-write-wins across workers.
*Fix:* this is the AP trade-off described in **[the adoption callout at the top of this
page](#troubleshooting-faq)** — never store money, billing meters, or idempotency
truth-of-record in Baldur's cache; use an ACID database for those. To shrink the window,
run Redis in HA (Sentinel/Cluster) so DEGRADED periods stay sub-30-seconds, and alert on the
DEGRADED-mode entry events so the periods aren't invisible.

### Health, metrics & shutdown

**`/metrics` returns `503`.**
*Cause:* the Prometheus client library isn't installed. Metric collection rides on an
optional dependency; without it, recording calls become no-ops and the scrape endpoint
returns `503` — your app keeps running exactly as before.
*Fix:*

```bash
pip install "baldur-framework[prometheus]"
```

The circuit-breaker, retry, and cardinality-guard metrics are OSS; dead-letter-queue depth
and replay-success metrics report on the PRO subsystems and only carry data with PRO
installed.

**Health checks return `503` during a deploy / shutdown.**
*Cause:* this is graceful-shutdown draining, working as designed. When the process gets
`SIGTERM`, readiness flips to `503` so new traffic routes elsewhere while in-flight requests
finish, and new requests get a `503` with a `Retry-After` header.
*Fix:* nothing — keep it. Liveness (`health/live/`) and `health/ping/` deliberately stay
`200` through the drain so the orchestrator doesn't hard-kill the pod mid-cleanup. On
Kubernetes, set `terminationGracePeriodSeconds` comfortably above the drain window (30 s by
default) so the platform's hard kill never lands before Baldur's own deadline.

### Audit

**The audit trail isn't recording anything. `(PRO)`**
*Cause:* audit ships **disabled by default** (`BALDUR_AUDIT_ENABLED=false`), and the
subsystem itself is a PRO capability.
*Fix:* run PRO and turn audit on:

```bash
export BALDUR_AUDIT_ENABLED=true
export BALDUR_SQL_DSN=postgresql://user:pass@host:5432/db   # persistent audit storage (optional)
```

Records flush through `BALDUR_REDIS_URL` and persist to your configured backend; the hash
chain makes the trail tamper-evident.

### Admin console & control

**The admin console is blank, or actions return `403`.**
*Cause:* one of several deliberate safety gates. The console binds to **localhost only** out
of the box (`http://localhost:9090/`), so it isn't reachable from another machine without
your own TLS proxy and an admin key. Destructive actions (reset a breaker, purge the queue,
flip the kill switch) are refused with `403` until the server is **explicitly unlocked**,
and the console checks the request origin to block DNS-rebinding.
*Fix:* reach it locally first at `http://localhost:9090/`. To use it from elsewhere, place
it behind a TLS proxy, set the admin key (entered once in the header bar), and add your
host to the allowed origins. For a destructive action, unlock the server with
`BALDUR_ADMIN_UNLOCK=1` and type the `CONFIRM` prompt — the unlock is a second gate so a
console left open in a browser tab can't force production.

**Nothing Baldur does has any effect — all self-healing is inert.**
*Cause:* the **kill switch** is in its DISABLED state. Someone flipped it (an incident, a
test) and it persists across restarts on purpose — Baldur won't silently re-arm automation
while you're still working an incident.
*Fix:* re-enable it from the admin console, the API, or `is_baldur_enabled()`'s matching
enable call. Disabling requires a reason and records who/why/when; re-enabling clears it.
Note that while disabled, even manual circuit-breaker actions are held back unless you
explicitly override them.

### PRO operational controls

**Emergency mode won't stand down / won't recover. `(PRO)`**
*Cause:* recovery is **gated and fail-closed**. Before any level drops, the recovery gate
reads live health metrics (error rate, CPU/load) against safe thresholds and refuses the
exit while either is still high — and if it **can't read the metrics at all, it fails closed**
and keeps emergency mode on rather than guessing.
*Fix:* confirm the metrics the gate reads are actually flowing and back within bounds. Once
satisfied, recovery proceeds one level at a time with a stabilization window between steps; a
mid-recovery re-check failure holds the current level rather than continuing down. An operator
who must exit regardless can **force** the release, deliberately bypassing the gate.

**`BulkheadFullError` — requests are rejected immediately. `(PRO)`**
*Cause:* the call's compartment is at capacity. A bulkhead admits a fixed number of
concurrent executions per domain; when full, calls **fail fast** (rejected on the spot) rather
than piling up. The defaults: `database` 10, `cache` 20, `external_api` 5 workers + 10
waiting, `message_queue` 15.
*Fix:* the error names the compartment and its occupancy (e.g. `12/10 active`) — that's your
diagnosis. Raise the compartment's capacity where it's created (in code), give the call a
**wait timeout** so it waits for a permit instead of failing fast, or set a `fallback` (which
answers only on a compartment-full rejection — your own business exceptions still propagate).
A "compartment not found" error means you decorated a call with a custom compartment that was
never registered; the message lists the registered ones.

**A canary rollout won't start, or seems bricked. `(PRO)`**
*Cause:* the gates in front of every step are **fail-closed**. Start/promote is refused while
the kill switch is engaged, while Emergency Mode is at or above its configured severity, or if
the governance check itself can't run. A rollout can also be blocked from advancing because
the stage's metric evaluation isn't connected, or by the chaos guard (a target cluster running
a chaos experiment). Only one rollout per config type may be active.
*Fix:* read the rollout's status — it reports the block reason. Clear the underlying condition
(disable the kill switch, let Emergency Mode subside, finish the chaos experiment) or, for a
genuine emergency, use the audited gate **bypass** (it demands a written reason and is flagged
for post-incident review). A stalled rollout is auto-rolled-back by the watchdog after its
deadline; `POST /canary/panic-rollback` reverts every active rollout at once.

**Governance blocks everything. `(PRO)`**
*Cause:* the pre-action gate stops at the **first failing check**, in fixed order: kill switch
→ emergency level → error budget. A declared **STRICT** operating mode, an active emergency at
or above the configured level, or a depleted error budget will each block automated actions —
and the block reason tells you which.
*Fix:* read the governance status view to see the active reason. Resolve it (re-enable the
system, let the emergency clear, restore the error budget) — STRICT also self-expires after its
timeout. When an operator genuinely must override, the **Break Glass** bypass lets everything
through, but it's recorded and flagged for a mandatory post-incident review. Note governance is
**fail-open**: a check that can't be evaluated allows the action and raises a FAILSAFE alert,
so "governance blocks everything" is never caused by a broken check — look for a real STRICT /
emergency / budget condition.

---

## Migration

**OSS → OSS + PRO: what changes, and what code do I rewrite?**
Nothing in your application code. Install `baldur-pro` and set `BALDUR_LICENSE_KEY`, and the
same `@baldur.protected` calls light up the PRO capabilities behind them: `dlq=True` becomes a
durable, replayable dead-letter queue; notification delivery, audit, emergency mode, bulkhead,
canary, throttle, governance, and the meta-watchdog all activate. A PRO subscription includes
every PRO feature — they aren't unlocked one at a time.

**The OSS "what you're missing" block vanished after I added PRO.**
*Cause:* on OSS the daily report carries a "what you're missing" insights block that estimates
the impact the PRO features would have had, drawn from your own production numbers. With PRO
active you're no longer missing them, so that block is replaced by an "Automated Actions"
section showing what PRO actually *did*.
*Fix:* expected — this is the report honestly shifting from "here's what you're missing" to
"here's what was handled for you." Nothing to do.

**Django-only → multiple frameworks: wiring didn't apply to the new app.**
*Cause:* each framework adapter must run `baldur.init()` from its own startup path for the
centralized wiring to take effect. Adding a second framework doesn't inherit the first one's
initialization.
*Fix:* wire each adapter independently — `baldur.adapters.django` in `INSTALLED_APPS` for
Django, the Baldur bootstrap for Flask, the lifespan handler for FastAPI, or a direct
`baldur.init()` call for a plain-Python service.

**Coexisting with a service mesh or API gateway.** *(light)*
The mesh secures the network *between* processes; Baldur protects the logic *inside* one. The
one place they collide is **retries** — if the mesh retries 3× and Baldur retries 3× on the
same failure, one call can hit the dependency 9×. The fix is **one signal, one owner**: let the
mesh retry network-level failures (dropped connections, TCP resets) and let Baldur retry
application-level failures (a specific exception, a business error) the mesh can only see as an
opaque `5xx`. The same split applies to circuit breaking. (A configurable layer-mode is not
implemented — this is a wiring principle, not a setting.)

**Single-worker dev → multi-worker production.**
The zero-config in-memory store is **single-process only** — copy it into a multi-worker
deployment and idempotency keys, rate-limit counters, and breaker state diverge silently per
worker, which breaks **correctness**, not just scale. Before running more than one worker, point
Baldur at Redis (`pip install "baldur-framework[...,redis]"`, then
`export BALDUR_REDIS_URL=redis://localhost:6379/0`) — no code changes — and understand the
DEGRADED-window semantics from the adoption callout at the top.

---

## Operational checks

**Reading the health endpoints.**
Baldur exposes five endpoints under the conventional `/api/baldur/` prefix (and on the admin
console for deployments without a web framework):

| Endpoint | Answers | Behavior |
|----------|---------|----------|
| `health/` | Full picture | `200` for healthy/degraded, `503` for unhealthy |
| `health/live/` | Is the process alive? | Always `200` while running, even during drain |
| `health/ready/` | Can it serve traffic? | `200` only when every configured DB connection is usable, else `503` |
| `health/pool/` | Connection-pool state | `200` healthy, `503` degraded/erroring |
| `health/ping/` | Fastest yes | Always `200`, no DB access — for high-frequency probes |

Only **unhealthy** maps to `503` on `health/` and depools the pod — **degraded** stays in
rotation (a background hiccup shouldn't pull healthy capacity). Append `?nocache=true` to
`health/` to bypass the precomputed cache.

**Interpreting the metrics.**
Metrics are served as a Prometheus text-exposition page (byte-exact for scrapers) plus a JSON
view of the control-API metrics. The Cardinality Guard keeps the series count flat: per-ID URL
paths normalize to `/{id}`, unrecognized paths collapse to one `UNMATCHED_ROUTE` series, domain
labels are capped (50 by default, overflow into one shared label), and the distinct-endpoint
count is capped (500 by default, evicted oldest-first). Gauges that should never go negative are
clamped at `0`.

**Adjusting log levels.**
The global level is `BALDUR_LOG_LEVEL`. Four event families have their own runtime override:

```bash
BALDUR_LOG_LEVEL=INFO
BALDUR_EVENT_LOGGING_DLQ_LOG_LEVEL=INFO
BALDUR_EVENT_LOGGING_CB_LOG_LEVEL=WARNING
BALDUR_EVENT_LOGGING_REPLAY_LOG_LEVEL=INFO
BALDUR_EVENT_LOGGING_SLA_LOG_LEVEL=WARNING
```

Event names follow `{component}.{entity}_{action}`, and the level is implied by the suffix:
`_failed` / `_blocked` / `_timeout` / `_exhausted` are at least `WARNING`, `_error` is `ERROR`,
and `_changed` / `_updated` are `INFO`.

**Diagnosing what a `protect()` zone composed.**
Set `BALDUR_LOG_LEVEL=DEBUG` and trigger the call path; the `protect.composer_built` event
reports the composed chain — for example `policies=['circuit_breaker']` means circuit-breaker
only (no timeout, no retry). The line is emitted once per unique zone, so you may need to
restart the worker to see it.

**Reading the daily report.**
The report is generated and stored in every tier; you read it on demand from the CLI (the same
data the report API serves):

```bash
baldur report                     # list recent reports
baldur report --date 2026-06-14   # one day's digest (or --date today)
baldur report --days 7            # list the last N reports
baldur report --json              # machine-readable output
```

A calm day collapses to a single `"All quiet — 0 processed, 0 alerts."` line; severity is
derived from contents (clean = info, task failures = warning, a critical alert = critical). With
PRO active the same report is delivered to Slack each morning and gains an "Automated Actions"
section.

**Reading the dashboard summary.**
One read-only call rolls up the whole self-healing picture:

```
GET /api/baldur/dashboard/summary/
```

The admin server and the `baldur` CLI serve the same data at `/dashboard/summary`. It returns a
single health verdict (`healthy` / `good` / `warning` / `critical`), overview counts with a
resolution rate, recent activity, the noisiest domains and failure types, and retry alerts. The
snapshot is cached for cheap polling and degrades gracefully (zeroed counts for any part it
can't read) rather than erroring. A Viewer role or higher can call it.

**Circuit-breaker manual ops.**
Inspect and drive breakers from the CLI (and the matching admin/Web-Console actions):

```bash
baldur cb list                    # state of every breaker
baldur cb reset <service>         # return a breaker to normal (destructive — needs unlock)
baldur cb force-open <service>    # take a dependency out of rotation
baldur cb force-close <service>   # restore it once you know it recovered
```

Add `--json` to any of these for machine-readable output. Resetting or forcing a breaker is a
destructive action, so it requires the server to be unlocked (`BALDUR_ADMIN_UNLOCK=1`).
