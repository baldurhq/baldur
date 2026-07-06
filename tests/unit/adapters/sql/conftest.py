"""
Shared fixtures for SQL adapter unit tests.

Uses stdlib sqlite3 in-memory databases so tests remain hermetic and
run without any external infra (no Docker, no drivers installed).
A single shared connection is handed to all repos in a test; sqlite
``:memory:`` databases are per-connection so sharing the handle is
required for cross-repo scenarios.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from baldur.adapters.sql.base import SchemaVersionManager
from baldur.settings.sql import SQLDialect, reset_sql_settings


@pytest.fixture(autouse=True)
def _reset_sql_singletons(monkeypatch):
    """Keep singletons + schema-applied cache pristine between tests.

    Pin the settings DSN to sqlite so default dialect inference resolves
    to SQLDialect.SQLITE for the whole test — matches what runtime
    callers see when ``BALDUR_SQL_DSN=sqlite://`` is configured.
    """
    monkeypatch.setenv("BALDUR_SQL_DSN", "sqlite:///:memory:")
    reset_sql_settings()
    SchemaVersionManager._reset_applied_cache()
    yield
    reset_sql_settings()
    SchemaVersionManager._reset_applied_cache()


@pytest.fixture
def sqlite_conn() -> Iterator[sqlite3.Connection]:
    """Shared in-memory sqlite connection.

    `:memory:` is per-connection in sqlite3, so the same handle must back
    every repo in a test to share schema + rows.
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def get_sqlite_conn(sqlite_conn) -> Callable[[], Any]:
    """``get_connection`` callable returning the shared sqlite handle."""
    return lambda: sqlite_conn


@pytest.fixture
def sqlite_dialect() -> SQLDialect:
    return SQLDialect.SQLITE
