---
title: FAQ
description: >-
  Common questions about Baldur — what it is, how it compares, what it costs
  your service, and when you need PRO.
---

# FAQ

Questions that come up when evaluating Baldur for the first time.
For operational issues (install errors, unexpected behavior, recovery steps),
see [Troubleshooting](troubleshooting.md).

---

## Is this production-ready?

Yes. The core resilience patterns (circuit breaker, retry, fallback,
dead-letter queue, idempotency) are tested under sustained load with
Sentinel failover, and the framework ships with architecture fitness gates
that run on every commit. The validated performance envelope is documented
on the [resource budget](concepts/foundations/resource-budget.md) page with
methodology and raw numbers — we publish only what we have measured, and
label everything else a target.

That said, Baldur is in early access. The API surface is stable, but minor
versions may include breaking changes (with a changelog entry). If you are
evaluating for a critical path, start with a single `@baldur.protected`
endpoint and expand from there.

---

## How is this different from tenacity / backoff / retry libraries?

Those libraries give you **one pattern** — retry with backoff. Baldur
composes retry with a circuit breaker, fallback, dead-letter queue,
idempotency guard, health checks, and observability into a single
decorator. The difference shows up when a dependency stays down:

- A retry library keeps hammering it.
- Baldur's circuit breaker trips and fails fast; a fallback serves a
  safe answer, or — when no fallback is set — the failed work is
  captured in a DLQ for later replay. After a cool-down the breaker
  lets a few trial calls through and closes again once they succeed.

If you already use tenacity and want to keep it, Baldur includes a
[tenacity bridge](concepts/foundations/composition.md) that routes
tenacity-managed calls through the rest of the pipeline.

---

## How is this different from Resilience4j or Polly?

Same idea, different ecosystem. Resilience4j is Java, Polly is .NET —
Python has had no equivalent. Baldur fills that gap with the same
pattern set (circuit breaker, retry, bulkhead, rate limiter, fallback)
plus a durable dead-letter queue and a built-in web console, designed
for Python's concurrency model and deployment patterns (Django, FastAPI,
Flask, gunicorn, Celery).

---

## Why not a service mesh or a sidecar?

A service mesh (Istio, Linkerd) operates at the **network layer** — it
can retry and circuit-break HTTP calls between services. It cannot:

- Retry at the **application level** (e.g., re-queue a Celery task).
- Apply **domain-aware fallbacks** (return a cached price instead of an
  error).
- Capture failed work in a **dead-letter queue** for replay.
- Enforce **idempotency** on business operations.

Baldur runs **inside your process** and sees application context that a
proxy never can. If you already have a mesh, Baldur complements it —
see [Baldur and your service mesh](concepts/foundations/service-mesh.md).

---

## What is the performance overhead?

Measured on a Django+PostgreSQL service behind gunicorn: **+1.1%**
per-request throughput overhead, measured below saturation.

The decorator itself costs tens of microseconds per call on the
in-memory backend.

Push a single synchronous worker to its throughput ceiling and the cost
is higher, but our figure for that is **withdrawn pending
re-measurement** — we would rather publish nothing there than a number
we have evidence against. The
[resource budget](concepts/foundations/resource-budget.md) page explains
what happened to it, what still stands, and the exact topology
everything was measured on.

These numbers are topology-specific — your results will vary with worker
count, concurrency model, and payload shape.

---

## Do I need Redis?

No. Baldur runs **zero-config on an in-memory backend** — no Redis, no
Docker, no environment variables. That is the default for single-process
deployments.

Point it at Redis when you need **cross-worker state sharing** (circuit
breaker state, rate-limit counters, DLQ persistence across restarts).
For production multi-worker deployments, Redis is recommended.
PRO supports Redis Sentinel for high availability.

---

## What frameworks does it support?

Django, FastAPI, Flask, and Celery — each with a dedicated adapter that
wires into the framework's startup and shutdown lifecycle. The core is
framework-agnostic, so plain Python scripts and CLIs work too. See
[Compatibility](compatibility.md) for the tested version matrix.

---

## What does the `@baldur.protected` decorator actually do?

It wraps your function in a composed resilience pipeline:

1. **Idempotency check** — deduplicates the call.
2. **Circuit breaker** — if the breaker is open, fail fast or fall back.
3. **Retry** — transient failures are retried with backoff.
4. **Fallback** — if the call cannot succeed, your fallback runs.
5. **Dead-letter queue** — a failure nothing else could save is captured
   for later replay.

By default only the circuit breaker is on — every other stage is
something you opt in with a keyword argument, and Baldur owns the
layering so the stages never undercut each other.
One decorator, one name string — that is the integration surface.
See [Composing with @baldur.protected](concepts/foundations/composition.md).

---

## How much does it cost?

The open-source core is **free forever** under the Apache License 2.0.

**Baldur PRO** starts at **$149/month** (or $1,490/year). It adds audit
trail, emergency mode, governance gates, unified notifications, canary
recovery, adaptive throttle, meta-watchdog, and DLQ operations at scale.
PRO is a Python package installed alongside the core — not a hosted
service.

See [Pricing](pricing.md) for the full comparison.

---

## When do I need PRO?

The OSS core covers the resilience patterns themselves. Consider PRO
when you need:

- **Audit trail** — tamper-evident logs for compliance or incident review.
- **Emergency mode** — one switch to shed load across all protected
  endpoints.
- **Governance gates** — enforce policies (approval, change windows)
  before circuit breaker overrides take effect.
- **Unified notifications** — Slack, PagerDuty, or webhook alerts from
  one configuration.
- **DLQ at scale** — batch replay, durable archival, and console-driven
  operations for thousands of entries.

The [tier model](concepts/foundations/tier-model.md) page has a decision
checklist.

---

## Is this a hosted service? Does it phone home?

No, and no. Baldur is a Python package that runs entirely inside your
process. It makes no calls to us and sends no telemetry — the only
network connections are the ones you configure yourself (your Redis,
your OpenTelemetry collector, your notification channels). Your data
never leaves your network.
