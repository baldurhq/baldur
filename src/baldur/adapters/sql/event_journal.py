"""
SQL EventJournal repository.

Append-only storage with monotonically increasing sequence numbers.
Gaps are allowed per the ABC contract — sequence assignment is
handled by the database's auto-increment primary key, so multi-writer
scenarios never collide.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from baldur.adapters.sql.base import (
    GenericSQLRepository,
    dialect_bigserial,
    dialect_json_type,
    dialect_timestamp_type,
)
from baldur.interfaces.event_journal import (
    EventJournalRepository,
    JournalEntry,
    JournalQueryFilter,
    JournalQueryResult,
)
from baldur.settings.sql import SQLDialect

__all__ = ["SQLEventJournalRepository"]


_TABLE = "baldur_event_journal"
_SCHEMA_VERSION = 1


def _ddl(dialect: SQLDialect) -> list[str]:
    ts = dialect_timestamp_type(dialect)
    js = dialect_json_type(dialect)
    pk = dialect_bigserial(dialect)
    return [
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            sequence {pk},
            event_type VARCHAR(256) NOT NULL,
            source VARCHAR(256) NOT NULL,
            service_name VARCHAR(256) NOT NULL,
            region VARCHAR(128) NOT NULL DEFAULT '',
            tier_id VARCHAR(128) NOT NULL DEFAULT '',
            timestamp {ts} NOT NULL,
            context {js} NOT NULL
        )
        """,
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_event_type ON {_TABLE} (event_type)",
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_service_name "
        f"ON {_TABLE} (service_name)",
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_timestamp ON {_TABLE} (timestamp)",
    ]


_SELECT_COLS = (
    "sequence, event_type, source, service_name, region, tier_id, timestamp, context"
)


class SQLEventJournalRepository(GenericSQLRepository, EventJournalRepository):
    """DB-API 2.0 backed append-only event journal."""

    def __init__(
        self,
        get_connection: Callable[[], Any],
        *,
        dialect: SQLDialect | None = None,
        autocommit_delegated: bool | None = None,
        max_query_limit: int = 10000,
    ) -> None:
        super().__init__(
            get_connection,
            dialect=dialect,
            autocommit_delegated=autocommit_delegated,
            schema=(_TABLE, _SCHEMA_VERSION, _ddl),
        )
        self._max_query_limit = max_query_limit

    # ----- Row <-> DTO ------------------------------------------------------

    def _row_to_entry(self, row: tuple) -> JournalEntry:
        return JournalEntry(
            sequence=int(row[0]),
            event_type=row[1],
            source=row[2],
            service_name=row[3],
            region=row[4] or "",
            tier_id=row[5] or "",
            timestamp=self._dt_from_db(row[6]),  # type: ignore[arg-type]
            context=self._loads_json(row[7]) or {},
        )

    # ----- Append / Query ---------------------------------------------------

    def append(self, entry: JournalEntry) -> int:
        new_id = self._execute_returning_id(
            f"""
            INSERT INTO {_TABLE}
            (event_type, source, service_name, region, tier_id, timestamp, context)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                entry.event_type,
                entry.source,
                entry.service_name,
                entry.region or "",
                entry.tier_id or "",
                self._dt_to_db(entry.timestamp),
                self._dumps_json(entry.context or {}),
            ),
        )
        return int(new_id or 0)

    def _build_filter_clauses(
        self, query_filter: JournalQueryFilter
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if query_filter.event_types:
            placeholders = ",".join([self._placeholder] * len(query_filter.event_types))
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(query_filter.event_types)
        if query_filter.service_name is not None:
            clauses.append("service_name = %s")
            params.append(query_filter.service_name)
        if query_filter.region is not None:
            clauses.append("region = %s")
            params.append(query_filter.region)
        if query_filter.start_time is not None:
            clauses.append("timestamp >= %s")
            params.append(self._dt_to_db(query_filter.start_time))
        if query_filter.end_time is not None:
            clauses.append("timestamp < %s")
            params.append(self._dt_to_db(query_filter.end_time))
        where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where_sql, params

    def _apply_context_filter(
        self, entries: list[JournalEntry], filters: dict[str, str] | None
    ) -> list[JournalEntry]:
        if not filters:
            return entries
        filtered: list[JournalEntry] = []
        for entry in entries:
            match = True
            for key, val in filters.items():
                if str(entry.context.get(key)) != val:
                    match = False
                    break
            if match:
                filtered.append(entry)
        return filtered

    def query(self, query_filter: JournalQueryFilter) -> JournalQueryResult:
        where_sql, params = self._build_filter_clauses(query_filter)
        count_sql = f"SELECT COUNT(*) FROM {_TABLE}{where_sql}"
        total_row = (
            self._fetch_one(count_sql, params)
            if not query_filter.context_filters
            else None
        )

        effective_limit = min(query_filter.limit, self._max_query_limit)
        # Context filtering happens in Python — fetch up to (limit + buffer)
        # candidates so post-filter we can still honor the requested limit.
        fetch_limit = (
            effective_limit * 4 if query_filter.context_filters else effective_limit
        )
        rows = self._fetch_all(
            f"SELECT {_SELECT_COLS} FROM {_TABLE}{where_sql} "
            f"ORDER BY sequence ASC LIMIT %s",
            params + [fetch_limit],
        )
        entries = [self._row_to_entry(r) for r in rows]
        entries = self._apply_context_filter(entries, query_filter.context_filters)

        if query_filter.context_filters is not None:
            total_count = len(entries)
            truncated = False
        else:
            total_count = int(total_row[0]) if total_row else len(entries)
            truncated = total_count > effective_limit
        return JournalQueryResult(
            entries=entries[:effective_limit],
            truncated=truncated,
            total_count=total_count,
        )

    def get_sequence_range(
        self,
        start_sequence: int,
        end_sequence: int,
    ) -> list[JournalEntry]:
        rows = self._fetch_all(
            f"SELECT {_SELECT_COLS} FROM {_TABLE} "
            f"WHERE sequence >= %s AND sequence < %s "
            f"ORDER BY sequence ASC",
            (start_sequence, end_sequence),
        )
        return [self._row_to_entry(r) for r in rows]

    def get_latest_sequence(self) -> int:
        row = self._fetch_one(f"SELECT COALESCE(MAX(sequence), 0) FROM {_TABLE}")
        return int(row[0]) if row else 0

    def count(self, query_filter: JournalQueryFilter) -> int:
        where_sql, params = self._build_filter_clauses(query_filter)
        if query_filter.context_filters:
            # Context is JSON — iterate and count in Python to match the
            # semantics of the in-memory / Redis adapters.
            rows = self._fetch_all(
                f"SELECT {_SELECT_COLS} FROM {_TABLE}{where_sql} ORDER BY sequence ASC",
                params,
            )
            entries = [self._row_to_entry(r) for r in rows]
            return len(
                self._apply_context_filter(entries, query_filter.context_filters)
            )
        row = self._fetch_one(f"SELECT COUNT(*) FROM {_TABLE}{where_sql}", params)
        return int(row[0]) if row else 0
