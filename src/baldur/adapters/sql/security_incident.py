"""
SQL SecurityIncident repository.

Framework-free adapter for ``SecurityIncidentRepository`` backed by any
DB-API 2.0 database. Hot columns (incident_type, severity, status,
source_ip, created_at) support the interface's query methods via SQL
indexes; remaining DTO fields live in a JSON ``data`` column.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from baldur.adapters.sql.base import (
    GenericSQLRepository,
    dialect_bigserial,
    dialect_json_type,
    dialect_timestamp_type,
)
from baldur.interfaces.repositories import (
    SecurityIncidentData,
    SecurityIncidentRepository,
    SecurityIncidentStatus,
)
from baldur.settings.sql import SQLDialect
from baldur.utils.time import utc_now

__all__ = ["SQLSecurityIncidentRepository"]


_TABLE = "baldur_security_incident"
_SCHEMA_VERSION = 1


def _ddl(dialect: SQLDialect) -> list[str]:
    ts = dialect_timestamp_type(dialect)
    js = dialect_json_type(dialect)
    pk = dialect_bigserial(dialect)
    return [
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id {pk},
            incident_type VARCHAR(64) NOT NULL,
            severity VARCHAR(32) NOT NULL,
            status VARCHAR(32) NOT NULL,
            source_ip VARCHAR(45),
            user_id BIGINT,
            created_at {ts} NOT NULL,
            resolved_at {ts},
            updated_at {ts} NOT NULL,
            data {js} NOT NULL
        )
        """,
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_status ON {_TABLE} (status)",
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_type_created "
        f"ON {_TABLE} (incident_type, created_at)",
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_severity ON {_TABLE} (severity)",
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_ip_created "
        f"ON {_TABLE} (source_ip, created_at)",
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_created_at ON {_TABLE} (created_at)",
    ]


_SELECT_COLS = (
    "id, incident_type, severity, status, source_ip, user_id, "
    "created_at, resolved_at, updated_at, data"
)


class SQLSecurityIncidentRepository(GenericSQLRepository, SecurityIncidentRepository):
    """DB-API 2.0 backed security incident repository."""

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

    def _row_to_data(self, row: tuple) -> SecurityIncidentData:
        data = self._loads_json(row[9]) or {}
        return SecurityIncidentData(
            id=int(row[0]),
            incident_type=row[1],
            severity=row[2],
            status=row[3],
            source_ip=row[4],
            user_id=row[5],
            user_agent=data.get("user_agent", "") or "",
            entity_refs=data.get("entity_refs", {}) or {},
            description=data.get("description", "") or "",
            raw_payload=data.get("raw_payload", {}) or {},
            assigned_to_id=data.get("assigned_to_id"),
            investigation_notes=data.get("investigation_notes", "") or "",
            created_at=self._dt_from_db(row[6]),
            resolved_at=self._dt_from_db(row[7]),
            updated_at=self._dt_from_db(row[8]),
        )

    def _payload_from_data(self, entry: SecurityIncidentData) -> str:
        return self._dumps_json(
            {
                "user_agent": entry.user_agent,
                "entity_refs": entry.entity_refs,
                "description": entry.description,
                "raw_payload": entry.raw_payload,
                "assigned_to_id": entry.assigned_to_id,
                "investigation_notes": entry.investigation_notes,
            }
        )

    # ----- Create / Get -----------------------------------------------------

    def create(
        self,
        incident_type: str,
        severity: str,
        description: str = "",
        source_ip: str | None = None,
        user_agent: str = "",
        user_id: int | None = None,
        entity_refs: dict[str, int] | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> SecurityIncidentData:
        now = utc_now()
        entry = SecurityIncidentData(
            id=0,
            incident_type=incident_type,
            severity=severity,
            status=SecurityIncidentStatus.OPEN.value,
            source_ip=source_ip,
            user_agent=user_agent,
            user_id=user_id,
            entity_refs=entity_refs or {},
            description=description,
            raw_payload=raw_payload or {},
            created_at=now,
            updated_at=now,
        )
        new_id = self._execute_returning_id(
            f"""
            INSERT INTO {_TABLE}
            (incident_type, severity, status, source_ip, user_id,
             created_at, updated_at, data)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                incident_type,
                severity,
                entry.status,
                source_ip,
                user_id,
                self._dt_to_db(now),
                self._dt_to_db(now),
                self._payload_from_data(entry),
            ),
        )
        entry.id = int(new_id or 0)
        return entry

    def get_by_id(self, id: int) -> SecurityIncidentData | None:
        row = self._fetch_one(
            f"SELECT {_SELECT_COLS} FROM {_TABLE} WHERE id = %s", (id,)
        )
        return self._row_to_data(row) if row else None

    # ----- Query methods ----------------------------------------------------

    def get_open_incidents(
        self,
        limit: int = 100,
    ) -> list[SecurityIncidentData]:
        rows = self._fetch_all(
            f"SELECT {_SELECT_COLS} FROM {_TABLE} "
            f"WHERE status = %s ORDER BY created_at DESC LIMIT %s",
            (SecurityIncidentStatus.OPEN.value, limit),
        )
        return [self._row_to_data(r) for r in rows]

    def get_by_type(
        self,
        incident_type: str,
        limit: int = 100,
    ) -> list[SecurityIncidentData]:
        rows = self._fetch_all(
            f"SELECT {_SELECT_COLS} FROM {_TABLE} "
            f"WHERE incident_type = %s ORDER BY created_at DESC LIMIT %s",
            (incident_type, limit),
        )
        return [self._row_to_data(r) for r in rows]

    def get_by_severity(
        self,
        severity: str,
        limit: int = 100,
    ) -> list[SecurityIncidentData]:
        rows = self._fetch_all(
            f"SELECT {_SELECT_COLS} FROM {_TABLE} "
            f"WHERE severity = %s ORDER BY created_at DESC LIMIT %s",
            (severity, limit),
        )
        return [self._row_to_data(r) for r in rows]

    def get_recent_by_ip(
        self,
        source_ip: str,
        hours: int = 24,
        limit: int = 100,
    ) -> list[SecurityIncidentData]:
        cutoff = utc_now() - timedelta(hours=hours)
        rows = self._fetch_all(
            f"SELECT {_SELECT_COLS} FROM {_TABLE} "
            f"WHERE source_ip = %s AND created_at >= %s "
            f"ORDER BY created_at DESC LIMIT %s",
            (source_ip, self._dt_to_db(cutoff), limit),
        )
        return [self._row_to_data(r) for r in rows]

    def count_by_type_since(
        self,
        incident_type: str,
        since: datetime,
    ) -> int:
        row = self._fetch_one(
            f"SELECT COUNT(*) FROM {_TABLE} "
            f"WHERE incident_type = %s AND created_at >= %s",
            (incident_type, self._dt_to_db(since)),
        )
        return int(row[0]) if row else 0

    # ----- Status mutations -------------------------------------------------

    def update_status(
        self,
        id: int,
        status: str,
        investigation_notes: str = "",
        assigned_to_id: int | None = None,
    ) -> bool:
        entry = self.get_by_id(id)
        if entry is None:
            return False
        now = utc_now()
        entry.status = status
        entry.updated_at = now
        if investigation_notes:
            entry.investigation_notes = investigation_notes
        if assigned_to_id is not None:
            entry.assigned_to_id = assigned_to_id
        self._execute(
            f"UPDATE {_TABLE} SET status = %s, updated_at = %s, data = %s "
            f"WHERE id = %s",
            (status, self._dt_to_db(now), self._payload_from_data(entry), id),
        )
        return True

    def mark_as_resolved(
        self,
        id: int,
        investigation_notes: str = "",
    ) -> bool:
        entry = self.get_by_id(id)
        if entry is None:
            return False
        now = utc_now()
        entry.status = SecurityIncidentStatus.RESOLVED.value
        entry.resolved_at = now
        entry.updated_at = now
        if investigation_notes:
            entry.investigation_notes = investigation_notes
        self._execute(
            f"UPDATE {_TABLE} SET status = %s, resolved_at = %s, updated_at = %s, "
            f"data = %s WHERE id = %s",
            (
                SecurityIncidentStatus.RESOLVED.value,
                self._dt_to_db(now),
                self._dt_to_db(now),
                self._payload_from_data(entry),
                id,
            ),
        )
        return True
