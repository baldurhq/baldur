"""
SQL Postmortem repository.

Framework-free adapter for ``PostmortemRepository`` backed by any
DB-API 2.0 database. ``incident_id`` is the UNIQUE lookup key;
``affected_services`` lives in the JSON ``data`` column and is
post-filtered in Python (cross-dialect JSON containment).

Safety guards for post-filter: fetch up to ``limit * 4`` candidates,
rely on ``(started_at)`` index, and warn when discard rate exceeds 75%.
"""
# D5: affected_services post-filtered in Python for cross-dialect JSON
# containment.

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
from baldur.interfaces.repositories import PostmortemData, PostmortemRepository
from baldur.settings.sql import SQLDialect

__all__ = ["SQLPostmortemRepository"]

logger = structlog.get_logger()

_TABLE = "baldur_postmortem"
_SCHEMA_VERSION = 1


def _ddl(dialect: SQLDialect) -> list[str]:
    ts = dialect_timestamp_type(dialect)
    js = dialect_json_type(dialect)
    return [
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id VARCHAR(36) PRIMARY KEY,
            incident_id VARCHAR(256) NOT NULL UNIQUE,
            started_at {ts},
            resolved_at {ts},
            duration_seconds REAL,
            source VARCHAR(16) DEFAULT 'auto',
            created_at {ts},
            data {js} NOT NULL
        )
        """,
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_started_at ON {_TABLE} (started_at)",
    ]


_SELECT_COLS = (
    "id, incident_id, started_at, resolved_at, duration_seconds, "
    "source, created_at, data"
)

_POST_FILTER_MULTIPLIER = 4
_POST_FILTER_WARN_THRESHOLD = 0.75


class SQLPostmortemRepository(GenericSQLRepository, PostmortemRepository):
    """DB-API 2.0 backed postmortem repository."""

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

    def _row_to_data(self, row: tuple) -> PostmortemData:
        data = self._loads_json(row[7]) or {}
        return PostmortemData(
            id=row[0],
            incident_id=row[1],
            started_at=self._dt_from_db(row[2]),
            resolved_at=self._dt_from_db(row[3]),
            duration_seconds=float(row[4]) if row[4] is not None else 0.0,
            source=row[5] or "auto",
            created_at=self._dt_from_db(row[6]),
            affected_services=data.get("affected_services", []) or [],
            timeline=data.get("timeline", []) or [],
            auto_actions=data.get("auto_actions", []) or [],
            recommendations=data.get("recommendations", []) or [],
            system_snapshot=data.get("system_snapshot", {}) or {},
        )

    def _payload_from_data(self, entry: PostmortemData) -> str:
        return self._dumps_json(
            {
                "affected_services": entry.affected_services,
                "timeline": entry.timeline,
                "auto_actions": entry.auto_actions,
                "recommendations": entry.recommendations,
                "system_snapshot": entry.system_snapshot,
            }
        )

    # ----- Save / Get -------------------------------------------------------

    def save(self, data: PostmortemData) -> bool:
        try:
            self._execute(
                f"""
                INSERT INTO {_TABLE}
                (id, incident_id, started_at, resolved_at, duration_seconds,
                 source, created_at, data)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    data.id,
                    data.incident_id,
                    self._dt_to_db(data.started_at),
                    self._dt_to_db(data.resolved_at),
                    data.duration_seconds,
                    data.source,
                    self._dt_to_db(data.created_at),
                    self._payload_from_data(data),
                ),
            )
            return True
        except Exception as exc:
            if "UNIQUE" in str(exc).upper() or "DUPLICATE" in str(exc).upper():
                return False
            raise

    def get_by_incident_id(self, incident_id: str) -> PostmortemData | None:
        row = self._fetch_one(
            f"SELECT {_SELECT_COLS} FROM {_TABLE} WHERE incident_id = %s",
            (incident_id,),
        )
        return self._row_to_data(row) if row else None

    # ----- Query helpers ----------------------------------------------------

    def _build_sql_filters(
        self,
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        min_duration: float | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if start_date is not None:
            clauses.append("started_at >= %s")
            params.append(self._dt_to_db(start_date))
        if end_date is not None:
            clauses.append("started_at < %s")
            params.append(self._dt_to_db(end_date))
        if min_duration is not None:
            clauses.append("duration_seconds >= %s")
            params.append(min_duration)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def find(
        self,
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        service: str | None = None,
        min_duration: float | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[PostmortemData]:
        where, params = self._build_sql_filters(
            start_date=start_date, end_date=end_date, min_duration=min_duration
        )
        fetch_limit = limit * _POST_FILTER_MULTIPLIER if service else limit
        sql = (
            f"SELECT {_SELECT_COLS} FROM {_TABLE}{where} "
            f"ORDER BY started_at DESC LIMIT %s OFFSET %s"
        )
        params.extend([fetch_limit, offset])
        rows = self._fetch_all(sql, params)
        results = [self._row_to_data(r) for r in rows]

        if service:
            pre_filter_count = len(results)
            results = [r for r in results if service in (r.affected_services or [])]
            if pre_filter_count > 0:
                discard_rate = 1.0 - len(results) / pre_filter_count
                if discard_rate > _POST_FILTER_WARN_THRESHOLD:
                    logger.warning(
                        "sql.postmortem_high_discard_rate",
                        discard_rate=round(discard_rate, 2),
                        service=service,
                        fetched=pre_filter_count,
                        kept=len(results),
                    )
            results = results[:limit]

        return results

    def count(
        self,
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        service: str | None = None,
        min_duration: float | None = None,
    ) -> int:
        where, params = self._build_sql_filters(
            start_date=start_date, end_date=end_date, min_duration=min_duration
        )
        if not service:
            row = self._fetch_one(f"SELECT COUNT(*) FROM {_TABLE}{where}", params)
            return int(row[0]) if row else 0
        rows = self._fetch_all(
            f"SELECT data FROM {_TABLE}{where} ORDER BY started_at DESC", params
        )
        total = 0
        for row in rows:
            data = self._loads_json(row[0]) or {}
            if service in (data.get("affected_services") or []):
                total += 1
        return total

    # ----- Update -----------------------------------------------------------

    def update_fields(
        self,
        incident_id: str,
        fields: dict[str, Any],
    ) -> bool:
        entry = self.get_by_incident_id(incident_id)
        if entry is None:
            return False

        json_fields = {
            "affected_services",
            "timeline",
            "auto_actions",
            "recommendations",
            "system_snapshot",
        }
        updatable_cols = {
            "started_at": lambda v: ("started_at = %s", self._dt_to_db(v)),
            "resolved_at": lambda v: ("resolved_at = %s", self._dt_to_db(v)),
            "duration_seconds": lambda v: ("duration_seconds = %s", v),
            "source": lambda v: ("source = %s", v),
        }
        col_updates: list[str] = []
        col_params: list[Any] = []

        for key, value in fields.items():
            if key in json_fields:
                current = getattr(entry, key, None)
                if isinstance(current, dict) and isinstance(value, dict):
                    current.update(value)
                    setattr(entry, key, current)
                else:
                    setattr(entry, key, value)
            elif key in updatable_cols:
                setattr(entry, key, value)
                clause, param = updatable_cols[key](value)
                col_updates.append(clause)
                col_params.append(param)

        col_updates.append("data = %s")
        col_params.append(self._payload_from_data(entry))
        col_params.append(incident_id)

        self._execute(
            f"UPDATE {_TABLE} SET {', '.join(col_updates)} WHERE incident_id = %s",
            col_params,
        )
        return True
