"""
SQL FailedOperation (DLQ) repository.

Framework-free adapter for ``FailedOperationRepository`` backed by any
DB-API 2.0 database. Structured columns back the hot lookup paths
(``status``, ``domain``, ``failure_type``, ``retry_count``); the full
DTO is persisted as JSON in ``data`` so evolving the DTO does not
require schema migration.

Atomic replay acquisition uses a conditional UPDATE; the ``WHERE``
clause is the concurrency guard, so a zero rowcount means the entry
was not eligible or was already claimed by another worker.
"""

# Reference: 429 Part 4.

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
    dialect_upsert_clause,
    sql_transaction,
)
from baldur.interfaces.repositories import (
    DLQCompressedEntry,
    DLQCompressedStatus,
    FailedOperationData,
    FailedOperationRepository,
    FailedOperationStatus,
)
from baldur.settings.sql import SQLDialect
from baldur.utils.time import utc_now

__all__ = ["SQLFailedOperationRepository"]

logger = structlog.get_logger()


_TABLE = "baldur_dlq"
# v2 adds the compressed-entry (status, compressed_at) index. Every statement
# in _ddl is IF NOT EXISTS, so an existing install re-runs the set harmlessly
# and picks up only what it is missing.
_SCHEMA_VERSION = 2
_COMPRESSED_TABLE = "baldur_dlq_compressed"


def _ddl(dialect: SQLDialect) -> list[str]:
    ts = dialect_timestamp_type(dialect)
    js = dialect_json_type(dialect)
    pk = dialect_bigserial(dialect)
    # No index on ``expires_at``: the column is present for DTO parity with
    # other adapters, but no read path filters on it yet. Add an index when
    # an actual expiry query is introduced.
    return [
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id {pk},
            domain VARCHAR(128) NOT NULL,
            failure_type VARCHAR(128) NOT NULL,
            status VARCHAR(32) NOT NULL,
            entity_type VARCHAR(128),
            entity_id VARCHAR(256),
            user_id BIGINT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 2,
            error_code VARCHAR(128) NOT NULL DEFAULT '',
            last_retry_at {ts},
            resolved_at {ts},
            created_at {ts} NOT NULL,
            updated_at {ts} NOT NULL,
            expires_at {ts},
            data {js} NOT NULL
        )
        """,
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_status_domain "
        f"ON {_TABLE} (status, domain)",
        f"CREATE INDEX IF NOT EXISTS idx_{_TABLE}_created_at ON {_TABLE} (created_at)",
        f"""
        CREATE TABLE IF NOT EXISTS {_COMPRESSED_TABLE} (
            id VARCHAR(256) PRIMARY KEY,
            domain VARCHAR(128) NOT NULL,
            failure_type VARCHAR(128) NOT NULL,
            error_code VARCHAR(128) NOT NULL,
            count INTEGER NOT NULL,
            status VARCHAR(32) NOT NULL,
            compressed_at {ts} NOT NULL,
            stale_at {ts},
            archived_at {ts},
            data {js} NOT NULL
        )
        """,
        f"CREATE INDEX IF NOT EXISTS idx_{_COMPRESSED_TABLE}_domain_status "
        f"ON {_COMPRESSED_TABLE} (domain, status)",
        # Serves the lifecycle sweep's (status, compressed_at) predicate and
        # its ascending order. The domain-leading index above cannot: the
        # sweep does not filter by domain.
        f"CREATE INDEX IF NOT EXISTS idx_{_COMPRESSED_TABLE}_status_compressed_at "
        f"ON {_COMPRESSED_TABLE} (status, compressed_at)",
    ]


_SELECT_COLS = (
    "id, domain, failure_type, status, entity_type, entity_id, user_id, "
    "retry_count, max_retries, error_code, last_retry_at, resolved_at, "
    "created_at, updated_at, expires_at, data"
)


class SQLFailedOperationRepository(GenericSQLRepository, FailedOperationRepository):
    """DB-API 2.0 backed DLQ repository."""

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

    def _row_to_data(self, row: tuple) -> FailedOperationData:
        data = self._loads_json(row[15]) or {}
        return FailedOperationData(
            # 538 D1: opaque-string id at the DTO boundary; the dense int PK
            # stays the storage form (str(pk) on read, int(id) on bind).
            id=str(row[0]),
            domain=row[1],
            failure_type=row[2],
            status=row[3],
            entity_type=row[4],
            entity_id=row[5],
            entity_refs=data.get("entity_refs", {}) or {},
            user_id=row[6],
            snapshot_data=data.get("snapshot_data", {}) or {},
            error_code=row[9] or "",
            error_message=data.get("error_message", "") or "",
            retry_count=int(row[7] or 0),
            max_retries=int(row[8] or 2),
            last_retry_at=self._dt_from_db(row[10]),
            request_data=data.get("request_data", {}) or {},
            response_data=data.get("response_data", {}) or {},
            metadata=data.get("metadata", {}) or {},
            resolved_at=self._dt_from_db(row[11]),
            resolved_by_id=data.get("resolved_by_id"),
            resolution_type=data.get("resolution_type", "") or "",
            resolution_note=data.get("resolution_note", "") or "",
            next_action_hint=data.get("next_action_hint", "") or "",
            recommended_action=data.get("recommended_action", "") or "",
            created_at=self._dt_from_db(row[12]),
            updated_at=self._dt_from_db(row[13]),
            expires_at=self._dt_from_db(row[14]),
        )

    def _payload_from_data(self, entry: FailedOperationData) -> str:
        return self._dumps_json(
            {
                "entity_refs": entry.entity_refs,
                "snapshot_data": entry.snapshot_data,
                "error_message": entry.error_message,
                "request_data": entry.request_data,
                "response_data": entry.response_data,
                "metadata": entry.metadata,
                "resolved_by_id": entry.resolved_by_id,
                "resolution_type": entry.resolution_type,
                "resolution_note": entry.resolution_note,
                "next_action_hint": entry.next_action_hint,
                "recommended_action": entry.recommended_action,
            }
        )

    # ----- Create / Get -----------------------------------------------------

    def create(
        self,
        domain: str,
        failure_type: str,
        error_message: str = "",
        error_code: str = "",
        entity_type: str | None = None,
        entity_id: str | None = None,
        entity_refs: dict[str, Any] | None = None,
        user_id: int | None = None,
        snapshot_data: dict[str, Any] | None = None,
        request_data: dict[str, Any] | None = None,
        response_data: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        retry_count: int = 0,
        max_retries: int = 2,
        next_action_hint: str = "",
        recommended_action: str = "",
        expires_at: datetime | None = None,
    ) -> FailedOperationData:
        now = utc_now()
        entry = FailedOperationData(
            id="",
            domain=domain,
            failure_type=failure_type,
            status=FailedOperationStatus.PENDING.value,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_refs=entity_refs or {},
            user_id=user_id,
            snapshot_data=snapshot_data or {},
            error_code=error_code,
            error_message=error_message,
            retry_count=retry_count,
            max_retries=max_retries,
            request_data=request_data or {},
            response_data=response_data or {},
            metadata=metadata or {},
            next_action_hint=next_action_hint,
            recommended_action=recommended_action,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
        )
        new_id = self._execute_returning_id(
            f"""
            INSERT INTO {_TABLE}
            (domain, failure_type, status, entity_type, entity_id, user_id,
             retry_count, max_retries, error_code, created_at, updated_at,
             expires_at, data)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                domain,
                failure_type,
                entry.status,
                entity_type,
                entity_id,
                user_id,
                retry_count,
                max_retries,
                error_code,
                self._dt_to_db(now),
                self._dt_to_db(now),
                self._dt_to_db(expires_at) if expires_at else None,
                self._payload_from_data(entry),
            ),
        )
        entry.id = str(new_id or 0)
        return entry

    def get_by_id(self, id: str) -> FailedOperationData | None:
        row = self._fetch_one(
            f"SELECT {_SELECT_COLS} FROM {_TABLE} WHERE id = %s", (int(id),)
        )
        return self._row_to_data(row) if row else None

    def get_pending_by_domain(
        self, domain: str, limit: int = 100
    ) -> list[FailedOperationData]:
        rows = self._fetch_all(
            f"SELECT {_SELECT_COLS} FROM {_TABLE} "
            f"WHERE status = %s AND domain = %s "
            f"ORDER BY created_at ASC LIMIT %s",
            (FailedOperationStatus.PENDING.value, domain, limit),
        )
        return [self._row_to_data(r) for r in rows]

    def get_pending_count_by_domain(self, domain: str) -> int:
        row = self._fetch_one(
            f"SELECT COUNT(*) FROM {_TABLE} WHERE status = %s AND domain = %s",
            (FailedOperationStatus.PENDING.value, domain),
        )
        return int(row[0]) if row else 0

    # ----- Status mutations -------------------------------------------------

    def update_status(
        self,
        id: str,
        status: str,
        resolution_type: str = "",
        resolution_note: str = "",
        resolved_by_id: int | None = None,
        recommended_action: str = "",
    ) -> bool:
        entry = self.get_by_id(id)
        if entry is None:
            return False
        now = utc_now()
        entry.status = status
        entry.updated_at = now
        if status == FailedOperationStatus.RESOLVED.value:
            entry.resolved_at = now
        if resolution_type:
            entry.resolution_type = resolution_type
        if resolution_note:
            entry.resolution_note = resolution_note
        if resolved_by_id is not None:
            entry.resolved_by_id = resolved_by_id
        if recommended_action:
            entry.recommended_action = recommended_action
        self._execute(
            f"UPDATE {_TABLE} SET status = %s, resolved_at = %s, updated_at = %s, "
            f"data = %s WHERE id = %s",
            (
                status,
                self._dt_to_db(entry.resolved_at),
                self._dt_to_db(now),
                self._payload_from_data(entry),
                int(id),
            ),
        )
        return True

    def increment_retry_count(self, id: str) -> bool:
        now = utc_now()
        self._execute(
            f"UPDATE {_TABLE} SET retry_count = retry_count + 1, "
            f"last_retry_at = %s, updated_at = %s WHERE id = %s",
            (self._dt_to_db(now), self._dt_to_db(now), int(id)),
        )
        return self.get_by_id(id) is not None

    def mark_as_resolved(
        self,
        id: str,
        resolution_type: str,
        resolution_note: str = "",
        resolved_by_id: int | None = None,
    ) -> bool:
        return self.update_status(
            id=id,
            status=FailedOperationStatus.RESOLVED.value,
            resolution_type=resolution_type,
            resolution_note=resolution_note,
            resolved_by_id=resolved_by_id,
        )

    def bulk_update_status(self, ids: list[str], status: str) -> int:
        if not ids:
            return 0
        now = utc_now()
        int_ids = [int(i) for i in ids]
        placeholders = ",".join([self._placeholder] * len(ids))
        # ``update_status`` rewrites the ``data`` JSON payload to carry
        # resolution_* fields. ``bulk_update_status`` never receives those,
        # so a single IN-list UPDATE on the columnar status + resolved_at
        # is equivalent to the loop and skips N reads + N writes.
        if status == FailedOperationStatus.RESOLVED.value:
            sql = (
                f"UPDATE {_TABLE} SET status = %s, resolved_at = %s, updated_at = %s "
                f"WHERE id IN ({placeholders})"
            )
            params: tuple[Any, ...] = (
                status,
                self._dt_to_db(now),
                self._dt_to_db(now),
                *int_ids,
            )
        else:
            sql = (
                f"UPDATE {_TABLE} SET status = %s, updated_at = %s "
                f"WHERE id IN ({placeholders})"
            )
            params = (status, self._dt_to_db(now), *int_ids)

        conn = self._borrow_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(self._prepare(sql), params)
            updated = int(cursor.rowcount or 0)
            if self._should_commit(conn):
                conn.commit()
            return updated
        except Exception:
            if self._should_commit(conn):
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    logger.warning("sql.bulk_update_rollback_failed", exc_info=True)
            raise
        finally:
            cursor.close()

    # ----- Query helpers ----------------------------------------------------

    def get_expired_operations(
        self, before_date: datetime, limit: int = 100
    ) -> list[FailedOperationData]:
        rows = self._fetch_all(
            f"SELECT {_SELECT_COLS} FROM {_TABLE} "
            f"WHERE expires_at IS NOT NULL AND expires_at < %s "
            f"ORDER BY expires_at ASC LIMIT %s",
            (self._dt_to_db(before_date), limit),
        )
        return [self._row_to_data(r) for r in rows]

    def find_by_status(
        self,
        status: str,
        domain: str | None = None,
        failure_type: str | None = None,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        sql = f"SELECT {_SELECT_COLS} FROM {_TABLE} WHERE status = %s"
        params: list[Any] = [status]
        if domain is not None:
            sql += " AND domain = %s"
            params.append(domain)
        if failure_type is not None:
            sql += " AND failure_type = %s"
            params.append(failure_type)
        sql += " ORDER BY created_at ASC LIMIT %s"
        params.append(limit)
        rows = self._fetch_all(sql, params)
        return [self._row_to_data(r) for r in rows]

    def _build_filter_clauses(
        self,
        *,
        status: str | None = None,
        domain: str | None = None,
        failure_type: str | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        if domain is not None:
            clauses.append("domain = %s")
            params.append(domain)
        if failure_type is not None:
            clauses.append("failure_type = %s")
            params.append(failure_type)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def find(
        self,
        *,
        status: str | None = None,
        domain: str | None = None,
        failure_type: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        where, params = self._build_filter_clauses(
            status=status, domain=domain, failure_type=failure_type
        )
        sql = (
            f"SELECT {_SELECT_COLS} FROM {_TABLE}{where} "
            f"ORDER BY created_at DESC LIMIT %s OFFSET %s"
        )
        params.extend([limit, offset])
        rows = self._fetch_all(sql, params)
        return [self._row_to_data(r) for r in rows]

    def count(
        self,
        *,
        status: str | None = None,
        domain: str | None = None,
        failure_type: str | None = None,
    ) -> int:
        where, params = self._build_filter_clauses(
            status=status, domain=domain, failure_type=failure_type
        )
        row = self._fetch_one(f"SELECT COUNT(*) FROM {_TABLE}{where}", params)
        return int(row[0]) if row else 0

    def count_created_in_window(self, start: datetime, end: datetime) -> int:
        """Count rows whose created_at is in the inclusive [start, end].

        Backed by ``idx_baldur_dlq_created_at`` — a range seek, not a scan.
        """
        row = self._fetch_one(
            f"SELECT COUNT(*) FROM {_TABLE} WHERE created_at BETWEEN %s AND %s",
            (self._dt_to_db(start), self._dt_to_db(end)),
        )
        return int(row[0]) if row else 0

    def find_replayable(
        self,
        max_retries: int,
        domain: str | None = None,
        failure_type: str | None = None,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        sql = (
            f"SELECT {_SELECT_COLS} FROM {_TABLE} "
            f"WHERE status = %s AND retry_count < %s"
        )
        params: list[Any] = [FailedOperationStatus.PENDING.value, max_retries]
        if domain is not None:
            sql += " AND domain = %s"
            params.append(domain)
        if failure_type is not None:
            sql += " AND failure_type = %s"
            params.append(failure_type)
        sql += " ORDER BY created_at ASC LIMIT %s"
        params.append(limit)
        rows = self._fetch_all(sql, params)
        return [self._row_to_data(r) for r in rows]

    def find_sla_breached(
        self,
        current_time: datetime,
        sla_thresholds: dict[str, timedelta],
    ) -> list[FailedOperationData]:
        # Domain-scoped index seeks instead of a full PENDING scan. Each
        # loop iteration hits ``idx_baldur_dlq_status_domain`` with a
        # ``created_at`` range, so total work is proportional to breached
        # rows — not to the PENDING backlog.
        default = timedelta(hours=24)
        pending = FailedOperationStatus.PENDING.value
        results: list[FailedOperationData] = []

        for domain, threshold in sla_thresholds.items():
            cutoff = current_time - threshold
            rows = self._fetch_all(
                f"SELECT {_SELECT_COLS} FROM {_TABLE} "
                f"WHERE status = %s AND domain = %s AND created_at < %s",
                (pending, domain, self._dt_to_db(cutoff)),
            )
            results.extend(self._row_to_data(r) for r in rows)

        # Domains with no explicit threshold fall back to ``default``. When
        # ``sla_thresholds`` is empty the NOT IN filter degenerates, so
        # handle the two shapes separately.
        cutoff_default = current_time - default
        if sla_thresholds:
            placeholders = ",".join([self._placeholder] * len(sla_thresholds))
            rows = self._fetch_all(
                f"SELECT {_SELECT_COLS} FROM {_TABLE} "
                f"WHERE status = %s AND domain NOT IN ({placeholders}) "
                f"AND created_at < %s",
                (pending, *sla_thresholds.keys(), self._dt_to_db(cutoff_default)),
            )
        else:
            rows = self._fetch_all(
                f"SELECT {_SELECT_COLS} FROM {_TABLE} "
                f"WHERE status = %s AND created_at < %s",
                (pending, self._dt_to_db(cutoff_default)),
            )
        results.extend(self._row_to_data(r) for r in rows)
        return results

    def find_expired(self, current_time: datetime) -> list[FailedOperationData]:
        rows = self._fetch_all(
            f"SELECT {_SELECT_COLS} FROM {_TABLE} "
            f"WHERE expires_at IS NOT NULL AND expires_at < %s",
            (self._dt_to_db(current_time),),
        )
        return [self._row_to_data(r) for r in rows]

    def get_statistics(self) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "total": 0,
            "by_status": {},
            "by_domain": {},
            "pending_by_domain": {},
            "pending_by_domain_and_failure_type": {},
        }
        row = self._fetch_one(f"SELECT COUNT(*) FROM {_TABLE}")
        stats["total"] = int(row[0]) if row else 0

        for status, count in self._fetch_all(
            f"SELECT status, COUNT(*) FROM {_TABLE} GROUP BY status"
        ):
            stats["by_status"][status] = int(count)

        for domain, count in self._fetch_all(
            f"SELECT domain, COUNT(*) FROM {_TABLE} GROUP BY domain"
        ):
            stats["by_domain"][domain] = int(count)

        pending = FailedOperationStatus.PENDING.value
        for domain, count in self._fetch_all(
            f"SELECT domain, COUNT(*) FROM {_TABLE} WHERE status = %s GROUP BY domain",
            (pending,),
        ):
            stats["pending_by_domain"][domain] = int(count)

        for domain, failure_type, count in self._fetch_all(
            f"SELECT domain, failure_type, COUNT(*) FROM {_TABLE} "
            f"WHERE status = %s GROUP BY domain, failure_type",
            (pending,),
        ):
            stats["pending_by_domain_and_failure_type"].setdefault(domain, {})[
                failure_type
            ] = int(count)
        return stats

    def get_facet_counts(
        self,
        *,
        status: str | None = None,
        domain: str | None = None,
    ) -> dict[str, dict[str, int]]:
        """Faceted status×domain counts via GROUP BY.

        ``by_status`` is scoped by ``domain``; ``by_domain`` is scoped by
        ``status``. ``GROUP BY`` drops zero-count buckets structurally.
        The domain-scoped ``by_status`` (WHERE domain GROUP BY status) is a
        covering scan over idx_baldur_dlq_status_domain; the status-scoped
        ``by_domain`` (WHERE status GROUP BY domain) is a prefix seek on the
        same composite. Both exact, both fine on the cold operator read path.
        """
        # D2/D3: faceted status×domain counts via GROUP BY.
        by_status: dict[str, int] = {}
        if domain is None:
            rows = self._fetch_all(
                f"SELECT status, COUNT(*) FROM {_TABLE} GROUP BY status"
            )
        else:
            rows = self._fetch_all(
                f"SELECT status, COUNT(*) FROM {_TABLE} "
                f"WHERE domain = %s GROUP BY status",
                (domain,),
            )
        for s, count in rows:
            by_status[s] = int(count)

        by_domain: dict[str, int] = {}
        if status is None:
            rows = self._fetch_all(
                f"SELECT domain, COUNT(*) FROM {_TABLE} GROUP BY domain"
            )
        else:
            rows = self._fetch_all(
                f"SELECT domain, COUNT(*) FROM {_TABLE} "
                f"WHERE status = %s GROUP BY domain",
                (status,),
            )
        for d, count in rows:
            by_domain[d] = int(count)

        return {"by_status": by_status, "by_domain": by_domain}

    # ----- Atomic replay ----------------------------------------------------

    def try_acquire_for_replay(
        self, id: str, max_retries: int, force: bool = False
    ) -> FailedOperationData | None:
        """Atomically flip PENDING → REPLAYING if retry budget remains.

        Uses a conditional UPDATE as the concurrency guard. ``cursor.rowcount``
        > 0 means *this* worker won the race; the row is then re-read within
        the same transaction so no other writer can delete or mutate it
        between the claim and the DTO return.

        ``force=True`` is the operator cap-override: it widens the WHERE status
        set to {PENDING, REQUIRES_REVIEW}, drops the ``retry_count < max_retries``
        bound, resets retry_count to a fresh budget (1), and stamps the
        ``metadata`` history scar into the JSON payload — all inside the same
        transaction so the SELECT→UPDATE claim stays race-free against a
        concurrent sweep. See ``FailedOperationRepository.try_acquire_for_replay``.
        """
        if force:
            return self._force_acquire_for_replay(id)

        now = utc_now()
        conn = self._borrow_connection()
        with sql_transaction(conn):
            cursor = conn.cursor()
            try:
                cursor.execute(
                    self._prepare(
                        f"UPDATE {_TABLE} SET status = %s, "
                        f"retry_count = retry_count + 1, "
                        f"last_retry_at = %s, updated_at = %s "
                        f"WHERE id = %s AND status = %s AND retry_count < %s"
                    ),
                    (
                        FailedOperationStatus.REPLAYING.value,
                        self._dt_to_db(now),
                        self._dt_to_db(now),
                        int(id),
                        FailedOperationStatus.PENDING.value,
                        max_retries,
                    ),
                )
                if cursor.rowcount == 0:
                    return None
                cursor.execute(
                    self._prepare(f"SELECT {_SELECT_COLS} FROM {_TABLE} WHERE id = %s"),
                    (int(id),),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
        return self._row_to_data(row) if row else None

    def _force_acquire_for_replay(self, id: str) -> FailedOperationData | None:
        """Cap-override acquire: REQUIRES_REVIEW/PENDING → REPLAYING with reset.

        The metadata history scar lives in the JSON ``data`` column (not a
        structured column), so the force path reads the row inside the
        transaction to compute the stamp, then writes blob + columns together
        under a widened conditional ``WHERE`` (still the concurrency guard).
        """
        now = utc_now()
        conn = self._borrow_connection()
        with sql_transaction(conn):
            cursor = conn.cursor()
            try:
                cursor.execute(
                    self._prepare(f"SELECT {_SELECT_COLS} FROM {_TABLE} WHERE id = %s"),
                    (int(id),),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                entry = self._row_to_data(row)
                if entry.status not in (
                    FailedOperationStatus.PENDING.value,
                    FailedOperationStatus.REQUIRES_REVIEW.value,
                ):
                    return None

                # D3/G5: stamp history before resetting to a fresh budget.
                metadata = dict(entry.metadata or {})
                metadata["previous_total_retries"] = (
                    metadata.get("previous_total_retries", 0) + entry.retry_count
                )
                metadata["force_redrive_count"] = (
                    metadata.get("force_redrive_count", 0) + 1
                )
                entry.metadata = metadata
                entry.status = FailedOperationStatus.REPLAYING.value
                entry.retry_count = 1
                entry.last_retry_at = now
                entry.updated_at = now

                cursor.execute(
                    self._prepare(
                        f"UPDATE {_TABLE} SET status = %s, retry_count = %s, "
                        f"last_retry_at = %s, updated_at = %s, data = %s "
                        f"WHERE id = %s AND status IN (%s, %s)"
                    ),
                    (
                        FailedOperationStatus.REPLAYING.value,
                        1,
                        self._dt_to_db(now),
                        self._dt_to_db(now),
                        self._payload_from_data(entry),
                        int(id),
                        FailedOperationStatus.PENDING.value,
                        FailedOperationStatus.REQUIRES_REVIEW.value,
                    ),
                )
                if cursor.rowcount == 0:
                    return None
                cursor.execute(
                    self._prepare(f"SELECT {_SELECT_COLS} FROM {_TABLE} WHERE id = %s"),
                    (int(id),),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
        return self._row_to_data(row) if row else None

    def complete_replay(
        self,
        id: str,
        success: bool,
        resolution_type: str = "",
        note: str = "",
        resolved_by_id: int | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> bool:
        entry = self.get_by_id(id)
        if entry is None:
            return False
        now = utc_now()
        if success:
            new_status = FailedOperationStatus.RESOLVED.value
            entry.resolved_at = now
        elif entry.retry_count >= entry.max_retries:
            # At cap: converge to the terminal review state (mirrors Redis)
            new_status = FailedOperationStatus.REQUIRES_REVIEW.value
        else:
            # Under cap: revert to pending for the next retry
            new_status = FailedOperationStatus.PENDING.value

        entry.status = new_status
        entry.updated_at = now
        if note:
            entry.resolution_note = note
            entry.error_message = note
        if resolution_type:
            entry.resolution_type = resolution_type
        if resolved_by_id is not None:
            entry.resolved_by_id = resolved_by_id
        if error_details:
            entry.metadata = {**(entry.metadata or {}), **error_details}

        self._execute(
            f"UPDATE {_TABLE} SET status = %s, resolved_at = %s, updated_at = %s, "
            f"data = %s WHERE id = %s",
            (
                new_status,
                self._dt_to_db(entry.resolved_at),
                self._dt_to_db(now),
                self._payload_from_data(entry),
                int(id),
            ),
        )
        return True

    def release_stale_replaying(self, older_than_minutes: int = 30) -> int:
        cutoff = utc_now() - timedelta(minutes=older_than_minutes)
        conn = self._borrow_connection()
        cursor = conn.cursor()
        try:
            stmt = self._prepare(
                f"UPDATE {_TABLE} SET status = %s, updated_at = %s "
                f"WHERE status = %s AND last_retry_at IS NOT NULL AND last_retry_at < %s"
            )
            cursor.execute(
                stmt,
                (
                    FailedOperationStatus.PENDING.value,
                    self._dt_to_db(utc_now()),
                    FailedOperationStatus.REPLAYING.value,
                    self._dt_to_db(cutoff),
                ),
            )
            released = int(cursor.rowcount or 0)
            if self._should_commit(conn):
                conn.commit()
            return released
        except Exception:
            if self._should_commit(conn):
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            raise
        finally:
            cursor.close()

    # ----- Cleanup ----------------------------------------------------------

    def archive_old_resolved(self, older_than_days: int = 30) -> int:
        cutoff = utc_now() - timedelta(days=older_than_days)
        conn = self._borrow_connection()
        cursor = conn.cursor()
        try:
            stmt = self._prepare(
                f"UPDATE {_TABLE} SET status = %s, updated_at = %s "
                f"WHERE status = %s AND resolved_at IS NOT NULL AND resolved_at < %s"
            )
            cursor.execute(
                stmt,
                (
                    FailedOperationStatus.ARCHIVED.value,
                    self._dt_to_db(utc_now()),
                    FailedOperationStatus.RESOLVED.value,
                    self._dt_to_db(cutoff),
                ),
            )
            archived = int(cursor.rowcount or 0)
            if self._should_commit(conn):
                conn.commit()
            return archived
        except Exception:
            if self._should_commit(conn):
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            raise
        finally:
            cursor.close()

    def purge_archived(  # noqa: C901
        self,
        ids: list[str] | None = None,
        older_than_days: int | None = None,
    ) -> int:
        if ids is not None and older_than_days is not None:
            raise ValueError("Specify either ids or older_than_days, not both")

        # Explicit empty-list short-circuit — no SQL round-trip, no
        # ``WHERE id IN (NULL)`` degenerate clause.
        if ids is not None and not ids:
            return 0

        # No-args is a no-op (fail-safe): a destructive purge with no selection
        # criteria deletes nothing. Use ``older_than_days=0`` to purge all
        # archived entries. Contract parity with the memory/Redis adapters.
        if ids is None and older_than_days is None:
            return 0

        conn = self._borrow_connection()
        cursor = conn.cursor()
        try:
            if ids is not None:
                # Reject non-archived entries (contract parity with memory adapter).
                int_ids = [int(i) for i in ids]
                placeholders = ",".join([self._placeholder] * len(ids))
                check_sql = (
                    f"SELECT id, status FROM {_TABLE} WHERE id IN ({placeholders})"
                )
                cursor.execute(check_sql, tuple(int_ids))
                for row in cursor.fetchall():
                    if row[1] != FailedOperationStatus.ARCHIVED.value:
                        raise ValueError(
                            f"Entry {row[0]} is not archived (status: {row[1]}). "
                            "Only archived entries can be purged."
                        )
                cursor.execute(
                    self._prepare(
                        f"DELETE FROM {_TABLE} WHERE id IN ({placeholders}) AND status = %s"
                    ),
                    tuple(int_ids) + (FailedOperationStatus.ARCHIVED.value,),
                )
            else:
                # older_than_days is not None (guaranteed by the no-args and
                # both-set guards above).
                assert older_than_days is not None
                cutoff = utc_now() - timedelta(days=older_than_days)
                cursor.execute(
                    self._prepare(
                        f"DELETE FROM {_TABLE} "
                        f"WHERE status = %s AND updated_at IS NOT NULL AND updated_at < %s"
                    ),
                    (FailedOperationStatus.ARCHIVED.value, self._dt_to_db(cutoff)),
                )
            purged = int(cursor.rowcount or 0)
            if self._should_commit(conn):
                conn.commit()
            return purged
        except Exception:
            if self._should_commit(conn):
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            raise
        finally:
            cursor.close()

    def get_cleanup_stats(self) -> dict[str, Any]:
        now = utc_now()
        day_30_ago = now - timedelta(days=30)
        day_90_ago = now - timedelta(days=90)
        row = self._fetch_one(f"SELECT COUNT(*) FROM {_TABLE}")
        total = int(row[0]) if row else 0
        by_status: dict[str, int] = {}
        for status, count in self._fetch_all(
            f"SELECT status, COUNT(*) FROM {_TABLE} GROUP BY status"
        ):
            by_status[status] = int(count)
        resolved_old = self._fetch_one(
            f"SELECT COUNT(*) FROM {_TABLE} WHERE status = %s "
            f"AND resolved_at IS NOT NULL AND resolved_at < %s",
            (FailedOperationStatus.RESOLVED.value, self._dt_to_db(day_30_ago)),
        )
        archived_old = self._fetch_one(
            f"SELECT COUNT(*) FROM {_TABLE} WHERE status = %s "
            f"AND updated_at IS NOT NULL AND updated_at < %s",
            (FailedOperationStatus.ARCHIVED.value, self._dt_to_db(day_90_ago)),
        )
        return {
            "total": total,
            "by_status": by_status,
            "resolved_older_than_30_days": int(resolved_old[0]) if resolved_old else 0,
            "archived_older_than_90_days": (
                int(archived_old[0]) if archived_old else 0
            ),
        }

    def count_archived_older_than(self, older_than_days: int) -> int:
        cutoff = utc_now() - timedelta(days=older_than_days)
        row = self._fetch_one(
            self._prepare(
                f"SELECT COUNT(*) FROM {_TABLE} "
                f"WHERE status = %s AND resolved_at IS NOT NULL AND resolved_at < %s"
            ),
            (FailedOperationStatus.ARCHIVED.value, self._dt_to_db(cutoff)),
        )
        return int(row[0]) if row else 0

    # ----- Size-limit overflow ---------------------------------------------

    def count_all(self) -> int:
        excluded = (
            FailedOperationStatus.RESOLVED.value,
            FailedOperationStatus.REJECTED.value,
            FailedOperationStatus.ARCHIVED.value,
        )
        placeholders = ",".join([self._placeholder] * len(excluded))
        row = self._fetch_one(
            f"SELECT COUNT(*) FROM {_TABLE} WHERE status NOT IN ({placeholders})",
            excluded,
        )
        return int(row[0]) if row else 0

    def count_by_domain(self, domain: str) -> int:
        row = self._fetch_one(
            f"SELECT COUNT(*) FROM {_TABLE} WHERE domain = %s", (domain,)
        )
        return int(row[0]) if row else 0

    def get_oldest_ids(self, count: int, domain: str | None = None) -> list[str]:
        if domain is None:
            rows = self._fetch_all(
                f"SELECT id FROM {_TABLE} ORDER BY created_at ASC LIMIT %s", (count,)
            )
        else:
            rows = self._fetch_all(
                f"SELECT id FROM {_TABLE} WHERE domain = %s "
                f"ORDER BY created_at ASC LIMIT %s",
                (domain, count),
            )
        return [str(r[0]) for r in rows]

    def delete(self, entry_id: str) -> bool:
        conn = self._borrow_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                self._prepare(f"DELETE FROM {_TABLE} WHERE id = %s"), (int(entry_id),)
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

    _EVICTION_PROTECTED = (
        FailedOperationStatus.REPLAYING.value,
        FailedOperationStatus.REVIEWING.value,
    )

    def evict_oldest(self, count: int, domain: str | None = None) -> int:
        oldest = self.get_oldest_ids(count, domain)
        if not oldest:
            return 0
        oldest_int = [int(i) for i in oldest]
        placeholders = ",".join([self._placeholder] * len(oldest))
        protected_ph = ",".join([self._placeholder] * len(self._EVICTION_PROTECTED))
        conn = self._borrow_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                self._prepare(
                    f"DELETE FROM {_TABLE} WHERE id IN ({placeholders})"
                    f" AND status NOT IN ({protected_ph})"
                ),
                tuple(oldest_int) + self._EVICTION_PROTECTED,
            )
            evicted = int(cursor.rowcount or 0)
            if self._should_commit(conn):
                conn.commit()
            return evicted
        except Exception:
            if self._should_commit(conn):
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
            raise
        finally:
            cursor.close()

    # ----- Compression -----------------------------------------------------

    def store_compressed_entry(self, entry: DLQCompressedEntry) -> bool:
        payload = self._dumps_json(
            {
                "first_seen": entry.first_seen.isoformat()
                if entry.first_seen
                else None,
                "last_seen": entry.last_seen.isoformat() if entry.last_seen else None,
                "sample_error_message": entry.sample_error_message,
                "sample_context": entry.sample_context,
            }
        )
        upsert_tail = dialect_upsert_clause(
            self._dialect,
            conflict_cols=["id"],
            update_cols=["count", "status", "data"],
        )
        stmt = (
            f"INSERT INTO {_COMPRESSED_TABLE} "
            f"(id, domain, failure_type, error_code, count, status, "
            f"compressed_at, stale_at, archived_at, data) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            f"{upsert_tail}"
        )
        self._execute(
            stmt,
            (
                entry.id,
                entry.domain,
                entry.failure_type,
                entry.error_code,
                entry.count,
                entry.status,
                self._dt_to_db(entry.compressed_at),
                self._dt_to_db(entry.stale_at),
                self._dt_to_db(entry.archived_at),
                payload,
            ),
        )
        return True

    def get_compressed_entries(
        self,
        domain: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[DLQCompressedEntry]:
        sql = (
            f"SELECT id, domain, failure_type, error_code, count, status, "
            f"compressed_at, stale_at, archived_at, data FROM {_COMPRESSED_TABLE}"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if domain is not None:
            clauses.append("domain = %s")
            params.append(domain)
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY compressed_at DESC LIMIT %s"
        params.append(limit)
        return [
            self._compressed_row_to_entry(row) for row in self._fetch_all(sql, params)
        ]

    def get_compressed_entries_before(
        self,
        *,
        status: str,
        before: datetime,
        limit: int = 100,
        offset: int = 0,
        after: datetime | None = None,
    ) -> list[DLQCompressedEntry]:
        """Query compressed entries in a cutoff window, oldest first.

        The (status, compressed_at) index serves both bounds and the order.
        """
        window = "WHERE status = %s AND compressed_at < %s "
        params: list[Any] = [status, before]
        if after is not None:
            window += "AND compressed_at >= %s "
            params.append(after)
        sql = (
            f"SELECT id, domain, failure_type, error_code, count, status, "
            f"compressed_at, stale_at, archived_at, data FROM {_COMPRESSED_TABLE} "
            f"{window}"
            f"ORDER BY compressed_at ASC LIMIT %s OFFSET %s"
        )
        params.extend([limit, offset])
        rows = self._fetch_all(sql, params)
        return [self._compressed_row_to_entry(row) for row in rows]

    def _compressed_row_to_entry(self, row: Any) -> DLQCompressedEntry:
        """Map a compressed-table row onto its DTO."""
        data = self._loads_json(row[9]) or {}
        first_seen = data.get("first_seen")
        last_seen = data.get("last_seen")
        return DLQCompressedEntry(
            id=row[0],
            domain=row[1],
            failure_type=row[2],
            error_code=row[3],
            count=int(row[4]),
            status=row[5],
            first_seen=(
                datetime.fromisoformat(first_seen)
                if isinstance(first_seen, str)
                else (first_seen or utc_now())
            ),
            last_seen=(
                datetime.fromisoformat(last_seen)
                if isinstance(last_seen, str)
                else (last_seen or utc_now())
            ),
            sample_error_message=data.get("sample_error_message", "") or "",
            sample_context=data.get("sample_context", {}) or {},
            compressed_at=self._dt_from_db(row[6]) or utc_now(),
            stale_at=self._dt_from_db(row[7]),
            archived_at=self._dt_from_db(row[8]),
        )

    def get_compressed_summary(self) -> dict[str, Any]:
        row = self._fetch_one(
            f"SELECT COUNT(*), COALESCE(SUM(count), 0) FROM {_COMPRESSED_TABLE}"
        )
        total_summaries = int(row[0]) if row else 0
        total_items = int(row[1]) if row else 0
        by_status: dict[str, int] = {}
        for status, count in self._fetch_all(
            f"SELECT status, COUNT(*) FROM {_COMPRESSED_TABLE} GROUP BY status"
        ):
            by_status[status] = int(count)
        return {
            "total_summaries": total_summaries,
            "total_compressed_items": total_items,
            "by_status": by_status,
        }

    def update_compressed_status(self, entry_id: str, new_status: str) -> bool:
        """Transition compressed entry lifecycle status.

        Stamps the matching timestamp column alongside the status, as the
        memory and Redis adapters do. STALE -> ARCHIVED is driven off
        ``stale_at``, so an unstamped transition would strand the entry.
        """
        now = utc_now()
        if new_status == DLQCompressedStatus.STALE.value:
            self._execute(
                f"UPDATE {_COMPRESSED_TABLE} SET status = %s, stale_at = %s "
                f"WHERE id = %s",
                (new_status, now, entry_id),
            )
        elif new_status == DLQCompressedStatus.ARCHIVED.value:
            self._execute(
                f"UPDATE {_COMPRESSED_TABLE} SET status = %s, archived_at = %s "
                f"WHERE id = %s",
                (new_status, now, entry_id),
            )
        else:
            self._execute(
                f"UPDATE {_COMPRESSED_TABLE} SET status = %s WHERE id = %s",
                (new_status, entry_id),
            )
        return True
