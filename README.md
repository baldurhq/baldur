# Baldur

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![PyPI](https://img.shields.io/pypi/v/baldur-framework.svg)](https://pypi.org/project/baldur-framework/)
[![Docs](https://img.shields.io/badge/docs-baldur.sh-1f6feb.svg)](https://baldur.sh)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/13522/badge)](https://www.bestpractices.dev/projects/13522)

**Baldur** is a self-healing reliability layer for Python applications. It puts
circuit breaker, retry, and fallback behind a single decorator, so a flaky
downstream stops cascading into your service. Durable dead-letter queue, audit,
and replay are available in [Baldur PRO](https://baldur.sh). The core is framework-agnostic, with
first-class adapters for Django, FastAPI, and Flask.

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

`@baldur.protected("name")` composes circuit breaker, retry, and fallback into
one pipeline. Add Redis (`pip install baldur-framework[redis]`,
`BALDUR_REDIS_URL=...`) when you move to multiple workers so the state is shared.

## Quickstart

Pick your framework and have a protected endpoint running:

- [Getting Started](docs/getting-started/index.md) — Django, FastAPI, Flask
- **Full documentation & concept guides:** <https://baldur.sh>

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

## Using Baldur with AI assistants

Building with an AI coding assistant (Claude Code, Cursor, Copilot, Codex)? Run
`baldur init-ai` in your repo to drop an `AGENTS.md` (read by Cursor, Copilot,
and Codex) plus a `CLAUDE.md` that imports it for Claude Code — together they
teach the assistant to reach for `@baldur.protected("name")` instead of
hand-rolling a circuit breaker. See
[Using Baldur with AI assistants](docs/getting-started/ai-assistants.md).

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
