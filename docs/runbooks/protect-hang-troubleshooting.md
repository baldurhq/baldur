# Protect Hang Troubleshooting Runbook

> **Purpose**: Diagnose and resolve apparent hangs inside `baldur.protect()` / `aprotect()` / `@protected` zones after #482 flipped `ProtectSettings.default_timeout_seconds` from `30.0` to `None`. Before #482, every protected call had an outer 30-second wall-clock safety net by default. After #482, callers who do not pass an explicit `timeout=` argument or set the env var rely solely on the I/O-client's own timeout — and a misconfigured client (httpx with no `timeout=`, raw socket, blocking subprocess) can now hang the worker indefinitely.
> **Audience**: Operator / SRE / on-call engineer triaging a hung worker, OR a developer who notices a request never returning under `protect()`.
> **Cadence**: Incident-driven. Read once during onboarding; refer back when a hang is suspected.

---

## TL;DR

After #482, `protect()` no longer adds a default 30 s timeout. Three resolution paths in order of preference:

1. **Add the timeout where the I/O happens** — set the client-level timeout on `httpx`, `psycopg`, `redis-py`, `requests`, `aiohttp`, etc. This is the right layer, because it surfaces a domain exception (`httpx.ReadTimeout` etc.) callers can branch on.
2. **Pass `timeout=N` per-call** — `protect("name", fn, timeout=30)` re-introduces a `TimeoutPolicy` wall-clock bound for that one call.
3. **Restore the framework-wide default** — set `BALDUR_PROTECT_DEFAULT_TIMEOUT_SECONDS=30` in the environment. This recovers pre-#482 behavior globally; expect ~30–50 μs/call overhead on the canonical default profile (the cost #482 was specifically removing).

Apply path 1 if you control the client. Apply path 2 if the call is a one-off or you cannot touch the client. Apply path 3 only as a temporary mitigation while paths 1–2 are rolled out.

---

## Symptom Checklist

If any of the following match, this runbook is for you:

- A worker thread / asyncio task is stuck inside a function decorated with `@protected` or wrapped by `protect()`, and never returns.
- After upgrading past #482, latency p99 for a service jumped to "infinite" (timeouts piling up at the load balancer instead of inside the app).
- DEBUG logs show `protect.composer_built` events with `policies=["circuit_breaker"]` (no `"timeout"` entry) for the affected zone.
- Your `httpx.AsyncClient(...)` / `psycopg.connect(...)` / `redis.Redis(...)` constructor was called without a `timeout=` argument, and no `BALDUR_PROTECT_DEFAULT_TIMEOUT_SECONDS` env var is set.

If none match, this is probably not a #482-related hang — check `docs/runbooks/data-consistency-boundaries.md` and the standard observability dashboards instead.

---

## Diagnostic Procedure

### Step 1 — Confirm the protected zone has no timeout policy

Set `BALDUR_LOG_LEVEL=DEBUG` (or whatever logging config your service uses to enable DEBUG output) and re-trigger the call path. Look for:

```text
event=protect.composer_built name=<your-service-name> timeout_seconds=None policies=['circuit_breaker']
```

`timeout_seconds=None` and the absence of `'timeout'` in `policies` confirm the call has no Baldur-level wall-clock bound. If `'timeout'` IS in the list, this is not a #482 issue — investigate the client / downstream instead.

The DEBUG line is emitted exactly once per unique `(name, timeout_seconds)` tuple (cache miss only, per #482 D8), so you may need to restart the worker to see it. If you cannot enable DEBUG, inspect `baldur.protect_facade._composer_cache` directly via a debugger:

```python
import baldur.protect_facade as p
p._composer_cache  # dict[(name, timeout_seconds), PolicyComposer]
```

### Step 2 — Identify the I/O client missing its own timeout

Trace the protected function down to its outermost network or subprocess call. Common culprits:

| Client | Default timeout | How to set |
|--------|-----------------|------------|
| `httpx.Client` / `httpx.AsyncClient` | **5 s** if `timeout=` omitted (good). **None** if `timeout=None` passed explicitly. | `httpx.Client(timeout=httpx.Timeout(connect=5, read=30))` |
| `requests.get()` / `Session.send` | **None** (will block forever) | `requests.get(url, timeout=(5, 30))` — (connect, read) |
| `aiohttp.ClientSession` | **5 min** total (`aiohttp.ClientTimeout(total=300)`) | `aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))` |
| `psycopg` / `psycopg2` | **None** at the client; PostgreSQL `statement_timeout` controls server-side. | `cur.execute("SET statement_timeout = '30s'")` per session, OR `psycopg.connect(..., options="-c statement_timeout=30000")` |
| `redis-py` | **None** at socket level | `redis.Redis(socket_timeout=5, socket_connect_timeout=5)` |
| `subprocess.run` | **None** | `subprocess.run([...], timeout=30)` |
| `urllib.request.urlopen` | **None** (uses `socket._GLOBAL_DEFAULT_TIMEOUT`) | `urlopen(url, timeout=30)` |

The **`requests` library is the most common offender** — its default-no-timeout behavior is famously surprising and was the implicit reason `ProtectSettings.default_timeout_seconds=30` existed pre-#482.

### Step 3 — Reproduce locally with reduced timeout

Before changing production, reproduce the hang in a unit test or a scratch script with a much shorter outer bound to confirm the diagnosis:

```python
import baldur.protect_facade

def call_downstream():
    # The actual protected function — no inner timeout.
    return some_client.fetch(url)

# Force a 2 s outer bound to verify the hang IS at the I/O layer,
# not somewhere else (deadlock, infinite loop, etc.).
result = baldur.protect_facade.protect("dx-debug", call_downstream, timeout=2.0)
```

If this raises `baldur.core.exceptions.TimeoutPolicyError` after 2 s, the hang is genuinely at the I/O layer and one of the three resolution paths below applies. If it returns successfully or raises something else, the issue is elsewhere — stop following this runbook.

### Step 4 — Confirm before going to a production change

Before you ship a fix, list every codepath that calls the affected `protect()` zone (or every callsite of the affected client). Adding a per-call `timeout=` covers ONE callsite; setting the env var covers ALL of them. Decide which scope matches the actual blast radius.

---

## Resolution Paths

### Path 1 (preferred) — Set the timeout at the I/O client

Most appropriate when:

- The same client is used by code outside Baldur's `protect()` (raw scripts, management commands, batch jobs) and would benefit equally.
- The downstream has well-known SLOs and the timeout value is meaningful (not arbitrary).
- The client raises a domain-specific exception (`httpx.ReadTimeout`, `psycopg.OperationalError`, `redis.TimeoutError`) that callers may want to handle distinctly from a generic `TimeoutPolicyError`.

Apply by editing the client constructor or per-call kwargs as shown in the Step 2 table. Verify with the same DEBUG / reproduce flow.

**Trade-off**: requires code change, may need a release.

### Path 2 — Pass `timeout=N` per `protect()` call

Most appropriate when:

- Path 1 is blocked (third-party client constructor not under your control, or shared with code that needs different bounds).
- The protected zone has a uniform deadline regardless of which client it dispatches to.
- You want a guaranteed Baldur-level wall-clock bound that triggers `CircuitBreakerPolicy` state transitions on timeout.

```python
result = baldur.protect_facade.protect("checkout.charge", charge_card, timeout=10.0)
```

```python
@baldur.protect_facade.protected("notification.send", timeout=5.0)
def send_email(addr: str, body: str) -> None:
    ...
```

Re-introduces the per-call `ThreadPoolExecutor.submit` cost (~30–50 μs on Windows) for that zone — the cost #482 was specifically removing. Acceptable for non-hot-path zones.

**Trade-off**: per-zone code change; per-call cost overhead returns for the modified zones only.

### Path 3 (temporary) — Restore the global 30 s default

Most appropriate as a **mitigation while paths 1 or 2 are rolled out**, not as a permanent solution.

```bash
export BALDUR_PROTECT_DEFAULT_TIMEOUT_SECONDS=30
# or in systemd / Kubernetes manifest, set the env var on the worker.
```

After setting, restart the worker — `ProtectSettings` is read once and cached by `get_protect_settings()`. To force re-read in tests, call `reset_protect_settings()`.

**Trade-off**: undoes the #482 hot-path optimization globally — every default-profile `protect()` call pays the executor-submit cost again. Acceptable as a temporary mitigation; not acceptable as the long-term answer.

---

## Verification After the Fix

For Path 1 or 2, run the Step 1 + Step 3 procedures again. Expected:

- `protect.composer_built` log line shows `'timeout'` in `policies` (Path 2 only — Path 1 doesn't change the Baldur-level chain).
- The reproduce script raises `TimeoutPolicyError` (Path 2) or the client's domain timeout exception (Path 1) within the configured bound.
- Original hang symptom is resolved in production (LB-level timeout count returns to baseline).

For Path 3, restart the worker and re-run Step 1: `policies` should now show `['circuit_breaker', 'timeout']` and `timeout_seconds=30.0`.

---

## Why #482 Removed the Safety Net

The 30 s default was originally established by #449 D7 under the rule "timeout is a pure safety feature like CB → on by default." 7A.1 microbenchmark regression #2 (post-#481) traced the per-call `ThreadPoolExecutor.submit` cost to ~30–50 μs on Windows, dominating the canonical `protect("name", fn)` profile. Industry-typical Python I/O clients enforce timeouts at their own boundary (see Step 2 table) — the 30 s outer net was rarely the firing deadline. The change traded the safety net for the hot-path performance, with this runbook + CHANGELOG migration hint covering the residual risk.

---

## Quick Reference

| Question | Answer |
|----------|--------|
| Does `protect("name", fn)` have a timeout by default after #482? | **No.** Set per-call `timeout=`, env var, or rely on the I/O-client's own timeout. |
| What env var restores pre-#482 behavior? | `BALDUR_PROTECT_DEFAULT_TIMEOUT_SECONDS=30` |
| How do I confirm a zone has no timeout policy? | `BALDUR_LOG_LEVEL=DEBUG` → check `protect.composer_built` log for `'timeout'` in `policies`. |
| Which I/O client is the most common offender? | `requests` — its default is no timeout. `psycopg` and raw `subprocess` are runners-up. |
| Is `aprotect()` affected? | Yes — same setting controls both sync and async paths. The async chain has no CB/Retry by default (those are sync-only), so the post-#482 default async chain is empty. |
