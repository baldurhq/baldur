# Baldur

[![CI](https://github.com/baldurhq/baldur/actions/workflows/ci-oss-mirror.yml/badge.svg)](https://github.com/baldurhq/baldur/actions/workflows/ci-oss-mirror.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![PyPI](https://img.shields.io/pypi/v/baldur-framework.svg)](https://pypi.org/project/baldur-framework/)
[![Docs](https://img.shields.io/badge/docs-baldur.sh-1f6feb.svg)](https://baldur.sh)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/13522/badge)](https://www.bestpractices.dev/projects/13522)

**English** | [한국어](README.ko.md)

> **This project is complete.** What was built, what the numbers said, and what survived: [retrospective (Korean)](POSTMORTEM.ko.md).

**An API you depend on goes down for an hour. What happens to your app?**

Requests hang until they time out, every worker fills up, and the jobs that
failed in that hour are gone. Whether it's OpenAI, your payment provider, or
your email service — Baldur fixes all three with one decorator, for Python
services that don't have anyone on call.

```python
import baldur


@baldur.protected("summarize", dlq=True, timeout=60.0)
def summarize(doc_id: str) -> str:
    return llm_api.summarize(doc_id)
```

No Redis, no Docker, no config to start: that decorator runs in-memory until
you go multi-process.

When the provider dies — or just gets slow — mid-traffic:

- **Your app keeps answering.** A hang becomes a failure at the 60-second
  bound, the circuit breaker opens, and calls fail fast — so a slow provider
  doesn't take every worker down with it. The endpoints that don't need it
  keep working.
- **Failed jobs are kept, not lost.** Every call that failed for good is
  captured with its arguments and listed in the built-in console.
- **They come back.** A small replay handler tells Baldur how to re-run one;
  replay the parked jobs from the console with a click, or automatically when
  the provider recovers — opt-in, with a Celery worker.

Django, FastAPI, Flask, and Celery adapters included.

![Terminal demo: the payment gateway becomes unreachable mid-traffic — five charges fail after their retries and the breaker trips, two more are rejected on the spot, all seven are captured, and on recovery Baldur replays all seven. Zero lost.](https://raw.githubusercontent.com/baldurhq/baldur/main/.github/assets/demo-self-healing.gif)

*The shipped demo's dependency is a payment gateway: it goes unreachable
mid-traffic, seven charges are captured with their arguments, and all seven are
replayed on recovery. Zero lost. Same loop for any call — a real run, with the
breaker states and DLQ tallies read live from the framework. The decorator
itself is `pip install baldur-framework` and nothing else; the demo adds the
`celery` extra for its in-process stand-in worker — still one process, no
Redis, no broker. Run it yourself:*

```bash
pip install "baldur-framework[celery]"
python -m baldur.scripts.demo_self_healing
```

**Already using your SDK's retries?** Keep them. Baldur doesn't replace retry —
it adds what retry can't: a breaker so one incident doesn't cost every request
its retries, one wall-clock bound on what the caller waits, a fallback, and the
capture-and-replay no retry library gives you.

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

## The same decorator, any dependency

A payment gateway, your database, an email provider — the call site never
changes:

```python
@baldur.protected("charge-customer", dlq=True)
def charge(order_id: str, amount_cents: int) -> dict:
    # Circuit breaker by default; dlq=True parks the call if it fails for
    # good, with its arguments, and replays it once the gateway recovers.
    return payment_gateway.charge(order_id, amount_cents)
```

When the gateway dies, the breaker opens and your service answers fast instead
of stacking up timeouts; the charges that failed on the way out wait in the
dead-letter queue and come back when it closes. (Replay is for work that failed
on the way out — never for a business rejection, and never for a checkout the
customer already walked away from:
[where that line sits](docs/concepts/foundations/dlq-replay.md).)

Need more than the default? Compose the pipeline declaratively:

```python
@baldur.protected(
    "summarize",
    timeout=30.0,                            # one bound on what the caller waits
    fallback=lambda: last_good_summary(),    # graceful answer while OPEN
    idempotency_key="doc_id",                # a redelivered job pays once
)
def summarize(doc_id: str) -> str:
    return llm_api.summarize(doc_id)
```

**Notice what isn't there: `retry=`.** Your SDK almost certainly retries
already — `anthropic` and `openai` default to two attempts with backoff, boto3
has an adaptive mode — and it retries better than a generic wrapper can,
because it knows which status codes are worth another attempt and honours
`retry-after`. Keep it. What no SDK gives you is the rest: a breaker, so a
provider incident doesn't mean *every* request pays its retries before failing;
one wall-clock bound on what your caller waits, retries included (an SDK's own
worst case is `timeout × (max_retries + 1)` — 30 minutes at `anthropic`'s
defaults); a fallback; and a dedup key that survives a job redelivery the SDK
never sees. `retry=True` is there for the calls that don't retry themselves.

Sync and async callables are both supported — the decorator auto-detects
coroutine functions.

## What's in the box (OSS, Apache-2.0)

| Capability | What it gives you |
|------------|-------------------|
| [Circuit breaker](docs/concepts/oss/circuit-breaker.md) | Stops cascading failure; bounded half-open probes on recovery |
| [Retry with backoff](docs/concepts/oss/retry.md) | Exponential backoff with jitter and bounded attempts |
| [Fallback & composition](docs/concepts/foundations/composition.md) | One ordered pipeline for all resilience patterns |
| [Idempotency](docs/concepts/oss/idempotency.md) | Concurrent duplicate calls execute the side effect exactly once |
| [Bulkhead isolation](docs/concepts/foundations/bulkhead.md) | Each dependency gets a fixed slice of concurrency, so one slow dependency can't drain every worker |
| [Dead-letter queue + replay](docs/concepts/foundations/dlq-replay.md) | A call that fails for good is captured with its context and replayed once the dependency recovers |
| [Health checks](docs/concepts/oss/health-check.md) | Liveness/readiness that reflect real dependency state |
| [Graceful shutdown](docs/concepts/oss/graceful-shutdown.md) | Drain in-flight work cleanly on restart and deploy |
| [Metrics](docs/concepts/oss/metrics.md) | Prometheus and OpenTelemetry, emitted by default |
| [System control](docs/concepts/oss/system-control.md) | Instant kill switch and dry-run mode for Baldur's automation — no redeploy |
| [Web console](docs/concepts/foundations/web-console.md) | Built-in operations console: live breaker state, controls, recovery |
| [Precomputed cache](docs/concepts/oss/precomputed-cache.md) | Health/status endpoints answer from a warm cache, so constant probing stays cheap |

The read path heals the same way. Here a Django app under live HTTP traffic
(recorded from a demo harness driving it) loses its network path to Redis for
21 seconds — every request keeps returning 200 off the in-memory cache tier,
and the Redis tier resyncs itself on recovery:

![Terminal demo: a Django app keeps serving 200s through a 21-second Redis outage](https://raw.githubusercontent.com/baldurhq/baldur/main/.github/assets/redis-dies-app-survives.gif)

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

See [Compatibility](docs/compatibility.md) for the full matrix, the
Python × Django test grid, and the version support policy.

## Running this across a fleet?

Baldur PRO adds the fleet-level machinery on top of the same API — nothing in
the core gets relicensed or replaced:
[DLQ at scale](docs/concepts/foundations/dlq-replay.md) (batch replay from the
console, success-rate-driven pacing, a disk-durable outbox, and archive/purge
retention), a hash-chained [audit trail](docs/concepts/pro/audit.md),
[unified notifications](docs/concepts/pro/unified-notification.md),
[emergency mode](docs/concepts/pro/emergency-mode.md),
[bulkhead thread-pool isolation](docs/concepts/foundations/bulkhead.md),
[adaptive throttling](docs/concepts/pro/throttle.md),
[canary recovery](docs/concepts/pro/canary-recovery.md),
[governance gates](docs/concepts/pro/governance.md), and a
[meta-watchdog](docs/concepts/pro/meta-watchdog.md) that watches Baldur itself.
See the full [OSS vs PRO capability matrix](docs/concepts/oss-vs-pro.md) and
[pricing](https://baldur.sh/pricing/).

## Early access

Baldur is in early access: the API is stable and the core is tested under
sustained load with Sentinel failover, but the project is young — minor
releases may still ship breaking changes, always with a changelog entry. It is
looking for a small number of teams already running a Python service in
production to work with directly. If that is you, the details and how to reach
me are in [Discussions](https://github.com/baldurhq/baldur/discussions).

## License

Baldur is released under the Apache License 2.0 — see [LICENSE](LICENSE) and
[NOTICE](NOTICE).

## Contributing

Contributions are welcome under the Apache License 2.0. Pull requests are
accepted through a sign-off-based [DCO](https://developercertificate.org/) flow —
see [CONTRIBUTING.md](CONTRIBUTING.md) for the full model.

- **Ideas, or showing what you built** →
  [Discussions](https://github.com/baldurhq/baldur/discussions).
- **Bugs / feature requests / docs** → open an issue or a pull request.
- **Security** → see [SECURITY.md](SECURITY.md) (no public issues for vulnerabilities).
- **Usage questions / commercial** → `support@baldur.sh`.
