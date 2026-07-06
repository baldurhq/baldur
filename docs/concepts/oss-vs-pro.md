# OSS vs PRO: capability matrix

> A precise, feature-by-feature map of what the free open-source core does on
> its own and what the PRO package adds. If you are deciding *which tier you
> need*, start with [the tier model](foundations/tier-model.md) — this page is
> the detailed reference it points to.

Baldur is one library with one API (`@baldur.protected`). OSS is the free,
Apache-2.0 core (`pip install baldur-framework`); PRO is a separate package you
add on top in production. PRO **adds** capability — it never replaces or
relicenses anything in the core.

## At a glance

| Capability | OSS core | PRO |
|------------|:--------:|:---:|
| Circuit breaker (incl. staged half-open recovery) | ✅ | ✅ |
| Retry with backoff | ✅ | ✅ |
| Idempotency | ✅ | ✅ |
| Health checks | ✅ | ✅ |
| Graceful shutdown / drain | ✅ | ✅ |
| Metrics (Prometheus / OpenTelemetry) | ✅ | ✅ |
| System control (runtime on/off) | ✅ | ✅ |
| Built-in web console (operate & recover) | ✅ | ✅ (＋PRO panels) |
| Precomputed cache | ✅ | ✅ |
| Dead-letter queue + replay | — | ✅ |
| Audit trail (hash-chained, exportable) | — | ✅ |
| Unified notification / alerting | — | ✅ |
| Emergency mode (coordinated load shedding) | — | ✅ |
| Bulkhead isolation (productized) | — | ✅ |
| Throttle / adaptive rate limiting | — | ✅ |
| Canary Recovery (config rollout + auto-rollback) | — | ✅ |
| Governance gates for risky automation | — | ✅ |
| Meta-Watchdog (Baldur watching itself) | — | ✅ |

A PRO subscription includes every PRO capability — they are not sold or unlocked
one at a time.

## What "OSS core" means in practice

The free tier is the **resilience patterns themselves**, with zero
infrastructure to start. A single service can survive a failing dependency,
tell a load balancer the truth, drain cleanly on restart, and expose metrics —
all on `pip install baldur-framework` alone, no license and no sign-up.

See the individual guides: [Circuit Breaker](oss/circuit-breaker.md),
[Retry](oss/retry.md), [Idempotency](oss/idempotency.md),
[Health Check](oss/health-check.md),
[Graceful Shutdown](oss/graceful-shutdown.md), [Metrics](oss/metrics.md),
[System Control](oss/system-control.md),
[Precomputed Cache](oss/precomputed-cache.md).

## Where the boundary actually sits

The boundary is deliberately drawn so you can write production-shaped code on
OSS and have the **same code** light up under PRO — but that means a few
features expose an OSS *API surface* whose heavier *backing* ships with PRO.
Knowing exactly where that line falls avoids surprises.

The clearest example is the dead-letter queue:

```python
@baldur.protected("charge-customer", retry=True, dlq=True)
def charge(order_id: str) -> dict:
    return payment_gateway.charge(order_id)
```

- **On OSS**, `dlq=True` is accepted and your code is correct, but there is no
  durable store behind it — a final failure is not set aside for later replay.
- **On PRO**, the exact same decorator now records every failed operation
  durably, survives restarts, and can be replayed once the dependency recovers.

The same shape applies to audit and notification: the hooks exist in the core,
the durable/coordinated machinery is PRO. This is intentional — you opt in once,
in code, and the capability becomes real when you add the PRO package and a
license. PRO-only settings left in an OSS install are simply **inert**; an OSS
deployment never breaks because a PRO knob was present.

## A note on naming: two kinds of "canary"

"Canary" appears in two unrelated places in Baldur. They are different features
at different tiers — do not confuse them:

- **Circuit-breaker staged recovery (OSS).** When an OSS circuit breaker leaves
  the OPEN state, it does not slam 100% of traffic back at the dependency. It
  ramps through staged percentages (a *canary ramp* in the half-open phase) and
  reverts to OPEN at the first sign the dependency is still unhealthy. This is
  part of the [circuit breaker](oss/circuit-breaker.md) and operates on
  **in-process traffic to one dependency**.
- **Canary Recovery (PRO).** A separate PRO feature that rolls a **configuration
  change** out to a small slice of your fleet first, watches it, and restores
  the previous configuration automatically if the rollout degrades. It operates
  on **fleet-wide configuration**, not circuit-breaker traffic. See
  [Canary Recovery](pro/canary-recovery.md).

In short: the OSS canary ramp recovers *one breaker's traffic*; PRO Canary
Recovery rolls out and rolls back *configuration across a fleet*.

## Turning PRO on

PRO is the OSS install plus the PRO package and a license, supplied through one
of these:

| Env Var | What it controls |
|---------|------------------|
| `BALDUR_LICENSE_KEY` | The PRO license, provided inline as a value |
| `BALDUR_LICENSE_FILE` | Path to a file that holds the PRO license |

Individual PRO features carry their own settings, documented in their own guides
and the [environment variable reference](../reference/env-vars.md).

## See also

- [Which tier do you need?](foundations/tier-model.md) — the narrative version
  of this page, with the adoption path
- [What is self-healing?](foundations/self-healing.md) — the problem both tiers
  exist to solve
- [Composing with `@baldur.protected`](foundations/composition.md) — the one API
  identical across tiers
