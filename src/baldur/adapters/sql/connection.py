"""
Default DB-API 2.0 connection factory.

This factory opens a fresh DB-API connection for every operation and does
not pool them. That is the connection model of every SQL store Baldur wires
from ``BALDUR_SQL_DSN``, and it suits the incident archives' write rate.
Baldur does not ship a connection pool: where the connection count
matters — a SQL dead-letter queue under a failure storm, a busy database —
register the provider under the name ``sql`` before ``baldur.init()`` with a
pooled callable (SQLAlchemy ``engine.raw_connection``, ``dj-db-conn-pool``,
PgBouncer ``getconn``, etc.). Discovery never overwrites an existing
registration. Examples::

    from sqlalchemy import create_engine
    engine = create_engine(DSN, pool_size=10, pool_pre_ping=True)
    repo = SQLFailedOperationRepository(engine.raw_connection)

    # Or with PgBouncer transaction pooling:
    pool = pgbouncer.ThreadedPool(...)
    repo = SQLFailedOperationRepository(pool.getconn,
                                        autocommit_delegated=True)

The first call to ``build_connection_factory()`` emits a one-shot
``sql.default_factory_no_pool`` INFO line saying so.

A PostgreSQL DSN is parsed when the factory is built, without dialing, so a
DSN libpq cannot parse fails at construction rather than at the first
write. The parse error is re-raised without the DSN: libpq echoes the whole
string, password included.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote, urlparse

import structlog

from baldur.core.process_utils import fork_safe_lock
from baldur.settings.sql import SQLDialect, infer_dialect, resolve_dsn

__all__ = ["build_connection_factory"]

logger = structlog.get_logger()

# One-shot warning gate. Module-level + lock so concurrent callers in
# multi-threaded startup paths emit exactly one record.
_warned_lock = fork_safe_lock()
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

    # ``psycopg2.connect`` runs this same parse before it dials, so no DSN
    # that connects is refused here; the parse itself does no I/O.
    try:
        psycopg2.extensions.parse_dsn(dsn)
        parsed = True
    except Exception:
        parsed = False
    if not parsed:
        # Security: libpq's parse error quotes the whole DSN, password
        # included. Raising here, outside the ``except`` block, leaves the
        # original off both ``__cause__`` and ``__context__``, so no
        # traceback or log line that renders this exception can reach it.
        raise ValueError(
            "baldur.sql: BALDUR_SQL_DSN is not a valid PostgreSQL connection "
            "string (the value is not shown because it can carry a password)"
        )

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

    The returned callable opens a *new* DB-API connection on every call,
    with no pool. To pool, register a pooled provider under ``sql`` before
    ``baldur.init()`` (see module docstring).

    Raises:
        ImportError: The driver for the DSN's dialect is not installed.
        ValueError: A PostgreSQL DSN that libpq cannot parse. The message
            names ``BALDUR_SQL_DSN`` and carries no part of the DSN.
    """
    global _warned
    if not _warned:
        with _warned_lock:
            if not _warned:
                logger.info(
                    "sql.default_factory_no_pool",
                    guidance=(
                        "SQL stores wired from BALDUR_SQL_DSN open a new "
                        "DB-API connection per operation, with no pool. To "
                        "pool, register a pooled provider under 'sql' before "
                        "baldur.init() (SQLAlchemy engine.raw_connection, "
                        "dj-db-conn-pool, PgBouncer getconn)."
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
