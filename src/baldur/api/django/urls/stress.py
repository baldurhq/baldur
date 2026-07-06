"""Stress test URL patterns — DEBUG-mode only (production excluded).

Activated when ``DEBUG=True`` or ``ENABLE_STRESS_TESTS=True`` in Django
settings. Otherwise ``urlpatterns`` is empty and no stress endpoints are
exposed.
"""

from __future__ import annotations

from django.conf import settings
from django.urls import path

urlpatterns = []

if getattr(settings, "DEBUG", False) or getattr(settings, "ENABLE_STRESS_TESTS", False):
    from baldur.api.django.stress_views import (
        advisory_lock_acquire,
        advisory_lock_contention,
        connection_leak_simulation,
        controlled_burst_failure,
        heavy_concurrent_query,
        pool_exhaust,
        pool_status,
        slow_query_5s,
        slow_query_10s,
        trigger_cb_failure,
    )

    urlpatterns = [
        path("stress/slow-5s/", slow_query_5s, name="stress-slow-5s"),
        path("stress/slow-10s/", slow_query_10s, name="stress-slow-10s"),
        path("stress/leak/", connection_leak_simulation, name="stress-leak"),
        path("stress/pool-status/", pool_status, name="stress-pool-status"),
        path("stress/heavy-query/", heavy_concurrent_query, name="stress-heavy-query"),
        # Advisory Lock API — non-invasive DB lock test
        path(
            "stress/advisory-lock/acquire/",
            advisory_lock_acquire,
            name="stress-advisory-lock-acquire",
        ),
        path(
            "stress/advisory-lock/contention/",
            advisory_lock_contention,
            name="stress-advisory-lock-contention",
        ),
        path(
            "stress/burst-failure/",
            controlled_burst_failure,
            name="stress-burst-failure",
        ),
        # Pool Exhaustion & CB Trigger API
        path("stress/pool-exhaust/", pool_exhaust, name="stress-pool-exhaust"),
        path(
            "stress/trigger-cb-failure/",
            trigger_cb_failure,
            name="stress-trigger-cb-failure",
        ),
    ]
