# Architecture

This document describes how the Baldur open-source core (`baldur-framework`) is
structured and the patterns the codebase enforces. It is the orientation map
referenced from [CONTRIBUTING.md](CONTRIBUTING.md); read it before opening a
non-trivial pull request.

## Design in one paragraph

Baldur is a **framework-agnostic reliability core** with optional, thin
integration adapters. The core knows nothing about Django, FastAPI, Flask,
Celery, Redis, or Prometheus; each of those is a plug-in adapter that depends on
the core, never the other way around. A single public facade —
`baldur.protected(...)` / `baldur.protect(...)` — composes circuit breaker,
retry, and fallback into one pipeline, and `baldur.init()` performs the
framework-specific wiring at startup.

## Repository layout

```
src/baldur/
  core/          Backoff, circuit-breaker state, execution engine, TLS, shutdown
  services/      Resilience services (CB, DLQ surface, retry, saga, chaos, ...)
  adapters/      Framework/infra adapters (Django/FastAPI/Flask/Redis/Celery/SQL/…)
  bridges/       Third-party resilience-library bridges (e.g. tenacity)
  interfaces/    Repository/Cache/Queue/Framework abstract contracts
  audit/         Audit logging, hash chain, WAL, multi-backend persistence
  coordination/  Leader election, consumer coordination
  metrics/       Prometheus metrics, drift detection
  scaling/       Token bucket, load shedding, graceful degradation
  resilience/    Bulkhead, hedging, policy composition
  settings/      Pydantic settings (BALDUR_* env vars)
  observability/ OpenTelemetry initialization
  ...            (cli/, decorators/, context/, models/, api/, notification/, dlq/)
```

## The OSS / PRO boundary

Baldur ships in two tiers. This repository is the **open-source core**. A
commercial tier (`baldur-pro`) lives in a **separate private repository** and
consumes this package as an ordinary dependency. The boundary is
**one-directional and enforced**:

- **The OSS core never imports the PRO package.** Extension points are declared
  here as `Protocol` contracts with **no-op default implementations**, so the
  core is fully functional and safe with the PRO tier absent.
- **PRO resolves lazily.** When the PRO package is installed it registers richer
  implementations through the provider registry; when it is absent, the no-op
  defaults stand in. Nothing in the core hard-depends on a PRO symbol existing.
- **Contributions land in the core only.** You do not need the PRO tier to
  build, test, or contribute. The published CI runs the suite with the private
  tiers absent — that is the reality contributors target.

## Enforced patterns

These are mechanically checked by the fitness-function suite under
`tests/architecture/`. When one fails, its message links to the rule. The main
ones:

- **`__all__`** is declared explicitly in every module (public API is opt-in).
- **Exception hierarchy** — domain errors inherit `BaldurError` and implement
  `extra_context()`.
- **Protocol vs ABC** — `Protocol` for external contracts, `ABC` for internal
  adapter base classes.
- **Lazy imports** — heavy or optional modules are exposed through
  `__getattr__` (PEP 562) plus `TYPE_CHECKING`, so importing `baldur` is cheap
  and optional extras stay optional.
- **Singletons** — stateful services expose a `get_*()` / `reset_*()` pair
  (the `reset_*` exists for test isolation).
- **Enums** — `(str, Enum)` inheritance so values serialize to JSON directly.
- **Time** — use `utils.time.utc_now()`, never `datetime.now()` /
  `datetime.utcnow()` directly.
- **No hardcoded operational values** — timeouts, thresholds, retry counts, and
  TTLs resolve through `settings/` (`BALDUR_*`) or a named module-level
  constant, never an inline literal at the use site.
- **Acyclic imports** — the first-party import-time graph must have no cycles.
- **Graceful degradation** — a disabled or failed feature must leave the system
  in a safe state; each path decides fail-open vs fail-closed explicitly.
- **Metrics** — Prometheus metric names are `baldur_`-prefixed; event names are
  string literals of the form `{component}.{entity}_{action}`.
- **No `print()`** in library code — use structured logging.

## Where to look

- Getting started and concept guides: <https://baldur.sh>
- Public API reference: <https://baldur.sh/reference/>
- Contribution workflow, DCO, and review bar: [CONTRIBUTING.md](CONTRIBUTING.md)
- Security policy and reporting: [SECURITY.md](SECURITY.md)
