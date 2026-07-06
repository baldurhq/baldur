"""
SQLAlchemy Pool Stats Provider.

Wraps existing get_pool_info() to implement PoolStatsProvider ABC.
Sync engine only — AsyncEngine is detected and rejected at registration.
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.interfaces.pool_monitor import PoolStats, PoolStatsProvider

logger = structlog.get_logger()


class SQLAlchemyPoolStatsProvider(PoolStatsProvider):
    """
    PoolStatsProvider implementation for SQLAlchemy connection pools.

    Wraps existing get_pool_info() (adapters/sqlalchemy_pool.py) and maps
    its output to PoolStats dataclass.

    Args:
        engine: SQLAlchemy Engine instance (sync only)
        pool_name: Pool identifier (Django DATABASES key as natural name)
    """

    def __init__(self, engine: Any = None, pool_name: str = "default"):
        if engine is not None:
            try:
                from sqlalchemy.ext.asyncio import AsyncEngine

                if isinstance(engine, AsyncEngine):
                    raise TypeError(
                        "Use async_engine.sync_engine to access "
                        "the underlying sync pool"
                    )
            except ImportError:
                pass
        self._engine = engine
        self._pool_name = pool_name

    def get_stats(self) -> PoolStats:
        """Get current pool statistics from SQLAlchemy pool."""
        from baldur.adapters.sqlalchemy_pool import get_pool_info

        info = get_pool_info()

        if not info:
            return PoolStats(
                pool_name=self._pool_name,
                max_connections=0,
                active_connections=0,
                available_connections=0,
                waiting_requests=0,
            )

        return PoolStats(
            pool_name=self._pool_name,
            max_connections=info.get("total_capacity", info.get("pool_size", 0)),
            active_connections=info.get("checkedout", info.get("num_checked_out", 0)),
            available_connections=info.get("checkedin", info.get("num_checked_in", 0)),
            waiting_requests=0,  # SQLAlchemy has no public queue depth API
        )


__all__ = ["SQLAlchemyPoolStatsProvider"]
