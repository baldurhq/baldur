"""
API layer for the baldur system.

This module contains REST API implementations for various frameworks.

Available APIs:
- django: Django REST Framework views, serializers, and URLs

Usage:
    # In your Django project's urls.py:
    from baldur.api.django import urls as baldur_urls

    urlpatterns = [
        path('api/baldur/', include(baldur_urls)),
    ]

Status: Internal
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

__all__ = ["django"]

if TYPE_CHECKING:
    from baldur.api import django as django


def __getattr__(name: str):
    # PEP 562 lazy import. The Django API (DRF serializers + the
    # PoolCircuitBreaker middleware) is loaded only on explicit
    # ``baldur.api.django`` access. Keeping it off the import-time path means
    # framework-free callers — ``baldur.api.middleware`` and the FastAPI / Flask
    # adapters that build on it, plus ``baldur.api.admin`` — never transitively
    # pull Django (which requires configured settings and would otherwise fail
    # or spawn a doomed background thread in a non-Django process).
    if name == "django":
        return importlib.import_module("baldur.api.django")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
