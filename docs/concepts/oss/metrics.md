# Metrics

> Ready-made, Prometheus-format metrics for everything the self-healing layer does — auto-recorded,
> with a built-in guard that stops them from blowing up your monitoring bill.

## What is it?

A **metric** is a number your monitoring system samples over time so you can see what your
application is actually doing: how many requests failed, how long they took, how full a queue is.
Think of the gauges on a car dashboard (speed, fuel, engine temperature), except the readings are
collected every few seconds and kept as history you can chart and alert on. The de-facto standard
for collecting them is **Prometheus**, which periodically "scrapes" a plain-text page your app
exposes and stores the numbers.

The catch is that the self-healing machinery (circuit breakers, retries, the dead-letter queue,
replays) is exactly the part you most want visibility into, and exactly the part you would
otherwise have to instrument by hand. In Baldur this is the **Metrics** feature: a ready-made set of
100+ Prometheus metrics for the whole self-healing layer, recorded automatically, plus a
**Cardinality Guard** that keeps the metric volume from spiralling out of control.

## Why it matters

Run self-healing without metrics and it is a black box. You cannot see how often a circuit trips,
how many retries are happening, how deep the dead-letter queue is, or whether replays are
succeeding, until an incident forces you to find out the hard way. Wiring all of that up by hand is
tedious, and easy to get subtly wrong.

There is a second, less obvious trap. The naive way to label metrics (one label value per user ID,
per order ID, or per raw URL path) quietly creates a brand-new time series for every distinct
value. A single counter labelled by raw URL becomes millions of series the first time a scanner
walks your site. This is a **cardinality explosion**, and it is the classic way to make a Prometheus
bill (and query latency) blow up overnight.

Baldur's Metrics feature removes both problems:

- **Visibility for free.** The resilience events you care about are recorded the moment they
  happen, with no instrumentation code on your side.
- **Cardinality stays bounded.** The Cardinality Guard normalizes and caps labels so the number of
  time series stays flat no matter how your traffic or your attackers behave.
- **No impossible readings.** Counts that should never go negative (like "items currently pending")
  are clamped at zero, so a restart can't surface a `-1` on your dashboard.

## How it works in Baldur

**Metrics are recorded automatically.** When a circuit breaker changes state, a retry runs out of
attempts, a dead-letter item is created or resolved, or a replay finishes, Baldur updates the
matching metric for you. You write no recording code — the numbers appear because the self-healing
layer is doing its job.

**Your monitoring system scrapes them in the standard format.** Baldur exposes its metrics as a
Prometheus text-exposition page (served byte-exact for scrapers) plus a JSON view of the
control-API metrics. The page mounts through your web framework's normal URL routing, and is also
available on Baldur's built-in admin console for deployments that run without a web framework. You
point your existing Prometheus server at it — nothing Baldur-specific to learn on the scraper side.

**You can instrument your own functions too.** Two decorators cover the common cases:

| Decorator | What it records |
|-----------|-----------------|
| `@track_counter` | Counts how often a function runs (optionally only on success, or only on failure) |
| `@track_execution_time` | Records how long a function takes, as a histogram |

**The Cardinality Guard keeps the series count flat.** This is the part that makes the metrics safe
to leave on in production:

| What you observe | When it happens |
|------------------|-----------------|
| `/api/users/123` and `/api/users/456` collapse to a single `/api/users/{id}` series | URL paths are normalized so per-ID values don't each spawn a new time series (UUIDs collapse the same way) |
| Unrecognized paths collapse to one `UNMATCHED_ROUTE` series | A scanner hitting thousands of random URLs can't inflate cardinality — only real, routable paths are tracked |
| A flood of new "domains" collapses to a single fallback label | Domain labels are capped (50 by default); anything past the cap is recorded under one shared label instead of unbounded new series |
| The oldest tracked endpoint quietly drops off | The number of distinct endpoints is capped (500 by default) and evicted oldest-first, so the series count has a hard ceiling |
| Odd characters in a label become `_`, long values are truncated | Label values are sanitized so they stay valid and bounded for Prometheus |
| A "pending" gauge shows `0`, never a negative number | Gauges that should never go below zero are clamped, so a restart can't surface an impossible reading |

**It degrades safely when Prometheus isn't installed.** Metric collection rides on an optional
dependency. If it isn't installed, recording calls quietly become no-ops and the scrape endpoint
returns a `503` — your application keeps running exactly as before. Metrics are an observability
layer, never a thing that can take your app down.

- **DLQ-depth and replay metrics are PRO.** The circuit-breaker, retry, and cardinality-guard
  metrics are OSS and recorded out of the box; the dead-letter-queue depth and replay-success
  metrics report on the PRO subsystems, so they only carry data once the PRO package is installed.

## Configuration

Metrics works out of the box and is on by default — the resilience events start being recorded as
soon as Baldur is initialized. The one thing you add is the optional Prometheus dependency, so the
text-exposition endpoint has something to render:

```bash
pip install "baldur-framework[prometheus]"
```

(The quotes matter in `zsh`/`fish`, which would otherwise treat the brackets as a glob.)

There are no metrics variables in the operator-tunable allowlist — the prefix, backend, and
Cardinality Guard limits ship with production-safe defaults and are treated as advanced settings for
now, so there is nothing you need to set for the common case. The complete operator-tunable list
lives in the [environment variables reference](../../reference/env-vars.md).

## See also

- [Getting Started](../../getting-started/index.md) — set it up
- [Control API & Metrics Reference](../../reference/services/control_and_metrics.md) — full options and signatures
- [Circuit Breaker](circuit-breaker.md) — one of the subsystems whose state these metrics report on
- [Environment Variables](../../reference/env-vars.md) — the complete operator-tunable list
