"""Meta-watchdog URL patterns (Baldur self-monitoring liveness + status).

Views: MetaWatchdogLivenessView, MetaWatchdogStatusView
(baldur.api.django.views.meta_watchdog).
"""

from __future__ import annotations

from django.urls import path

from baldur.api.django.views.meta_watchdog import (
    MetaWatchdogLivenessView,
    MetaWatchdogStatusView,
)

urlpatterns = [
    # Liveness probe (K8s — detects watchdog loop stuck)
    path(
        "health/meta-watchdog/",
        MetaWatchdogLivenessView.as_view(),
        name="meta-watchdog-liveness",
    ),
    # Status (full state + per-component status)
    path(
        "meta/status/",
        MetaWatchdogStatusView.as_view(),
        name="meta-watchdog-status",
    ),
]
