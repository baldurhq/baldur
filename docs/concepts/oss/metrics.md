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
- **Cardinality stays bounded.** The Cardinality Guard labels requests by route rather than by raw
  path and caps domain labels, so the series count follows what your code declares, not what your
  traffic sends (one Django exception is spelled out below).
- **No impossible readings.** Counts that should never go negative (like "items currently pending")
  are clamped at zero, so a restart can't surface a `-1` on your dashboard.

## How it works in Baldur

**Metrics are recorded automatically.** When a circuit breaker changes state, a retry runs out of
attempts, a dead-letter item is created or resolved, or a replay finishes, Baldur updates the
matching metric for you. You write no recording code: the numbers appear because the self-healing
layer is doing its job.

**Your monitoring system scrapes them in the standard format.** Baldur exposes its metrics as a
Prometheus text-exposition page (served byte-exact for scrapers) plus a JSON view of the
control-API metrics. Baldur's built-in admin server serves the page at `/prometheus` whichever
framework you run, and without one. It listens on port 9090 and on localhost only by default, so a
Prometheus server on another host or pod cannot reach it until you open the admin server up; the
[admin server settings](../../reference/env-vars.md#admin-server) cover the port and the key that a
non-localhost bind requires. On Django, Baldur's REST URLs (the `[django-api]` extra) also serve the
page at `prometheus/` under wherever you include them. Flask and FastAPI apps get no route of their
own. Either way you point your existing Prometheus server at it, with nothing Baldur-specific to
learn on the scraper side.

**You can instrument your own functions too.** Two decorators cover the common cases:

| Decorator | What it records |
|-----------|-----------------|
| `@track_counter` | Counts calls that return; pass `on_failure=True` to count calls that raise as well, and add `on_success=False` to count only those |
| `@track_execution_time` | Records how long a function takes, as a histogram |

**The Cardinality Guard keeps traffic-driven labels bounded.** This is the part that makes the
metrics safe to leave on in production:

| What you observe | When it happens |
|------------------|-----------------|
| `/api/users/123` and `/api/users/456` land on one series labelled with their route, such as `/api/users/<int:pk>/` | HTTP request metrics are labelled with the route your framework matched, not the raw path, so per-ID values (UUIDs included) don't each spawn a new time series |
| Unrouted paths collapse to one `UNMATCHED_ROUTE` series | A scanner hitting thousands of random URLs can't inflate cardinality: the endpoint label takes one value per registered route, plus this one. On Django, paths under `/health`, `/ready`, `/metrics` and `/favicon.ico` are the exception and keep their raw path |
| New domain names past the cap share one `OTHER_DOMAIN` label | Domain labels (the per-service name the retry and dead-letter metrics are filed under) are capped at 50 by default, Baldur's own built-in domains included; names past the cap are recorded under that one label instead of unbounded new series |
| Odd characters in a domain name become `_`, and a name that still isn't a valid identifier lands on `OTHER_DOMAIN` | Domain names are lowercased and sanitized (`Pay-API.v2` is recorded as `pay_api_v2`); one longer than 64 characters or starting with a digit is refused and shares the `OTHER_DOMAIN` label |
| A "pending" gauge shows `0`, never a negative number | Gauges that should never go below zero are clamped, so a restart can't surface an impossible reading |

The guard does not rewrite the `name` you pass to `protect()`. The circuit-breaker series and the
per-call `protect()` series carry that name verbatim, one series per distinct name, so build names
from fixed strings such as `"payments"`, never from an order or user ID.

**It degrades safely when Prometheus isn't installed.** Metric collection rides on an optional
dependency. If it isn't installed, recording calls quietly become no-ops and the scrape endpoint
returns a `503`; your application keeps running exactly as before. Metrics are an observability
layer, never a thing that can take your app down.

**A metric only carries data when its subsystem runs.** The circuit-breaker, retry,
dead-letter-queue, replay and HTTP request metrics are recorded out of the box on OSS; series that
report on PRO subsystems (adaptive throttle, emergency mode, canary rollouts) start carrying data
once the PRO package is installed and those services run.

## Configuration

Metrics works out of the box and is on by default: the resilience events start being recorded as
soon as Baldur is initialized. The one thing you add is the optional Prometheus dependency, so the
text-exposition endpoint has something to render:

```bash
pip install "baldur-framework[prometheus]"
```

(The quotes matter in `zsh`/`fish`, which would otherwise treat the brackets as a glob.)

The Metrics feature itself has no variables in the operator-tunable allowlist. Baldur's own metric
names always start with `baldur_`, and the export backend and Cardinality Guard limits ship with
production-safe defaults, so there is nothing you need to set for the common case.

Two admin-server variables decide where the scrape page is reachable. `BALDUR_ADMIN_PORT` (default
`9090`) moves it, and `BALDUR_ADMIN_ENABLED=false` removes it, which leaves a Flask, FastAPI or
framework-free app with no scrape page at all. Prometheus itself also defaults to port 9090, so on a
host that runs both, move one of them: Baldur does not start its admin server on a port that is
already taken, and your app runs on without the page. The complete operator-tunable list lives in
the [environment variables reference](../../reference/env-vars.md).

## See also

- [Getting Started](../../getting-started/index.md) — set it up
- [Control API & Metrics Reference](../../reference/services/control_and_metrics.md) — full options and signatures
- [Circuit Breaker](circuit-breaker.md) — one of the subsystems whose state these metrics report on
- [Environment Variables](../../reference/env-vars.md) — the complete operator-tunable list
