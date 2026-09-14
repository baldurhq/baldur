# Getting Started

Get a Baldur-protected endpoint running. Every quickstart
starts with **zero infrastructure**: no Redis, no Docker, no environment
variables. Baldur's in-memory fallback makes `pip install` → `@protected` →
working code the whole first-run path.

The first code sample is always the marquee facade,
`@baldur.protected("name")`, which composes circuit breaker, retry, fallback,
and the dead-letter queue behind a single decorator. DLQ capture stores a
snapshot of the failing call's arguments so it can be replayed, so it is
[opt-in per call](../concepts/foundations/dlq-replay.md#why-capture-is-opt-in-per-call)
with `dlq=True` — the quickstarts show the flag in place.

## See it work first

Before wiring anything into your app, watch the whole loop run in one
process — no Redis, no database, no broker:

```bash
pip install "baldur-framework[celery]"
python -m baldur.scripts.demo_self_healing
```

A fake payment gateway dies mid-traffic. Every charge that fails is captured
with its arguments, the circuit breaker opens and rejects the rest instantly
(those are captured too), and when the gateway comes back the breaker closes
and every captured charge is replayed. The summary line at the end is computed
from what actually happened, so it is also a smoke test of your install.

## Pick your framework

- [Django](django.md)
- [FastAPI](fastapi.md)
- [Flask](flask.md)
- [Celery](celery.md) — background tasks (workers + scheduled jobs)

Each quickstart assumes you have used the target framework at least once, and
ends with a short "Going to production" appendix covering the one thing the
zero-config path leaves out: a shared cache backend for multi-worker
deployments.

## Compatibility

Baldur supports Python 3.11–3.13 and Django 4.2 / 5.2 LTS / 6.x.
