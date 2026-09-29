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

The Daily Report makes the invisible visible. A digest answers the question every operator
actually has (*is everything OK?*) without anyone opening a dashboard. A day with nothing recorded
produces no report at all and a quiet one stays short, so a report that suddenly runs long is itself
the signal that yesterday was busy. For a small team with nobody watching screens, it's the
cheapest possible proof that your safety net is doing its job.

## How it works in Baldur

Baldur builds a report for the previous day from the activity it recorded, stores it, and keeps a
rolling history (about three months by default) that you can query by date. The build runs on
Baldur's built-in scheduler, once when the process running the scheduler starts and every 24 hours
after that, so a restart runs it again for the previous day. Each host runs its own scheduler by
default: with several hosts, every one of them builds the report, so keep the job on one host by
setting `BALDUR_SCHEDULER_DISABLED_JOBS=daily_report` on the others.

The digest, the formatted text that PRO posts, is **adaptive** to stay readable. (`baldur report`
and the report API return the stored report instead: every counter as JSON, zeros included.)

- Every digest opens with a core summary line, so a delivered report always tells you it ran.
- Detail sections (auto-processing of archived/expired/recovered items, circuit-breaker activity,
  errors, and the like) appear **only when they have something to report**. A day whose recorded
  activity falls in none of those sections collapses to a single line: *"All quiet — 0 processed,
  0 alerts."*
- A day with **nothing recorded at all** (no activity and no pending backlog) is skipped outright:
  no digest is sent and nothing is stored for that date.
- PRO posts the digest at a priority derived from its contents: *info* for a clean day, *medium*
  when tasks failed, *critical* when a critical alert was recorded.

How you *get* the report depends on your tier — you either pull it on demand or have it delivered:

| What you observe | When it happens |
|------------------|-----------------|
| `baldur report` or the report API returns stored reports as JSON counts (the API also serves a multi-day trend) | Any time — the report is generated and stored in every tier |
| An *"All quiet — 0 processed, 0 alerts"* digest | Something was recorded that day, but none of it falls in a section the digest shows |
| No digest (and no stored report) for a date | Nothing was recorded that day — generation skips it entirely |
| Expanded detail sections | Those events actually occurred that day |
| The digest pushed to your Slack automatically each day | **PRO** — see *Tier behavior* below |
| A "what you're missing" insights block inside the report | **OSS only** — see *Tier behavior* below |

The report ships **disabled by default**; you turn it on once you want the daily digest.

## Configuration

The Daily Report's knobs (whether it's on, when it runs, how long history is kept, and how often
the insights block appears) are advanced settings rather than part of the stable operator
environment-variable allowlist, so they may change before they're promoted, and the published
reference does not list them yet. The report is **off by default**.

Reading a stored report needs no configuration of its own: the CLI reads the same store the report
API serves. Run it with your app's `BALDUR_*` environment, and from the app's working directory
when the store is the default local file, whose path is relative:

```bash
baldur report                      # list recent reports
baldur report --date YYYY-MM-DD    # show one day's report (the newest covers yesterday)
```

## Tier behavior

The Daily Report runs in every tier, but *how you get it* and *what it contains* scope to the
features you have active.

- **In OSS**: Baldur generates the report from your real activity, keeps the rolling history, and
  you **read it on demand**: `baldur report` on the command line, or the report API. OSS already
  captures failed work at `dlq=True` call sites and, once its
  [automatic replay is set up](dlq-replay.md#closing-the-loop-making-automatic-replay-actually-drain)
  (a registered replay handler and a Celery worker, at minimum), replays it when a dependency
  recovers, so the report shows that activity: the dead-letter queue section and the auto-replay
  line are both OSS. What OSS does not do is *tune* that recovery for you, so the report also
  carries a **"what you're missing" insights block**: drawn entirely from your own production
  numbers, it estimates the impact the PRO features would have had, for example *N circuit-breaker
  trips with no automatic degradation* (N counts every open and every close, so one outage that
  recovers counts twice), *N operations captured in the dead-letter queue and replayed at a fixed
  batch size rather than one adapted to the recovering dependency*, or *N drift warnings you had to
  resolve by hand*. It's a directional estimate from your data, not a synthetic demo, and it
  appears on a cadence you control.

- **With PRO active**: the same report is **delivered to Slack automatically** each day. The
  transport it goes out on ships with PRO, so in OSS the report is generated and stored but pushing
  it to a channel is a PRO capability. The "what you're missing" block disappears, because you are
  no longer missing it. The **"Automated Actions" section**, which on OSS carries the auto-replayed
  dead-letter batches, fills out with the rest of what Baldur did while you were away: canary
  rollouts and rollbacks, and emergency-level changes. The same daily digest shifts from
  *"here's what you're missing"* to *"here's what was handled for you."*

### Sections by tier

Every section the digest can contain belongs to a tier, and a section appears only when its producing
feature is part of the tier you're running — so the digest never shows a section for a capability you
don't have. The sections a report can carry today (on OSS their data sits in the stored report's
counts; with PRO the posted digest shows them as sections):

| Section | What it covers | Tier |
|---------|----------------|------|
| `auto_processing` | Items auto-processed yesterday: recovered by auto-replay, and with PRO also archived, expired and purged | OSS |
| `alerts` | SLA-drift warnings: dead-letter recovery running close to or past its time target | OSS |
| `circuit_breaker` | Circuit-breaker transitions (opened / closed) | OSS |
| `errors` | Task failures and critical alerts | OSS |
| `custom` | Reserved for custom counters; nothing records them yet, so it does not appear | OSS |
| `shadow_pro` | The OSS-only "what you're missing" insights block | OSS |
| `dlq` | Dead-letter queue activity: new, resolved (every resolution, manual ones included), pending | OSS |
| `automated_actions` | Heading for the actions Baldur took for you | OSS |
| `auto_replay` | Dead-letter batches auto-replayed | OSS |
| `canary` | Canary rollouts completed and rolled back | PRO |
| `emergency` | Emergency-level changes | PRO |
| `governance` | Governance blocks of the few actions that record them here (a blocked dead-letter replay does not) | PRO |

New features add their own sections to the digest as they ship.

## See also

- [What is self-healing?](self-healing.md) — the activity the report summarizes
- [OSS vs PRO tier model](tier-model.md) — what the "what you're missing" block is comparing
- [Metrics](../oss/metrics.md) — the live, scrape-anytime view of the same healing activity
- [Unified Notification](../pro/unified-notification.md) — how the report reaches Slack with PRO
- [Getting Started](../../getting-started/index.md) — set Baldur up in five minutes
