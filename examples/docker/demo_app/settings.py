"""Django settings for the Baldur Grafana demo app.

A self-contained Django service for the ``examples/docker`` Grafana stack. It
wires the full OSS observability surface the ``baldur-overview.json`` dashboard
reads, so ``docker compose -f examples/docker/docker-compose.yml up -d`` renders
real data with no external app:

- ``HttpMetricsMiddleware`` emits the framework-native HTTP latency histogram
  (``baldur_http_request_duration_seconds``). The Django adapter auto-injects it
  on startup, so this explicit listing is redundant-but-harmless — kept as a
  belt-and-suspenders example of where the middleware lives.
- ``baldur.adapters.django`` in ``INSTALLED_APPS`` calls ``baldur.init()`` on
  startup and auto-wires OTEL Django instrumentation when ``OTEL_ENABLED=true``.
- The Baldur health/metrics URLs are included at ``api/baldur/`` (see urls.py),
  which exposes the Prometheus text exposition at ``/api/baldur/prometheus/``
  for the collector to scrape.

Zero infrastructure: in-memory SQLite + Baldur's in-memory fallback (no Redis).
This is a demo, not a production template — the in-memory fallback is
single-process only.
"""

from __future__ import annotations

SECRET_KEY = "demo-insecure-key-do-not-use-in-production"  # noqa: S105
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    # DRF — the Baldur health/Prometheus views are DRF APIViews.
    "rest_framework",
    # Baldur Django integration — calls baldur.init() on startup.
    "baldur.adapters.django",
]

MIDDLEWARE = [
    # Framework-native HTTP RED metrics — emits
    # baldur_http_request_duration_seconds (the latency panel's source).
    # The adapter auto-injects this on startup; this explicit listing is
    # redundant-but-harmless (the idempotency guard prevents a double-add).
    "baldur.api.django.middleware.http_metrics.HttpMetricsMiddleware",
]

ROOT_URLCONF = "urls"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

USE_TZ = True
