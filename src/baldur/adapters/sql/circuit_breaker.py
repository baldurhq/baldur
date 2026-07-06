"""
SQL CircuitBreakerState repository.

Framework-free adapter for ``CircuitBreakerStateRepository`` backed by
any DB-API 2.0 database. ``service_name`` is the natural key — one row
per named breaker.

Atomic operations (``atomic_force_open`` / ``atomic_force_close`` /
``atomic_reset`` / ``try_acquire_for_replay`` analogues) rely on
``SELECT ... FOR UPDATE`` where supported; sqlite degrades to the
implicit row-level locking provided by its single-writer model.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import structlog

from baldur.adapters.sql.base import (
    GenericSQLRepository,
    dialect_bigserial,
    dialect_json_type,
    dialect_timestamp_type,
)
from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
    CircuitBreakerStateRepository,
)
from baldur.settings.sql import SQLDialect
from baldur.utils.time import utc_now

__all__ = ["SQLCircuitBreakerStateRepository"]

logger = structlog.get_logger()


_TABLE = "baldur_cb_state"
_SCHEMA_VERSION = 2


def _ddl(dialect: SQLDialect) -> list[str]:
    ts = dialect_timestamp_type(dialect)
    js = dialect_json_type(dialect)
    pk = dialect_bigserial(dialect)
    # 476 D8: ``half_open_window_started_at`` is the watermark for the
    # current HALF_OPEN trial window. Used by ``try_acquire_half_open_slot``
    # to detect a stalled window (worker crashed mid-trial) and auto-reset
    # on the next acquire.
    return [
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id {pk},
            service_name VARCHAR(256) NOT NULL UNIQUE,
            state VARCHAR(32) NOT NULL,
            failure_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            half_open_request_count INTEGER NOT NULL DEFAULT 0,
            half_open_window_started_at {ts},
            last_failure_at {ts},
            opened_at {ts},
            manually_controlled INTEGER NOT NULL DEFAULT 0,
            controlled_by_id BIGINT,
            control_reason VARCHAR(512) NOT NULL DEFAULT '',
            manual_override_expires_at {ts},
            created_at {ts} NOT NULL,
            updated_at {ts} NOT NULL,
            metadata {js} NOT NULL
        )
        """,
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_state ON {_TABLE} (state)",
    ]


_SELECT_COLS = (
    "id, service_name, state, failure_count, success_count, "
    "half_open_request_count, half_open_window_started_at, "
    "last_failure_at, opened_at, "
    "manually_controlled, controlled_by_id, control_reason, "
    "manual_override_expires_at, created_at, updated_at, metadata"
)


class SQLCircuitBreakerStateRepository(
    GenericSQLRepository, CircuitBreakerStateRepository
):
    """DB-API 2.0 backed circuit-breaker state repository."""

    def __init__(
        self,
        get_connection: Callable[[], Any],
        *,
        dialect: SQLDialect | None = None,
        autocommit_delegated: bool | None = None,
    ) -> None:
        super().__init__(
            get_connection,
            dialect=dialect,
            autocommit_delegated=autocommit_delegated,
            schema=(_TABLE, _SCHEMA_VERSION, _ddl),
        )
        # 476: marker for the most recent try_acquire_half_open_slot result.
        # See InMemoryCircuitBreakerStateRepository for the contract.
        self._last_acquire_marker: str = ""

    # ----- Row <-> DTO ------------------------------------------------------

    def _row_to_data(self, row: tuple) -> CircuitBreakerStateData:
        metadata = self._loads_json(row[15]) or {}
        return CircuitBreakerStateData(
            id=int(row[0]),
            service_name=row[1],
            state=row[2],
            failure_count=int(row[3] or 0),
            success_count=int(row[4] or 0),
            half_open_request_count=int(row[5] or 0),
            half_open_window_started_at=self._dt_from_db(row[6]),
            last_failure_at=self._dt_from_db(row[7]),
            opened_at=self._dt_from_db(row[8]),
            manually_controlled=bool(row[9]),
            controlled_by_id=row[10],
            control_reason=row[11] or "",
            manual_override_expires_at=self._dt_from_db(row[12]),
            created_at=self._dt_from_db(row[13]),
            updated_at=self._dt_from_db(row[14]),
            metadata=metadata,
        )

    # ----- Read -------------------------------------------------------------

    def get_by_service_name(self, service_name: str) -> CircuitBreakerStateData | None:
        row = self._fetch_one(
            f"SELECT {_SELECT_COLS} FROM {_TABLE} WHERE service_name = %s",
            (service_name,),
        )
        return self._row_to_data(row) if row else None

    def get_or_create(self, service_name: str) -> CircuitBreakerStateData:
        existing = self.get_by_service_name(service_name)
        if existing is not None:
            return existing
        now = utc_now()
        self._execute(
            f"INSERT INTO {_TABLE} "
            f"(service_name, state, failure_count, success_count, "
            f"half_open_request_count, manually_controlled, control_reason, "
            f"created_at, updated_at, metadata) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                service_name,
                CircuitBreakerStateEnum.CLOSED.value,
                0,
                0,
                0,
                0,
                "",
                self._dt_to_db(now),
                self._dt_to_db(now),
                self._dumps_json({}),
            ),
        )
        result = self.get_by_service_name(service_name)
        if result is None:
            # Rare race — another worker inserted concurrently and our
            # SELECT won but the caller needs a valid row.
            raise RuntimeError(
                f"baldur.sql: circuit breaker '{service_name}' vanished after insert"
            )
        return result

    def get_all_states(self) -> list[CircuitBreakerStateData]:
        rows = self._fetch_all(
            f"SELECT {_SELECT_COLS} FROM {_TABLE} ORDER BY service_name ASC"
        )
        return [self._row_to_data(r) for r in rows]

    # ----- Mutations --------------------------------------------------------

    def update_state(
        self,
        service_name: str,
        state: str,
        failure_count: int | None = None,
        success_count: int | None = None,
        opened_at: datetime | None = None,
        last_failure_at: datetime | None = None,  # noqa: ARG002 — interface compat
        half_open_request_count: int | None = None,
        reset_half_open_count: bool = False,
    ) -> bool:
        existing = self.get_by_service_name(service_name)
        if existing is None:
            return False
        now = utc_now()
        resolved_fail = (
            failure_count if failure_count is not None else existing.failure_count
        )
        resolved_success = (
            success_count if success_count is not None else existing.success_count
        )
        resolved_opened = opened_at if opened_at is not None else existing.opened_at

        if reset_half_open_count:
            resolved_half_open = 0
            window_changed = True
        elif half_open_request_count is not None:
            resolved_half_open = half_open_request_count
            window_changed = False
        else:
            resolved_half_open = existing.half_open_request_count
            window_changed = False

        if window_changed:
            self._execute(
                f"UPDATE {_TABLE} SET state = %s, failure_count = %s, "
                f"success_count = %s, opened_at = %s, "
                f"half_open_request_count = %s, "
                f"half_open_window_started_at = NULL, "
                f"updated_at = %s "
                f"WHERE service_name = %s",
                (
                    state,
                    resolved_fail,
                    resolved_success,
                    self._dt_to_db(resolved_opened),
                    resolved_half_open,
                    self._dt_to_db(now),
                    service_name,
                ),
            )
        else:
            self._execute(
                f"UPDATE {_TABLE} SET state = %s, failure_count = %s, "
                f"success_count = %s, opened_at = %s, "
                f"half_open_request_count = %s, updated_at = %s "
                f"WHERE service_name = %s",
                (
                    state,
                    resolved_fail,
                    resolved_success,
                    self._dt_to_db(resolved_opened),
                    resolved_half_open,
                    self._dt_to_db(now),
                    service_name,
                ),
            )
        return True

    def try_acquire_half_open_slot(  # noqa: C901, PLR0912, PLR0915
        self,
        service_name: str,
        limit: int,
        stuck_timeout_seconds: int,
    ) -> tuple[bool, str, str]:
        """Atomic HALF_OPEN slot acquisition.

        Uses ``SELECT ... FOR UPDATE NOWAIT`` (PostgreSQL/MySQL 8+) to
        serialize concurrent acquires; on lock contention, fails open with
        ``(False, current_state, current_state)``. SQLite falls back
        to its implicit single-writer model (no NOWAIT clause).
        """
        # 476 D2/C6/D8: serialize acquires; fail open on contention.
        # Ensure the row exists so the SELECT below has a target.
        self.get_or_create(service_name)

        if self._dialect == SQLDialect.SQLITE:
            select_sql = (
                f"SELECT state, half_open_request_count, "
                f"half_open_window_started_at "
                f"FROM {_TABLE} WHERE service_name = %s"
            )
        else:
            select_sql = (
                f"SELECT state, half_open_request_count, "
                f"half_open_window_started_at "
                f"FROM {_TABLE} WHERE service_name = %s FOR UPDATE NOWAIT"
            )

        conn = self._borrow_connection()
        cursor = conn.cursor()
        try:
            try:
                cursor.execute(self._prepare(select_sql), (service_name,))
                row = cursor.fetchone()
            except Exception as e:
                # Lock contention (LockNotAvailable) — fail-open per C6.
                if self._should_commit(conn):
                    try:
                        conn.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                logger.warning(
                    "sql_cb_repo.try_acquire_half_open_slot_lock_contention",
                    service=service_name,
                    error=str(e),
                )
                current = self.get_by_service_name(service_name)
                current_state = current.state if current else "closed"
                self._last_acquire_marker = "rejected"
                return (False, current_state, current_state)

            if row is None:
                self._last_acquire_marker = "no_op"
                return (False, "closed", "closed")

            current_state = row[0]
            count = int(row[1] or 0)
            watermark_dt = self._dt_from_db(row[2])
            now = utc_now()

            if current_state == "half_open" and count >= limit:
                window_age = (
                    (now - watermark_dt).total_seconds()
                    if watermark_dt is not None
                    else float("inf")
                )
                if window_age > stuck_timeout_seconds:
                    cursor.execute(
                        self._prepare(
                            f"UPDATE {_TABLE} SET state = %s, success_count = 0, "
                            f"half_open_request_count = 1, "
                            f"half_open_window_started_at = %s, "
                            f"updated_at = %s WHERE service_name = %s"
                        ),
                        (
                            "half_open",
                            self._dt_to_db(now),
                            self._dt_to_db(now),
                            service_name,
                        ),
                    )
                    if self._should_commit(conn):
                        conn.commit()
                    self._last_acquire_marker = "stuck_recovery"
                    return (True, "half_open", "half_open")

                if self._should_commit(conn):
                    conn.commit()
                self._last_acquire_marker = "rejected"
                return (False, "half_open", "half_open")

            if current_state == "open":
                cursor.execute(
                    self._prepare(
                        f"UPDATE {_TABLE} SET state = %s, success_count = 0, "
                        f"half_open_request_count = 1, "
                        f"half_open_window_started_at = %s, "
                        f"updated_at = %s WHERE service_name = %s"
                    ),
                    (
                        "half_open",
                        self._dt_to_db(now),
                        self._dt_to_db(now),
                        service_name,
                    ),
                )
                if self._should_commit(conn):
                    conn.commit()
                self._last_acquire_marker = "transition"
                return (True, "open", "half_open")

            if current_state == "half_open" and count < limit:
                cursor.execute(
                    self._prepare(
                        f"UPDATE {_TABLE} SET "
                        f"half_open_request_count = half_open_request_count + 1, "
                        f"updated_at = %s WHERE service_name = %s"
                    ),
                    (self._dt_to_db(now), service_name),
                )
                if self._should_commit(conn):
                    conn.commit()
                self._last_acquire_marker = "increment"
                return (True, "half_open", "half_open")

            if self._should_commit(conn):
                conn.commit()
            self._last_acquire_marker = "no_op"
            return (False, current_state, current_state)
        except Exception:
            if self._should_commit(conn):
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            self._last_acquire_marker = ""
            raise
        finally:
            try:
                cursor.close()
            except Exception:  # noqa: BLE001
                pass

    def reset_half_open_count(self, service_name: str) -> None:
        """Reset HALF_OPEN counter and clear window watermark."""
        # 476 G8: watermark reset paired with the HALF_OPEN counter.
        now = utc_now()
        self._execute(
            f"UPDATE {_TABLE} SET half_open_request_count = 0, "
            f"half_open_window_started_at = NULL, updated_at = %s "
            f"WHERE service_name = %s",
            (self._dt_to_db(now), service_name),
        )

    def record_failure(self, service_name: str) -> CircuitBreakerStateData:
        self.get_or_create(service_name)
        now = utc_now()
        self._execute(
            f"UPDATE {_TABLE} SET failure_count = failure_count + 1, "
            f"last_failure_at = %s, updated_at = %s WHERE service_name = %s",
            (self._dt_to_db(now), self._dt_to_db(now), service_name),
        )
        result = self.get_by_service_name(service_name)
        if result is None:
            # python -O strips asserts; raise explicitly so a vanished row
            # surfaces as a loud, typed error rather than AttributeError on
            # the caller's ``result.state`` access.
            raise RuntimeError(
                f"baldur.sql: circuit breaker '{service_name}' vanished "
                "after record_failure update"
            )
        return result

    def record_success(self, service_name: str) -> CircuitBreakerStateData:
        self.get_or_create(service_name)
        now = utc_now()
        self._execute(
            f"UPDATE {_TABLE} SET success_count = success_count + 1, "
            f"updated_at = %s WHERE service_name = %s",
            (self._dt_to_db(now), service_name),
        )
        result = self.get_by_service_name(service_name)
        if result is None:
            raise RuntimeError(
                f"baldur.sql: circuit breaker '{service_name}' vanished "
                "after record_success update"
            )
        return result

    def record_success_with_close_check(self, service_name, success_threshold):  # noqa: C901, PLR0912
        """Atomic HALF_OPEN -> CLOSED close-check via SELECT FOR UPDATE NOWAIT.

        Mirrors ``try_acquire_half_open_slot``'s SQL pattern.
        Concurrent transactions on the same row are serialized by the
        row-level lock; on ``NOWAIT`` lock contention the driver exception
        is re-raised so the Layered wrapper records the
        degraded-mode metric and delegates to L1.

        Branches mirror the Redis Lua:
        - ``state='half_open'``: increment ``success_count``; transition
          to CLOSED + reset counters/watermarks when the threshold is
          crossed (``did_close=True``). Otherwise persist the increment.
        - ``state='closed'``: race-loser / post-crash convergence -- no
          write, return ``(did_close=False, state='closed', count=0)``.
        - ``state in {open, missing, unknown}``: stale relative to the
          caller's HALF_OPEN expectation; the wrapper's stale-L2 guard
          falls back to L1's atomic close path.
        """
        from baldur.interfaces.repositories import CircuitBreakerCloseAttempt

        # D1: branches mirror the Redis Lua. D6 step 5: re-raise NOWAIT
        # contention so the Layered wrapper records degraded mode + delegates.
        # Ensure the row exists so the SELECT below has a target.
        self.get_or_create(service_name)

        if self._dialect == SQLDialect.SQLITE:
            select_sql = (
                f"SELECT state, success_count FROM {_TABLE} WHERE service_name = %s"
            )
        else:
            select_sql = (
                f"SELECT state, success_count FROM {_TABLE} "
                f"WHERE service_name = %s FOR UPDATE NOWAIT"
            )

        def _attempt(state_str, count, did_close):
            state_data = CircuitBreakerStateData(
                service_name=service_name,
                id=None,
                state=state_str,
                failure_count=0,
                success_count=count,
                last_failure_at=None,
                opened_at=None,
                manually_controlled=False,
                controlled_by_id=None,
                control_reason="",
                manual_override_expires_at=None,
                half_open_request_count=0,
                half_open_window_started_at=None,
                metadata={},
                created_at=None,
                updated_at=None,
            )
            return CircuitBreakerCloseAttempt(state=state_data, did_close=did_close)

        conn = self._borrow_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(self._prepare(select_sql), (service_name,))
            row = cursor.fetchone()

            if row is None:
                if self._should_commit(conn):
                    conn.commit()
                return _attempt("missing", 0, False)

            current_state = row[0]
            current_count = int(row[1] or 0)
            now = utc_now()

            if current_state == CircuitBreakerStateEnum.HALF_OPEN.value:
                new_count = current_count + 1
                if new_count >= success_threshold:
                    cursor.execute(
                        self._prepare(
                            f"UPDATE {_TABLE} SET state = %s, failure_count = 0, "
                            f"success_count = 0, opened_at = NULL, "
                            f"half_open_request_count = 0, "
                            f"half_open_window_started_at = NULL, "
                            f"updated_at = %s WHERE service_name = %s"
                        ),
                        (
                            CircuitBreakerStateEnum.CLOSED.value,
                            self._dt_to_db(now),
                            service_name,
                        ),
                    )
                    if self._should_commit(conn):
                        conn.commit()
                    return _attempt(CircuitBreakerStateEnum.CLOSED.value, 0, True)

                cursor.execute(
                    self._prepare(
                        f"UPDATE {_TABLE} SET success_count = %s, "
                        f"updated_at = %s WHERE service_name = %s"
                    ),
                    (new_count, self._dt_to_db(now), service_name),
                )
                if self._should_commit(conn):
                    conn.commit()
                return _attempt(
                    CircuitBreakerStateEnum.HALF_OPEN.value, new_count, False
                )

            if current_state == CircuitBreakerStateEnum.CLOSED.value:
                if self._should_commit(conn):
                    conn.commit()
                return _attempt(CircuitBreakerStateEnum.CLOSED.value, 0, False)

            # state in {open, unknown}: stale sentinel -- no write.
            if self._should_commit(conn):
                conn.commit()
            return _attempt(current_state, 0, False)
        except Exception:
            if self._should_commit(conn):
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            raise
        finally:
            try:
                cursor.close()
            except Exception:  # noqa: BLE001
                pass

    def record_failure_with_open_check(self, service_name):
        # 656 D7: failure-side symmetric mirror of the close-check.
        """Atomic HALF_OPEN -> OPEN re-open check via SELECT FOR UPDATE NOWAIT.

        Symmetric mirror of ``record_success_with_close_check``, scoped to the
        failure path. A single HALF_OPEN failure re-opens unconditionally (no
        threshold). Concurrent transactions on the same row are serialized by
        the row-level lock; on ``NOWAIT`` contention the driver exception is
        re-raised so the Layered wrapper records the degraded-mode metric and
        delegates to L1. The locked read-decide-write is factored into
        ``_open_check_locked``; this method owns the connection / commit /
        rollback boilerplate and commits once after the decision.

        Branches mirror the Redis Lua:
        - ``state='half_open'``: transition to OPEN, set ``opened_at``, reset
          counters/watermarks (``did_open=True``).
        - ``state='open'``: race-loser / already-open -- no write, return
          ``(did_open=False, state='open', <existing opened_at>)``.
        - ``state in {closed, missing, unknown}``: stale relative to the
          caller's HALF_OPEN expectation; the wrapper trusts L2 (closed) or
          falls back to L1's atomic re-open path (missing/unknown).
        """
        self.get_or_create(service_name)

        conn = self._borrow_connection()
        cursor = conn.cursor()
        try:
            attempt = self._open_check_locked(cursor, service_name)
            if self._should_commit(conn):
                conn.commit()
            return attempt
        except Exception:
            if self._should_commit(conn):
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            raise
        finally:
            try:
                cursor.close()
            except Exception:  # noqa: BLE001
                pass

    def _open_check_locked(self, cursor, service_name):
        """Locked read-decide-write for ``record_failure_with_open_check``.

        Runs inside the caller's transaction (the SELECT takes the row lock);
        the caller commits / rolls back. Returns the ``CircuitBreakerOpenAttempt``
        without committing.
        """
        from baldur.interfaces.repositories import CircuitBreakerOpenAttempt

        def _attempt(state_str, opened_at, did_open):
            state_data = CircuitBreakerStateData(
                service_name=service_name,
                id=None,
                state=state_str,
                failure_count=0,
                success_count=0,
                last_failure_at=None,
                opened_at=opened_at,
                manually_controlled=False,
                controlled_by_id=None,
                control_reason="",
                manual_override_expires_at=None,
                half_open_request_count=0,
                half_open_window_started_at=None,
                metadata={},
                created_at=None,
                updated_at=None,
            )
            return CircuitBreakerOpenAttempt(state=state_data, did_open=did_open)

        if self._dialect == SQLDialect.SQLITE:
            select_sql = (
                f"SELECT state, opened_at FROM {_TABLE} WHERE service_name = %s"
            )
        else:
            select_sql = (
                f"SELECT state, opened_at FROM {_TABLE} "
                f"WHERE service_name = %s FOR UPDATE NOWAIT"
            )

        cursor.execute(self._prepare(select_sql), (service_name,))
        row = cursor.fetchone()
        if row is None:
            return _attempt("missing", None, False)

        current_state = row[0]
        current_opened_at = self._dt_from_db(row[1])

        if current_state == CircuitBreakerStateEnum.HALF_OPEN.value:
            now = utc_now()
            cursor.execute(
                self._prepare(
                    f"UPDATE {_TABLE} SET state = %s, failure_count = 0, "
                    f"success_count = 0, opened_at = %s, "
                    f"half_open_request_count = 0, "
                    f"half_open_window_started_at = NULL, "
                    f"updated_at = %s WHERE service_name = %s"
                ),
                (
                    CircuitBreakerStateEnum.OPEN.value,
                    self._dt_to_db(now),
                    self._dt_to_db(now),
                    service_name,
                ),
            )
            return _attempt(CircuitBreakerStateEnum.OPEN.value, now, True)

        if current_state == CircuitBreakerStateEnum.OPEN.value:
            return _attempt(
                CircuitBreakerStateEnum.OPEN.value, current_opened_at, False
            )

        # state in {closed, unknown}: stale sentinel -- no write.
        return _attempt(current_state, current_opened_at, False)

    def set_manual_control(
        self,
        service_name: str,
        state: str,
        controlled_by_id: int | None = None,
        reason: str = "",
        expires_at: datetime | None = None,
    ) -> bool:
        self.get_or_create(service_name)
        now = utc_now()
        opened_at_expr = (
            self._dt_to_db(now) if state == CircuitBreakerStateEnum.OPEN.value else None
        )
        # Preserve existing opened_at unless transitioning to OPEN.
        if state == CircuitBreakerStateEnum.OPEN.value:
            self._execute(
                f"UPDATE {_TABLE} SET state = %s, manually_controlled = 1, "
                f"controlled_by_id = %s, control_reason = %s, "
                f"manual_override_expires_at = %s, opened_at = %s, updated_at = %s "
                f"WHERE service_name = %s",
                (
                    state,
                    controlled_by_id,
                    reason,
                    self._dt_to_db(expires_at),
                    opened_at_expr,
                    self._dt_to_db(now),
                    service_name,
                ),
            )
        else:
            self._execute(
                f"UPDATE {_TABLE} SET state = %s, manually_controlled = 1, "
                f"controlled_by_id = %s, control_reason = %s, "
                f"manual_override_expires_at = %s, updated_at = %s "
                f"WHERE service_name = %s",
                (
                    state,
                    controlled_by_id,
                    reason,
                    self._dt_to_db(expires_at),
                    self._dt_to_db(now),
                    service_name,
                ),
            )
        return True

    def clear_manual_control(
        self, service_name: str, preserve_reason: bool = False
    ) -> bool:
        existing = self.get_by_service_name(service_name)
        if existing is None:
            return False
        now = utc_now()
        if preserve_reason:
            self._execute(
                f"UPDATE {_TABLE} SET manually_controlled = 0, "
                f"controlled_by_id = NULL, manual_override_expires_at = NULL, "
                f"updated_at = %s WHERE service_name = %s",
                (self._dt_to_db(now), service_name),
            )
        else:
            self._execute(
                f"UPDATE {_TABLE} SET manually_controlled = 0, "
                f"controlled_by_id = NULL, control_reason = '', "
                f"manual_override_expires_at = NULL, updated_at = %s "
                f"WHERE service_name = %s",
                (self._dt_to_db(now), service_name),
            )
        return True

    def reset(self, service_name: str) -> bool:
        existing = self.get_by_service_name(service_name)
        if existing is None:
            return False
        now = utc_now()
        self._execute(
            f"UPDATE {_TABLE} SET state = %s, failure_count = 0, success_count = 0, "
            f"half_open_request_count = 0, half_open_window_started_at = NULL, "
            f"last_failure_at = NULL, opened_at = NULL, "
            f"manually_controlled = 0, controlled_by_id = NULL, control_reason = '', "
            f"manual_override_expires_at = NULL, updated_at = %s "
            f"WHERE service_name = %s",
            (CircuitBreakerStateEnum.CLOSED.value, self._dt_to_db(now), service_name),
        )
        return True

    def delete_state(self, service_name: str) -> bool:
        conn = self._borrow_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                self._prepare(f"DELETE FROM {_TABLE} WHERE service_name = %s"),
                (service_name,),
            )
            deleted = bool(cursor.rowcount)
            if self._should_commit(conn):
                conn.commit()
            return deleted
        except Exception:
            if self._should_commit(conn):
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            raise
        finally:
            cursor.close()

    # ----- Atomic operations (C15: BYO conn; rely on driver locking) --------

    def atomic_force_open(
        self,
        service_name: str,
        reason: str = "",
        controlled_by_id: int | None = None,
        ttl_minutes: int = 90,
    ) -> tuple[bool, str, str]:
        entry = self.get_or_create(service_name)
        previous = entry.state
        expires_at = (
            utc_now() + timedelta(minutes=ttl_minutes) if ttl_minutes > 0 else None
        )
        self.set_manual_control(
            service_name=service_name,
            state=CircuitBreakerStateEnum.OPEN.value,
            controlled_by_id=controlled_by_id,
            reason=reason,
            expires_at=expires_at,
        )
        return (True, previous, CircuitBreakerStateEnum.OPEN.value)

    def atomic_force_close(
        self,
        service_name: str,
        reason: str = "",
        controlled_by_id: int | None = None,
    ) -> tuple[bool, str, str]:
        entry = self.get_or_create(service_name)
        previous = entry.state
        now = utc_now()
        self._execute(
            f"UPDATE {_TABLE} SET state = %s, failure_count = 0, success_count = 0, "
            f"half_open_request_count = 0, half_open_window_started_at = NULL, "
            f"opened_at = NULL, "
            f"manually_controlled = 1, controlled_by_id = %s, control_reason = %s, "
            f"manual_override_expires_at = NULL, updated_at = %s "
            f"WHERE service_name = %s",
            (
                CircuitBreakerStateEnum.CLOSED.value,
                controlled_by_id,
                reason,
                self._dt_to_db(now),
                service_name,
            ),
        )
        return (True, previous, CircuitBreakerStateEnum.CLOSED.value)

    def atomic_reset(
        self,
        service_name: str,
        reason: str = "",
        controlled_by_id: int | None = None,
    ) -> tuple[bool, str, str]:
        entry = self.get_by_service_name(service_name)
        if entry is None:
            return (False, "", "")
        previous = entry.state
        now = utc_now()
        self._execute(
            f"UPDATE {_TABLE} SET state = %s, failure_count = 0, success_count = 0, "
            f"half_open_request_count = 0, half_open_window_started_at = NULL, "
            f"last_failure_at = NULL, "
            f"opened_at = NULL, manually_controlled = 0, controlled_by_id = %s, "
            f"control_reason = %s, manual_override_expires_at = NULL, updated_at = %s "
            f"WHERE service_name = %s",
            (
                CircuitBreakerStateEnum.CLOSED.value,
                controlled_by_id,
                reason,
                self._dt_to_db(now),
                service_name,
            ),
        )
        return (True, previous, CircuitBreakerStateEnum.CLOSED.value)
