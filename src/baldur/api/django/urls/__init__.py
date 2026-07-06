"""Baldur API URL configuration.

Include in your Django project's ``urls.py``::

    from baldur.api.django import urls as baldur_urls

    urlpatterns = [
        # ...your other urls
        path("api/baldur/", include(baldur_urls)),
    ]

Per-domain URL groups live in sibling modules of this package (see
``control.py``, ``health.py``, ``chaos.py``, etc.). Each module exposes
its own ``urlpatterns`` list. The aggregation below assembles them into
the single ``urlpatterns`` Django expects.

Conditional groups (``dna``, ``compliance``, ``stress``) handle their own
import gating internally and contribute an empty list when their
dependencies / flags are not satisfied.

Gate type per conditional group:
- ``dna``: ImportError (optional view package).
- ``compliance``: ``compliance.enabled`` settings gate (Dormant tier per
  impl 527 D8) wrapping the ImportError fallback.
- ``schema``: ``openapi.enabled`` settings gate (530 D11) wrapping the
  ImportError fallback for drf-spectacular.
- ``stress``: ImportError (optional view package).
"""

from __future__ import annotations

from baldur.api.django.urls import (
    audit,
    auto_tuning,
    canary,
    cascade,
    chaos,
    compliance,
    config_history,
    control,
    dlq,
    dna,
    emergency,
    error_budget,
    features,
    governance,
    grafana_webhook,
    health,
    l2_storage,
    meta_watchdog,
    postmortem,
    recovery,
    reports,
    runtime_config,
    schema,
    stress,
    system_control,
    tiering,
    xtest,
)

app_name = "baldur"

urlpatterns = [
    *control.urlpatterns,
    *health.urlpatterns,
    *dlq.urlpatterns,
    *reports.urlpatterns,
    *system_control.urlpatterns,
    *runtime_config.urlpatterns,
    *governance.urlpatterns,
    *config_history.urlpatterns,
    *tiering.urlpatterns,
    *emergency.urlpatterns,
    *cascade.urlpatterns,
    *auto_tuning.urlpatterns,
    *error_budget.urlpatterns,
    *chaos.urlpatterns,
    *l2_storage.urlpatterns,
    *audit.urlpatterns,
    *postmortem.urlpatterns,
    *grafana_webhook.urlpatterns,
    *recovery.urlpatterns,
    *meta_watchdog.urlpatterns,
    *canary.urlpatterns,
    *features.urlpatterns,
    # Conditional groups (empty list if unavailable)
    *dna.urlpatterns,
    *compliance.urlpatterns,
    *schema.urlpatterns,
    *xtest.urlpatterns,
    *stress.urlpatterns,
]
