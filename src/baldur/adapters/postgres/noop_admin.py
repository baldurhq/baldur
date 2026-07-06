"""
No-op PostgreSQL admin provider.

Returns safe defaults for runtimes that cannot reach a Postgres backend
(SQLite-only deployments, Django absent + no ``BALDUR_SQL_DSN``).

Consumers should check :meth:`NoopPgAdmin.is_available` and omit
PG-specific keys from response dicts when False — preserves fail-open
observability while making "feature N/A" explicit.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from baldur.interfaces.pg_admin import ConnectionStats, PgAdminProvider

__all__ = ["NoopPgAdmin"]


class NoopPgAdmin(PgAdminProvider):
    """Safe-default implementation when no Postgres backend is reachable."""

    def is_available(self) -> bool:
        return False

    def ping(self) -> bool:
        return False

    def get_connection_stats(self) -> ConnectionStats:
        return ConnectionStats(
            total_connections=0, active=0, idle=0, idle_in_transaction=0
        )

    def get_active_connection_count(self) -> int:
        return 0

    def pg_sleep(self, seconds: float) -> None:
        return None

    def execute_slow_query(self, seconds: int) -> None:
        return None

    def get_backend_pid_with_delay(self, delay_seconds: float = 0.01) -> int:
        return 0

    def acquire_advisory_lock(self, lock_id: int, wait: bool = True) -> bool:
        return False

    def acquire_advisory_lock_shared(self, lock_id: int, wait: bool = True) -> bool:
        return False

    def release_advisory_lock(self, lock_id: int) -> bool:
        return False

    def release_advisory_lock_shared(self, lock_id: int) -> bool:
        return False

    def try_advisory_lock(self, lock_id: int) -> bool:
        return False

    def set_lock_timeout(self, timeout_ms: int) -> None:
        return None

    def set_statement_timeout(self, timeout_ms: int) -> None:
        return None

    def reset_timeouts(self) -> None:
        return None

    def execute_aggregate_query(
        self, table_name: str
    ) -> tuple[int, float, float, float]:
        return (0, 0.0, 0.0, 0.0)

    def execute_nonexistent_table_query(self) -> None:
        return None

    def execute_timeout_query(
        self, timeout_ms: int = 1, sleep_seconds: int = 1
    ) -> None:
        return None

    @contextmanager
    def advisory_lock_context(
        self, lock_id: int, exclusive: bool = True, wait: bool = True
    ) -> Generator[bool, None, None]:
        yield False

    @contextmanager
    def timeout_context(
        self, lock_timeout_ms: int = 0, statement_timeout_ms: int = 0
    ) -> Generator[None, None, None]:
        yield

    def create_cursor(self) -> Any:
        raise RuntimeError(
            "NoopPgAdmin.create_cursor: no PostgreSQL backend is configured. "
            "Set BALDUR_SQL_DSN=postgresql://... or install Django with a "
            "configured DATABASES setting."
        )

    def execute_with_cursor(
        self, cursor: Any, query: str, params: list[Any] | None = None
    ) -> Any:
        return None
