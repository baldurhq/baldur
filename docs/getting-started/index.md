# Getting Started

Get a Baldur-protected endpoint running. Every quickstart
starts with **zero infrastructure**: no Redis, no Docker, no environment
variables. Baldur's in-memory fallback makes `pip install` → `@protected` →
working code the whole first-run path.

The first code sample is always the marquee facade,
`@baldur.protected("name")`, which composes circuit breaker, retry, and
fallback behind a single decorator.

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
