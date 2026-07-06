"""
Pre-computed Cache Service - Compute Functions for L3 Endpoints.
"""

from __future__ import annotations

from typing import Any

import structlog

from .constants import CACHE_KEY_ERROR_BUDGET, CACHE_KEY_HEALTH, CACHE_KEY_POOL_STATUS
from .multi_tier import get_cached_response
from .worker import get_precomputed_cache_worker

logger = structlog.get_logger()


# =============================================================================
# Compute Functions for L3 Endpoints
# =============================================================================


def compute_health_status() -> dict[str, Any]:
    """Compute health status for caching."""
    try:
        from baldur.services.health_check import get_health_check_service

        service = get_health_check_service()
        health = service.get_overall_health()
        return health.to_dict()
    except Exception as e:
        logger.exception(
            "precomputed_cache.health_compute_failed",
            error=e,
        )
        return {"status": "error", "error": str(e)}


def compute_error_budget_status() -> dict[str, Any]:
    """Compute error budget status for caching."""
    try:
        from baldur.utils.time import utc_now

        try:
            from baldur_pro.services.error_budget import (
                get_error_budget_service,
            )
        except ImportError:
            get_error_budget_service = None  # type: ignore[assignment,misc]

        service = get_error_budget_service()
        budget_status = service.get_budget_status("availability")

        return {
            "status": "success",
            "data": budget_status.to_dict(),
            "timestamp": utc_now().isoformat(),
        }
    except Exception as e:
        logger.exception(
            "precomputed_cache.error_budget_compute_failed",
            error=e,
        )
        return {"status": "error", "error": str(e)}


def compute_pool_status() -> dict[str, Any]:
    """Compute connection pool status for caching."""
    try:
        import os

        # Skip real DB I/O in test mode.
        if os.getenv("BALDUR_TEST_MODE", "").lower() == "true":
            return {"status": "test_mode", "message": "Skipped in test mode"}

        from baldur.factory import ProviderRegistry

        pool_info = ProviderRegistry.pool_info.get().get_pool_info()

        db_provider = ProviderRegistry.database_health.get()
        conn_info = db_provider.check_connection("default")

        pg_admin = ProviderRegistry.pg_admin.get()

        is_exhausted = pool_info.get("pool_exhausted", False)

        response: dict[str, Any] = {
            "status": "exhausted" if is_exhausted else "healthy",
            "sqlalchemy_pool": pool_info,
            "connection_usable": conn_info.is_usable,
            "use_connection_pool": os.getenv("USE_CONNECTION_POOL", "FALSE") == "TRUE",
        }
        if pg_admin.is_available():
            stats = pg_admin.get_connection_stats()
            response["pg_stats"] = {
                "total_connections": stats.total_connections,
                "active": stats.active,
                "idle": stats.idle,
                "idle_in_transaction": stats.idle_in_transaction,
            }
        return response
    except Exception as e:
        logger.exception(
            "precomputed_cache.pool_status_compute_failed",
            error=e,
        )
        return {"status": "error", "error": str(e)}


def register_default_compute_functions() -> None:
    """Register default compute functions for L3 endpoints.

    Resolves the worker lazily via ``get_precomputed_cache_worker()`` so that
    registration always targets the live instance — after
    ``reset_precomputed_cache_worker()`` rebinds the global, a module-level
    ``_worker`` binding would otherwise register into the stale pre-reset
    instance while the run-state lives on the new one (test-only divergence).
    """
    worker = get_precomputed_cache_worker()
    worker.register(CACHE_KEY_HEALTH, compute_health_status)
    worker.register(CACHE_KEY_ERROR_BUDGET, compute_error_budget_status)
    worker.register(CACHE_KEY_POOL_STATUS, compute_pool_status)
    logger.info("precomputed_cache.compute_functions_registered")


# =============================================================================
# Public API for Views
# =============================================================================


def get_cached_health() -> dict[str, Any]:
    """Get cached health status."""
    return get_cached_response(CACHE_KEY_HEALTH, compute_health_status)


def get_cached_error_budget() -> dict[str, Any]:
    """Get cached error budget status."""
    return get_cached_response(CACHE_KEY_ERROR_BUDGET, compute_error_budget_status)


def get_cached_pool_status() -> dict[str, Any]:
    """Get cached pool status."""
    return get_cached_response(CACHE_KEY_POOL_STATUS, compute_pool_status)
