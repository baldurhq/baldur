"""
Unit tests for baldur.adapters.sql.connection.

Coverage:
- DSN scheme → driver selection (sqlite works without extras;
  postgres/mysql raise a helpful ImportError when the driver is absent).
- build_connection_factory default path reads resolve_dsn() (singleton).
- One-shot dev/test factory warning (PR2 review fix #5).
"""

from __future__ import annotations

import sqlite3
import sys
from unittest.mock import patch

import pytest

from baldur.adapters.sql import connection as connection_mod
from baldur.adapters.sql.connection import (
    _reset_default_factory_warning,
    build_connection_factory,
)
from baldur.settings.sql import reset_sql_settings


@pytest.fixture(autouse=True)
def _reset_settings():
    reset_sql_settings()
    _reset_default_factory_warning()
    yield
    reset_sql_settings()


class TestBuildConnectionFactoryBehavior:
    """Driver dispatch based on DSN scheme."""

    def test_sqlite_dsn_returns_working_sqlite_factory(self):
        factory = build_connection_factory("sqlite:///:memory:")
        conn = factory()
        assert isinstance(conn, sqlite3.Connection)
        # Verify the factory opens usable connections.
        cur = conn.cursor()
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)
        conn.close()

    def test_sqlite_empty_path_falls_back_to_memory(self):
        factory = build_connection_factory("sqlite:///")
        conn = factory()
        try:
            assert isinstance(conn, sqlite3.Connection)
        finally:
            conn.close()

    def test_postgres_dsn_raises_importerror_when_driver_missing(self, monkeypatch):
        """psycopg2 absent → helpful install message."""
        monkeypatch.setitem(sys.modules, "psycopg2", None)
        with pytest.raises(ImportError, match="psycopg2"):
            build_connection_factory("postgresql://user@host/db")

    def test_mysql_dsn_raises_importerror_when_driver_missing(self, monkeypatch):
        """mysql-connector absent → helpful install message."""
        monkeypatch.setitem(sys.modules, "mysql.connector", None)
        monkeypatch.setitem(sys.modules, "mysql", None)
        with pytest.raises(ImportError, match="mysql-connector-python"):
            build_connection_factory("mysql://user:pw@host/db")

    def test_default_dsn_uses_singleton_resolve(self):
        """build_connection_factory() without args reads resolve_dsn()."""
        with patch.object(
            connection_mod, "resolve_dsn", return_value="sqlite:///:memory:"
        ) as spy:
            factory = build_connection_factory()
            conn = factory()
        spy.assert_called_once()
        assert isinstance(conn, sqlite3.Connection)
        conn.close()


# ---------------------------------------------------------------------------
# PR2 review fix #5 — one-shot dev/test factory warning
# ---------------------------------------------------------------------------


_WARNING_EVENT = "sql.default_factory_no_pool"


class TestDefaultFactoryWarningBehavior:
    """``build_connection_factory`` emits a one-shot WARNING."""

    def _spy_logger(self, monkeypatch):
        """Replace the module logger with a recorder; return the call list."""
        calls: list[tuple[str, dict]] = []

        class _Recorder:
            def warning(self, event, **kwargs):
                calls.append((event, kwargs))

            # Ignore any other levels emitted incidentally.
            def __getattr__(self, _name):
                return lambda *a, **kw: None

        monkeypatch.setattr(connection_mod, "logger", _Recorder())
        return calls

    def test_first_call_emits_warning(self, monkeypatch):
        """First invocation → warning event is recorded once."""
        calls = self._spy_logger(monkeypatch)
        build_connection_factory("sqlite:///:memory:")
        events = [e for e, _ in calls]
        assert events.count(_WARNING_EVENT) == 1

    def test_second_call_does_not_emit_warning(self, monkeypatch):
        """Second invocation does not emit again — gate stays armed."""
        calls = self._spy_logger(monkeypatch)
        build_connection_factory("sqlite:///:memory:")
        build_connection_factory("sqlite:///:memory:")
        events = [e for e, _ in calls]
        assert events.count(_WARNING_EVENT) == 1

    def test_reset_helper_re_arms_warning(self, monkeypatch):
        """``_reset_default_factory_warning`` lets the next call emit again."""
        calls = self._spy_logger(monkeypatch)
        build_connection_factory("sqlite:///:memory:")
        _reset_default_factory_warning()
        build_connection_factory("sqlite:///:memory:")
        events = [e for e, _ in calls]
        assert events.count(_WARNING_EVENT) == 2

    def test_warning_payload_includes_guidance(self, monkeypatch):
        """The emitted record carries operator-facing guidance text."""
        calls = self._spy_logger(monkeypatch)
        build_connection_factory("sqlite:///:memory:")
        # Find the warning entry.
        warning = next(kw for ev, kw in calls if ev == _WARNING_EVENT)
        assert "guidance" in warning
        assert "pool" in warning["guidance"].lower()
