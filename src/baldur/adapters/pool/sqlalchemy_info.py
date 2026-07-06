"""
SQLAlchemy-routed PoolInfoProvider implementation.

Used in framework-free deployments (Flask, FastAPI, plain Python). The
caller passes a SQLAlchemy ``Engine`` whose ``.pool`` attribute is the
``QueuePool`` (or compatible) instance.
"""

from __future__ import annotations

from typing import Any

from baldur.interfaces.pool_info import PoolInfoProvider

__all__ = ["SQLAlchemyPoolInfoProvider"]


class SQLAlchemyPoolInfoProvider(PoolInfoProvider):
    """PoolInfoProvider backed by a directly-passed SQLAlchemy engine."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def get_pool_info(self) -> dict[str, Any]:
        from baldur.adapters.sqlalchemy_pool import _extract_pool_info

        if self._engine is None:
            return {}
        try:
            pool = self._engine.pool
        except Exception:
            return {}
        if pool is None:
            return {}
        return _extract_pool_info(pool)
