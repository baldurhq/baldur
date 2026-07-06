"""
PostgreSQL Admin Provider Interface.

Abstract interface for PostgreSQL administrative SQL primitives
(``pg_stat_activity``, ``pg_advisory_lock``, ``pg_sleep``, session-timeout
setters, stress-test cursor escape hatches). Implementations inject their
own session/connection acquisition so Django, DB-API 2.0 (psycopg2), and
no-op runtimes can all satisfy the same contract.

Replaces ``baldur.adapters.postgres.repository.PostgresRepository`` (515).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

__all__ = [
    "AdvisoryLockResult",
    "ConnectionStats",
    "PgAdminProvider",
]


@dataclass(frozen=True)
class ConnectionStats:
    """PostgreSQL connection statistics from ``pg_stat_activity``."""

    total_connections: int
    active: int
    idle: int
    idle_in_transaction: int


@dataclass(frozen=True)
class AdvisoryLockResult:
    """Result of an advisory lock attempt."""

    acquired: bool
    lock_id: int
    error: str | None = None


class PgAdminProvider(ABC):
    """Abstract interface for PostgreSQL admin SQL primitives.

    Concrete implementations route SQL through a runtime-specific cursor
    (Django ``connections[alias].cursor()``, DB-API 2.0 ``conn.cursor()``,
    or noop). Callers should not assume any specific backend.

    Availability gate: ``is_available()`` returns False for the no-op
    implementation so consumers can omit PG-specific keys from their
    response dicts when the underlying runtime cannot satisfy the contract.
    """

    @abstractmethod
    def is_available(self) -> bool:
        """Return True iff this provider can execute PG admin SQL."""
        ...

    # =========================================================================
    # Connection & Health Check
    # =========================================================================

    @abstractmethod
    def ping(self) -> bool:
        """Verify connectivity with ``SELECT 1``."""
        ...

    @abstractmethod
    def get_connection_stats(self) -> ConnectionStats:
        """Read connection state counts from ``pg_stat_activity``."""
        ...

    @abstractmethod
    def get_active_connection_count(self) -> int:
        """Return the count of ``state = 'active'`` rows in ``pg_stat_activity``."""
        ...

    # =========================================================================
    # Sleep & Delay
    # =========================================================================

    @abstractmethod
    def pg_sleep(self, seconds: float) -> None:
        """Execute ``SELECT pg_sleep(...)``."""
        ...

    @abstractmethod
    def execute_slow_query(self, seconds: int) -> None:
        """Hold a backend session via ``pg_sleep`` for the given duration."""
        ...

    @abstractmethod
    def get_backend_pid_with_delay(self, delay_seconds: float = 0.01) -> int:
        """Return ``pg_backend_pid()`` after a brief ``pg_sleep`` delay."""
        ...

    # =========================================================================
    # Advisory Lock Operations
    # =========================================================================

    @abstractmethod
    def acquire_advisory_lock(self, lock_id: int, wait: bool = True) -> bool:
        """Acquire an exclusive advisory lock."""
        ...

    @abstractmethod
    def acquire_advisory_lock_shared(self, lock_id: int, wait: bool = True) -> bool:
        """Acquire a shared advisory lock."""
        ...

    @abstractmethod
    def release_advisory_lock(self, lock_id: int) -> bool:
        """Release an exclusive advisory lock."""
        ...

    @abstractmethod
    def release_advisory_lock_shared(self, lock_id: int) -> bool:
        """Release a shared advisory lock."""
        ...

    @abstractmethod
    def try_advisory_lock(self, lock_id: int) -> bool:
        """Attempt a non-blocking exclusive advisory lock."""
        ...

    # =========================================================================
    # Session Settings
    # =========================================================================

    @abstractmethod
    def set_lock_timeout(self, timeout_ms: int) -> None:
        """Set session-level ``lock_timeout`` in milliseconds (0 = unlimited)."""
        ...

    @abstractmethod
    def set_statement_timeout(self, timeout_ms: int) -> None:
        """Set session-level ``statement_timeout`` in milliseconds (0 = unlimited)."""
        ...

    @abstractmethod
    def reset_timeouts(self) -> None:
        """Reset both ``lock_timeout`` and ``statement_timeout`` to unlimited."""
        ...

    # =========================================================================
    # Stress Test Primitives
    # =========================================================================

    @abstractmethod
    def execute_aggregate_query(
        self, table_name: str
    ) -> tuple[int, float, float, float]:
        """Execute ``COUNT/AVG/MAX/MIN`` over ``table_name``.

        Returns ``(total_count, avg_price, max_price, min_price)``.
        """
        ...

    @abstractmethod
    def execute_nonexistent_table_query(self) -> None:
        """Execute a query against a missing table (used to trigger CB failures)."""
        ...

    @abstractmethod
    def execute_timeout_query(
        self, timeout_ms: int = 1, sleep_seconds: int = 1
    ) -> None:
        """Execute a query that exceeds ``statement_timeout`` (CB testing)."""
        ...

    # =========================================================================
    # Context Managers for Complex Operations
    # =========================================================================

    @abstractmethod
    @contextmanager
    def advisory_lock_context(
        self, lock_id: int, exclusive: bool = True, wait: bool = True
    ) -> Generator[bool, None, None]:
        """Hold an advisory lock for the duration of a ``with`` block."""
        ...

    @abstractmethod
    @contextmanager
    def timeout_context(
        self, lock_timeout_ms: int = 0, statement_timeout_ms: int = 0
    ) -> Generator[None, None, None]:
        """Apply session timeouts within a ``with`` block, restoring on exit."""
        ...

    # =========================================================================
    # Cursor escape hatches (pool exhaustion / stress test)
    # =========================================================================

    @abstractmethod
    def create_cursor(self) -> Any:
        """Create a cursor whose lifecycle is owned by the caller.

        Used by pool-exhaustion paths that hold cursors externally
        (``StressTestService._held_connections``).
        """
        ...

    @abstractmethod
    def execute_with_cursor(
        self, cursor: Any, query: str, params: list[Any] | None = None
    ) -> Any:
        """Execute ``query`` on a caller-supplied ``cursor`` and return ``fetchone()``."""
        ...
