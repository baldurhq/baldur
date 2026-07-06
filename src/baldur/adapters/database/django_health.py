"""
Django Database Health Adapter.

DatabaseHealthProvider backed by django.db.connections.
"""

from __future__ import annotations

from baldur.interfaces.database_health import (
    DatabaseConnectionInfo,
    DatabaseHealthProvider,
)

__all__ = ["DjangoDatabaseHealthAdapter"]


class DjangoDatabaseHealthAdapter(DatabaseHealthProvider):
    """DatabaseHealthProvider backed by django.db.connections."""

    def check_connection(self, alias: str = "default") -> DatabaseConnectionInfo:
        from django.db import connections

        conn = connections[alias]
        # Active probe: open a connection if one is not present and execute a
        # round-trip query. Django's `is_usable()` only validates the currently
        # held connection — with the default `CONN_MAX_AGE=0`, requests start
        # with `self.connection is None` or a closed connection, and
        # `is_usable()` returns False without ever touching the database. The
        # cascade contract is "is the database reachable?", not "is the saved
        # connection still good?", so we issue a real `SELECT 1` here.
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            is_usable = True
        except Exception:
            is_usable = False
        return DatabaseConnectionInfo(
            alias=alias,
            vendor=conn.vendor,
            is_usable=is_usable,
        )

    def list_aliases(self) -> list[str]:
        from django.db import connections

        return list(connections)

    def close_all(self) -> None:
        from django.db import connections

        for conn in connections.all():
            conn.close()
