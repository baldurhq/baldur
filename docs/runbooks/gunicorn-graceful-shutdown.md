# Gunicorn Graceful Shutdown Runbook

> **Purpose**: Wire baldur's `GracefulShutdownCoordinator` into a gunicorn deployment so SIGTERM triggers a real drain — registered shutdown handlers fire, in-flight HTTP requests complete, and the load balancer evicts the worker socket before the process exits.
> **Audience**: Operator deploying baldur under gunicorn (the canonical OSS WSGI server) at OSS or PRO tier.
> **Cadence**: One-time read at deployment + revisit when changing `--graceful-timeout` or `BALDUR_SHUTDOWN_*` settings.

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

Both patterns import `baldur.adapters.gunicorn.hooks` at gunicorn's config-parse time, which is the signal baldur looks for when deciding whether to emit the `baldur.gunicorn_hooks_not_installed` WARNING.

---

## What the Hooks Do

| Hook | Trigger | Responsibility |
|------|---------|----------------|
| `post_worker_init` | After fork, when worker is ready | Marks `GUNICORN_WORKER=1`, populates `coordinator._tracker`, installs a *chained* SIGTERM handler that fires `coordinator.initiate_shutdown` then delegates to gunicorn's `handle_exit`, and **re-starts the `init()`-started background daemon workers for all adapters** (see below) |
| `worker_int` | SIGINT/SIGQUIT forwarded to worker | Calls `coordinator.initiate_shutdown()` for parity with the chained SIGTERM handler |
| `worker_exit` | Worker about to terminate | Blocks (up to 30s) waiting for the coordinator drain thread to complete, then stops Django background daemon threads cleanly |

Why chained SIGTERM and not `worker_int`? Because gunicorn's `worker_int` only fires for SIGINT/SIGQUIT. The normal graceful-shutdown path is **SIGTERM forwarded from master to worker**, which runs gunicorn's `handle_exit` directly without invoking any user hook. Chaining is the only way to plug `coordinator.initiate_shutdown` into the worker's SIGTERM lifecycle without breaking gunicorn's own drain.

### Per-Worker Background-Worker Restart (all adapters)

Background daemon workers started by `baldur.init()` — the meta-watchdog (detect + escalate), the precomputed-cache proactive-refresh worker, the system-metrics (CPU/memory) cache, and the dormant capacity-reservation / cell-topology services — run on `threading` daemons. **Threads do not survive `fork()`**, and `init()` is not re-run inside forked workers, so each worker started in the master is dead in the children. Every one of these starters skips the gunicorn master (`is_gunicorn_master()`), so they do not even start in the master.

`post_worker_init` closes this gap **for every framework adapter, not just Django**: after it sets `GUNICORN_WORKER=1` (which flips `is_gunicorn_master()` to `False`), it calls `baldur.bootstrap.start_background_workers()`, re-starting every *enabled* worker once per forked worker. Django deployments additionally get their Django-only extra threads (Prometheus gauge hydration, correlation-engine loop, PRO scaling threads) re-started on top. **Consequence:** if you run Flask / FastAPI / plain-Python under gunicorn, wiring these hooks is what makes the proactive loops run — without the hooks, the workers stay off (and the `baldur.gunicorn_hooks_not_installed` WARNING fires; see below). This is the same one-time hook wiring already required for graceful shutdown.

---

## In-Flight HTTP Drain Semantics

`RequestTrackingMiddleware` (auto-injected via `configure_baldur()`) wraps every request in `RequestLifecycleContext`, which calls `coordinator._tracker.start_request()` on entry and `end_request(success=...)` on exit. The drain loop (`shutdown_coordinator._drain_and_shutdown`) reads `tracker.get_pending_count()` each cycle and only declares HTTP drained when count reaches 0.

This means the drain loop **actually waits for in-flight HTTP work** instead of declaring itself done immediately. A 25s POST during shutdown completes naturally — gunicorn's `worker_exit` blocks on `coordinator.wait_for_shutdown(timeout=30.0)` until the drain loop finishes, the LB has already stopped routing new traffic (see "Retry-After Semantics" below), and the request returns its real response.

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

The `Retry-After` value is `coordinator.get_stats().remaining_drain_time` — i.e., how long the drain loop will still wait. Clients see a meaningful retry hint that aligns with real worker availability. For the rare case where `remaining_drain_time` is `None` (TERMINATING / TERMINATED phase racing with the middleware), the fallback is `BALDUR_SHUTDOWN_DRAIN_DEFAULT_RETRY_AFTER_SECONDS` (default 5s).

**Why `Connection: close`?** L7 load balancers (envoy, nginx, GCLB, ALB) keep HTTP/1.1 keep-alive connections to the same worker socket even after a 503 — RFC 7230 §6.6 treats 503 as retryable-but-keep-alive by default. The `Connection: close` header is the standard signal that forces the LB to evict the socket and route subsequent requests to other workers. Without it, the LB would keep dispatching to the draining worker until the keep-alive timeout — well past the drain window.

### Liveness exemption

`/api/baldur/health/live/` and `/api/baldur/health/ping/` (baldur-canonical) plus any path listed in `BALDUR_SHUTDOWN_DRAIN_LIVENESS_PATHS` (operator override) **stay 200 during drain**. Drain is a normal lifecycle phase, not a liveness failure. If liveness probes flipped to 503, k8s would SIGKILL the pod mid-drain — the opposite of what graceful shutdown is supposed to achieve.

Use the override when your k8s `livenessProbe` targets a non-baldur path:

```yaml
env:
  - name: BALDUR_SHUTDOWN_DRAIN_LIVENESS_PATHS
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

## Pre-Flight Check: `--graceful-timeout` vs `BALDUR_SHUTDOWN_DEFAULT_DRAIN_TIMEOUT_SECONDS`

The hard rule: **gunicorn's `--graceful-timeout` must be `>= BALDUR_SHUTDOWN_DEFAULT_DRAIN_TIMEOUT_SECONDS + buffer`**, where the buffer covers handler `on_force_shutdown` time. Concrete example:

| Setting | Value |
|---------|-------|
| `BALDUR_SHUTDOWN_DEFAULT_DRAIN_TIMEOUT_SECONDS` | 30.0 (baldur default) |
| Buffer for handler force-shutdown | 5.0 |
| `gunicorn --graceful-timeout` | **35** (or higher) |

If gunicorn's timeout is shorter, the master sends SIGKILL while the drain thread is still running. The WAL flush gets cut off, leader leases stay stuck, and in-flight POST bodies are lost — the symptoms graceful shutdown was supposed to prevent.

To inspect gunicorn's effective config:

```bash
gunicorn --print-config -c gunicorn.conf.py myapp.wsgi:application
```

baldur intentionally does **not** add a runtime drift-detection warning for this. Gunicorn already prints its config to stderr at boot, and a baldur-side check would just duplicate that signal.

---

## Troubleshooting `baldur.gunicorn_hooks_not_installed` WARNING

If you see this WARNING in your logs ~2 seconds after `baldur.init()`:

```
baldur.gunicorn_hooks_not_installed
hint=Running under gunicorn but baldur.adapters.gunicorn.hooks was not imported.
     Wire via 'gunicorn -c <path-to-hooks-module>' or re-export the hooks in
     your gunicorn.conf.py. See docs/runbooks/gunicorn-graceful-shutdown.md.
```

**What it means**: the SERVER_SOFTWARE env var indicates you are running under gunicorn, but `sys.modules['baldur.adapters.gunicorn.hooks']` is missing. baldur's signal-handler registration self-skipped (correctly, to avoid clobbering gunicorn's own SIGTERM handler), but no replacement was wired in. **Result**: SIGTERM bypasses baldur entirely. No registered handler fires.

**Fix**: pick one of the two wiring patterns above and redeploy.

**Tunable**: `BALDUR_SHUTDOWN_HOOKS_CHECK_DELAY_SECONDS` (default 2.0, range 0.5–30.0). If `post_worker_init` runs late on your platform and the WARNING is a false positive, raise the delay.

**Suppress**: do not. The WARNING is intentionally fail-open — you keep serving traffic — but the underlying drain is broken. Suppressing the WARNING does not fix the drain.

---

## Access-Log Middleware Ordering

If you use an external access-log middleware and want drain-503 responses to appear in that log, the access-log middleware must be placed **before** baldur's early group (= further out in the middleware stack). baldur's own `AuditMiddleware` (`DEFAULT_TAIL_GROUP`, innermost) sits **after** `DrainAwareMiddleware` and therefore does **not** see the drain-503 short-circuit response.

This is intentional. Drain-503 is a process-lifecycle event, logged by `DrainAwareMiddleware` itself via `structlog` (`drain_aware_middleware.request_rejected`). It is not a chain-integrity audit event.

---

## Settings Reference

| Setting | Default | Range | Purpose |
|---------|---------|-------|---------|
| `BALDUR_SHUTDOWN_DEFAULT_DRAIN_TIMEOUT_SECONDS` | 30.0 | 5–300 | Drain loop deadline |
| `BALDUR_SHUTDOWN_DRAIN_DEFAULT_RETRY_AFTER_SECONDS` | 5.0 | 1–300 | Retry-After fallback when phase != DRAINING |
| `BALDUR_SHUTDOWN_DRAIN_LIVENESS_PATHS` | `[]` | `list[str]` | Extra liveness paths exempted from drain-503 |
| `BALDUR_SHUTDOWN_HOOKS_CHECK_DELAY_SECONDS` | 2.0 | 0.5–30.0 | Delay before `gunicorn_hooks_not_installed` check |
| `BALDUR_DRAIN_AWARE_MIDDLEWARE_ENABLED` | True | bool | Toggle (Django settings only, not env) |
| `BALDUR_REQUEST_TRACKING_MIDDLEWARE_ENABLED` | True | bool | Toggle (Django settings only, not env) |
