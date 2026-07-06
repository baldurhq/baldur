"""Integration-test URL conf for 530 Wave 6F.

Mounts the full ``baldur.api.django.urls`` namespace under ``/api/baldur/``
so the ``/schema/``, ``/docs/``, ``/redoc/``, and ``/features/`` routes
resolve through the production URL aggregation (`urls/__init__.py`).

Used by ``override_settings(ROOT_URLCONF="tests.integration.api.urls_530")``
in the integration tests.
"""

from __future__ import annotations

from django.urls import include, path

from baldur.api.django import urls as baldur_urls

urlpatterns = [
    path("api/baldur/", include(baldur_urls)),
]
