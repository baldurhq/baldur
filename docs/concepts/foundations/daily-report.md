# Daily Report

> Once a day, Baldur rolls everything it did to keep your services healthy into one short digest — so you can tell at a glance whether yesterday was calm or a near-miss, without opening a dashboard.

## What is it?

Most monitoring shouts at you in the moment: an alert fires, a dashboard turns red, and you go
look. A **daily report** is the calm opposite: a scheduled summary that arrives once a day and
tells you what happened over the last 24 hours and what was handled automatically. Think of it as
the end-of-shift handover note an on-call engineer leaves for the next person: *here's what broke,
here's what recovered, here's what still needs eyes.*

In Baldur's terms, the Daily Report gathers a full day of self-healing activity — circuit-breaker
trips, the items it auto-processed (archived, expired, recovered, purged), and errors — and rolls it
into a single digest, kept as a running history you can look back through.

## Why it matters

Self-healing is mostly invisible when it works. Baldur quietly retries a flaky call, trips a circuit
before a slow dependency drags everything down, returns a fallback so a user never sees an error,
and if none of that ever surfaces, you have no idea whether the system is coasting or quietly
catching fires all day.

The Daily Report makes the invisible visible. One digest a day answers the question every operator
actually has (*is everything OK?*) without anyone opening a dashboard. On a calm day it collapses
to a single "all quiet" line, so a report that suddenly runs long is itself the signal that
yesterday was busy. For a small team with nobody watching screens, it's the cheapest possible proof
that your safety net is doing its job.

## How it works in Baldur

Once a day, Baldur builds a report for the previous day from the activity it recorded, stores it, and
keeps a rolling history (about three months by default) that you can query by date.

The report is **adaptive**, to stay readable:

- A core summary line is **always** present — it confirms the report ran at all.
- Detail sections (auto-processing of archived/expired/recovered items, circuit-breaker activity,
  errors, and the like) appear **only when they have something to report**. A day with no incidents
  collapses to a single line: *"All quiet — 0 processed, 0 alerts."*
- The report's severity is derived from its contents: a clean day is *info*, task failures make it
  *warning*, a critical alert makes it *critical*.

How you *get* the report depends on your tier — you either pull it on demand or have it delivered:

| What you observe | When it happens |
|------------------|-----------------|
| `baldur report` (or the report API / admin console) returns the day's digest and a multi-day trend | Any time — the report is generated and stored in every tier |
| An *"All quiet — 0 processed, 0 alerts"* one-liner | The day had no healing activity and no incidents |
| Expanded detail sections | Those events actually occurred that day |
| The digest pushed to your Slack automatically each morning | **PRO** — see *Tier behavior* below |
| A "what you're missing" insights block inside the report | **OSS only** — see *Tier behavior* below |

The report ships **disabled by default**; you turn it on once you want the daily digest.

## Configuration

The Daily Report's knobs — whether it's on, when it runs, how long history is kept, and how often
the insights block appears — are treated as advanced settings in v1.0 rather than part of the stable
operator environment-variable allowlist, so they may change before they're promoted. See the
[API Reference](../../reference/index.md) for the current settings; the report is **off by default**.

Reading a stored report needs no extra configuration — the CLI talks to the same data the report API
serves:

```bash
baldur report                 # list recent reports
baldur report --date today    # show one day's report (or any YYYY-MM-DD)
```

## Tier behavior

The Daily Report runs in every tier, but *how you get it* and *what it contains* scope to the
features you have active.

- **In OSS**: Baldur generates the report from your real activity, keeps the rolling history, and you
  **read it on demand** — `baldur report` on the command line, or the report API / admin console.
  OSS already captures failed work and replays it when a dependency recovers, so the digest shows
  that activity — the dead-letter queue section and the auto-replay line are both OSS. What OSS does
  not do is *tune* that recovery for you, so the report also carries a **"what you're missing"
  insights block**: drawn entirely from your own production numbers, it estimates the impact the PRO
  features would have had — for example *N circuit-breaker trips with no automatic degradation*,
  *N operations captured in the dead-letter queue and replayed at a fixed batch size rather than one
  adapted to the recovering dependency*, or *N drift warnings you had to resolve by hand*. It's a
  directional estimate from your data, not a synthetic demo, and it appears on a cadence you control.

- **With PRO active**: the same report is **delivered to Slack automatically** each day — Baldur's
  notification transports ship with PRO, so in OSS the report is generated and stored but pushing it
  to a channel is a PRO capability. The "what you're missing" block disappears, because you are no
  longer missing it. The **"Automated Actions" section** — which on OSS carries the auto-replayed
  dead-letter batches — fills out with the rest of what Baldur did while you were away: canary
  rollouts and rollbacks, emergency-level changes, governance blocks, alongside richer sections fed
  by the PRO features. The same daily digest shifts from *"here's what you're missing"* to *"here's
  what was handled for you."*

### Sections by tier

Every section the digest can contain belongs to a tier, and a section appears only when its producing
feature is part of the tier you're running — so the digest never shows a section for a capability you
don't have. The sections the Slack digest can carry today:

| Section | What it covers | Tier |
|---------|----------------|------|
| `auto_processing` | Items auto-processed yesterday — archived, expired, recovered, purged | OSS |
| `alerts` | Config-drift warnings | OSS |
| `circuit_breaker` | Circuit-breaker transitions (opened / closed) | OSS |
| `errors` | Task failures and critical alerts | OSS |
| `custom` | Any custom counters you record | OSS |
| `shadow_pro` | The OSS-only "what you're missing" insights block | OSS |
| `dlq` | Dead-letter queue activity — new, auto-resolved, pending | OSS |
| `automated_actions` | Heading for the actions Baldur took for you | OSS |
| `auto_replay` | Dead-letter batches auto-replayed | OSS |
| `canary` | Canary rollouts completed and rolled back | PRO |
| `emergency` | Emergency-level changes | PRO |
| `governance` | Governance policy blocks | PRO |

Features that arrive after v1.0 add their own sections to the digest as they ship.

## See also

- [What is self-healing?](self-healing.md) — the activity the report summarizes
- [OSS vs PRO tier model](tier-model.md) — what the "what you're missing" block is comparing
- [Metrics](../oss/metrics.md) — the live, scrape-anytime view of the same healing activity
- [Unified Notification](../pro/unified-notification.md) — how the report reaches Slack with PRO
- [Getting Started](../../getting-started/index.md) — set Baldur up in five minutes
