"""
DB-API 2.0 Database Health Adapter.

DatabaseHealthProvider backed by a DB-API 2.0 ``get_connection`` callable.
Used by Flask / FastAPI / plain-Python deployments to satisfy the OSS
health-probe contract without requiring Django.

Lifecycle (515 D2): open-per-check. Each ``check_connection()`` opens a
connection via ``get_connection()``, runs ``SELECT 1``, closes the
connection. Pooled callables (SQLAlchemy ``engine.raw_connection``,
PgBouncer ``getconn``) reuse connections via the callable; the dev
``build_connection_factory`` opens fresh (acceptable for low-frequency
health probes).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog

from baldur.interfaces.database_health import (
    DatabaseConnectionInfo,
    DatabaseHealthProvider,
)
from baldur.settings.sql import SQLDialect

__all__ = ["SQLDatabaseHealthAdapter"]

logger = structlog.get_logger()


class SQLDatabaseHealthAdapter(DatabaseHealthProvider):
    """DatabaseHealthProvider backed by a DB-API 2.0 ``get_connection`` callable.

    Args:
        get_connection: callable returning a DB-API 2.0 connection. Each
            ``check_connection()`` invocation opens a fresh connection
            (or borrows one from the underlying pool) and closes it after
            ``SELECT 1`` completes.
        dialect: SQLDialect — used as the ``vendor`` field, matching the
            semantics of Django's ``conn.vendor`` (vendor name, not version).
    """

    def __init__(
        self,
        get_connection: Callable[[], Any],
        dialect: SQLDialect,
    ) -> None:
        self._get_connection = get_connection
        self._dialect = dialect

    def check_connection(self, alias: str = "default") -> DatabaseConnectionInfo:
        is_usable = False
        try:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                try:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                    is_usable = True
                finally:
                    try:
                        cursor.close()
                    except Exception:
                        pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as e:
            logger.debug(
                "sql_database_health.check_failed",
                alias=alias,
                dialect=self._dialect.value,
                error=str(e),
            )
        return DatabaseConnectionInfo(
            alias=alias,
            vendor=self._dialect.value,
            is_usable=is_usable,
        )

    def list_aliases(self) -> list[str]:
        return ["default"]

    def close_all(self) -> None:
        return None
