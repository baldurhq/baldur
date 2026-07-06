"""URL configuration for the Baldur Grafana demo app."""

from __future__ import annotations

import os

from django.urls import include, path
from views import (
    demo,
    flaky,
    idempotent,
    pro_metrics_demo,
    shutdown_sim,
    system_control_cycle,
)

urlpatterns = [
    path("demo/", demo),
    path("flaky/", flaky),
    path("idempotent/", idempotent),
    path("system-control/", system_control_cycle),
    # Baldur health + Prometheus exposition. The collector scrapes
    # /api/baldur/prometheus/ (examples/monitoring/otel-collector.yml).
    path("api/baldur/", include("baldur.api.django.urls.health")),
]

# Demo metric-simulation endpoints, OFF unless DEMO_SIMULATE_METRICS=1.
# - /pro/ drives the PRO operations recorders so those panels populate for
#   dashboard verification (the 7.6B-style panel-population check). Tier-honesty:
#   in the default OSS demo the PRO panels MUST read "No data" (the OSS
#   store_to_dlq path is a no-op and no PRO services run), so the demo never
#   advertises PRO capability it does not ship.
# - /shutdown-sim/ walks the OSS Graceful Shutdown recorder through a drain so
#   that panel moves (a real SIGTERM kills the scraped endpoint mid-drain and is
#   never observable).
# Both simulate metric series without the underlying event, so they stay opt-in.
if os.environ.get("DEMO_SIMULATE_METRICS") == "1":
    urlpatterns.append(path("pro/", pro_metrics_demo))
    urlpatterns.append(path("shutdown-sim/", shutdown_sim))
