# Baldur

[![CI](https://github.com/baldurhq/baldur/actions/workflows/ci-oss-mirror.yml/badge.svg)](https://github.com/baldurhq/baldur/actions/workflows/ci-oss-mirror.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![PyPI](https://img.shields.io/pypi/v/baldur-framework.svg)](https://pypi.org/project/baldur-framework/)
[![Docs](https://img.shields.io/badge/docs-baldur.sh-1f6feb.svg)](https://baldur.sh)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/13522/badge)](https://www.bestpractices.dev/projects/13522)

**Baldur** is a self-healing reliability layer for Python applications. It puts
circuit breaker, retry, and fallback behind a single decorator, so a flaky
downstream stops cascading into your service — and it ships the operational
surface you need to actually run that in production: health checks, Prometheus
and OpenTelemetry metrics, graceful shutdown, and a built-in web console. The
core is framework-agnostic, with first-class adapters for Django, FastAPI,
Flask, and Celery.

## Why Baldur?

- **One decorator, whole pipeline.** `@baldur.protected("name")` composes
  circuit breaker, retry with backoff, timeout, fallback, and idempotency into
  one ordered pipeline — instead of hand-wiring three separate libraries and
  hoping they interact correctly under failure.
- **Zero-config start, production path built in.** Out of the box everything
  runs on an in-memory backend — no Redis, no env vars, no Docker. When you
  move to multiple workers, add Redis and the same code shares state across
  the fleet. Call sites never change.
- **Operate it, don't just import it.** A built-in web console shows every
  breaker's live state and gives you runtime on/off controls; health checks
  tell your load balancer the truth; metrics come standard.
- **Framework-native.** Django, FastAPI, Flask, and Celery adapters wire the
  cache, metrics, and lifecycle hooks at startup, so protection works with
  your framework's idioms rather than around them.

## Install

The Python package is `baldur` (you `import baldur`); the PyPI distribution is
`baldur-framework`.

```bash
pip install baldur-framework                 # framework-agnostic core
pip install baldur-framework[django]         # Django integration
pip install baldur-framework[fastapi]        # FastAPI integration
pip install baldur-framework[flask]          # Flask integration
pip install baldur-framework[celery]         # Celery task protection
pip install baldur-framework[redis]          # Redis-backed shared state
pip install baldur-framework[prometheus]     # Prometheus metrics
```

## Quick example

```python
import baldur


@baldur.protected("charge-customer")
def charge(order_id: str) -> dict:
    # Wrapped in a circuit breaker by default. With zero configuration this
    # runs on an in-memory fallback — no Redis, no env vars, no Docker.
    return payment_gateway.charge(order_id)
```

When the payment gateway starts failing, the breaker opens and your service
answers fast instead of stacking up timeouts. Need more than the default?
Compose the pipeline declaratively:

```python
@baldur.protected(
    "charge-customer",
    retry=True,                              # retry with exponential backoff
    timeout=5.0,                             # per-call time budget
    fallback=lambda: {"status": "queued"},   # graceful answer while OPEN
    idempotency_key="order_id",              # dedupe concurrent duplicates
)
def charge(order_id: str) -> dict:
    return payment_gateway.charge(order_id)
```

Sync and async callables are both supported — the decorator auto-detects
coroutine functions.

## What's in the box (OSS, Apache-2.0)

| Capability | What it gives you |
|------------|-------------------|
| [Circuit breaker](docs/concepts/oss/circuit-breaker.md) | Stops cascading failure; bounded half-open probes on recovery |
| [Retry with backoff](docs/concepts/oss/retry.md) | Exponential backoff with jitter and bounded attempts |
| [Fallback & composition](docs/concepts/foundations/composition.md) | One ordered pipeline for all resilience patterns |
| [Idempotency](docs/concepts/oss/idempotency.md) | Concurrent duplicate calls execute the side effect exactly once |
| [Health checks](docs/concepts/oss/health-check.md) | Liveness/readiness that reflect real dependency state |
| [Graceful shutdown](docs/concepts/oss/graceful-shutdown.md) | Drain in-flight work cleanly on restart and deploy |
| [Metrics](docs/concepts/oss/metrics.md) | Prometheus and OpenTelemetry, emitted by default |
| [System control](docs/concepts/oss/system-control.md) | Instant kill switch and dry-run mode for Baldur's automation — no redeploy |
| [Web console](docs/concepts/foundations/web-console.md) | Built-in operations console: live breaker state, controls, recovery |
| [Precomputed cache](docs/concepts/oss/precomputed-cache.md) | Health/status endpoints answer from a warm cache, so constant probing stays cheap |

## Baldur PRO

PRO adds the durable, fleet-level machinery on top of the same API — nothing in
the core gets relicensed or replaced. Highlights: durable
[dead-letter queue + replay](docs/concepts/pro/dlq-replay.md) (a failed
operation is stored, survives restarts, and is replayed once the dependency
recovers), hash-chained [audit trail](docs/concepts/pro/audit.md),
[unified notifications](docs/concepts/pro/unified-notification.md),
[emergency mode](docs/concepts/pro/emergency-mode.md),
[bulkhead isolation](docs/concepts/pro/bulkhead.md),
[adaptive throttling](docs/concepts/pro/throttle.md),
[canary recovery](docs/concepts/pro/canary-recovery.md),
[governance gates](docs/concepts/pro/governance.md), and a
[meta-watchdog](docs/concepts/pro/meta-watchdog.md) that watches Baldur itself.

See the full [OSS vs PRO capability matrix](docs/concepts/oss-vs-pro.md) and
[pricing](https://baldur.sh/pricing/).

## Documentation

Full documentation lives at **<https://baldur.sh>**.

- [What is Baldur?](docs/what-is-baldur.md) — the problem it solves and how
- Getting started: [Django](docs/getting-started/django.md) ·
  [FastAPI](docs/getting-started/fastapi.md) ·
  [Flask](docs/getting-started/flask.md) ·
  [Celery](docs/getting-started/celery.md)
- [Concept guides](https://baldur.sh) — one page per capability, linked
  throughout this README
- [API reference](https://baldur.sh/reference/)
- [Troubleshooting](docs/troubleshooting.md)
- [Compatibility](docs/compatibility.md)

## Using Baldur with AI assistants

Building with an AI coding assistant (Claude Code, Cursor, Copilot, Codex)? Run
`baldur init-ai` in your repo to drop an `AGENTS.md` (read by Cursor, Copilot,
and Codex) plus a `CLAUDE.md` that imports it for Claude Code — together they
teach the assistant to reach for `@baldur.protected("name")` instead of
hand-rolling a circuit breaker. See
[Using Baldur with AI assistants](docs/getting-started/ai-assistants.md).

## Compatibility

| Component | Minimum | Tested in CI |
|-----------|---------|--------------|
| Python | 3.11 | 3.11 · 3.12 · 3.13 |
| Django | 4.2 | 4.2 LTS · 5.2 LTS · 6.0 |
| FastAPI | 0.100 | latest ≥ floor (smoke) |
| Flask | 2.3 | latest ≥ floor (smoke) |
| Celery | 5.3 | 5.4 |
| Redis server | — | 7.x |

See [Compatibility](docs/compatibility.md) for the full matrix, the diagonal
Python × Django test grid, and the version support policy.

## License

Baldur is released under the Apache License 2.0 — see [LICENSE](LICENSE) and
[NOTICE](NOTICE).

## Contributing

Contributions are welcome under the Apache License 2.0. Pull requests are
accepted through a sign-off-based [DCO](https://developercertificate.org/) flow —
see [CONTRIBUTING.md](CONTRIBUTING.md) for the full model.

- **Bugs / feature requests / docs** → open an issue or a pull request.
- **Security** → see [SECURITY.md](SECURITY.md) (no public issues for vulnerabilities).
- **Usage questions / commercial** → `support@baldur.sh`.
