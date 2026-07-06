"""
SQL CascadeEventArchive repository.

Framework-free adapter for ``CascadeEventArchiveRepository`` backed by
any DB-API 2.0 database. ``cascade_id`` is the natural primary key.
Hash chain ordering (``get_chain``) returns ASC by timestamp for
integrity verification; ``find`` returns DESC for recency.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

import structlog

from baldur.adapters.sql.base import (
    GenericSQLRepository,
    dialect_json_type,
    dialect_timestamp_type,
)
from baldur.interfaces.repositories import CascadeEventArchiveRepository
from baldur.models.cascade_event import CascadeEventData
from baldur.settings.sql import SQLDialect

logger = structlog.get_logger()

__all__ = ["SQLCascadeEventArchiveRepository"]


_TABLE = "baldur_cascade_event"
_SCHEMA_VERSION = 1


def _ddl(dialect: SQLDialect) -> list[str]:
    ts = dialect_timestamp_type(dialect)
    js = dialect_json_type(dialect)
    return [
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            cascade_id VARCHAR(256) PRIMARY KEY,
            namespace VARCHAR(128) NOT NULL,
            trigger_type VARCHAR(64) NOT NULL,
            current_hash VARCHAR(128) NOT NULL,
            previous_hash VARCHAR(128) DEFAULT '',
            total_effects INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            timestamp {ts},
            archived_at {ts},
            version VARCHAR(16) DEFAULT '1.0',
            is_test INTEGER DEFAULT 0,
            data {js} NOT NULL
        )
        """,
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_ns_ts "
        f"ON {_TABLE} (namespace, timestamp)",
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_trigger ON {_TABLE} (trigger_type)",
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_ts ON {_TABLE} (timestamp)",
    ]


_SELECT_COLS = (
    "cascade_id, namespace, trigger_type, current_hash, previous_hash, "
    "total_effects, success_count, failure_count, timestamp, archived_at, "
    "version, is_test, data"
)


class SQLCascadeEventArchiveRepository(
    GenericSQLRepository, CascadeEventArchiveRepository
):
    """DB-API 2.0 backed cascade event archive repository."""

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

    def _row_to_data(self, row: tuple) -> CascadeEventData:
        data = self._loads_json(row[12]) or {}
        return CascadeEventData(
            cascade_id=row[0],
            namespace=row[1],
            trigger_type=row[2],
            current_hash=row[3],
            previous_hash=row[4] or "",
            total_effects=int(row[5] or 0),
            success_count=int(row[6] or 0),
            failure_count=int(row[7] or 0),
            timestamp=self._dt_from_db(row[8]),
            archived_at=self._dt_from_db(row[9]),
            version=row[10] or "1.0",
            is_test=bool(row[11]),
            trigger_details=data.get("trigger_details", {}) or {},
            effects=data.get("effects", []) or [],
            causation_chain=data.get("causation_chain", []) or [],
            external_trace=data.get("external_trace"),
        )

    def _payload_from_data(self, entry: CascadeEventData) -> str:
        return self._dumps_json(
            {
                "trigger_details": entry.trigger_details,
                "effects": entry.effects,
                "causation_chain": entry.causation_chain,
                "external_trace": entry.external_trace,
            }
        )

    # ----- Save / Get -------------------------------------------------------

    def save(self, data: CascadeEventData) -> bool:
        try:
            self._execute(
                f"""
                INSERT INTO {_TABLE}
                (cascade_id, namespace, trigger_type, current_hash, previous_hash,
                 total_effects, success_count, failure_count, timestamp,
                 archived_at, version, is_test, data)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    data.cascade_id,
                    data.namespace,
                    data.trigger_type,
                    data.current_hash,
                    data.previous_hash,
                    data.total_effects,
                    data.success_count,
                    data.failure_count,
                    self._dt_to_db(data.timestamp),
                    self._dt_to_db(data.archived_at),
                    data.version,
                    int(data.is_test),
                    self._payload_from_data(data),
                ),
            )
            return True
        except Exception as exc:
            if "UNIQUE" in str(exc).upper() or "DUPLICATE" in str(exc).upper():
                return False
            raise

    def get_by_cascade_id(self, cascade_id: str) -> CascadeEventData | None:
        row = self._fetch_one(
            f"SELECT {_SELECT_COLS} FROM {_TABLE} WHERE cascade_id = %s",
            (cascade_id,),
        )
        return self._row_to_data(row) if row else None

    # ----- Query methods ----------------------------------------------------

    def _build_filter_clauses(
        self,
        *,
        namespace: str | None = None,
        trigger_type: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        is_test: bool | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if namespace is not None:
            clauses.append("namespace = %s")
            params.append(namespace)
        if trigger_type is not None:
            clauses.append("trigger_type = %s")
            params.append(trigger_type)
        if start_date is not None:
            clauses.append("timestamp >= %s")
            params.append(self._dt_to_db(start_date))
        if end_date is not None:
            clauses.append("timestamp < %s")
            params.append(self._dt_to_db(end_date))
        if is_test is not None:
            clauses.append("is_test = %s")
            params.append(int(is_test))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def find(
        self,
        *,
        namespace: str | None = None,
        trigger_type: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        is_test: bool | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[CascadeEventData]:
        where, params = self._build_filter_clauses(
            namespace=namespace,
            trigger_type=trigger_type,
            start_date=start_date,
            end_date=end_date,
            is_test=is_test,
        )
        sql = (
            f"SELECT {_SELECT_COLS} FROM {_TABLE}{where} "
            f"ORDER BY timestamp DESC LIMIT %s OFFSET %s"
        )
        params.extend([limit, offset])
        rows = self._fetch_all(sql, params)
        return [self._row_to_data(r) for r in rows]

    def count(
        self,
        *,
        namespace: str | None = None,
        trigger_type: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> int:
        where, params = self._build_filter_clauses(
            namespace=namespace,
            trigger_type=trigger_type,
            start_date=start_date,
            end_date=end_date,
        )
        row = self._fetch_one(f"SELECT COUNT(*) FROM {_TABLE}{where}", params)
        return int(row[0]) if row else 0

    def delete_older_than(self, cutoff: datetime) -> int:
        conn = self._borrow_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                self._prepare(f"DELETE FROM {_TABLE} WHERE timestamp < %s"),
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

    def get_chain(
        self,
        namespace: str,
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[CascadeEventData]:
        clauses = ["namespace = %s"]
        params: list[Any] = [namespace]
        if start_date is not None:
            clauses.append("timestamp >= %s")
            params.append(self._dt_to_db(start_date))
        if end_date is not None:
            clauses.append("timestamp < %s")
            params.append(self._dt_to_db(end_date))
        where = " WHERE " + " AND ".join(clauses)
        rows = self._fetch_all(
            f"SELECT {_SELECT_COLS} FROM {_TABLE}{where} ORDER BY timestamp ASC",
            params,
        )
        return [self._row_to_data(r) for r in rows]
