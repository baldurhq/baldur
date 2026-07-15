# DLQ Two-Layer Activation Runbook

> **Purpose**: Activate Baldur's full "DLQ absorbs all failures" contract by configuring **both** layers of DLQ protection — view-level `@dlq_protect`/`@protected` AND middleware-level `BALDUR_DLQ_ELIGIBLE_PATHS`. Single-layer setups miss failures that originate before the view dispatcher (ORM connection setup, middleware-stage exceptions during downstream outages), producing 10–22 % absorption gaps under storm conditions.
> **Audience**: Operators integrating Baldur into a Django app + anyone running scenario 7B.2 or the equivalent production storm.
> **Cadence**: One-time setup per Django app + revisit when adding new failure-prone endpoints.

---

## TL;DR

Baldur ships **two independent DLQ-storage layers** for Django apps:

| Layer | Where it fires | What it catches |
|---|---|---|
| **View-level** (`@dlq_protect`, `@protected(dlq=True)`) | Inside the wrapped function body | Failures inside business logic — anything the function raises after retry exhaustion |
| **Middleware-level** (`BaldurMiddleware` + `BALDUR_DLQ_ELIGIBLE_PATHS`) | At the Django request boundary | Failures BEFORE the view runs (ORM connection setup, middleware-layer exceptions) AND preemptive store when middleware-CB is OPEN |

The two layers are independent — view-CB is for business-logic flow control, middleware-CB is for cross-cutting infrastructure protection. **Both must be activated** for the framework's "DLQ absorbs ALL failures" contract to hold in production. Cat 7B.2 reverse-verify (2026-05-12) measured the gap: 1-layer setup absorbs 78–97 % of storm failures with high between-run variance; 2-layer setup deterministically absorbs 100 % (run-to-run ZCARD identical) and sustains 19× higher RPS by short-circuiting view-retry via middleware-CB.

**The single most important rule**: **if your README/SLO promises "we absorb every failure under storm", enable both layers**. View-level alone is correct architecture for the business-logic flow but does not honor the framework-level "all failures" contract by itself.

---

## Background — Why Two Layers Exist

### Layer 1: view-level

```python
# yourapp/views.py
from baldur.decorators import dlq_protect

@dlq_protect("payment.charge")
def _charge_impl(order_id, amount):
    Order.objects.get_or_create(id=order_id, defaults={...})
    response = requests.post(payment_api, json={...})
    response.raise_for_status()
    ...
```

`@dlq_protect` (or `@protected(dlq=True, retry=True)`) wraps the wrapped function body. When the body raises after retry exhaustion, `DLQSink.persist()` writes to `baldur:dlq:pending` in Redis. **What this catches**: anything raised AFTER the view's `def` line has been entered.

**What this does NOT catch**: anything that fails BEFORE Django dispatches to the view function — including:
- Connection-pool setup failures when the DB has just gone down
- Middleware-stage exceptions (e.g., a session-load middleware accessing a dead DB)
- Routing or DRF pre-dispatch errors

Under steady-state production these are rare. **Under storm conditions** (downstream DB or auth service goes 100 % unreachable), they account for 10–22 % of all failures, depending on connection-pool warmup state and request RPS.

### Layer 2: middleware-level

`baldur.api.django.middleware.BaldurMiddleware` (`src/baldur/api/django/middleware/baldur.py`) wraps every request that matches a configured path pattern. Two paths into DLQ:

1. **Preemptive (when middleware-CB is OPEN)** — `baldur.py:208-244`: middleware tracks its own domain-keyed CB based on observed 5xx responses. After `failure_threshold` 5xx responses on the same domain, middleware-CB OPENs and subsequent requests for that path go straight into DLQ without reaching the view.
2. **At-exception in main path** — `baldur.py:263-282`: if the view raises a DB-related exception that escapes the view's own try/except, middleware catches it via its `MONITORED_DB_ERRORS` filter and stores to DLQ.

Both paths require `_is_dlq_eligible(request)` to return True, which requires `BALDUR_DLQ_ELIGIBLE_PATHS` to include the request path.

### Why both layers are needed

A failure starting from the same downstream outage can land in either layer depending on **when it manifests** during request handling. View-level handles the steady-state "retry-exhausted" case; middleware-level handles the pre-dispatch + post-CB-OPEN case. Without middleware-level activation:

- Pre-dispatch failures get a 5xx response from Django but no DLQ entry
- After middleware-CB OPENs, every request becomes a fast 5xx with no DLQ entry
- Result: real-world absorption rate drops 10–22 % vs. the "all failures" framework promise

---

## Phase 1 — Activate view-level (if not already)

### Step 1.1 — Identify the failure-domain functions

For each endpoint that calls a downstream that can fail (DB, payment API, third-party service), find the function whose body does the downstream call. Use a unique `name` per failure domain — Baldur tracks CB / retry / DLQ statistics per `name`.

### Step 1.2 — Wrap with `@dlq_protect` or `@protected`

The two decorators are equivalent shorthand for the same policy chain:

```python
# Equivalent — pick one
@dlq_protect("payment.charge")
def _charge_impl(order_id, amount): ...

# OR
from baldur.protect_facade import protected

@protected(name="payment.charge", dlq=True, retry=True, circuit_breaker=True)
def _charge_impl(order_id, amount): ...
```

Use `@protected(circuit_breaker=False)` ONLY when you need to disable the view-CB explicitly (e.g., scenario 7B.2 storm-write-pressure measurement). For production, leave the CB on — it's a key part of the storm-protection story.

### Step 1.3 — Verify

After deploy, drive one failure and check Redis:

```bash
redis-cli ZCARD baldur:dlq:pending
# Should increment by 1 after each retry-exhausted failure
```

**Next step go/no-go**: ZCARD increments after retry exhaustion → Phase 1 done, proceed to Phase 2.

---

## Phase 2 — Activate middleware-level

### Step 2.1 — Confirm `BaldurMiddleware` is in `MIDDLEWARE`

Open your Django `settings.py` and verify the middleware order:

```python
MIDDLEWARE = [
    "baldur.api.django.middleware.HealthBridgeMiddleware",   # must be first
    "baldur.api.django.middleware.BaldurMiddleware",          # must be second
    # ... your other middleware ...
]
```

If `BaldurMiddleware` is not present, add it. The order matters — `HealthBridgeMiddleware` must precede `BaldurMiddleware` per the docstring at `baldur.py:19`.

### Step 2.2 — Add `BALDUR_DLQ_ELIGIBLE_PATHS`

In the same `settings.py`, add path regexes for the endpoints whose failures should be middleware-absorbed:

```python
BALDUR_DLQ_ELIGIBLE_PATHS = [
    r"^/api/charge/?$",
    r"^/api/orders/[0-9]+/refund/?$",
    # ... add patterns for each failure-prone endpoint ...
]

BALDUR_DOMAIN_MAPPING = {
    "/api/charge":          "payment.charge",
    "/api/orders":          "order",
    # ... pairs each path with a CB / metrics domain name ...
}
```

**Path-regex tips**:
- Use `^` and `$` anchors to avoid accidentally matching `/api/charge_status/` when you only want `/api/charge`.
- The `BALDUR_DOMAIN_MAPPING` keys are **substring matches**, not regex — `"/api/charge"` matches any path containing that substring.
- Domain names should be stable across releases — they appear in Prometheus labels and CB keys.

### Step 2.3 — Restart the Django process

`BaldurMiddleware._load_path_patterns()` reads settings **once per class** at first request (`baldur.py:172-173` `_paths_loaded` flag). A live process picks up the change only on restart.

```bash
# Docker compose
docker compose restart django_app

# Or directly
sudo systemctl restart gunicorn
```

### Step 2.4 — Verify pattern load

Check the Django process logs immediately after restart:

```
[info] baldur_middleware.loaded_patterns dlq_eligible_paths_count=2 domain_mapping_count=2 infra_paths_count=0
```

`dlq_eligible_paths_count` must be ≥ 1 — otherwise the pattern list did not load. Common causes:
- Settings module not the one actually loaded (`DJANGO_SETTINGS_MODULE` env mismatch)
- Typo in the variable name — must be exactly `BALDUR_DLQ_ELIGIBLE_PATHS` (not `*_PATTERNS` or `*_PATH`)
- Pattern list left as an empty list `[]`

**Next step go/no-go**: `dlq_eligible_paths_count` matches the count in your `settings.py` → Phase 2 done, proceed to verification.

---

## Phase 3 — Verify both layers fire under storm

### Step 3.1 — Drive a storm

Either run the existing scenario:

```bash
python examples/scripts/cat7b2_run.py
```

Or simulate a downstream outage in production-like fashion: Toxiproxy block the DB / payment API for 60 s while driving steady RPS.

### Step 3.2 — Check absorption rate

```bash
# Before storm
PRE=$(redis-cli ZCARD baldur:dlq:pending)
# After storm + 10 s settle
POST=$(redis-cli ZCARD baldur:dlq:pending)
echo "DLQ delta: $((POST - PRE))"
```

Compare to the number of failed requests your load tool reported. With both layers active, the delta should be **≥ the failed-request count** (small surplus from pre-CB-OPEN view-retry double-stores is normal and harmless — typically < 1 %).

### Step 3.3 — Check the path mix

Inspect Django logs for the CB transition event:

```
[info] [EventHandler] CB state changed: service=payment.charge, cell_id=, closed -> open
```

If you see this within the first few seconds of the storm, middleware-CB is functioning. The bulk of subsequent failures will be absorbed via the preemptive path (`baldur.py:208-244`), keeping per-request latency at ~50 ms instead of the 3.5 s view-retry budget.

**Common observation**: under a sustained 100 % downstream-failure storm with both layers, p50 latency drops from ~3500 ms (view-retry-bound) to ~50 ms (middleware-CB short-circuit) — that's the 2-layer architecture working as designed.

---

## Common Mistakes

### Mistake 1 — Activate only view-level, advertise "absorbs all failures"

This was Cat 7B.2's original setup (2026-05-10). Single-run measurement got 97.3 % absorption (G7 < 5 % tolerance), but reverse-verify on 2026-05-12 showed between-run variance of 10–22 %. The framework's "all failures" promise was being half-tested. Always activate both layers if the SLO depends on the "all" word.

### Mistake 2 — Forget to restart Django after editing settings

`_paths_loaded` is class-level and one-shot. Hot-reload (`--reload` gunicorn flag) reloads the module but the class flag persists if the module isn't reloaded. Forced restart is the safest path.

### Mistake 3 — Regex pattern without anchors

`BALDUR_DLQ_ELIGIBLE_PATHS = [r"/charge"]` will match `/charge`, `/discharge`, `/api/charge_audit`, and any other path containing the substring as a regex-anywhere match. Always use `^` and `$` unless you have a specific reason to permit prefix / suffix matches.

### Mistake 4 — Overlap with `BALDUR_INFRA_FAILURE_PATHS`

Both lists are loaded by `_load_path_patterns`. `BALDUR_INFRA_FAILURE_PATHS` controls "treat 5xx as infrastructure failure for CB", which interacts with the middleware CB threshold. If a path matches both lists, behavior follows the dispatch order in `baldur.py:266-310` — DB exceptions go to `database` domain regardless, while 5xx-only responses route to the inferred domain. Generally safe to include the same paths in both lists.

### Mistake 5 — Assume `circuit_breaker=False` on `@protected` disables the middleware CB too

It does NOT. View-level `circuit_breaker=False` only disables the view-stage CB inside the @protected wrapper. Middleware-CB tracks the same domain independently via its 5xx-response counter (`baldur.py:266-278`). This is intentional — the two CBs serve different roles. Cat 7B.2 explicitly relies on this fact to keep the view-retry path active while still benefiting from middleware preemptive DLQ.

---

## Cross-References

- `src/baldur/api/django/middleware/baldur.py:144-188` — `_load_path_patterns()` docstring with full settings examples (`BALDUR_INFRA_FAILURE_PATHS`, `BALDUR_DOMAIN_MAPPING`)
- `src/baldur/api/django/middleware/baldur.py:208-244` — preemptive DLQ-store when middleware-CB is OPEN
- `src/baldur/api/django/middleware/baldur.py:263-310` — at-exception DLQ-store in main path
- `src/baldur/protect_facade.py:559-589` — `protect()` public API + view-level `@protected` decorator semantics
- `examples/django_app/payment_app/settings.py` — reference `BALDUR_DLQ_ELIGIBLE_PATHS` setup for the testbed (added 2026-05-12 by `c88f1ab5 test(7B.2): activate middleware-level DLQ for 2-layer storm absorption (F3)`)
- Internal DLQ write-pressure scenario result — demonstrates the 1-layer vs 2-layer gap with measured numbers (not part of the published docs)
- `docs/runbooks/data-consistency-boundaries.md` — sibling runbook on what to store in Baldur vs ACID DB; this runbook assumes you have read it
- `docs/runbooks/protect-hang-troubleshooting.md` — sibling runbook on `protect()` hang diagnosis; relevant if view-retry path appears to stall under storm

---

## Rollback

If activating Layer 2 produces unexpected behavior (rare — the change is purely additive), remove the two settings lines:

```python
# Remove or comment out
# BALDUR_DLQ_ELIGIBLE_PATHS = [...]
# BALDUR_DOMAIN_MAPPING = {...}
```

Restart Django. `dlq_eligible_paths_count=0` confirms rollback. Layer 1 (view-level @protected) continues to function normally — the layers are independent. No data migration or state cleanup is required.

If you find any DLQ entries were stored by Layer 2 that you'd rather not have (e.g., entries from a 5xx that wasn't actually a downstream failure), drain them via the standard DLQ replay tooling — they're indistinguishable from Layer-1 entries once written.
