"""
No-op Database Health Adapter.

Returns safe defaults for environments without a database.
"""

from __future__ import annotations

from baldur.interfaces.database_health import (
    DatabaseConnectionInfo,
    DatabaseHealthProvider,
)

__all__ = ["NoopDatabaseHealthAdapter"]


class NoopDatabaseHealthAdapter(DatabaseHealthProvider):
    """No-op implementation for environments without a database."""

    def check_connection(self, alias: str = "default") -> DatabaseConnectionInfo:
        return DatabaseConnectionInfo(alias=alias, vendor="unknown", is_usable=False)

    def list_aliases(self) -> list[str]:
        return []

    def close_all(self) -> None:
        pass
