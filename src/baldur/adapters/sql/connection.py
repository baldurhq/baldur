"""
Default DB-API 2.0 connection factory.

⚠️  **Dev / test convenience only — not for production.**
This factory opens a fresh DB-API connection on every call and never
closes or pools them. 429 C15 mandates that Baldur does *not* ship a
connection pool: production deployments MUST replace the callable with
a pooled equivalent (SQLAlchemy ``engine.raw_connection``,
``dj-db-conn-pool``, PgBouncer ``getconn``, etc.). Production examples::

    from sqlalchemy import create_engine
    engine = create_engine(DSN, pool_size=10, pool_pre_ping=True)
    repo = SQLFailedOperationRepository(engine.raw_connection)

    # Or with PgBouncer transaction pooling:
    pool = pgbouncer.ThreadedPool(...)
    repo = SQLFailedOperationRepository(pool.getconn,
                                        autocommit_delegated=True)

The first call to ``build_connection_factory()`` emits a one-shot
``baldur.sql.default_factory_no_pool`` warning so an accidental
production deployment surfaces in logs.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote, urlparse

import structlog

from baldur.settings.sql import SQLDialect, infer_dialect, resolve_dsn

__all__ = ["build_connection_factory"]

logger = structlog.get_logger()

# One-shot warning gate. Module-level + lock so concurrent callers in
# multi-threaded startup paths emit exactly one record.
_warned_lock = threading.Lock()
_warned: bool = False


def _sqlite_factory(dsn: str) -> Callable[[], Any]:
    path = dsn.replace("sqlite:///", "", 1) or ":memory:"

    def _connect() -> Any:
        conn = sqlite3.connect(path, check_same_thread=False)
        # Baldur's base layer serializes access — foreign keys + row factory
        # are harmless defaults that make diagnostics nicer.
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    return _connect


def _postgres_factory(dsn: str) -> Callable[[], Any]:
    try:
        import psycopg2
    except ImportError as exc:
        raise ImportError(
            "baldur.sql: psycopg2 is required for postgresql DSNs "
            "(pip install psycopg2-binary)"
        ) from exc

    def _connect() -> Any:
        return psycopg2.connect(dsn)

    return _connect


def _mysql_factory(dsn: str) -> Callable[[], Any]:
    try:
        import mysql.connector  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "baldur.sql: mysql-connector-python is required for mysql DSNs "
            "(pip install mysql-connector-python)"
        ) from exc

    parsed = urlparse(dsn)
    kwargs: dict[str, Any] = {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
    }
    if parsed.password:
        kwargs["password"] = unquote(parsed.password)
    database = (parsed.path or "").lstrip("/")
    if database:
        kwargs["database"] = database

    def _connect() -> Any:
        return mysql.connector.connect(**kwargs)

    return _connect


def build_connection_factory(dsn: str | None = None) -> Callable[[], Any]:
    """Return a ``get_connection`` callable suitable for Baldur SQL repos.

    When ``dsn`` is None, ``resolve_dsn()`` is used — the documented
    precedence chain (``BALDUR_SQL_DSN`` > ``BALDUR_POSTGRES_*`` fallback).

    The returned callable opens a *new* DB-API connection on every call
    and never closes them. Suitable for dev / tests only; wrap or replace
    with a pooled callable for production (see module docstring).
    """
    global _warned
    if not _warned:
        with _warned_lock:
            if not _warned:
                logger.warning(
                    "sql.default_factory_no_pool",
                    guidance=(
                        "build_connection_factory is dev/test only — opens a "
                        "new DB-API connection per call and never closes them. "
                        "Wrap with a pool (SQLAlchemy engine.raw_connection, "
                        "dj-db-conn-pool, PgBouncer getconn) for production."
                    ),
                )
                _warned = True

    dsn = dsn or resolve_dsn()
    dialect = infer_dialect(dsn)
    if dialect == SQLDialect.SQLITE:
        return _sqlite_factory(dsn)
    if dialect == SQLDialect.MYSQL:
        return _mysql_factory(dsn)
    return _postgres_factory(dsn)


def _reset_default_factory_warning() -> None:
    """Test helper — re-arm the one-shot warning."""
    global _warned
    with _warned_lock:
        _warned = False
