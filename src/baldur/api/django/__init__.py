"""
Django integration for the baldur system: middleware and the REST API.

The middleware under this package imports with only Django installed
(``baldur-framework[django]``)::

    # In your Django project's settings.py:
    MIDDLEWARE = [
        "baldur.audit.trace.trace_id_middleware",                  # [1] Trace ID
        "baldur.api.django.middleware.HealthBridgeMiddleware",     # [2] Health Bridge
        "baldur.api.django.tiering.TieringMiddleware",             # [3] Tiering
        "baldur.api.django.middleware.BaldurMiddleware",      # [4] Baldur
        # ... Django Core Middlewares ...
        "baldur.api.django.pool_circuit_breaker.PoolCircuitBreakerMiddleware",  # [8] Pool CB
        # ... other middlewares ...
        "baldur.api.django.audit_middleware.AuditMiddleware",      # [11] Audit (last!)
    ]

The REST API (DRF views, serializers and ``baldur.api.django.urls``) is built
on Django REST framework and needs ``baldur-framework[django-api]``::

    # In your Django project's urls.py:
    from baldur.api.django import urls as baldur_urls

    urlpatterns = [
        path('api/baldur/', include(baldur_urls)),
    ]

The names exported here resolve on access (PEP 562), so importing this package
or any middleware under it does not import the REST API.
"""

# Lazy package — register names in `_LAZY_IMPORTS`; never add an eager
# top-level `from baldur.api.django.X import ...` here. Importing any module
# under this package runs this file first, so one eager import of a DRF-backed
# module (views, serializers) makes every middleware require DRF, and a
# `[django]` install without `[django-api]` then fails at WSGI startup.

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from baldur.api.django.audit_middleware import (
        AuditMiddleware,
        is_audit_middleware_enabled,
    )
    from baldur.api.django.middleware import (
        BaldurMiddleware,
        HealthBridgeMiddleware,
    )
    from baldur.api.django.pool_circuit_breaker import (
        PoolCircuitBreaker,
        PoolCircuitBreakerMiddleware,
        circuit_breaker_reset,
        circuit_breaker_status,
    )
    from baldur.api.django.serializers import (
        AuditLogListResponseSerializer,
        ControlAPIActions,
        ControlAPIEnvironments,
        ControlErrorResponseSerializer,
        ControlRequestSerializer,
        ControlResponseSerializer,
        ControlStatusResponseSerializer,
        MetricsResponseSerializer,
        ServiceStateSerializer,
    )
    from baldur.api.django.tiering import TieringMiddleware
    from baldur.api.django.views import (
        BaldurHealthView,
        BaldurMetricsView,
        ControlActionView,
        ControlAPIService,
        ControlAuditView,
        ControlStatusView,
        DLQReplayView,
        QuickAllowView,
        QuickBlockView,
        QuickResetView,
        ServiceStatusView,
        get_control_api_service,
    )
    from baldur.scaling.tiering import (
        TierRegistry,
        get_tier_registry,
    )

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # Middleware (DRF-free)
    "AuditMiddleware": ("baldur.api.django.audit_middleware", "AuditMiddleware"),
    "is_audit_middleware_enabled": (
        "baldur.api.django.audit_middleware",
        "is_audit_middleware_enabled",
    ),
    "BaldurMiddleware": ("baldur.api.django.middleware", "BaldurMiddleware"),
    "HealthBridgeMiddleware": (
        "baldur.api.django.middleware",
        "HealthBridgeMiddleware",
    ),
    "PoolCircuitBreaker": (
        "baldur.api.django.pool_circuit_breaker",
        "PoolCircuitBreaker",
    ),
    "PoolCircuitBreakerMiddleware": (
        "baldur.api.django.pool_circuit_breaker",
        "PoolCircuitBreakerMiddleware",
    ),
    "circuit_breaker_reset": (
        "baldur.api.django.pool_circuit_breaker",
        "circuit_breaker_reset",
    ),
    "circuit_breaker_status": (
        "baldur.api.django.pool_circuit_breaker",
        "circuit_breaker_status",
    ),
    "TieringMiddleware": ("baldur.api.django.tiering", "TieringMiddleware"),
    "TierRegistry": ("baldur.scaling.tiering", "TierRegistry"),
    "get_tier_registry": ("baldur.scaling.tiering", "get_tier_registry"),
    # REST API (needs Django REST framework)
    "AuditLogListResponseSerializer": (
        "baldur.api.django.serializers",
        "AuditLogListResponseSerializer",
    ),
    "ControlAPIActions": ("baldur.api.django.serializers", "ControlAPIActions"),
    "ControlAPIEnvironments": (
        "baldur.api.django.serializers",
        "ControlAPIEnvironments",
    ),
    "ControlErrorResponseSerializer": (
        "baldur.api.django.serializers",
        "ControlErrorResponseSerializer",
    ),
    "ControlRequestSerializer": (
        "baldur.api.django.serializers",
        "ControlRequestSerializer",
    ),
    "ControlResponseSerializer": (
        "baldur.api.django.serializers",
        "ControlResponseSerializer",
    ),
    "ControlStatusResponseSerializer": (
        "baldur.api.django.serializers",
        "ControlStatusResponseSerializer",
    ),
    "MetricsResponseSerializer": (
        "baldur.api.django.serializers",
        "MetricsResponseSerializer",
    ),
    "ServiceStateSerializer": (
        "baldur.api.django.serializers",
        "ServiceStateSerializer",
    ),
    "BaldurHealthView": ("baldur.api.django.views", "BaldurHealthView"),
    "BaldurMetricsView": ("baldur.api.django.views", "BaldurMetricsView"),
    "ControlActionView": ("baldur.api.django.views", "ControlActionView"),
    "ControlAPIService": ("baldur.api.django.views", "ControlAPIService"),
    "ControlAuditView": ("baldur.api.django.views", "ControlAuditView"),
    "ControlStatusView": ("baldur.api.django.views", "ControlStatusView"),
    "DLQReplayView": ("baldur.api.django.views", "DLQReplayView"),
    "QuickAllowView": ("baldur.api.django.views", "QuickAllowView"),
    "QuickBlockView": ("baldur.api.django.views", "QuickBlockView"),
    "QuickResetView": ("baldur.api.django.views", "QuickResetView"),
    "ServiceStatusView": ("baldur.api.django.views", "ServiceStatusView"),
    "get_control_api_service": (
        "baldur.api.django.views",
        "get_control_api_service",
    ),
}


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        # Resolve live on each access (no globals() memoization) so the package
        # reflects the current submodule attribute — a test that patches
        # `<this package>.<submodule>.<name>` must not be shadowed by a value
        # cached from an earlier patch. importlib already caches the module
        # import, so the cost is a dict lookup.
        return getattr(importlib.import_module(module_path), attr_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return list(__all__)


# `pool_circuit_breaker` is not exported: it names both a submodule and the
# instance that submodule defines, and once the submodule is imported the
# import system binds the module over the package-level name. The instance is
# `baldur.api.django.pool_circuit_breaker.pool_circuit_breaker`.
__all__ = [
    # Views
    "ControlActionView",
    "ControlStatusView",
    "ServiceStatusView",
    "ControlAuditView",
    "QuickAllowView",
    "QuickBlockView",
    "QuickResetView",
    "BaldurHealthView",
    "BaldurMetricsView",
    "DLQReplayView",
    # Service
    "get_control_api_service",
    "ControlAPIService",
    # Serializers
    "ControlRequestSerializer",
    "ControlResponseSerializer",
    "ControlErrorResponseSerializer",
    "ControlStatusResponseSerializer",
    "ServiceStateSerializer",
    "AuditLogListResponseSerializer",
    "MetricsResponseSerializer",
    # Constants
    "ControlAPIActions",
    "ControlAPIEnvironments",
    # Middleware - Gateway Pipeline
    "HealthBridgeMiddleware",
    "BaldurMiddleware",
    "AuditMiddleware",
    "is_audit_middleware_enabled",
    "PoolCircuitBreakerMiddleware",
    "PoolCircuitBreaker",
    "circuit_breaker_status",
    "circuit_breaker_reset",
    "TieringMiddleware",
    "TierRegistry",
    "get_tier_registry",
]
