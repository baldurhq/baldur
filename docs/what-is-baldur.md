---
title: What is Baldur?
description: >-
  Orientation for new readers — what Baldur is, what it does when a dependency
  fails, where it sits in your stack, and where to go next.
---

# What is Baldur?

> A self-healing reliability layer for Python applications — circuit breaker,
> retry, fallback, and dead-letter queue behind one decorator.

Every application depends on things that fail: a payment gateway times out, a
database gets slow, a third-party API starts returning errors. Baldur wraps the
calls you care about in a composed resilience pipeline, so those failures are
contained, retried, and recovered from automatically — instead of becoming
customer-visible errors and after-hours pages.

```python
import baldur


@baldur.protected("charge-customer")
def charge(order_id: str) -> dict:
    return payment_gateway.charge(order_id)
```

That decorator is the whole integration. With zero configuration it runs on an
in-memory backend — no Redis, no Docker, no environment variables.

## What it does when a call fails

- **Stops hammering a dependency that is down.** A circuit breaker trips after
  repeated failures and fails fast until recovery probes confirm the
  dependency is healthy again.
- **Retries what deserves retrying.** Transient errors are retried with
  backoff, so a failure that would succeed on the next attempt never reaches
  your users.
- **Falls back to a safe answer.** When the call cannot succeed, your fallback
  runs instead of an exception propagating.
- **Sets work aside instead of dropping it** *(PRO)*. Failed work lands in a
  durable dead-letter queue and can be replayed — from code or from the web
  console — once the dependency recovers.

The concept behind all four, and why hand-rolling them in every project goes
wrong, is covered in
[What is self-healing?](concepts/foundations/self-healing.md).

## Where it sits in your stack

Baldur is a **library inside your process** — `pip install`, decorate, done.
It is not a proxy, not a sidecar, and not a hosted service; the framework runs
entirely inside your own application, and nothing about your deployment
changes.

- **Framework-agnostic core** with adapters for Django, FastAPI, Flask, and
  Celery.
- **Zero-config by default**: in-memory state for a single process; point it
  at Redis when you scale to multiple workers.
- **Operable from the browser**: a built-in web console shows what is failing
  and lets you recover — no separate deployment.
- **Observable**: Prometheus metrics and OpenTelemetry traces out of the box.

Already running a service mesh? Baldur complements it rather than competing
with it — see
[Baldur and your service mesh](concepts/foundations/service-mesh.md).

## OSS and PRO

The core is free and Apache-2.0 licensed (`pip install baldur-framework`), and
covers the resilience patterns themselves: circuit breaker, retry, fallback,
idempotency, health checks, metrics, and the web console. **Baldur PRO** is a
separate package you add on top for critical workloads: a durable dead-letter
queue with replay, audit trail, emergency mode, governance gates, unified
notifications, and more. PRO adds capability — it never replaces or
relicenses anything in the core.

See [the tier model](concepts/foundations/tier-model.md) for how to decide
which tier you need, the
[capability matrix](concepts/oss-vs-pro.md) for the feature-by-feature map,
and [Pricing](pricing.md) for plans.

## Where to go next

- [Getting Started](getting-started/index.md) — a protected endpoint in your
  framework, starting from zero infrastructure.
- [What is self-healing?](concepts/foundations/self-healing.md) — the concept
  behind the library.
- [Web Console](concepts/foundations/web-console.md) — operate and recover
  from the browser.
- [Pricing](pricing.md) — the OSS core is free forever; PRO plans start at
  $149/month.
