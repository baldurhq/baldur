# Rate limiting in Baldur

> "Rate limiting" means several different things inside Baldur. This page is the map: what each piece limits, whether it counts across your whole fleet or just one process, and where to reach for a hard fleet-wide quota (spoiler: your gateway, not Baldur).

## What is it?

Rate limiting is putting a ceiling on how many requests something will accept in a stretch of time, and turning the rest away, usually with an HTTP `429 Too Many Requests`. It shows up in two directions. **Inbound**, you cap how much traffic your own service admits so a flood can't bury it. **Outbound**, you cap how hard your service hits a dependency, so you don't get yourself banned by an API that is itself rate-limited.

Baldur does not have a single "rate limiter." It has a handful of distinct mechanisms, each solving a different slice of that problem at a different scope. The confusing part is that they all get called "rate limiting," so the same words point at five different things. This guide names each one, says plainly what it does, and answers the question everyone eventually asks: *if I run ten copies of my service behind a load balancer, does the limit apply as a total across all ten, or ten times over?*

## Why it matters

The honest answer to that fleet-total question is usually "ten times over," and knowing which mechanism behaves which way is the difference between a limit that protects you and one you only think is protecting you.

Two failure modes come from getting this wrong. You reach for the per-endpoint cap expecting it to enforce a company-wide budget, and instead every instance quietly enforces its own copy of the number. Or you want to stop your workers from stampeding a dependency that just returned `429`, and you reach for an inbound request counter, which is the wrong tool entirely. A clear map of what limits what, and at what scope, is what keeps you from picking the wrong primitive under pressure.

## How it works in Baldur

Five mechanisms make up Baldur's rate-limiting surface. Here is what each one limits and how it counts:

| Mechanism | What it limits | Scope across a fleet | Tier |
|-----------|----------------|----------------------|------|
| Per-endpoint cap (`@rate_limit`) | inbound requests to one endpoint in a window; over-limit requests get `429` | **per instance** — each process counts on its own, so N instances admit up to N× the number | OSS, off by default |
| `429`-storm detection (circuit breaker) | watches for a burst of `429`s coming *back* from a dependency and trips the breaker | per instance by default; fleet-shared when opted in | OSS, on by default (built into the circuit breaker, not a separate toggle). A detection signal that trips protection, not an admission cap |
| Admin / control API protection | Baldur's own management endpoints, per client | **fleet-shared** via Redis (one real count across workers) | OSS, on out of the box |
| Adaptive Throttle | inbound admission, with the cap moving up and down on live latency | per-instance count; the limit *value* is shared across the fleet | PRO |
| Outbound cooldown coordination | calls your service makes to a dependency that rate-limits you; shares one backoff cooldown so the fleet backs off together | **fleet-shared** cooldown state | OSS, through the retry integration |

The admin / control API row is worth calling out: Baldur rate-limits its *own* management API out of the box, and it is the one place Baldur enforces a genuinely shared, per-client count across all your workers by default (roughly 100 requests per minute per client). If the shared store is unavailable it falls back to a stricter per-instance limit rather than removing the limit, so an outage tightens the door instead of opening it.

### The fleet-total question, answered honestly

Notice what is *not* in that table: a single knob that caps total requests per minute across your whole fleet. Baldur enforces no general fleet-wide inbound quota on any default request path. The inbound mechanisms (the per-endpoint cap, and Adaptive Throttle) count per process, so ten instances mean up to ten times the per-instance number. The genuinely fleet-shared pieces are narrow by design: the admin API protection (scoped to Baldur's own management endpoints), the `429`-storm detector (a signal, not a quota), and the outbound cooldown (shared *state*, not an inbound counter).

This is a deliberate choice, not a missing feature. An exact fleet total requires a shared-counter round-trip on every single request, and under Baldur's fail-open principle that counter has to relax to per-instance behavior the moment the shared store degrades. A "hard" quota that silently becomes ×N during a Redis blip is not honest, so Baldur does not pretend to offer one. The right place for a true fleet-wide inbound cap is *before* traffic reaches your app, at the load balancer or gateway, which sees all traffic in one place and rejects excess before it ever costs you a worker.

### Which one do you actually want?

Most "I need rate limiting" requests are really one of three distinct intents. Match yours to the primitive:

| What you actually want | Reach for | Tier |
|------------------------|-----------|------|
| Keep one instance from being buried by its own inbound flood | the per-endpoint cap (`@rate_limit`), remembering each instance counts on its own | OSS |
| Keep your fleet from hammering a dependency that is rate-limiting you | the outbound cooldown coordinator and the circuit breaker's `429`-storm detection, with Bulkhead and Adaptive Throttle for admission control | OSS + PRO |
| Enforce a hard total request budget across the whole fleet | your gateway or load balancer, not Baldur | infra |

For that third row, the tools that belong at the edge already do this well. Keep the config at pointer level so it does not rot against tool versions:

- **Nginx**: `limit_req_zone` plus `limit_req` on the location gives you a shared token-bucket cap at the proxy.
- **Envoy**: the global rate limit filter enforces a true cluster-wide budget against an external rate-limit service.
- **Cloud API gateways** (AWS API Gateway, Google Cloud Armor, and equivalents): per-key throttling configured in the console.

If you cannot put the cap at the edge, the practical in-app approximation is to divide your target fleet total by the number of instances and set that as each instance's per-endpoint cap. It drifts as you scale, but it is honest about being per-instance.

> **Not to be confused with:** Baldur's notification layer also talks about "rate limiting," but that one is alert-fatigue cooldown — don't page me about the same incident fifty times — not request throughput. Different domain entirely.

## Configuration

There are no rate-limit-specific operator environment variables in the v1.0 public allowlist. The two that shape rate-limiting behavior are the shared-state and entitlement knobs:

| Env Var | Default | What it controls |
|---------|---------|------------------|
| `BALDUR_REDIS_URL` | `redis://localhost:6379/0` | whether the admin-API count and the outbound cooldown are shared across workers; without it both fall back to per-instance |
| `BALDUR_LICENSE_KEY` |  | PRO entitlement (unset in OSS mode); Adaptive Throttle activates when Baldur initializes with a valid license |

The individual limits and windows (the per-endpoint cap, the admin-API rate, the throttle's floor and ceiling) are set in code or through advanced settings that are not part of the public operator-tunable environment-variable allowlist for v1.0.

## Tier behavior

- **In OSS**: the per-endpoint cap (`@rate_limit`, opt-in), the circuit breaker's `429`-storm detection, the always-on admin / control API protection, the outbound cooldown coordinator reached through Baldur's retry integration, and Bulkhead's per-dependency capacity isolation so one slow dependency can only saturate its own compartment instead of taking the whole service down.
- **With PRO active**: Adaptive Throttle adds a self-adjusting inbound admission limit that tracks live latency (and sheds the least important traffic first when it tightens under pressure), and Bulkhead gains thread-pool isolation with execution-timeout containment. Neither adds a fleet-wide inbound quota, which stays at the gateway.

## See also

- [Adaptive Throttle](../pro/throttle.md) — the PRO self-adjusting inbound limit, and the outbound Rate Limit Coordinator covered in depth
- [Circuit Breaker](../oss/circuit-breaker.md) — the `429`-storm detection that trips the breaker
- [Bulkhead](bulkhead.md) — per-dependency capacity isolation so one slow dependency can't sink the whole service
- [Baldur and your service mesh](service-mesh.md) — the same "infra owns the network, Baldur owns the code" split that puts a fleet-wide quota at the gateway
- [Retry](../oss/retry.md) — the retry integration the outbound cooldown plugs into
- [OSS vs PRO tier model](tier-model.md) — what each tier includes
- [Environment Variables](../../reference/env-vars.md) — the complete operator-tunable list
- [Getting Started](../../getting-started/index.md) — set Baldur up
