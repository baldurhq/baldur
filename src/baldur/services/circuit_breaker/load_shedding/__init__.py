"""
Load Shedding (partial blocking)

When core services show signs of failure, traffic to non-core services is
limited first to concentrate resources on the core services.

Usage:
    manager = get_load_shedding_manager()

    # Look up the allowed traffic ratio for a service
    allowed_traffic = manager.evaluate_shedding("review-api")  # Returns 50.0 (%)

    # Check whether Shedding is active
    if manager.is_shedding_active():
        print(f"Current shedding level: {manager.get_current_level()}")
"""

from __future__ import annotations

import threading

# ============================================================
# Dashboard
# ============================================================
from .dashboard import (
    LoadSheddingDashboard,
)

# ============================================================
# Error Rate Provider
# ============================================================
from .error_rate import (
    ErrorRateProvider,
)

# ============================================================
# Manager
# ============================================================
from .manager import (
    LoadSheddingManager,
)

# ============================================================
# Middleware
# ============================================================
from .shedding_middleware import (
    LoadSheddingMiddleware,
)

# ============================================================
# Data Models
# ============================================================
from .shedding_models import (
    SheddingAuditEntry,
    SheddingDecision,
    SheddingState,
    SheddingStatus,
)

# ============================================================
# Module-level Convenience Functions
# ============================================================

_manager: LoadSheddingManager | None = None
_manager_lock = threading.Lock()
_middleware: LoadSheddingMiddleware | None = None
_middleware_lock = threading.Lock()
_dashboard: LoadSheddingDashboard | None = None
_dashboard_lock = threading.Lock()


def get_load_shedding_manager() -> LoadSheddingManager:
    """
    Return the LoadSheddingManager singleton instance.

    Returns:
        LoadSheddingManager: singleton instance
    """
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = LoadSheddingManager()
    return _manager


def reset_load_shedding_manager() -> None:
    """Reset the singleton instance (for tests)."""
    global _manager, _middleware, _dashboard
    if _manager is not None:
        _manager.reset()
    _manager = None
    _middleware = None
    _dashboard = None
    LoadSheddingManager.reset_instance()


def get_load_shedding_middleware() -> LoadSheddingMiddleware:
    """
    Return the LoadSheddingMiddleware instance.

    Returns:
        LoadSheddingMiddleware instance
    """
    global _middleware
    if _middleware is None:
        with _middleware_lock:
            if _middleware is None:
                _middleware = LoadSheddingMiddleware(get_load_shedding_manager())
    return _middleware


def get_load_shedding_dashboard() -> LoadSheddingDashboard:
    """
    Return the LoadSheddingDashboard instance.

    Returns:
        LoadSheddingDashboard instance
    """
    global _dashboard
    if _dashboard is None:
        with _dashboard_lock:
            if _dashboard is None:
                _dashboard = LoadSheddingDashboard(get_load_shedding_manager())
    return _dashboard


# ============================================================
# Convenience Functions
# ============================================================


def register_load_shedding_service(config) -> bool:
    """
    Register a service.

    Args:
        config: Service configuration (ServiceConfig)

    Returns:
        bool: Whether registration succeeded
    """
    return get_load_shedding_manager().register_service(config)


def evaluate_shedding(service_id: str) -> float:
    """
    Look up the allowed traffic ratio for a service.

    Args:
        service_id: Service ID

    Returns:
        float: Allowed traffic ratio (0~100)
    """
    return get_load_shedding_manager().evaluate_shedding(service_id)


def should_allow_shedding_request(service_id: str) -> SheddingDecision:
    """
    Decide whether to allow a request.

    Args:
        service_id: Service ID

    Returns:
        SheddingDecision: whether allowed and detailed info
    """
    return get_load_shedding_manager().should_allow_request(service_id)


def is_shedding_active() -> bool:
    """
    Whether Shedding is active.

    Returns:
        bool: Whether active
    """
    return get_load_shedding_manager().is_shedding_active()


def get_shedding_status() -> SheddingStatus:
    """
    Look up the current Shedding state.

    Returns:
        SheddingStatus: current state
    """
    return get_load_shedding_manager().get_status()


def set_service_error_rate(service_id: str, error_rate: float) -> None:
    """
    Set a service's error rate (for tests / external metric integration).

    Args:
        service_id: Service ID
        error_rate: Error rate (0~100)
    """
    get_load_shedding_manager().set_error_rate(service_id, error_rate)


def update_shedding_state() -> SheddingAuditEntry | None:
    """
    Update the Shedding state.

    Returns:
        SheddingAuditEntry on a level change, None otherwise
    """
    return get_load_shedding_manager().update_shedding_state()


# ============================================================
# Public API
# ============================================================
__all__ = [
    # Data Models
    "SheddingState",
    "SheddingDecision",
    "SheddingStatus",
    "SheddingAuditEntry",
    # Error Rate Provider
    "ErrorRateProvider",
    # Manager
    "LoadSheddingManager",
    # Middleware
    "LoadSheddingMiddleware",
    # Dashboard
    "LoadSheddingDashboard",
    # Convenience Functions
    "get_load_shedding_manager",
    "reset_load_shedding_manager",
    "get_load_shedding_middleware",
    "get_load_shedding_dashboard",
    "register_load_shedding_service",
    "evaluate_shedding",
    "should_allow_shedding_request",
    "is_shedding_active",
    "get_shedding_status",
    "set_service_error_rate",
    "update_shedding_state",
]
