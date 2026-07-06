# Django Quickstart

Protect a Django view with Baldur. No Redis, no Docker, no
environment variables. The in-memory fallback covers the whole first run.

> Supports Python 3.11–3.13 and Django 4.2 / 5.2 LTS / 6.x. Assumes you have
> built a Django app before.

## 1. Install

```bash
pip install baldur-framework[django]
```

## 2. Add Baldur to your settings

Add `baldur.adapters.django` to `INSTALLED_APPS`. Its app config calls
`baldur.init()` on startup for you — there is nothing else to wire.

```python
--8<-- "examples/quickstart_django/settings.py"
```

That also wires HTTP latency (RED) automatically: the adapter injects its
metrics middleware on startup, so the `baldur_http_request_duration_seconds`
histogram behind the overview's HTTP Latency panel populates with no middleware
to add — the same out-of-the-box behavior as the Flask and FastAPI quickstarts.

## 3. Protect a view

`@baldur.protected("demo")` wraps the view in Baldur's composed resilience
pipeline (circuit breaker on by default):

```python
--8<-- "examples/quickstart_django/views.py"
```

Route it:

```python
--8<-- "examples/quickstart_django/urls.py"
```

## 4. Run it

Start the dev server and call the route:

```bash
python manage.py runserver
curl http://127.0.0.1:8000/demo/
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

The quickstart ships a smoke test that drives the view through Django's
in-process test client — no server, no infra:

```bash
pytest examples/quickstart_django/test_smoke.py
```

Browse the full runnable app:
[`examples/quickstart_django/`](https://github.com/gotoUSA/baldur-python/tree/main/examples/quickstart_django).

## Going to production

!!! danger "The in-memory fallback is single-process only"

    The zero-config path uses Baldur's in-memory cache. It keeps state in a
    per-process store, so copying this quickstart into a multi-worker
    deployment (`gunicorn --workers N`, `uvicorn --workers N`) does **not**
    degrade gracefully: idempotency keys, rate-limit counters, and circuit
    breaker state diverge silently per worker. That breaks **correctness**,
    not just scale. The in-memory store also grows unbounded. This is a
    hazard, not a tuning knob: give Baldur a shared backend before you run
    more than one worker.

Point Baldur at Redis so all workers share state. No code changes needed: set one
environment variable before starting the server:

```bash
pip install baldur-framework[django,redis]
export BALDUR_REDIS_URL=redis://localhost:6379/0
```

That is the only addition the production path needs over the quickstart path.
