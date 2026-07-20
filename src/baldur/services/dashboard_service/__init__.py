"""
Dashboard Service Package

Provides centralized dashboard statistics and monitoring operations.

.. versionadded:: 2.1.0
    Converted from the flat ``dashboard_service.py`` file to the
    ``dashboard_service/`` package.

Usage:
    from baldur.services.dashboard_service import (
        get_dashboard_service,
        DashboardService,
        DashboardSummary,
        Distribution,
        AlertInfo,
        invalidate_dashboard_cache,
    )
"""

# Dynamic forwarding (event_bus pattern)
# Expose every attribute of the service module at the package level, keeping the
# existing `from baldur.services.dashboard_service import ...` pattern working.
import sys as _sys

from baldur.services.dashboard_service import service as _service_module
from baldur.services.dashboard_service.models import (
    AlertInfo,
    DashboardSummary,
    Distribution,
)
from baldur.services.dashboard_service.service import (
    DashboardService,
    _dashboard_service,
    _get_statistics_repo,
    _has_statistics_adapter,
    get_dashboard_service,
    invalidate_dashboard_cache,
    logger,
    reset_dashboard_service,
)

_pkg = _sys.modules[__name__]
for _name in dir(_service_module):
    if not _name.startswith("__") and not hasattr(_pkg, _name):
        setattr(_pkg, _name, getattr(_service_module, _name))
del _name, _pkg

__all__ = [
    # Models
    "Distribution",
    "AlertInfo",
    "DashboardSummary",
    # Service
    "DashboardService",
    # Singleton & Helpers
    "get_dashboard_service",
    "reset_dashboard_service",
    "invalidate_dashboard_cache",
]
