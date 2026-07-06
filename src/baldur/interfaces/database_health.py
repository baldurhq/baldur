"""
Database Health Provider Interface.

Abstract interface for database connection health monitoring.
Provides framework-agnostic access to database connection metadata.

Replaces direct django.db.connections usage in service layer.

Layered architecture:
    ConnectionHealthMonitor (state tracking) — uses this via callback
    DatabaseHealthProvider  (connection metadata) — THIS LAYER
    Django/SQLAlchemy/...   (actual DB driver)

Implementations:
    - DjangoDatabaseHealthAdapter: wraps django.db.connections
    - NoopDatabaseHealthAdapter: returns safe defaults (no DB)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DatabaseConnectionInfo",
    "DatabaseHealthProvider",
]


@dataclass(frozen=True)
class DatabaseConnectionInfo:
    """Database connection metadata returned by health check."""

    alias: str
    vendor: str = "unknown"
    is_usable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class DatabaseHealthProvider(ABC):
    """
    Abstract interface for database connection health monitoring.

    Provides framework-agnostic access to database connection metadata.
    Replaces direct django.db.connections usage in service layer.
    """

    @abstractmethod
    def check_connection(self, alias: str = "default") -> DatabaseConnectionInfo:
        """
        Check a specific database connection.

        Returns connection metadata including vendor and usability status.
        """
        ...

    @abstractmethod
    def list_aliases(self) -> list[str]:
        """
        List all configured database aliases.

        Replaces: for alias in django.db.connections
        """
        ...

    @abstractmethod
    def close_all(self) -> None:
        """
        Close all database connections.

        Used in Gunicorn post_fork hooks to clean up parent FDs.
        Replaces: for conn in connections.all(): conn.close()
        """
        ...

    def health_check(self) -> bool:
        """
        Convenience: check default connection health.

        Can be registered as a callback in ConnectionHealthMonitor:
            monitor.register_health_check(
                ConnectionType.DATABASE, "default", provider.health_check
            )
        """
        try:
            return self.check_connection().is_usable
        except Exception:
            return False
