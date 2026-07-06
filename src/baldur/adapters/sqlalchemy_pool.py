"""
SQLAlchemy Connection Pool Adapter.

Thin wrapper that delegates pool-stats discovery to
``ProviderRegistry.pool_info.get().get_pool_info()`` (515 D5). Backwards-
compatible import path — direct callers of ``get_pool_info()`` continue
to work and transparently pick up the registry-resolved provider
(Django / SQLAlchemy / Noop).

``_extract_pool_info`` remains in this module because both
``DjangoPoolInfoProvider`` and ``SQLAlchemyPoolInfoProvider`` reuse it.

Usage:
    from baldur.adapters.sqlalchemy_pool import get_pool_info

    pool_info = get_pool_info()
    # {'pool_type': 'QueuePool', 'pool_size': 10, ...}
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()


def get_pool_info() -> dict[str, Any]:
    """Return connection pool stats via the active ``PoolInfoProvider``.

    Delegates to ``ProviderRegistry.pool_info.get().get_pool_info()``.
    Returns an empty dict when no pool is reachable in the current runtime.
    """
    try:
        from baldur.factory.registry import ProviderRegistry

        return ProviderRegistry.pool_info.get().get_pool_info()
    except Exception as e:
        logger.debug(
            "sqlalchemy_pool.retrieve_pool_info_failed",
            error=str(e),
        )
        return {}


def _extract_pool_info(pool: Any) -> dict[str, Any]:
    """Extract metrics from a SQLAlchemy-compatible pool object.

    Shared by ``DjangoPoolInfoProvider`` and ``SQLAlchemyPoolInfoProvider``.
    """
    try:
        pool_size = pool.size()
        checkedout = pool.checkedout()
        checkedin = pool.checkedin()
        overflow = pool.overflow()
        max_overflow = getattr(pool, "_max_overflow", 0)

        return {
            "pool_type": type(pool).__name__,
            "pool_size": pool_size,
            "max_overflow": max_overflow,
            "checkedin": checkedin,
            "checkedout": checkedout,
            "overflow": overflow,
            "total_capacity": pool_size + max_overflow,
            "available": checkedin,
            "pool_exhausted": checkedin == 0 and checkedout >= pool_size,
        }
    except Exception as e:
        return {"pool_type": type(pool).__name__, "error": str(e)}


__all__ = [
    "get_pool_info",
]
