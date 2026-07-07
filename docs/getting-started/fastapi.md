# FastAPI Quickstart

Protect a FastAPI route with Baldur. No Redis, no Docker, no
environment variables. The in-memory fallback covers the whole first run.

> Supports Python 3.11–3.13. Assumes you have built a FastAPI app before.

## 1. Install

```bash
pip install baldur-framework[fastapi]
```

## 2. Wire Baldur into the app

`fastapi_lifespan` calls `baldur.init()` on startup; `BaldurMiddleware` adds
rate-limit, backpressure, and circuit-breaker pre-flight at the ASGI edge. The
`/demo` route is protected by the marquee `@baldur.protected` facade:

```python
--8<-- "examples/quickstart_fastapi/app.py"
```

!!! note "Baldur does not authenticate your routes"

    The middleware adds resilience (rate-limit, backpressure, circuit
    breaker), **not** authentication. Your app keeps owning endpoint auth
    (FastAPI `Depends`, your own dependencies). Baldur's own operational
    surface (the built-in admin server and
    [Web Console](../concepts/foundations/web-console.md)) is access-controlled
    separately by a key you configure; see that guide for the role model. The
    Django adapter additionally registers Django permission groups for its own
    admin/API views; there is no equivalent host-app role system on FastAPI.

## 3. Run it

Start the server and call the route:

```bash
uvicorn app:app --reload
curl http://127.0.0.1:8000/demo
# {"status": "ok", "service": "demo"}
```

That's it. The response just travelled through a circuit breaker.

### See Baldur's events

Baldur logs to stdout automatically. Raise the log level to watch circuit
breaker and rate-limit events as you exercise the endpoint:

```bash
export BALDUR_LOG_LEVEL=INFO   # circuit opened/closed, rate-limit blocks, ...
```

### Verify without a browser

The quickstart ships a smoke test that drives the route through FastAPI's
in-process `TestClient` — no server, no infra:

```bash
pytest examples/quickstart_fastapi/test_smoke.py
```

Browse the full runnable app:
[`examples/quickstart_fastapi/`](https://github.com/baldurhq/baldur/tree/main/examples/quickstart_fastapi).

## Going to production

!!! danger "The in-memory fallback is single-process only"

    The zero-config path uses Baldur's in-memory cache. It keeps state in a
    per-process store, so copying this quickstart into a multi-worker
    deployment (`uvicorn --workers N`, `gunicorn -k uvicorn.workers...`) does
    **not** degrade gracefully: idempotency keys, rate-limit counters, and
    circuit breaker state diverge silently per worker. That breaks
    **correctness**, not just scale. The in-memory store also grows unbounded.
    This is a hazard, not a tuning knob: give Baldur a shared backend before
    you run more than one worker.

Point Baldur at Redis so all workers share state. No code changes needed: set one
environment variable before starting the server:

```bash
pip install baldur-framework[fastapi,redis]
export BALDUR_REDIS_URL=redis://localhost:6379/0
```

That is the only addition the production path needs over the quickstart path.
