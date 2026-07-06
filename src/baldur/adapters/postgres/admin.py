"""
PostgreSQL admin SQL primitives — single class, backend-injected.

:class:`PgAdmin` holds every PG-admin SQL string (``pg_stat_activity``,
``pg_advisory_lock``, ``pg_sleep``, session-timeout setters, stress-test
primitives). Backend access is injected as two callables — ``get_session``
(context-managed cursor for the 19 methods whose SQL emission fits one
``with`` block) and ``get_connection`` (raw connection used only by
``create_cursor``, whose returned cursor outlives any ``with`` block).

Replaces ``baldur.adapters.postgres.repository.PostgresRepository``
(515 — Framework Independence for Postgres Adapters).
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

import structlog

from baldur.interfaces.pg_admin import (
    ConnectionStats,
    PgAdminProvider,
)

__all__ = ["PgAdmin"]

logger = structlog.get_logger()


class PgAdmin(PgAdminProvider):
    """PostgreSQL admin SQL primitives with callable-injected backend access.

    Args:
        get_session: callable returning a context manager that yields a
            DB-API cursor whose underlying connection is held stable for
            the entire ``with`` block — multiple ``cursor.execute()``
            calls within one ``with get_session() as cur:`` block MUST
            hit the same backend session. Required for
            ``advisory_lock_context`` correctness.
        get_connection: callable returning a raw DB-API connection,
            used only by ``create_cursor``. That method returns a cursor
            whose lifecycle outlives any ``with`` block — callers store
            and close it on a separate code path. Separating this from
            ``get_session`` keeps the context-managed contract honest.
        label: identifier used in log records (e.g. DB alias).
    """

    def __init__(
        self,
        get_session: Callable[[], AbstractContextManager[Any]],
        get_connection: Callable[[], Any],
        label: str = "default",
    ) -> None:
        self._get_session = get_session
        self._get_connection = get_connection
        self._label = label

    def is_available(self) -> bool:
        return True

    # =========================================================================
    # Connection & Health Check
    # =========================================================================

    def ping(self) -> bool:
        try:
            with self._get_session() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            return True
        except Exception as e:
            logger.exception(
                "pg_admin.ping_failed",
                label=self._label,
                error=e,
            )
            return False

    def get_connection_stats(self) -> ConnectionStats:
        with self._get_session() as cursor:
            cursor.execute(
                """
                SELECT
                    count(*) as total_connections,
                    count(*) FILTER (WHERE state = 'active') as active,
                    count(*) FILTER (WHERE state = 'idle') as idle,
                    count(*) FILTER (WHERE state = 'idle in transaction') as idle_in_tx
                FROM pg_stat_activity
                WHERE datname = current_database()
                """
            )
            row = cursor.fetchone()

        return ConnectionStats(
            total_connections=row[0],
            active=row[1],
            idle=row[2],
            idle_in_transaction=row[3],
        )

    def get_active_connection_count(self) -> int:
        with self._get_session() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE state = 'active'"
            )
            result = cursor.fetchone()
        return result[0] if result else 0

    # =========================================================================
    # Sleep & Delay
    # =========================================================================

    def pg_sleep(self, seconds: float) -> None:
        with self._get_session() as cursor:
            cursor.execute(f"SELECT pg_sleep({seconds})")
            cursor.fetchone()

    def execute_slow_query(self, seconds: int) -> None:
        with self._get_session() as cursor:
            cursor.execute(f"SELECT pg_sleep({seconds})")
            cursor.fetchone()

    def get_backend_pid_with_delay(self, delay_seconds: float = 0.01) -> int:
        with self._get_session() as cursor:
            cursor.execute(f"SELECT pg_backend_pid(), pg_sleep({delay_seconds})")
            result = cursor.fetchone()
        return result[0] if result else 0

    # =========================================================================
    # Advisory Lock Operations
    # =========================================================================

    def acquire_advisory_lock(self, lock_id: int, wait: bool = True) -> bool:
        with self._get_session() as cursor:
            if wait:
                cursor.execute("SELECT pg_advisory_lock(%s)", [lock_id])
                return True
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock_id])
            result = cursor.fetchone()
            return result[0] if result else False

    def acquire_advisory_lock_shared(self, lock_id: int, wait: bool = True) -> bool:
        with self._get_session() as cursor:
            if wait:
                cursor.execute("SELECT pg_advisory_lock_shared(%s)", [lock_id])
                return True
            cursor.execute("SELECT pg_try_advisory_lock_shared(%s)", [lock_id])
            result = cursor.fetchone()
            return result[0] if result else False

    def release_advisory_lock(self, lock_id: int) -> bool:
        with self._get_session() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])
            result = cursor.fetchone()
        return result[0] if result else False

    def release_advisory_lock_shared(self, lock_id: int) -> bool:
        with self._get_session() as cursor:
            cursor.execute("SELECT pg_advisory_unlock_shared(%s)", [lock_id])
            result = cursor.fetchone()
        return result[0] if result else False

    def try_advisory_lock(self, lock_id: int) -> bool:
        return self.acquire_advisory_lock(lock_id, wait=False)

    # =========================================================================
    # Session Settings
    # =========================================================================

    def set_lock_timeout(self, timeout_ms: int) -> None:
        with self._get_session() as cursor:
            if timeout_ms == 0:
                cursor.execute("SET lock_timeout = '0'")
            else:
                cursor.execute(f"SET lock_timeout = '{timeout_ms}ms'")

    def set_statement_timeout(self, timeout_ms: int) -> None:
        with self._get_session() as cursor:
            if timeout_ms == 0:
                cursor.execute("SET statement_timeout = '0'")
            else:
                cursor.execute(f"SET statement_timeout = '{timeout_ms}ms'")

    def reset_timeouts(self) -> None:
        with self._get_session() as cursor:
            cursor.execute("SET lock_timeout = '0'")
            cursor.execute("SET statement_timeout = '0'")

    # =========================================================================
    # Stress Test Primitives
    # =========================================================================

    def execute_aggregate_query(
        self, table_name: str
    ) -> tuple[int, float, float, float]:
        with self._get_session() as cursor:
            cursor.execute(
                f"""
                SELECT
                    COUNT(*) as total_products,
                    AVG(price) as avg_price,
                    MAX(price) as max_price,
                    MIN(price) as min_price
                FROM {table_name}
                WHERE is_active = true
                """
            )
            row = cursor.fetchone()

        return (
            row[0],
            float(row[1]) if row[1] else 0.0,
            float(row[2]) if row[2] else 0.0,
            float(row[3]) if row[3] else 0.0,
        )

    def execute_nonexistent_table_query(self) -> None:
        with self._get_session() as cursor:
            cursor.execute("SELECT * FROM __nonexistent_table_for_cb_test__")

    def execute_timeout_query(
        self, timeout_ms: int = 1, sleep_seconds: int = 1
    ) -> None:
        with self._get_session() as cursor:
            cursor.execute(f"SET statement_timeout = '{timeout_ms}ms'")
            cursor.execute(f"SELECT pg_sleep({sleep_seconds})")

    # =========================================================================
    # Context Managers
    # =========================================================================

    @contextmanager
    def advisory_lock_context(
        self, lock_id: int, exclusive: bool = True, wait: bool = True
    ) -> Generator[bool, None, None]:
        """Hold an advisory lock for the duration of the ``with`` block.

        The acquire and release must share one backend session — both
        SQL statements run inside the same ``with self._get_session()``
        block to guarantee that.
        """
        with self._get_session() as cursor:
            acquired = False
            try:
                if exclusive:
                    if wait:
                        cursor.execute("SELECT pg_advisory_lock(%s)", [lock_id])
                        acquired = True
                    else:
                        cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock_id])
                        result = cursor.fetchone()
                        acquired = result[0] if result else False
                else:
                    if wait:
                        cursor.execute("SELECT pg_advisory_lock_shared(%s)", [lock_id])
                        acquired = True
                    else:
                        cursor.execute(
                            "SELECT pg_try_advisory_lock_shared(%s)", [lock_id]
                        )
                        result = cursor.fetchone()
                        acquired = result[0] if result else False

                yield acquired
            finally:
                if acquired:
                    try:
                        if exclusive:
                            cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_id])
                        else:
                            cursor.execute(
                                "SELECT pg_advisory_unlock_shared(%s)",
                                [lock_id],
                            )
                    except Exception as e:
                        logger.warning(
                            "pg_admin.release_lock_failed",
                            label=self._label,
                            lock_id=lock_id,
                            error=e,
                        )

    @contextmanager
    def timeout_context(
        self, lock_timeout_ms: int = 0, statement_timeout_ms: int = 0
    ) -> Generator[None, None, None]:
        """Apply session timeouts within a ``with`` block, restoring on exit.

        DeadlineContext compatibility: if the deadline-aware statement
        timeout is shorter than the requested value, the deadline wins
        (prevents overshooting an upstream deadline).
        """
        try:
            from baldur.scaling.deadline_context import (
                get_deadline_aware_statement_timeout,
            )

            deadline_timeout = get_deadline_aware_statement_timeout(
                default_db_timeout_ms=(
                    statement_timeout_ms if statement_timeout_ms > 0 else 30_000
                ),
            )
            if deadline_timeout is not None:
                statement_timeout_ms = deadline_timeout
        except ImportError:
            pass

        try:
            if lock_timeout_ms > 0:
                self.set_lock_timeout(lock_timeout_ms)
            if statement_timeout_ms > 0:
                self.set_statement_timeout(statement_timeout_ms)
            yield
        finally:
            self.reset_timeouts()

    # =========================================================================
    # Cursor escape hatches
    # =========================================================================

    def create_cursor(self) -> Any:
        return self._get_connection().cursor()

    def execute_with_cursor(
        self, cursor: Any, query: str, params: list[Any] | None = None
    ) -> Any:
        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)
        return cursor.fetchone()
