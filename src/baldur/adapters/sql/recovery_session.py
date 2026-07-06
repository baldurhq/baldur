"""
SQL RecoverySessionArchive repository.

Framework-free adapter for ``RecoverySessionArchiveRepository`` backed by
any DB-API 2.0 database. ``session_id`` is the natural primary key.
``update()`` performs a full-record replacement (all columns).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from baldur.adapters.sql.base import (
    GenericSQLRepository,
    dialect_json_type,
    dialect_timestamp_type,
)
from baldur.interfaces.repositories import RecoverySessionArchiveRepository
from baldur.models.recovery_session import RecoverySessionData
from baldur.settings.sql import SQLDialect
from baldur.utils.time import utc_now

__all__ = ["SQLRecoverySessionArchiveRepository"]


_TABLE = "baldur_recovery_session"
_SCHEMA_VERSION = 1


def _ddl(dialect: SQLDialect) -> list[str]:
    ts = dialect_timestamp_type(dialect)
    js = dialect_json_type(dialect)
    return [
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            session_id VARCHAR(256) PRIMARY KEY,
            namespace VARCHAR(128) NOT NULL,
            trigger_level VARCHAR(32) NOT NULL,
            status VARCHAR(32) NOT NULL,
            initiated_by VARCHAR(256) DEFAULT 'system',
            started_at {ts},
            completed_at {ts},
            duration_seconds REAL,
            abort_reason TEXT DEFAULT '',
            cascade_event_id VARCHAR(256) DEFAULT '',
            requires_approval INTEGER DEFAULT 0,
            approved_by VARCHAR(256) DEFAULT '',
            approved_at {ts},
            created_at {ts},
            updated_at {ts},
            data {js} NOT NULL
        )
        """,
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_ns ON {_TABLE} (namespace)",
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_status ON {_TABLE} (status)",
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_started ON {_TABLE} (started_at)",
    ]


_SELECT_COLS = (
    "session_id, namespace, trigger_level, status, initiated_by, "
    "started_at, completed_at, duration_seconds, abort_reason, "
    "cascade_event_id, requires_approval, approved_by, approved_at, "
    "created_at, updated_at, data"
)


class SQLRecoverySessionArchiveRepository(
    GenericSQLRepository, RecoverySessionArchiveRepository
):
    """DB-API 2.0 backed recovery session archive repository."""

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

    # ----- Row <-> DTO ------------------------------------------------------

    def _row_to_data(self, row: tuple) -> RecoverySessionData:
        data = self._loads_json(row[15]) or {}
        return RecoverySessionData(
            session_id=row[0],
            namespace=row[1],
            trigger_level=row[2],
            status=row[3],
            initiated_by=row[4] or "system",
            started_at=self._dt_from_db(row[5]),
            completed_at=self._dt_from_db(row[6]),
            duration_seconds=float(row[7]) if row[7] is not None else None,
            abort_reason=row[8] or "",
            cascade_event_id=row[9] or "",
            requires_approval=bool(row[10]),
            approved_by=row[11] or "",
            approved_at=self._dt_from_db(row[12]),
            created_at=self._dt_from_db(row[13]),
            updated_at=self._dt_from_db(row[14]),
            steps_data=data.get("steps_data", []) or [],
            metadata=data.get("metadata", {}) or {},
        )

    def _payload_from_data(self, entry: RecoverySessionData) -> str:
        return self._dumps_json(
            {
                "steps_data": entry.steps_data,
                "metadata": entry.metadata,
            }
        )

    def _entry_to_params(self, data: RecoverySessionData) -> tuple:
        return (
            data.session_id,
            data.namespace,
            data.trigger_level,
            data.status,
            data.initiated_by,
            self._dt_to_db(data.started_at),
            self._dt_to_db(data.completed_at),
            data.duration_seconds,
            data.abort_reason,
            data.cascade_event_id,
            int(data.requires_approval),
            data.approved_by,
            self._dt_to_db(data.approved_at),
            self._dt_to_db(data.created_at),
            self._dt_to_db(data.updated_at),
            self._payload_from_data(data),
        )

    # ----- Save / Get -------------------------------------------------------

    def save(self, data: RecoverySessionData) -> bool:
        now = utc_now()
        if data.created_at is None:
            data.created_at = now
        if data.updated_at is None:
            data.updated_at = now
        try:
            self._execute(
                f"""
                INSERT INTO {_TABLE}
                (session_id, namespace, trigger_level, status, initiated_by,
                 started_at, completed_at, duration_seconds, abort_reason,
                 cascade_event_id, requires_approval, approved_by, approved_at,
                 created_at, updated_at, data)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                self._entry_to_params(data),
            )
            return True
        except Exception as exc:
            if "UNIQUE" in str(exc).upper() or "DUPLICATE" in str(exc).upper():
                return False
            raise

    def get_by_session_id(self, session_id: str) -> RecoverySessionData | None:
        row = self._fetch_one(
            f"SELECT {_SELECT_COLS} FROM {_TABLE} WHERE session_id = %s",
            (session_id,),
        )
        return self._row_to_data(row) if row else None

    # ----- Query methods ----------------------------------------------------

    def _build_filter_clauses(
        self,
        *,
        namespace: str | None = None,
        status: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if namespace is not None:
            clauses.append("namespace = %s")
            params.append(namespace)
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        if start_date is not None:
            clauses.append("started_at >= %s")
            params.append(self._dt_to_db(start_date))
        if end_date is not None:
            clauses.append("started_at < %s")
            params.append(self._dt_to_db(end_date))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def find(
        self,
        *,
        namespace: str | None = None,
        status: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[RecoverySessionData]:
        where, params = self._build_filter_clauses(
            namespace=namespace,
            status=status,
            start_date=start_date,
            end_date=end_date,
        )
        sql = (
            f"SELECT {_SELECT_COLS} FROM {_TABLE}{where} "
            f"ORDER BY started_at DESC LIMIT %s OFFSET %s"
        )
        params.extend([limit, offset])
        rows = self._fetch_all(sql, params)
        return [self._row_to_data(r) for r in rows]

    def count(
        self,
        *,
        namespace: str | None = None,
        status: str | None = None,
    ) -> int:
        where, params = self._build_filter_clauses(namespace=namespace, status=status)
        row = self._fetch_one(f"SELECT COUNT(*) FROM {_TABLE}{where}", params)
        return int(row[0]) if row else 0

    # ----- Update / Delete --------------------------------------------------

    def update(self, data: RecoverySessionData) -> bool:
        data.updated_at = utc_now()
        conn = self._borrow_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                self._prepare(
                    f"""
                    UPDATE {_TABLE} SET
                        namespace = %s, trigger_level = %s, status = %s,
                        initiated_by = %s, started_at = %s, completed_at = %s,
                        duration_seconds = %s, abort_reason = %s,
                        cascade_event_id = %s, requires_approval = %s,
                        approved_by = %s, approved_at = %s,
                        created_at = %s, updated_at = %s, data = %s
                    WHERE session_id = %s
                    """
                ),
                (
                    data.namespace,
                    data.trigger_level,
                    data.status,
                    data.initiated_by,
                    self._dt_to_db(data.started_at),
                    self._dt_to_db(data.completed_at),
                    data.duration_seconds,
                    data.abort_reason,
                    data.cascade_event_id,
                    int(data.requires_approval),
                    data.approved_by,
                    self._dt_to_db(data.approved_at),
                    self._dt_to_db(data.created_at),
                    self._dt_to_db(data.updated_at),
                    self._payload_from_data(data),
                    data.session_id,
                ),
            )
            updated = bool(cursor.rowcount)
            if self._should_commit(conn):
                conn.commit()
            return updated
        except Exception:
            if self._should_commit(conn):
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            raise
        finally:
            cursor.close()

    def delete_older_than(self, cutoff: datetime) -> int:
        conn = self._borrow_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                self._prepare(f"DELETE FROM {_TABLE} WHERE started_at < %s"),
                (self._dt_to_db(cutoff),),
            )
            deleted = int(cursor.rowcount or 0)
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
