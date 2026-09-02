# Gunicorn Graceful Shutdown Runbook

> **Purpose**: Wire baldur's `GracefulShutdownCoordinator` into a gunicorn deployment so SIGTERM triggers a real drain — registered shutdown handlers fire, in-flight HTTP requests complete, and the load balancer evicts the worker socket before the process exits.
> **Audience**: Operator deploying baldur under gunicorn (the canonical OSS WSGI server) at OSS or PRO tier.
> **Cadence**: One-time read at deployment + revisit when changing `--graceful-timeout`, `--timeout`, or any recovery-shutdown setting in the reference table below.

---

## TL;DR

baldur's framework-agnostic shutdown chain (`baldur.init()` → `GracefulShutdownCoordinator` → 13 shutdown handlers) needs a small wire-up at gunicorn boot to receive SIGTERM properly. Without that wire-up, baldur runs as if shutdown never happened: the WAL never flushes, leader leases never release, the bulkhead never drains its queue, and the LB keeps routing traffic to a worker that is about to die. **Pick one of the two wiring patterns below.** If you skip both, baldur emits a `baldur.gunicorn_hooks_not_installed` WARNING ~2 seconds after startup — that warning is the documented troubleshooting entry point.

---

## Two Wiring Patterns

### Pattern A — `gunicorn -c <hooks-path>`

Point gunicorn at baldur's shipped hooks module:

```bash
gunicorn -c $(python -c "import baldur.adapters.gunicorn.hooks as h; print(h.__file__)") myapp.wsgi:application
```

Or in a Dockerfile / k8s Deployment manifest:

```dockerfile
CMD ["gunicorn", "-c", "/usr/local/lib/python3.12/site-packages/baldur/adapters/gunicorn/hooks.py", "myapp.wsgi:application"]
```

This is the simplest pattern — no user-side `gunicorn.conf.py` is needed.

### Pattern B — Re-export in your `gunicorn.conf.py`

If you already maintain a `gunicorn.conf.py` for other settings (worker count, timeouts, log format), re-export baldur's hooks into it:

```python
# gunicorn.conf.py
from baldur.adapters.gunicorn.hooks import (
    post_worker_init,
    worker_int,
    worker_exit,
)

# your other settings
workers = 4
timeout = 30
graceful_timeout = 35
```

**Both patterns wire the hooks correctly. Only Pattern B is visible to baldur's wiring check.** Pattern B imports `baldur.adapters.gunicorn.hooks` under its own name at gunicorn's config-parse time, which is the signal the check looks for. Pattern A does not: gunicorn loads any `-c` file under the module name `__config__`, and the hooks module imports nothing from baldur at module level, so the dotted name never enters `sys.modules`. gunicorn still picks the three callables out of that module and the drain works exactly the same — but the check reports `baldur.gunicorn_hooks_not_installed` and the confirming INFO line never appears. If you want the check to agree with your deployment, use Pattern B.

### Migrating off `baldur.server`

`baldur.server` was a second hook surface carrying `post_fork_reset`, `post_worker_init_start` and `worker_exit_cleanup`. It is removed; `baldur.adapters.gunicorn` is the only hook surface baldur ships. If your `gunicorn.conf.py` looks like the left column, replace it with the right:

```python
# BEFORE — baldur.server (removed)
def post_fork(server, worker):
    from django.db import connections
    for conn in connections.all():
        conn.close()
    from baldur.server import post_fork_reset
    post_fork_reset(worker)

def post_worker_init(worker):
    from baldur.server import post_worker_init_start
    post_worker_init_start(worker)

def worker_exit(server, worker):
    from baldur.server import worker_exit_cleanup
    worker_exit_cleanup(worker)
```

```python
# AFTER — baldur.adapters.gunicorn
from baldur.adapters.gunicorn.hooks import (
    post_worker_init,
    worker_int,
    worker_exit,
)


def post_fork(server, worker):
    # Django's own responsibility — never a baldur hook member.
    from django.db import connections
    for conn in connections.all():
        conn.close()
```

Two things to get right:

- **Import at module level, not inside a hook body.** Under `--preload` the master parses the config and never runs `post_worker_init`; a function-body import leaves `baldur.adapters.gunicorn.hooks` out of the master's `sys.modules`, so the check below false-positives on a correctly wired deployment.
- **`worker_int` is new to you.** The old surface had no SIGINT/SIGQUIT member; re-export it or those signals bypass the drain.

The resets the old `post_fork_reset` performed are not carried over as a fourth hook. Three of the five were ineffective (the OpenTelemetry provider is set-once, the mmap snapshot premise matched no shipped code path, and gunicorn already reseeds the RNG in every worker before either hook runs); the Redis one is unnecessary because redis-py resets its connection pool on a pid change by itself. What remains rides `post_worker_init`.

---

## What the Hooks Do

| Hook | Trigger | Responsibility |
|------|---------|----------------|
| `post_worker_init` | After fork, when worker is ready | Marks `GUNICORN_WORKER=1`, populates `coordinator._tracker`, installs a *chained* SIGTERM handler that fires `coordinator.initiate_shutdown` then delegates to gunicorn's `handle_exit`, and **re-starts the `init()`-started background daemon workers for all adapters** (see below) |
| `worker_int` | SIGINT/SIGQUIT forwarded to worker | Calls `coordinator.initiate_shutdown()` for parity with the chained SIGTERM handler |
| `worker_exit` | Worker about to terminate (and, for an already-dead worker, in the master) | Returns immediately unless it is running in the worker it was handed; otherwise waits for the coordinator drain thread (`BALDUR_RECOVERY_SHUTDOWN_DEFAULT_DRAIN_TIMEOUT_SECONDS`), resets the Django background-thread start guards, flushes and closes the audit system — on every exit, including a `max_requests` recycle — and emits `shutdown.worker_exit_completed` |

Why chained SIGTERM and not `worker_int`? Because gunicorn's `worker_int` only fires for SIGINT/SIGQUIT. The normal graceful-shutdown path is **SIGTERM forwarded from master to worker**, which runs gunicorn's `handle_exit` directly without invoking any user hook. Chaining is the only way to plug `coordinator.initiate_shutdown` into the worker's SIGTERM lifecycle without breaking gunicorn's own drain.

### Per-Worker Background-Worker Restart (all adapters)

Background daemon workers started by `baldur.init()` — the meta-watchdog (detect + escalate), the precomputed-cache proactive-refresh worker, the system-metrics (CPU/memory) cache, and the dormant capacity-reservation / cell-topology services — run on `threading` daemons. **Threads do not survive `fork()`**, and `init()` is not re-run inside forked workers, so each worker started in the master is dead in the children. Every one of these starters skips a fork source (`is_fork_source_process()` — under gunicorn that is the master; the same composed predicate also covers a Celery worker main process on a forking pool), so they do not even start in the master.

`post_worker_init` closes this gap **for every framework adapter, not just Django**: after it sets `GUNICORN_WORKER=1` (which flips `is_fork_source_process()` to `False`), it calls `baldur.bootstrap.start_background_workers()`, re-starting every *enabled* worker once per forked worker. Django deployments additionally get their Django-only extra threads (Prometheus gauge hydration, correlation-engine loop, PRO scaling threads) re-started on top. **Consequence:** if you run Flask / FastAPI / plain-Python under gunicorn, wiring these hooks is what makes the proactive loops run — without the hooks, the workers stay off (and the `baldur.gunicorn_hooks_not_installed` WARNING fires; see below). This is the same one-time hook wiring already required for graceful shutdown.

---

## In-Flight HTTP Drain Semantics

`RequestTrackingMiddleware` (auto-injected via `configure_baldur()`) wraps every request in `RequestLifecycleContext`, which calls `coordinator._tracker.start_request()` on entry and `end_request(success=...)` on exit. The drain loop (`shutdown_coordinator._drain_and_shutdown`) reads `tracker.get_pending_count()` each cycle and only declares HTTP drained when count reaches 0.

This means the drain loop **actually waits for in-flight HTTP work** instead of declaring itself done immediately. A 25s POST during shutdown completes naturally — gunicorn's `worker_exit` blocks on `coordinator.wait_for_shutdown()` for `BALDUR_RECOVERY_SHUTDOWN_DEFAULT_DRAIN_TIMEOUT_SECONDS` (30.0 by default) until the drain loop finishes, the LB has already stopped routing new traffic (see "Retry-After Semantics" below), and the request returns its real response.

If `BALDUR_REQUEST_TRACKING_MIDDLEWARE_ENABLED=False` (operator opt-out), the drain loop sees `pending_count=0` every cycle and exits as soon as registered handlers report drained — exactly the pre-471 behavior, plus the LB-eviction contract.

---

## Retry-After Semantics

Once `coordinator.initiate_shutdown()` fires, the phase moves to `DRAINING` and `DrainAwareMiddleware` starts returning 503 to new requests:

```
HTTP/1.1 503 Service Unavailable
Retry-After: 27
Connection: close
Content-Type: text/plain; charset=utf-8

Service draining for shutdown.
```

The `Retry-After` value is `coordinator.get_stats().remaining_drain_time` — i.e., how long the drain loop will still wait. Clients see a meaningful retry hint that aligns with real worker availability. For the rare case where `remaining_drain_time` is `None` (TERMINATING / TERMINATED phase racing with the middleware), the fallback is `BALDUR_RECOVERY_SHUTDOWN_DRAIN_DEFAULT_RETRY_AFTER_SECONDS` (default 5s).

**Why `Connection: close`?** L7 load balancers (envoy, nginx, GCLB, ALB) keep HTTP/1.1 keep-alive connections to the same worker socket even after a 503 — RFC 7230 §6.6 treats 503 as retryable-but-keep-alive by default. The `Connection: close` header is the standard signal that forces the LB to evict the socket and route subsequent requests to other workers. Without it, the LB would keep dispatching to the draining worker until the keep-alive timeout — well past the drain window.

### Liveness exemption

`/api/baldur/health/live/` and `/api/baldur/health/ping/` (baldur-canonical) plus any path listed in `BALDUR_RECOVERY_SHUTDOWN_DRAIN_LIVENESS_PATHS` (operator override) **stay 200 during drain**. Drain is a normal lifecycle phase, not a liveness failure. If liveness probes flipped to 503, k8s would SIGKILL the pod mid-drain — the opposite of what graceful shutdown is supposed to achieve.

Use the override when your k8s `livenessProbe` targets a non-baldur path:

```yaml
env:
  - name: BALDUR_RECOVERY_SHUTDOWN_DRAIN_LIVENESS_PATHS
    value: '["/livez", "/healthz/live"]'
```

### Health-bridge readiness

`/api/baldur/health/l3/` and `/api/baldur/health/bridge/` (the DB-independent readiness endpoints) flip to 503 during DRAINING with a `status: draining` payload:

```json
{
  "status": "draining",
  "shutdown": {
    "phase": "draining",
    "retry_after_seconds": 27,
    "in_flight_count": 3
  },
  ...
}
```

This makes k8s `readinessProbe` flip the pod's endpoint slice to NotReady, which deregisters it from the Service's load balancer — new connections stop arriving immediately, and only the in-flight requests counted in `in_flight_count` need to drain before the worker exits cleanly.

---

## Pre-Flight Check: gunicorn's two timeouts vs `BALDUR_RECOVERY_SHUTDOWN_DEFAULT_DRAIN_TIMEOUT_SECONDS`

The hard rule: **gunicorn's `--graceful-timeout` must be `>= BALDUR_RECOVERY_SHUTDOWN_DEFAULT_DRAIN_TIMEOUT_SECONDS + buffer`**, where the buffer covers handler `on_force_shutdown` time. Concrete example:

| Setting | Value |
|---------|-------|
| `BALDUR_RECOVERY_SHUTDOWN_DEFAULT_DRAIN_TIMEOUT_SECONDS` | 30.0 (baldur default) |
| Buffer for handler force-shutdown | 5.0 (`BALDUR_DLQ_OUTBOX_JOIN_TIMEOUT_SECONDS`, the largest single consumer) |
| `gunicorn --graceful-timeout` | **35** (or higher) |

If gunicorn's timeout is shorter, the master sends SIGKILL while the drain thread is still running. The WAL flush gets cut off, leader leases stay stuck, and in-flight POST bodies are lost — the symptoms graceful shutdown was supposed to prevent.

**What fills the buffer.** The largest named consumer is the DLQ outbox teardown: it flushes the buffered DLQ entries through the real store, joins the writer thread, and spills whatever is left to the local fallback tier. Its whole budget is `BALDUR_DLQ_OUTBOX_JOIN_TIMEOUT_SECONDS` (default 5.0), which is where the 5.0 above comes from. Raise the buffer by the same amount if you raise that setting.

**Also check `--timeout`, not only `--graceful-timeout`.** They govern different exits. `--graceful-timeout` bounds the shutdown path; `--timeout` (default 30) is the arbiter's worker watchdog and is what bounds `worker_exit` on the **recycle** path (`max_requests`, `--reload`), where no shutdown was ever initiated. `worker_exit` has two unconditional steps on that path — it tears down the DLQ outbox and then flushes and closes the audit system — so either a slow DLQ fallback destination or a slow audit destination can hold the hook past the watchdog and turn a routine recycle into a `WORKER TIMEOUT` (CRITICAL) plus a SIGABRT. The outbox teardown's contribution is bounded by `BALDUR_DLQ_OUTBOX_JOIN_TIMEOUT_SECONDS` plus a one-second floor for the spill, so about 6 s at defaults — **plus one un-preemptable write**: the spill checks its deadline between entries, and a single `fsync` on a stalled mount blocks past it with nothing able to interrupt it. A worker heartbeats at least every `timeout/2`, so it enters the hook with 15-30 s of watchdog budget left at the default. If your audit destination or your DLQ local-fallback destination is remote or slow, raise `--timeout` rather than shrinking the teardown budget — a smaller budget does not make the write faster, it just discards more entries at the deadline (each discarded batch is reported at CRITICAL as `dlq_outbox.shutdown_dump_incomplete`). Setting `--timeout 0` disables the watchdog entirely — legitimate with `gthread`/`gevent`, but then nothing bounds the hook from outside.

To inspect gunicorn's effective config:

```bash
gunicorn --print-config -c gunicorn.conf.py myapp.wsgi:application
```

baldur intentionally does **not** add a runtime drift-detection warning for this. Gunicorn already prints its config to stderr at boot, and a baldur-side check would just duplicate that signal.

---

## Troubleshooting the hook-wiring check

~2 seconds after `baldur.init()`, baldur reports which way the check went — exactly one of these two lines, once per process:

```
baldur.gunicorn_hooks_installed     [info]     the hooks are wired; SIGTERM reaches the coordinator
baldur.gunicorn_hooks_not_installed [warning]  running under gunicorn with no hooks imported
```

Look for the INFO line after a wiring change: it is the positive confirmation. An *absent* WARNING is not the same evidence — a check that never ran, or a gunicorn that was never detected, also produces no WARNING.

**On Pattern A, neither line means what it says.** The check reads `sys.modules`, and Pattern A never puts the hooks module there (see the note under the two patterns), so a correctly wired Pattern A deployment gets the WARNING and never the INFO. Confirm Pattern A wiring by its behavior instead — `shutdown.worker_drained` (or, on a recycle, `shutdown.worker_exit_completed`) in the worker's log at exit.

### `baldur.gunicorn_hooks_not_installed`

If you see this WARNING in your logs ~2 seconds after `baldur.init()`:

```
baldur.gunicorn_hooks_not_installed
hint=Running under gunicorn but baldur.adapters.gunicorn.hooks was not imported.
     Wire via 'gunicorn -c <path-to-hooks-module>' or re-export the hooks in
     your gunicorn.conf.py. See docs/runbooks/gunicorn-graceful-shutdown.md.
```

**What it means**: the SERVER_SOFTWARE env var indicates you are running under gunicorn, but `sys.modules['baldur.adapters.gunicorn.hooks']` is missing. baldur's signal-handler registration self-skipped (correctly, to avoid clobbering gunicorn's own SIGTERM handler), but no replacement was wired in. **Result**: SIGTERM bypasses baldur entirely. No registered handler fires.

**Fix**: pick one of the two wiring patterns above and redeploy. If you are already on **Pattern A**, this WARNING is a known false positive — the hooks are wired and the drain works; switch to Pattern B if you want the check to say so.

**Tunable**: `BALDUR_RECOVERY_SHUTDOWN_HOOKS_CHECK_DELAY_SECONDS` (default 2.0, range 0.5–30.0). If `post_worker_init` runs late on your platform and the WARNING is a false positive, raise the delay.

**Suppress**: do not. The WARNING is intentionally fail-open — you keep serving traffic — but the underlying drain is broken. Suppressing the WARNING does not fix the drain.

---

## Access-Log Middleware Ordering

If you use an external access-log middleware and want drain-503 responses to appear in that log, the access-log middleware must be placed **before** baldur's early group (= further out in the middleware stack). baldur's own `AuditMiddleware` (`DEFAULT_TAIL_GROUP`, innermost) sits **after** `DrainAwareMiddleware` and therefore does **not** see the drain-503 short-circuit response.

This is intentional. Drain-503 is a process-lifecycle event, logged by `DrainAwareMiddleware` itself via `structlog` (`drain_aware_middleware.request_rejected`). It is not a chain-integrity audit event.

---

## Settings Reference

| Setting | Default | Range | Purpose |
|---------|---------|-------|---------|
| `BALDUR_RECOVERY_SHUTDOWN_DEFAULT_DRAIN_TIMEOUT_SECONDS` | 30.0 | 5–300 | Drain loop deadline |
| `BALDUR_RECOVERY_SHUTDOWN_DRAIN_DEFAULT_RETRY_AFTER_SECONDS` | 5.0 | 1–300 | Retry-After fallback when phase != DRAINING |
| `BALDUR_RECOVERY_SHUTDOWN_DRAIN_LIVENESS_PATHS` | `[]` | `list[str]` | Extra liveness paths exempted from drain-503 |
| `BALDUR_RECOVERY_SHUTDOWN_HOOKS_CHECK_DELAY_SECONDS` | 2.0 | 0.5–30.0 | Delay before `gunicorn_hooks_not_installed` check |
| `BALDUR_DRAIN_AWARE_MIDDLEWARE_ENABLED` | True | bool | Toggle (Django settings only, not env) |
| `BALDUR_REQUEST_TRACKING_MIDDLEWARE_ENABLED` | True | bool | Toggle (Django settings only, not env) |
