"""
Unit tests for baldur.adapters.sql.connection.

Coverage:
- DSN scheme → driver selection (sqlite works without extras;
  postgres/mysql raise a helpful ImportError when the driver is absent).
- build_connection_factory default path reads resolve_dsn() (singleton).
- One-shot no-pool line (PR2 review fix #5; INFO since 801 G13).
- A PostgreSQL DSN parses at construction without dialing, and a parse
  failure carries no part of the DSN (801 D6).
"""

from __future__ import annotations

import sqlite3
import sys
from types import SimpleNamespace
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
# 801 D6 (external review E4) — a PostgreSQL DSN parses at construction
# ---------------------------------------------------------------------------

_DSN_PASSWORD = "s3cretpw"


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """Every exception reachable through ``__cause__`` and ``__context__``."""
    seen: list[BaseException] = []
    pending: list[BaseException | None] = [exc]
    while pending:
        link = pending.pop()
        if link is None or any(link is s for s in seen):
            continue
        seen.append(link)
        pending.extend((link.__cause__, link.__context__))
    return seen


class _Psycopg2Stub:
    """A ``psycopg2`` whose parse accepts or rejects, and whose dial is recorded.

    A rejection quotes the whole DSN, as libpq's parse error does.
    """

    def __init__(self, *, parses: bool) -> None:
        self.connect_calls: list[str] = []
        self.parsed: list[str] = []
        self._parses = parses
        self.extensions = SimpleNamespace(parse_dsn=self._parse_dsn)

    def _parse_dsn(self, dsn: str) -> dict:
        self.parsed.append(dsn)
        if not self._parses:
            raise Exception(f'missing "=" after "{dsn}" in connection info string')
        return {}

    def connect(self, dsn: str) -> object:
        self.connect_calls.append(dsn)
        return object()


class TestPostgresDsnParseBehavior:
    """``_postgres_factory`` parses the DSN without dialing and redacts a failure.

    ``psycopg2.connect`` runs the same parse before it dials, so the check
    refuses no DSN that connects today; libpq echoes the whole DSN in its
    parse error, so the re-raise carries no part of it on any link.
    """

    def test_a_parsable_dsn_builds_a_factory_without_dialing(self, monkeypatch):
        # Given — a DSN whose host cannot resolve
        stub = _Psycopg2Stub(parses=True)
        monkeypatch.setitem(sys.modules, "psycopg2", stub)
        dsn = f"postgresql://u:{_DSN_PASSWORD}@nonexistent.invalid/db"

        # When
        factory = build_connection_factory(dsn)

        # Then — parsed once, dialed only when the factory is called
        assert stub.parsed == [dsn]
        assert stub.connect_calls == []
        factory()
        assert stub.connect_calls == [dsn]

    @pytest.mark.parametrize(
        "dsn",
        [
            f"postgersql://u:{_DSN_PASSWORD}@h/db",
            f"postgresql://user:{_DSN_PASSWORD}@/db?invalid_opt=1",
        ],
        ids=["scheme_typo", "unknown_query_parameter"],
    )
    def test_an_unparsable_dsn_raises_value_error_naming_the_variable_only(
        self, monkeypatch, dsn
    ):
        stub = _Psycopg2Stub(parses=False)
        monkeypatch.setitem(sys.modules, "psycopg2", stub)

        with pytest.raises(ValueError, match="BALDUR_SQL_DSN") as excinfo:
            build_connection_factory(dsn)

        assert stub.parsed == [dsn]
        assert stub.connect_calls == []
        for link in _exception_chain(excinfo.value):
            assert _DSN_PASSWORD not in str(link)

    @pytest.mark.parametrize(
        ("dsn", "parses"),
        [
            ("postgresql://u:p@nonexistent.invalid/db", True),
            (f"postgersql://u:{_DSN_PASSWORD}@h/db", False),
            (f"postgresql://user:{_DSN_PASSWORD}@/db?invalid_opt=1", False),
        ],
        ids=["unresolvable_host", "scheme_typo", "unknown_query_parameter"],
    )
    def test_the_installed_libpq_agrees_with_the_stubbed_verdicts(self, dsn, parses):
        """The stub's premises hold for the real driver: libpq parses an
        unresolvable host without I/O and rejects both malformed forms."""
        pytest.importorskip("psycopg2")

        if parses:
            build_connection_factory(dsn)
            return
        with pytest.raises(ValueError) as excinfo:
            build_connection_factory(dsn)
        for link in _exception_chain(excinfo.value):
            assert _DSN_PASSWORD not in str(link)


# ---------------------------------------------------------------------------
# PR2 review fix #5 — one-shot no-pool line
# ---------------------------------------------------------------------------


_NO_POOL_EVENT = "sql.default_factory_no_pool"


class TestDefaultFactoryWarningBehavior:
    """``build_connection_factory`` emits a one-shot INFO line (801 G13).

    It fires once per process on every DSN-wired boot, which is the
    documented PRO production path, so it states the connection model
    instead of warning about it.
    """

    def _spy_logger(self, monkeypatch):
        """Replace the module logger with a recorder; return the call list."""
        calls: list[tuple[str, dict]] = []

        class _Recorder:
            def info(self, event, **kwargs):
                calls.append((event, kwargs))

            # Ignore any other levels emitted incidentally.
            def __getattr__(self, _name):
                return lambda *a, **kw: None

        monkeypatch.setattr(connection_mod, "logger", _Recorder())
        return calls

    def test_first_call_emits_the_line(self, monkeypatch):
        """First invocation → the event is recorded once."""
        calls = self._spy_logger(monkeypatch)
        build_connection_factory("sqlite:///:memory:")
        events = [e for e, _ in calls]
        assert events.count(_NO_POOL_EVENT) == 1

    def test_second_call_does_not_emit_again(self, monkeypatch):
        """Second invocation does not emit again — gate stays armed."""
        calls = self._spy_logger(monkeypatch)
        build_connection_factory("sqlite:///:memory:")
        build_connection_factory("sqlite:///:memory:")
        events = [e for e, _ in calls]
        assert events.count(_NO_POOL_EVENT) == 1

    def test_reset_helper_re_arms_the_line(self, monkeypatch):
        """``_reset_default_factory_warning`` lets the next call emit again."""
        calls = self._spy_logger(monkeypatch)
        build_connection_factory("sqlite:///:memory:")
        _reset_default_factory_warning()
        build_connection_factory("sqlite:///:memory:")
        events = [e for e, _ in calls]
        assert events.count(_NO_POOL_EVENT) == 2

    def test_line_payload_includes_guidance(self, monkeypatch):
        """The emitted record carries operator-facing guidance text."""
        calls = self._spy_logger(monkeypatch)
        build_connection_factory("sqlite:///:memory:")
        # Find the emitted entry.
        record = next(kw for ev, kw in calls if ev == _NO_POOL_EVENT)
        assert "guidance" in record
        assert "pool" in record["guidance"].lower()
