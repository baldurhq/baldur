# Dashboard Service

> One read-only call that rolls Baldur's whole self-healing picture — what's failing, what recovered, and how healthy you are right now — into a single snapshot, so any monitoring screen can show system status at a glance.

## What is it?

A monitoring screen needs to answer one question fast: *how are things right now?* The naive way is
to let every widget run its own database query — one for counts, one for recent activity, one for the
error breakdown. That does not scale: a dozen panels times a dozen viewers means hundreds of queries
hammering your database, often returning slightly different numbers because each ran at a slightly
different moment.

A **dashboard service** (or "summary endpoint") solves this by aggregating everything into one
consistent snapshot behind a single call. Think of a car's dashboard: rather than walking around to
check the fuel tank, the engine temperature, and the tire pressure one at a time, you glance at one
panel that gathers them all.

In Baldur's terms, the Dashboard Service is that single read-model — one request returns the entire
self-healing picture (status counts, recent activity, the error distribution, retry alerts, a
resolution rate, and one overall health verdict), cached so it stays cheap to poll.

## Why it matters

Self-healing produces a lot of small facts: how many operations are pending, how many failed
permanently, which domains are noisiest, how often calls needed retrying. Scattered across separate
queries, those facts are expensive to collect and easy to read inconsistently.

The Dashboard Service turns them into one cheap, consistent answer. Any monitoring UI (Baldur's own
Web Console panel, a chart in your existing tooling, or a one-line `curl`) can hit a single endpoint
and get the same snapshot, including a single rolled-up health verdict you can wire straight to a
green/amber/red light. For a small team without a dedicated monitoring stack, that one endpoint is
the whole *"is the system OK right now?"* view.

## How it works in Baldur

Baldur exposes one read-only endpoint that returns the full snapshot:

```
GET /api/baldur/dashboard/summary/
```

The admin server and the `baldur` CLI serve the same data at `/dashboard/summary` — all three share
a single URL contract. It is a read-only endpoint: a Viewer role or higher can call it.

A single response gathers everything a monitoring panel needs:

| Section | What it tells you |
|---------|-------------------|
| **Health status** | One rolled-up verdict — `healthy`, `good`, `warning`, or `critical` |
| **Overview** | Total / pending / resolved / failed / archived counts, plus a resolution-rate percentage |
| **Recent activity** | New vs. resolved counts over the last 24 hours and 7 days |
| **Distribution** | The noisiest domains and the most common failure types |
| **Alerts** | How many items have a high retry count, and the average retry count |

The **health status** rolls the raw counts into a single word so a UI can show one status light: it
reads *healthy* when nothing is pending or failed, eases to *good* with a small pending backlog,
becomes *warning* as that backlog or the failure count climbs, and *critical* when both are high.

Two design choices keep it cheap and safe to poll:

- **Caching.** The snapshot is cached for a short window, so many clients polling every few seconds
  do not each trigger a fresh round of database queries. The cached snapshot refreshes automatically
  when that window expires; an application can also invalidate it programmatically to force the next
  read to recompute immediately after a significant change.
- **Graceful degradation.** If the cache or the underlying data store is briefly unavailable, the
  endpoint still returns a snapshot — with zeroed counts for the part it could not read — rather than
  failing. A monitoring endpoint that errors out under stress is worse than one that honestly reports
  "nothing to show right now."

## Configuration

The Dashboard Service has no operator environment variables in the stable v1.0 allowlist. The cache
windows it uses — how long a snapshot stays cached before the next read refreshes it — are treated as
advanced settings that may change before they are promoted to the stable operator contract; see the
[API Reference](../../reference/index.md) for the current values.

Reading the dashboard needs no configuration of its own. The summary endpoint is available wherever
you have mounted Baldur's admin or Django API, and caching works out of the box with the built-in
in-memory cache. Pointing Baldur at Redis makes that cache shared across processes, so every worker
serving the endpoint reads from one warm snapshot.

## Tier behavior

The summary endpoint is available in **every tier**; what scopes by tier is one optional enrichment
section.

- **In OSS**: every section above is populated from your own production data — the health verdict,
  the counts, recent activity, the distribution, and the retry alerts are all computed from what your
  services actually did. Nothing in the OSS snapshot is stubbed or held back.

- **With PRO active**: the same snapshot gains one extra **recovery-coordination section**, which
  summarizes what PRO's automated recovery is doing across your workers. It is purely additive — when
  PRO is not installed the section is simply absent and the rest of the response is identical. A
  monitoring UI written against the OSS summary keeps working unchanged; the recovery section just
  appears once PRO has something to report.

## See also

- [What is self-healing?](self-healing.md) — the activity this snapshot summarizes
- [Daily Report](daily-report.md) — the once-a-day digest of the same activity, where the dashboard is the live "right now" view
- [Metrics](../oss/metrics.md) — the continuously scraped time-series view, next to the dashboard's point-in-time snapshot
- [Health Check](../oss/health-check.md) — the load-balancer probe, next to the dashboard's rolled-up health verdict
- [OSS vs PRO tier model](tier-model.md) — what the PRO recovery section adds
- [Getting Started](../../getting-started/index.md) — set Baldur up in five minutes
