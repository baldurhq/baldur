# Dashboard Service

> One read-only call that rolls up what Baldur's dead letter queue holds (what's failing, what recovered, and how the backlog looks right now) into a single snapshot, so any monitoring screen can show it at a glance.

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

In Baldur's terms, the Dashboard Service is that single read-model over the dead letter queue, where
Baldur parks the operations that failed. One request returns the backlog picture (status counts,
recent activity, the error distribution, a replay-count alert, a resolution rate, and one overall
health verdict), cached so it stays cheap to poll.

## Why it matters

Self-healing leaves a lot of small facts behind in that queue: how many parked operations are still
pending, how many were resolved, which domains are noisiest, how many replay attempts the entries
needed. Scattered across separate queries, those facts are expensive to collect and easy to read
inconsistently.

The Dashboard Service turns them into one cheap, consistent answer. Any monitoring UI (Baldur's own
Web Console panel, a chart in your existing tooling, or a one-line `curl`) can hit a single endpoint
and get the same snapshot, including a single rolled-up health verdict for a status light. For a
small team without a dedicated monitoring stack, that one endpoint answers *is failed work piling up
right now?*

## How it works in Baldur

Baldur exposes one read-only endpoint that returns the full snapshot:

```
GET /api/baldur/dashboard/summary/
```

Baldur's built-in admin server serves the same handler at `/dashboard/summary`; it starts with
`baldur.init()` by default, or in the foreground with `baldur admin`. Either way it is a read-only
endpoint that a Viewer role or higher can call.

A single response gathers everything a monitoring panel needs:

| Section | What it tells you |
|---------|-------------------|
| **Health status** | One rolled-up verdict — `healthy`, `good`, `warning`, or `critical` |
| **Overview** | Total / pending / resolved / failed / archived entry counts, plus a resolution-rate percentage |
| **Recent activity** | New vs. resolved entries over the last 24 hours and 7 days |
| **Distribution** | The noisiest domains and the most common failure types |
| **Alerts** | The average replay count per entry, plus a high-retry figure derived from that average (zero until the average passes five), not a count of entries |

Every section counts dead-letter entries, read through a statistics source rather than from the
queue itself. Baldur registers that source by itself only when `BALDUR_SQL_DSN` is set; it reads the
SQL dead-letter table, and its totals cover entries created in the last 30 days. The numbers
therefore track your captured failures only when the queue itself is stored in SQL
(`BALDUR_DLQ_BACKEND=sql`, or a DSN with no Redis URL configured). On the memory or Redis store every
count reads zero and the verdict reads `healthy`, the same payload an empty queue returns. The dead
letter queue's own read endpoints still list those entries, because they read the queue directly.

The **health status** rolls the counts into a single word so a UI can show one status light. It
reads *healthy* when nothing is pending or failed, *good* with up to ten entries pending and none
failed, *warning* past that, and *critical* only when more than fifty are pending and more than five
have failed. The built-in statistics sources leave the failed count at zero (an entry that runs out
of replays is counted in the total under its own status), so today the pending backlog alone moves
the light and it stops at *warning*. A *healthy* reading can also mean no data: it is what the
snapshot says when no statistics source is registered or the store could not be read.

Two design choices keep it cheap and safe to poll:

- **Caching.** The snapshot is cached for a short window, so many clients polling every few seconds
  do not each trigger a fresh round of database queries. The cached snapshot refreshes automatically
  when that window expires; an application can also invalidate it programmatically to force the next
  read to recompute immediately after a significant change.
- **Graceful degradation.** If the cache or the statistics store is unavailable, the endpoint still
  returns a snapshot, with zeroed counts for each section it could not read, rather than failing.
  Nothing in the payload marks the gap and the verdict is computed from those zeroes, so a snapshot
  taken while the store is down reads *healthy*. Each failed read is logged at WARNING or above;
  check the server log before trusting a green light during an incident.

## Configuration

The Dashboard Service has no operator environment variables of its own in the stable allowlist. How
long a snapshot stays cached (30 seconds by default) is an advanced setting that may change before it
is promoted to the stable operator contract.

Beyond the statistics source described above, reading the dashboard needs no configuration of its
own. The summary endpoint is available wherever you have mounted Baldur's Django API or run its admin
server, and caching works out of the box with the built-in in-memory cache. Pointing Baldur at Redis
makes that cache shared across processes, so every worker serving the endpoint reads from one warm
snapshot.

## Tier behavior

The summary endpoint is available in **every tier**; what scopes by tier is one optional enrichment
section.

- **In OSS**: every section above is computed by OSS itself, and nothing in the snapshot is stubbed
  or held back for PRO. Whether the counts reflect your traffic depends on the statistics source
  described above, not on the tier.

- **With PRO installed**: the snapshot gains one extra `recovery` section about PRO's coordinated
  recovery: how many recovery sessions are running, how many recovery approvals are waiting or have
  gone stale, recovery totals, and a health word of its own. It appears whenever the PRO package is
  installed and its recovery state can be read, with zero counts before any recovery has run. It is
  purely additive: without PRO the section is absent and the rest of the response is identical, so a
  monitoring UI written against the OSS summary keeps working unchanged.

## See also

- [What is self-healing?](self-healing.md) — the activity this snapshot summarizes
- [DLQ + Replay](dlq-replay.md) — the queue these counts describe, and how to store it in SQL
- [Daily Report](daily-report.md) — the once-a-day digest of the same activity, where the dashboard is the live "right now" view
- [Metrics](../oss/metrics.md) — the continuously scraped time-series view, next to the dashboard's point-in-time snapshot
- [Health Check](../oss/health-check.md) — the load-balancer probe, next to the dashboard's rolled-up health verdict
- [OSS vs PRO tier model](tier-model.md) — what the PRO recovery section adds
- [Getting Started](../../getting-started/index.md) — set Baldur up in five minutes
