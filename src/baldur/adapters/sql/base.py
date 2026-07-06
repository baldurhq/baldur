"""
Generic DB-API 2.0 repository base and transaction primitives.

Three responsibilities:

1. ``GenericSQLRepository`` — thin helpers over a user-supplied
   ``get_connection`` callable (BYO pool: PgBouncer, dj-db-conn-pool,
   SQLAlchemy pool). Baldur does not own its own pool.

2. ``sql_transaction`` — cross-repo transaction context manager that
   suspends repo-scoped auto-commit for the duration of the with-block
   on that specific connection.

3. ``SchemaVersionManager`` — maintains ``baldur_schema_version`` and
   bootstraps CREATE TABLE IF NOT EXISTS for repos that opt into
   managed-schema mode (``BALDUR_SQL_SCHEMA_MANAGED=1``, default).

Key design choices: a BYO connection callable (no built-in pool),
repo-scoped auto-commit with explicit transactions, and CREATE TABLE
IF NOT EXISTS with internal version bookkeeping.
"""
# Design refs: 429 C15 (BYO connection callable, no built-in pool),
# C16 (repo-scoped auto-commit + explicit transaction), C12 (CREATE
# TABLE IF NOT EXISTS with internal version bookkeeping).

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import structlog

from baldur.settings.sql import SQLDialect, get_sql_settings
from baldur.utils.time import utc_now

__all__ = [
    "GenericSQLRepository",
    "SchemaVersionManager",
    "dialect_bigserial",
    "dialect_json_type",
    "dialect_timestamp_type",
    "dialect_upsert_clause",
    "sql_transaction",
]

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Transaction suspension — per-thread set of connection ids under an
# explicit ``sql_transaction`` scope. Scoped on id(conn) so BYO pools can
# still participate when callers hand the same borrowed conn to multiple
# repos. Thread-local because DB-API connections are not thread-safe.
# ---------------------------------------------------------------------------
_tls = threading.local()


def _suspended_conns() -> set[int]:
    s = getattr(_tls, "suspended", None)
    if s is None:
        s = set()
        _tls.suspended = s
    return s


@contextmanager
def sql_transaction(conn: Any) -> Any:
    """Suspend repo-scoped auto-commit for the duration of the block.

    Usage::

        with sql_transaction(conn):
            dlq_repo.save(...)
            cb_repo.update(...)
        # single commit (or rollback on exception) applies to both.

    All repositories whose ``get_connection`` returns ``conn`` during
    the block skip their per-call commit. The context manager itself
    issues the final commit, or rollback on exception.
    """
    conn_id = id(conn)
    suspended = _suspended_conns()
    reentrant = conn_id in suspended
    if not reentrant:
        suspended.add(conn_id)
    try:
        yield conn
    except Exception:
        if not reentrant:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                logger.warning("sql.transaction_rollback_failed", exc_info=True)
        raise
    else:
        if not reentrant:
            conn.commit()
    finally:
        if not reentrant:
            suspended.discard(conn_id)


def _dialect_placeholder(dialect: SQLDialect) -> str:
    return "?" if dialect == SQLDialect.SQLITE else "%s"


def dialect_json_type(dialect: SQLDialect) -> str:
    """Column type for JSON-shaped payloads, per dialect."""
    if dialect == SQLDialect.POSTGRESQL:
        return "JSONB"
    if dialect == SQLDialect.MYSQL:
        return "JSON"
    return "TEXT"


def dialect_timestamp_type(dialect: SQLDialect) -> str:
    """Column type for UTC timestamps, per dialect."""
    if dialect == SQLDialect.POSTGRESQL:
        return "TIMESTAMPTZ"
    if dialect == SQLDialect.MYSQL:
        return "DATETIME(6)"
    return "TEXT"


def dialect_bigserial(dialect: SQLDialect) -> str:
    """Auto-increment PRIMARY KEY fragment, per dialect."""
    if dialect == SQLDialect.POSTGRESQL:
        return "BIGSERIAL PRIMARY KEY"
    if dialect == SQLDialect.MYSQL:
        return "BIGINT AUTO_INCREMENT PRIMARY KEY"
    return "INTEGER PRIMARY KEY AUTOINCREMENT"


def dialect_upsert_clause(
    dialect: SQLDialect,
    conflict_cols: list[str],
    update_cols: list[str],
) -> str:
    """Return the dialect-specific UPSERT tail for an ``INSERT ... VALUES``.

    Example result (postgres)::

        ON CONFLICT (id) DO UPDATE SET count = EXCLUDED.count, status = EXCLUDED.status

    Callers compose the full statement as
    ``f"INSERT INTO t (...) VALUES (...) {dialect_upsert_clause(...)}"``.
    """
    conflict = ", ".join(conflict_cols)
    if dialect == SQLDialect.POSTGRESQL:
        assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        return f"ON CONFLICT ({conflict}) DO UPDATE SET {assignments}"
    if dialect == SQLDialect.MYSQL:
        assignments = ", ".join(f"{c} = VALUES({c})" for c in update_cols)
        return f"ON DUPLICATE KEY UPDATE {assignments}"
    # sqlite
    assignments = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
    return f"ON CONFLICT({conflict}) DO UPDATE SET {assignments}"


class GenericSQLRepository:
    """DB-API 2.0 repository helpers.

    Subclasses inherit this class *plus* a domain-specific ABC
    (``FailedOperationRepository`` etc.). The base exposes helpers only
    — no ABC method has a default implementation here, so there is no
    MRO ambiguity.

    Connection ownership: ``get_connection`` is a user-supplied
    callable. Baldur does not own a pool. Each helper borrows a
    connection via the callable and relies on the callable's
    ``close()`` / return-to-pool semantics. Common implementations:

    * ``get_connection = lambda: psycopg2.connect(DSN)`` — direct.
    * ``get_connection = engine.raw_connection`` — SQLAlchemy pool.
    * ``get_connection = lambda: pgbouncer_pool.getconn()`` — external
      pooler.
    """

    def __init__(
        self,
        get_connection: Callable[[], Any],
        *,
        dialect: SQLDialect | None = None,
        autocommit_delegated: bool | None = None,
        schema: tuple[str, int, Callable[[SQLDialect], list[str]]] | None = None,
    ) -> None:
        self._get_connection = get_connection
        settings = get_sql_settings()
        self._dialect = dialect or settings.resolved_dialect()
        # When autocommit_delegated=True, Baldur skips its own commit/rollback
        # (escape hatch for PgBouncer transaction-pooling mode).
        self._autocommit_delegated = (
            settings.autocommit
            if autocommit_delegated is None
            else autocommit_delegated
        )
        self._placeholder = _dialect_placeholder(self._dialect)
        # Lazy schema bootstrap — DDL runs on first connection borrow, not in
        # __init__. Keeps ProviderRegistry repo construction cheap and keeps
        # a transient DB outage from cascading into init() failures.
        self._schema: tuple[str, int, Callable[[SQLDialect], list[str]]] | None = schema
        self._schema_ready: bool = schema is None

    # ----- Connection + commit lifecycle ------------------------------------

    def _borrow_connection(self) -> Any:
        if not self._schema_ready:
            self._ensure_schema_ready()
        conn = self._get_connection()
        if conn is None:
            raise RuntimeError(
                "baldur.sql: get_connection returned None — check DSN / driver"
            )
        return conn

    def _ensure_schema_ready(self) -> None:
        """Apply the subclass-declared schema once, on first DB touch.

        Safe under concurrency: ``SchemaVersionManager._applied`` is a
        process-wide set guarded by a lock, so a second thread racing past
        ``_schema_ready`` will find the work already done and no-op. On
        failure the flag stays ``False`` so the next call retries.

        When invoked inside a user's ``sql_transaction`` scope the bootstrap
        cannot commit on its own — the outer scope owns the transaction —
        so we defer marking ``_schema_ready`` until the schema is durably
        persisted. ``SchemaVersionManager._applied`` is the source of truth:
        it is only updated after a real commit, so we check membership to
        decide whether to short-circuit on subsequent calls.
        """
        if self._schema_ready or self._schema is None:
            return
        repo_name, version, ddl_factory = self._schema
        self._ensure_schema(repo_name, version, ddl_factory(self._dialect))
        key = (id(self._get_connection), repo_name, version)
        if key in SchemaVersionManager._applied:
            self._schema_ready = True

    def _should_commit(self, conn: Any) -> bool:
        if self._autocommit_delegated:
            return False
        return id(conn) not in _suspended_conns()

    @contextmanager
    def _cursor(self) -> Any:
        """Borrow a connection and open a cursor, committing on success.

        Rollback and re-raise on exception. Skips commit when the
        connection is in a ``sql_transaction`` scope or when
        ``autocommit_delegated=True``.
        """
        conn = self._borrow_connection()
        cursor = conn.cursor()
        try:
            yield cursor
        except Exception:
            if self._should_commit(conn):
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    logger.warning("sql.cursor_rollback_failed", exc_info=True)
            raise
        else:
            if self._should_commit(conn):
                conn.commit()
        finally:
            try:
                cursor.close()
            except Exception:  # noqa: BLE001
                pass

    # ----- Low-level query helpers ------------------------------------------

    def _prepare(self, sql: str) -> str:
        """Translate the canonical ``%s`` placeholders to the dialect's."""
        if self._placeholder == "%s":
            return sql
        return sql.replace("%s", self._placeholder)

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self._cursor() as cur:
            cur.execute(self._prepare(sql), tuple(params))

    def _executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> None:
        with self._cursor() as cur:
            cur.executemany(self._prepare(sql), [tuple(p) for p in seq])

    def _fetch_one(self, sql: str, params: Iterable[Any] = ()) -> tuple | None:
        with self._cursor() as cur:
            cur.execute(self._prepare(sql), tuple(params))
            return cur.fetchone()

    def _fetch_all(self, sql: str, params: Iterable[Any] = ()) -> list[tuple]:
        with self._cursor() as cur:
            cur.execute(self._prepare(sql), tuple(params))
            return list(cur.fetchall())

    def _execute_returning_id(self, sql: str, params: Iterable[Any] = ()) -> int | None:
        """Insert and return the generated primary key, cross-dialect."""
        conn = self._borrow_connection()
        cursor = conn.cursor()
        try:
            stmt = self._prepare(sql)
            if (
                self._dialect == SQLDialect.POSTGRESQL
                and " RETURNING " not in stmt.upper()
            ):
                stmt = stmt.rstrip().rstrip(";") + " RETURNING id"
            cursor.execute(stmt, tuple(params))
            new_id: int | None
            if self._dialect == SQLDialect.POSTGRESQL:
                row = cursor.fetchone()
                new_id = int(row[0]) if row else None
            else:
                new_id = int(cursor.lastrowid) if cursor.lastrowid else None
            if self._should_commit(conn):
                conn.commit()
            return new_id
        except Exception:
            if self._should_commit(conn):
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    logger.warning("sql.insert_rollback_failed", exc_info=True)
            raise
        finally:
            try:
                cursor.close()
            except Exception:  # noqa: BLE001
                pass

    # ----- Value serialization ---------------------------------------------

    @staticmethod
    def _dumps_json(value: Any) -> str:
        if value is None:
            return "null"
        return json.dumps(value, default=GenericSQLRepository._json_default)

    @staticmethod
    def _loads_json(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value  # psycopg2 with JSONB auto-decodes
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _json_default(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError(
            f"Object of type {type(value).__name__} is not JSON serializable"
        )

    @staticmethod
    def _dt_to_db(value: datetime | None) -> Any:
        # DB-API 2.0 drivers (psycopg2, mysql-connector) accept datetime
        # directly. sqlite3 requires an ISO string for TIMESTAMPTZ-shaped
        # columns — we represent timestamps as TEXT in sqlite.
        return value

    @staticmethod
    def _dt_from_db(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        if isinstance(value, str):
            # sqlite path. Accept both naive ISO strings and full RFC 3339.
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None
        return None

    # ----- DDL helpers ------------------------------------------------------

    def _ensure_schema(
        self,
        repo_name: str,
        version: int,
        ddl_statements: list[str],
    ) -> None:
        """Ensure table DDL is applied, once per repo, if schema is managed."""
        settings = get_sql_settings()
        if not settings.schema_managed:
            return
        manager = SchemaVersionManager(self._get_connection, dialect=self._dialect)
        manager.ensure(repo_name, version, ddl_statements)

    # Public accessor used by discover_* callbacks to surface the dialect.
    @property
    def dialect(self) -> SQLDialect:
        return self._dialect


class SchemaVersionManager:
    """Owns the ``baldur_schema_version`` bookkeeping table.

    Repos call ``ensure(repo_name, version, ddl_statements)`` during
    first use. DDL runs exactly once per (repo_name, version) pair per
    database — subsequent calls are no-ops.
    """

    _TABLE = "baldur_schema_version"
    _BOOTSTRAP_REPO = "__meta__"
    # Cache which (dsn_id, repo_name, version) pairs have already been
    # applied in this process — keeps repo construction cheap.
    _applied: set[tuple[int, str, int]] = set()
    _applied_lock = threading.Lock()

    def __init__(
        self,
        get_connection: Callable[[], Any],
        *,
        dialect: SQLDialect,
    ) -> None:
        self._get_connection = get_connection
        self._dialect = dialect
        self._placeholder = _dialect_placeholder(dialect)

    def _prepare(self, sql: str) -> str:
        if self._placeholder == "%s":
            return sql
        return sql.replace("%s", self._placeholder)

    def _bootstrap(self, conn: Any) -> None:
        ts_type = dialect_timestamp_type(self._dialect)
        ddl = (
            f"CREATE TABLE IF NOT EXISTS {self._TABLE} ("
            f"repo_name VARCHAR(128) PRIMARY KEY,"
            f"version INTEGER NOT NULL,"
            f"updated_at {ts_type} NOT NULL)"
        )
        cur = conn.cursor()
        try:
            cur.execute(ddl)
        finally:
            cur.close()

    def _read_version(self, conn: Any, repo_name: str) -> int:
        cur = conn.cursor()
        try:
            cur.execute(
                self._prepare(
                    f"SELECT version FROM {self._TABLE} WHERE repo_name = %s"
                ),
                (repo_name,),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0
        finally:
            cur.close()

    def _write_version(self, conn: Any, repo_name: str, version: int) -> None:
        now = utc_now()
        cur = conn.cursor()
        try:
            if self._dialect == SQLDialect.POSTGRESQL:
                cur.execute(
                    self._prepare(
                        f"INSERT INTO {self._TABLE} (repo_name, version, updated_at) "
                        f"VALUES (%s, %s, %s) "
                        f"ON CONFLICT (repo_name) DO UPDATE SET "
                        f"version = EXCLUDED.version, updated_at = EXCLUDED.updated_at"
                    ),
                    (repo_name, version, now),
                )
            elif self._dialect == SQLDialect.MYSQL:
                cur.execute(
                    self._prepare(
                        f"INSERT INTO {self._TABLE} (repo_name, version, updated_at) "
                        f"VALUES (%s, %s, %s) "
                        f"ON DUPLICATE KEY UPDATE "
                        f"version = VALUES(version), updated_at = VALUES(updated_at)"
                    ),
                    (repo_name, version, now),
                )
            else:  # sqlite
                cur.execute(
                    self._prepare(
                        f"INSERT INTO {self._TABLE} (repo_name, version, updated_at) "
                        f"VALUES (%s, %s, %s) "
                        f"ON CONFLICT(repo_name) DO UPDATE SET "
                        f"version = excluded.version, updated_at = excluded.updated_at"
                    ),
                    (repo_name, version, now.isoformat()),
                )
        finally:
            cur.close()

    def ensure(self, repo_name: str, version: int, ddl_statements: list[str]) -> None:
        key = (id(self._get_connection), repo_name, version)
        # Serialize the whole critical section. The bookkeeping read + DDL
        # execution + cache update must all happen atomically per process.
        # Concurrent callers (whether multi-thread or repeated calls on a
        # shared sqlite connection) wait on this lock and find the cache
        # populated when their turn comes — avoiding both duplicate DDL
        # work and "two threads on the same DB-API connection" cursor
        # races.
        with self._applied_lock:
            if key in self._applied:
                return

            conn = self._get_connection()
            if conn is None:
                raise RuntimeError(
                    "baldur.sql: get_connection returned None during schema bootstrap"
                )
            # Bootstrap may run inside a user's ``sql_transaction`` scope
            # (lazy init triggered by the first repo write). In that case
            # the user owns commit/rollback — issuing our own would
            # prematurely close the outer transaction and silently commit
            # half-finished work. Skip both commit and rollback when
            # suspended; defer marking ``_applied`` until the next
            # non-suspended call can commit durably.
            suspended = id(conn) in _suspended_conns()
            try:
                self._bootstrap(conn)
                current = self._read_version(conn, repo_name)
                if current >= version:
                    if not suspended:
                        conn.commit()
                        self._applied.add(key)
                    return
                for stmt in ddl_statements:
                    cur = conn.cursor()
                    try:
                        cur.execute(stmt)
                    finally:
                        cur.close()
                self._write_version(conn, repo_name, version)
                if not suspended:
                    conn.commit()
                    self._applied.add(key)
                logger.info(
                    "sql.schema_applied",
                    repo=repo_name,
                    version=version,
                    dialect=self._dialect.value,
                )
            except Exception:
                if not suspended:
                    try:
                        conn.rollback()
                    except Exception:  # noqa: BLE001
                        logger.warning("sql.schema_rollback_failed", exc_info=True)
                raise

    @classmethod
    def _reset_applied_cache(cls) -> None:
        """Clear the process-wide applied cache (testing only)."""
        with cls._applied_lock:
            cls._applied.clear()
