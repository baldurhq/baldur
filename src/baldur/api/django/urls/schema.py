"""OpenAPI schema + docs UI URL patterns — 530 Wave 6F (D11).

Two-layer gate (mirrors ``urls/compliance.py`` precedent):

1. ``BALDUR_OPENAPI_ENABLED=0`` → empty pattern list. Lets operators
   hide the surface even when drf-spectacular is installed (security
   ask per 530 D11 rationale).
2. drf-spectacular not importable → empty pattern list. The OSS install
   without the ``[openapi]`` extras degrades silently rather than
   crashing URL loading.

All three routes (``/schema/``, ``/docs/``, ``/redoc/``) are
authenticated (``IsBaldurAuthenticated`` — 530 D11). Swagger UI ships
an ``Authorize`` button that attaches the ``Bearer <jwt>`` header to
subsequent ``/schema/`` requests, so no template override is needed.
"""

from __future__ import annotations

from django.urls import path

from baldur.api.django.permissions import IsBaldurAuthenticated
from baldur.settings.openapi import get_openapi_settings

__all__ = ["urlpatterns"]

if not get_openapi_settings().enabled:
    urlpatterns: list = []
else:
    try:
        from drf_spectacular.views import (
            SpectacularAPIView,
            SpectacularRedocView,
            SpectacularSwaggerView,
        )

        urlpatterns = [
            path(
                "schema/",
                SpectacularAPIView.as_view(
                    permission_classes=[IsBaldurAuthenticated],
                ),
                name="openapi-schema",
            ),
            path(
                "docs/",
                SpectacularSwaggerView.as_view(
                    url_name="baldur:openapi-schema",
                    permission_classes=[IsBaldurAuthenticated],
                ),
                name="openapi-swagger-ui",
            ),
            path(
                "redoc/",
                SpectacularRedocView.as_view(
                    url_name="baldur:openapi-schema",
                    permission_classes=[IsBaldurAuthenticated],
                ),
                name="openapi-redoc",
            ),
        ]
    except ImportError:
        urlpatterns = []
