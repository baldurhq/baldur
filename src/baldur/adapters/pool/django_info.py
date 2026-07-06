"""
Django-routed PoolInfoProvider implementation.

Wraps the legacy ``baldur.adapters.sqlalchemy_pool.get_pool_info``
discovery logic (``conn.connection._pool`` → ``dj_db_conn_pool.pool_container``
→ ``conn.pool.pool``). Used when Django is the active runtime.
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.interfaces.pool_info import PoolInfoProvider

__all__ = ["DjangoPoolInfoProvider"]

logger = structlog.get_logger()


class DjangoPoolInfoProvider(PoolInfoProvider):
    """PoolInfoProvider that walks Django's ``connections["default"]`` graph."""

    def __init__(self, alias: str = "default") -> None:
        self._alias = alias

    def get_pool_info(self) -> dict[str, Any]:
        from baldur.adapters.sqlalchemy_pool import _extract_pool_info

        try:
            from django.db import connections
        except ImportError:
            return {}

        try:
            conn = connections[self._alias]
            conn.ensure_connection()

            if hasattr(conn, "connection") and conn.connection is not None:
                raw_conn = conn.connection
                if hasattr(raw_conn, "_pool"):
                    return _extract_pool_info(raw_conn._pool)

            try:
                from dj_db_conn_pool.core.mixins.core import pool_container

                if pool_container.has(self._alias):
                    return _extract_pool_info(pool_container.get(self._alias))
            except ImportError:
                pass

            if (
                hasattr(conn, "pool")
                and conn.pool is not None
                and hasattr(conn.pool, "pool")
            ):
                return _extract_pool_info(conn.pool.pool)

            return {}
        except Exception as e:
            logger.debug(
                "pool_info.django_retrieve_failed",
                alias=self._alias,
                error=str(e),
            )
            return {}
