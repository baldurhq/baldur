"""
SQL Statistics repository.

Framework-free adapter for ``StatisticsRepositoryInterface`` backed by
any DB-API 2.0 database. Reads from tables owned by other SQL repos
(``baldur_dlq``, ``baldur_cb_state``) — does NOT declare its own schema.

Key design points:
- All aggregation queries enforce a ``created_at`` time-range filter
  (default 30 days) to prevent unbounded full-table scans.
- CB methods apply graceful degradation: catch OperationalError when
  the CB table has not been bootstrapped yet and return empty defaults.
- Registered via ``ProviderRegistry.register_statistics_adapter()``
  singleton, NOT via GenericProviderRegistry.
"""

# Reference: 433 D7.

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import structlog

from baldur.adapters.sql.base import (
    GenericSQLRepository,
    dialect_upsert_clause,
)
from baldur.adapters.sql.circuit_breaker import _TABLE as CB_TABLE
from baldur.adapters.sql.failed_operation import _TABLE as DLQ_TABLE
from baldur.interfaces.statistics import (
    AuditTrailEntry,
    CircuitBreakerInfo,
    CircuitBreakerSummary,
    CleanupStats,
    DomainDistribution,
    EntityAuditTrail,
    FailureTypeDistribution,
    PaginatedResult,
    RecentActivity,
    StatisticsRepositoryInterface,
    StatusCounts,
)
from baldur.settings.sql import SQLDialect
from baldur.utils.time import utc_now

__all__ = ["SQLStatisticsRepository"]

logger = structlog.get_logger()

_DEFAULT_RANGE_DAYS = 30

_ALLOWED_ORDER_COLS = frozenset(
    {
        "id",
        "domain",
        "failure_type",
        "status",
        "entity_type",
        "entity_id",
        "retry_count",
        "error_code",
        "created_at",
        "updated_at",
        "resolved_at",
    }
)


class SQLStatisticsRepository(GenericSQLRepository, StatisticsRepositoryInterface):
    """DB-API 2.0 backed statistics repository.

    Reads from ``baldur_dlq`` and ``baldur_cb_state`` tables directly.
    Does not own a table — schema bootstrap is skipped.
    """

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
            schema=None,
        )

    # ----- DLQ Statistics ---------------------------------------------------

    def _default_cutoff(self) -> str:
        return self._dt_to_db(utc_now() - timedelta(days=_DEFAULT_RANGE_DAYS))

    def get_status_counts(self) -> StatusCounts:
        try:
            cutoff = self._default_cutoff()
            rows = self._fetch_all(
                f"SELECT status, COUNT(*) FROM {DLQ_TABLE} "
                f"WHERE created_at >= %s GROUP BY status",
                (cutoff,),
            )
            counts = StatusCounts()
            for status, count in rows:
                c = int(count)
                counts.total += c
                if hasattr(counts, status):
                    setattr(counts, status, c)
            return counts
        except Exception:
            logger.warning("sql.statistics_status_counts_failed", exc_info=True)
            return StatusCounts()

    def get_domain_distribution(self, limit: int = 10) -> list[DomainDistribution]:
        try:
            cutoff = self._default_cutoff()
            total_row = self._fetch_one(
                f"SELECT COUNT(*) FROM {DLQ_TABLE} WHERE created_at >= %s",
                (cutoff,),
            )
            total = int(total_row[0]) if total_row else 0
            if total == 0:
                return []
            rows = self._fetch_all(
                f"SELECT domain, COUNT(*) AS cnt FROM {DLQ_TABLE} "
                f"WHERE created_at >= %s "
                f"GROUP BY domain ORDER BY cnt DESC LIMIT %s",
                (cutoff, limit),
            )
            return [
                DomainDistribution(
                    domain=row[0] or "unknown",
                    count=int(row[1]),
                    percentage=round(int(row[1]) / total * 100, 2),
                )
                for row in rows
            ]
        except Exception:
            logger.warning("sql.statistics_domain_dist_failed", exc_info=True)
            return []

    def get_failure_type_distribution(
        self, limit: int = 10
    ) -> list[FailureTypeDistribution]:
        try:
            cutoff = self._default_cutoff()
            total_row = self._fetch_one(
                f"SELECT COUNT(*) FROM {DLQ_TABLE} WHERE created_at >= %s",
                (cutoff,),
            )
            total = int(total_row[0]) if total_row else 0
            if total == 0:
                return []
            rows = self._fetch_all(
                f"SELECT failure_type, COUNT(*) AS cnt FROM {DLQ_TABLE} "
                f"WHERE created_at >= %s "
                f"GROUP BY failure_type ORDER BY cnt DESC LIMIT %s",
                (cutoff, limit),
            )
            return [
                FailureTypeDistribution(
                    failure_type=row[0] or "unknown",
                    count=int(row[1]),
                    percentage=round(int(row[1]) / total * 100, 2),
                )
                for row in rows
            ]
        except Exception:
            logger.warning("sql.statistics_failure_dist_failed", exc_info=True)
            return []

    def get_recent_activity(self, hours: int = 24, days: int = 7) -> RecentActivity:
        try:
            now = utc_now()
            hours_ago = self._dt_to_db(now - timedelta(hours=hours))
            days_ago = self._dt_to_db(now - timedelta(days=days))
            prev_week_start = self._dt_to_db(now - timedelta(days=days * 2))

            def _count(sql: str, params: tuple) -> int:
                row = self._fetch_one(sql, params)
                return int(row[0]) if row else 0

            new_in_24h = _count(
                f"SELECT COUNT(*) FROM {DLQ_TABLE} WHERE created_at >= %s",
                (hours_ago,),
            )
            resolved_in_24h = _count(
                f"SELECT COUNT(*) FROM {DLQ_TABLE} WHERE resolved_at >= %s",
                (hours_ago,),
            )
            new_in_7d = _count(
                f"SELECT COUNT(*) FROM {DLQ_TABLE} WHERE created_at >= %s",
                (days_ago,),
            )
            resolved_in_7d = _count(
                f"SELECT COUNT(*) FROM {DLQ_TABLE} WHERE resolved_at >= %s",
                (days_ago,),
            )
            prev_week = _count(
                f"SELECT COUNT(*) FROM {DLQ_TABLE} "
                f"WHERE created_at >= %s AND created_at < %s",
                (prev_week_start, days_ago),
            )
            if new_in_7d > prev_week * 1.1:
                trend = "up"
            elif new_in_7d < prev_week * 0.9:
                trend = "down"
            else:
                trend = "stable"
            return RecentActivity(
                new_in_24h=new_in_24h,
                resolved_in_24h=resolved_in_24h,
                new_in_7d=new_in_7d,
                resolved_in_7d=resolved_in_7d,
                trend=trend,
            )
        except Exception:
            logger.warning("sql.statistics_recent_activity_failed", exc_info=True)
            return RecentActivity()

    def get_resolution_rate(self, days: int = 30) -> float:
        try:
            since = self._dt_to_db(utc_now() - timedelta(days=days))
            total_row = self._fetch_one(
                f"SELECT COUNT(*) FROM {DLQ_TABLE} WHERE created_at >= %s",
                (since,),
            )
            total = int(total_row[0]) if total_row else 0
            if total == 0:
                return 0.0
            resolved_row = self._fetch_one(
                f"SELECT COUNT(*) FROM {DLQ_TABLE} "
                f"WHERE created_at >= %s AND status = %s",
                (since, "resolved"),
            )
            resolved = int(resolved_row[0]) if resolved_row else 0
            return round(resolved / total, 4)
        except Exception:
            logger.warning("sql.statistics_resolution_rate_failed", exc_info=True)
            return 0.0

    def get_avg_retry_count(self) -> float:
        try:
            cutoff = self._default_cutoff()
            row = self._fetch_one(
                f"SELECT AVG(retry_count) FROM {DLQ_TABLE} WHERE created_at >= %s",
                (cutoff,),
            )
            return round(float(row[0] or 0.0), 2) if row else 0.0
        except Exception:
            logger.warning("sql.statistics_avg_retry_failed", exc_info=True)
            return 0.0

    # ----- DLQ List Operations (Paginated) ----------------------------------

    def list_entries(
        self,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
        domain: str | None = None,
        failure_type: str | None = None,
        order_by: str = "-created_at",
    ) -> PaginatedResult:
        try:
            clauses: list[str] = []
            params: list[Any] = []
            if status:
                clauses.append("status = %s")
                params.append(status)
            if domain:
                clauses.append("domain = %s")
                params.append(domain)
            if failure_type:
                clauses.append("failure_type = %s")
                params.append(failure_type)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

            total_row = self._fetch_one(
                f"SELECT COUNT(*) FROM {DLQ_TABLE}{where}", params
            )
            total = int(total_row[0]) if total_row else 0

            col = order_by.lstrip("-")
            if col not in _ALLOWED_ORDER_COLS:
                col = "created_at"
            direction = "DESC" if order_by.startswith("-") else "ASC"
            offset = (page - 1) * page_size

            select_cols = (
                "id, domain, failure_type, status, entity_type, entity_id, "
                "retry_count, error_code, created_at, updated_at, resolved_at"
            )
            rows = self._fetch_all(
                f"SELECT {select_cols} FROM {DLQ_TABLE}{where} "
                f"ORDER BY {col} {direction} LIMIT %s OFFSET %s",
                params + [page_size, offset],
            )
            items = [
                {
                    "id": r[0],
                    "domain": r[1],
                    "failure_type": r[2],
                    "status": r[3],
                    "entity_type": r[4],
                    "entity_id": r[5],
                    "retry_count": r[6],
                    "error_code": r[7],
                    "created_at": self._dt_from_db(r[8]),
                    "updated_at": self._dt_from_db(r[9]),
                    "resolved_at": self._dt_from_db(r[10]),
                }
                for r in rows
            ]
            return PaginatedResult(
                items=items,
                total=total,
                page=page,
                page_size=page_size,
                has_next=offset + page_size < total,
                has_prev=page > 1,
            )
        except Exception:
            logger.warning("sql.statistics_list_entries_failed", exc_info=True)
            return PaginatedResult(page=page, page_size=page_size)

    def get_entry_detail(self, entry_id: str) -> dict[str, Any] | None:
        try:
            row = self._fetch_one(
                f"SELECT id, domain, failure_type, status, entity_type, "
                f"entity_id, user_id, retry_count, max_retries, error_code, "
                f"last_retry_at, resolved_at, created_at, updated_at, "
                f"expires_at, data FROM {DLQ_TABLE} WHERE id = %s",
                (entry_id,),
            )
            if not row:
                return None
            data = self._loads_json(row[15]) or {}
            return {
                "id": row[0],
                "domain": row[1],
                "failure_type": row[2],
                "status": row[3],
                "entity_type": row[4],
                "entity_id": row[5],
                "user_id": row[6],
                "retry_count": row[7],
                "max_retries": row[8],
                "error_code": row[9],
                "last_retry_at": self._dt_from_db(row[10]),
                "resolved_at": self._dt_from_db(row[11]),
                "created_at": self._dt_from_db(row[12]),
                "updated_at": self._dt_from_db(row[13]),
                "expires_at": self._dt_from_db(row[14]),
                "error_message": data.get("error_message", ""),
                "snapshot_data": data.get("snapshot_data", {}),
                "metadata": data.get("metadata", {}),
            }
        except Exception:
            logger.warning("sql.statistics_entry_detail_failed", exc_info=True)
            return None

    # ----- SLA Monitoring ---------------------------------------------------

    def get_sla_breaches(
        self,
        sla_threshold_hours: int = 4,
        statuses: list[str] | None = None,
    ) -> dict[str, int]:
        try:
            if statuses is None:
                statuses = ["pending", "reviewing", "requires_review"]
            cutoff = self._dt_to_db(utc_now() - timedelta(hours=sla_threshold_hours))
            placeholders = ",".join([self._placeholder] * len(statuses))
            rows = self._fetch_all(
                f"SELECT domain, COUNT(*) FROM {DLQ_TABLE} "
                f"WHERE status IN ({placeholders}) AND created_at < %s "
                f"GROUP BY domain",
                (*statuses, cutoff),
            )
            return {row[0]: int(row[1]) for row in rows}
        except Exception:
            logger.warning("sql.statistics_sla_breaches_failed", exc_info=True)
            return {}

    # ----- Cleanup Operations -----------------------------------------------

    def get_cleanup_stats(self) -> CleanupStats:
        try:
            now = utc_now()
            day_30_ago = self._dt_to_db(now - timedelta(days=30))
            day_90_ago = self._dt_to_db(now - timedelta(days=90))

            total_row = self._fetch_one(f"SELECT COUNT(*) FROM {DLQ_TABLE}")
            total = int(total_row[0]) if total_row else 0

            by_status: dict[str, int] = {}
            for status, count in self._fetch_all(
                f"SELECT status, COUNT(*) FROM {DLQ_TABLE} GROUP BY status"
            ):
                by_status[status] = int(count)

            resolved_old = self._fetch_one(
                f"SELECT COUNT(*) FROM {DLQ_TABLE} "
                f"WHERE status = %s AND resolved_at IS NOT NULL AND resolved_at < %s",
                ("resolved", day_30_ago),
            )
            archived_old = self._fetch_one(
                f"SELECT COUNT(*) FROM {DLQ_TABLE} "
                f"WHERE status = %s AND updated_at IS NOT NULL AND updated_at < %s",
                ("archived", day_90_ago),
            )
            return CleanupStats(
                total=total,
                by_status=by_status,
                resolved_older_than_30_days=(
                    int(resolved_old[0]) if resolved_old else 0
                ),
                archived_older_than_90_days=(
                    int(archived_old[0]) if archived_old else 0
                ),
            )
        except Exception:
            logger.warning("sql.statistics_cleanup_stats_failed", exc_info=True)
            return CleanupStats()

    def archive_old_entries(self, older_than_days: int = 30) -> int:
        try:
            _now = utc_now()
            cutoff = self._dt_to_db(_now - timedelta(days=older_than_days))
            now = self._dt_to_db(_now)
            conn = self._borrow_connection()
            cursor = conn.cursor()
            try:
                cursor.execute(
                    self._prepare(
                        f"UPDATE {DLQ_TABLE} SET status = %s, updated_at = %s "
                        f"WHERE status = %s AND resolved_at IS NOT NULL "
                        f"AND resolved_at < %s"
                    ),
                    ("archived", now, "resolved", cutoff),
                )
                archived = int(cursor.rowcount or 0)
                if self._should_commit(conn):
                    conn.commit()
                logger.info(
                    "sql.statistics_archived_entries",
                    archived_count=archived,
                )
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
        except Exception:
            logger.warning("sql.statistics_archive_failed", exc_info=True)
            return 0

    def purge_archived(
        self,
        ids: list[str] | None = None,
        older_than_days: int | None = None,
    ) -> int:
        try:
            conn = self._borrow_connection()
            cursor = conn.cursor()
            try:
                if ids:
                    placeholders = ",".join([self._placeholder] * len(ids))
                    cursor.execute(
                        self._prepare(
                            f"DELETE FROM {DLQ_TABLE} "
                            f"WHERE status = %s AND id IN ({placeholders})"
                        ),
                        ("archived", *ids),
                    )
                elif older_than_days is not None:
                    cutoff = self._dt_to_db(utc_now() - timedelta(days=older_than_days))
                    cursor.execute(
                        self._prepare(
                            f"DELETE FROM {DLQ_TABLE} "
                            f"WHERE status = %s AND updated_at IS NOT NULL "
                            f"AND updated_at < %s"
                        ),
                        ("archived", cutoff),
                    )
                else:
                    cursor.execute(
                        self._prepare(f"DELETE FROM {DLQ_TABLE} WHERE status = %s"),
                        ("archived",),
                    )
                purged = int(cursor.rowcount or 0)
                if self._should_commit(conn):
                    conn.commit()
                logger.info(
                    "sql.statistics_purged_entries",
                    purged_count=purged,
                )
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
        except Exception:
            logger.warning("sql.statistics_purge_failed", exc_info=True)
            return 0

    # ----- Circuit Breaker Statistics (graceful degradation) -----------------

    def get_circuit_breaker_summary(self) -> CircuitBreakerSummary:
        try:
            rows = self._fetch_all(
                f"SELECT state, COUNT(*) FROM {CB_TABLE} GROUP BY state"
            )
            summary = CircuitBreakerSummary()
            for state, count in rows:
                c = int(count)
                summary.total += c
                if state == "closed":
                    summary.closed = c
                elif state == "open":
                    summary.open = c
                elif state == "half_open":
                    summary.half_open = c
            return summary
        except Exception:
            logger.debug(
                "sql.statistics_cb_summary_degraded",
                exc_info=True,
            )
            return CircuitBreakerSummary()

    def list_circuit_breakers(self) -> list[CircuitBreakerInfo]:
        try:
            rows = self._fetch_all(
                f"SELECT service_name, state, failure_count, success_count, "
                f"last_failure_at, updated_at FROM {CB_TABLE}"
            )
            return [
                CircuitBreakerInfo(
                    service_name=r[0],
                    state=r[1],
                    failure_count=int(r[2] or 0),
                    success_count=int(r[3] or 0),
                    last_failure_time=self._dt_from_db(r[4]),
                    last_state_change=self._dt_from_db(r[5]),
                )
                for r in rows
            ]
        except Exception:
            logger.debug(
                "sql.statistics_cb_list_degraded",
                exc_info=True,
            )
            return []

    # ----- Persistence (hybrid storage) -------------------------------------

    def persist_entry(self, entry_data: dict[str, Any]) -> str | None:
        try:
            entry_id = entry_data.get("id")
            if entry_id is None:
                return None
            now = utc_now()
            payload = self._dumps_json(
                {
                    k: v
                    for k, v in entry_data.items()
                    if k
                    not in (
                        "id",
                        "domain",
                        "failure_type",
                        "status",
                        "entity_type",
                        "entity_id",
                        "user_id",
                        "retry_count",
                        "max_retries",
                        "error_code",
                        "last_retry_at",
                        "resolved_at",
                        "created_at",
                        "updated_at",
                        "expires_at",
                    )
                }
            )
            upsert_tail = dialect_upsert_clause(
                self._dialect,
                conflict_cols=["id"],
                update_cols=[
                    "status",
                    "retry_count",
                    "resolved_at",
                    "updated_at",
                    "data",
                ],
            )
            self._execute(
                f"INSERT INTO {DLQ_TABLE} "
                f"(id, domain, failure_type, status, entity_type, entity_id, "
                f"user_id, retry_count, max_retries, error_code, "
                f"created_at, updated_at, data) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                f"{upsert_tail}",
                (
                    entry_id,
                    entry_data.get("domain", ""),
                    entry_data.get("failure_type", ""),
                    entry_data.get("status", "pending"),
                    entry_data.get("entity_type"),
                    entry_data.get("entity_id"),
                    entry_data.get("user_id"),
                    entry_data.get("retry_count", 0),
                    entry_data.get("max_retries", 2),
                    entry_data.get("error_code", ""),
                    self._dt_to_db(entry_data.get("created_at") or now),
                    self._dt_to_db(now),
                    payload,
                ),
            )
            return str(entry_id)
        except Exception:
            logger.warning("sql.statistics_persist_entry_failed", exc_info=True)
            return None

    def sync_from_runtime(self, entries: list[dict[str, Any]]) -> int:
        synced = 0
        for entry_data in entries:
            if self.persist_entry(entry_data):
                synced += 1
        logger.info(
            "sql.statistics_synced_entries",
            synced=synced,
            entries_count=len(entries),
        )
        return synced

    # ----- Audit Trail Integration ------------------------------------------

    def get_audit_trail_by_entity(
        self,
        entity_id: str,
        entity_type: str = "dlq_entry",
    ) -> EntityAuditTrail:
        trail = EntityAuditTrail(
            entity_id=entity_id,
            entity_type=entity_type,
            domain="unknown",
            entries=[],
        )
        if entity_type == "dlq_entry":
            try:
                row = self._fetch_one(
                    f"SELECT domain, status, created_at, resolved_at, data "
                    f"FROM {DLQ_TABLE} WHERE id = %s",
                    (entity_id,),
                )
                if row:
                    trail.domain = row[0] or "unknown"
                    trail.current_status = row[1] or "unknown"
                    trail.created_at = self._dt_from_db(row[2])
                    trail.resolved_at = self._dt_from_db(row[3])
                    data = self._loads_json(row[4]) or {}
                    audit_refs = data.get("metadata", {}).get("audit_references", [])
                    for ref in audit_refs:
                        trail.entries.append(
                            AuditTrailEntry(
                                timestamp=trail.created_at or utc_now(),
                                action=ref.get("action", "unknown"),
                                actor_id=ref.get("actor_id"),
                                status=ref.get("status"),
                                hash_chain=ref.get("hash"),
                            )
                        )
            except Exception:
                logger.debug("sql.statistics_audit_trail_failed", exc_info=True)

        try:
            from baldur.factory import ProviderRegistry

            audit_adapter = ProviderRegistry.get_audit_adapter()
            if hasattr(audit_adapter, "get_entries_by_entity"):
                audit_entries = audit_adapter.get_entries_by_entity(
                    entity_id=entity_id, entity_type=entity_type
                )
                for ae in audit_entries:
                    trail.entries.append(
                        AuditTrailEntry(
                            timestamp=ae.timestamp,
                            action=(
                                ae.action.value
                                if hasattr(ae.action, "value")
                                else str(ae.action)
                            ),
                            actor_id=ae.actor_id,
                            status=(ae.new_value if hasattr(ae, "new_value") else None),
                            details=(ae.details if hasattr(ae, "details") else None),
                            hash_chain=(ae.hash if hasattr(ae, "hash") else None),
                            previous_hash=(
                                ae.previous_hash
                                if hasattr(ae, "previous_hash")
                                else None
                            ),
                        )
                    )
        except Exception:
            logger.debug("sql.statistics_audit_adapter_lookup_skipped", exc_info=True)
        return trail

    def link_audit_entry(
        self,
        entity_id: str,
        entity_type: str,
        action: str,
        actor_id: str | None = None,
        status: str | None = None,
        details: str | None = None,
        audit_record_hash: str | None = None,
    ) -> bool:
        if entity_type != "dlq_entry":
            return False
        try:
            row = self._fetch_one(
                f"SELECT data FROM {DLQ_TABLE} WHERE id = %s",
                (entity_id,),
            )
            if not row:
                return False
            data = self._loads_json(row[0]) or {}
            metadata = data.get("metadata", {})
            audit_refs = metadata.get("audit_references", [])
            audit_refs.append(
                {
                    "action": action,
                    "actor_id": actor_id,
                    "status": status,
                    "hash": audit_record_hash,
                }
            )
            metadata["audit_references"] = audit_refs
            data["metadata"] = metadata
            self._execute(
                f"UPDATE {DLQ_TABLE} SET data = %s, updated_at = %s WHERE id = %s",
                (self._dumps_json(data), self._dt_to_db(utc_now()), entity_id),
            )
            return True
        except Exception:
            logger.warning("sql.statistics_link_audit_failed", exc_info=True)
            return False

    # ----- Async Persistence Config -----------------------------------------

    def should_persist_async(self) -> bool:
        return False

    def get_async_persist_task_name(self) -> str | None:
        return None
