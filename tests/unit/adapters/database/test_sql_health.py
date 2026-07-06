"""
Unit tests for :class:`baldur.adapters.database.sql_health.SQLDatabaseHealthAdapter`
(515 D1/D2/D8).

Source: ``src/baldur/adapters/database/sql_health.py``

Lifecycle (D2): open-per-check. Each ``check_connection()`` invocation
opens a fresh connection via the injected ``get_connection`` callable,
runs ``SELECT 1``, closes the connection. The exception path returns
``is_usable=False`` without propagating so the OSS health-probe contract
holds for Flask / FastAPI deployments.

Vendor field (D8): ``vendor`` is the ``SQLDialect.value`` — one of
``"postgresql"``, ``"mysql"``, ``"sqlite"``. Matches Django's
``conn.vendor`` semantics (vendor name, not version).

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §6.7 parametrize over dialect × success/failure (6-case matrix).
- §8.5 Dependency interaction — get_connection call count, cursor.execute
  call count.
- §8.4 Side effects — cursor and connection closed even on exception.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from baldur.adapters.database.sql_health import SQLDatabaseHealthAdapter
from baldur.interfaces.database_health import (
    DatabaseConnectionInfo,
    DatabaseHealthProvider,
)
from baldur.settings.sql import SQLDialect


@pytest.fixture
def make_adapter():
    """Factory: build an adapter from a mocked ``get_connection`` callable.

    Returns ``(adapter, get_connection_mock, conn_mock, cursor_mock)`` so
    individual tests can re-wire close behavior.
    """

    def _build(
        dialect: SQLDialect = SQLDialect.POSTGRESQL,
        execute_side_effect: Exception | None = None,
        conn_side_effect: Exception | None = None,
    ):
        cursor = MagicMock(name="cursor")
        if execute_side_effect is not None:
            cursor.execute.side_effect = execute_side_effect

        conn = MagicMock(name="conn")
        conn.cursor.return_value = cursor

        get_connection = MagicMock(name="get_connection")
        if conn_side_effect is not None:
            get_connection.side_effect = conn_side_effect
        else:
            get_connection.return_value = conn

        adapter = SQLDatabaseHealthAdapter(
            get_connection=get_connection, dialect=dialect
        )
        return adapter, get_connection, conn, cursor

    return _build


class TestSQLDatabaseHealthAdapterContract:
    """ABC inheritance and pinned attribute contract."""

    def test_subclass_of_database_health_provider(self, make_adapter):
        adapter, *_ = make_adapter()
        assert isinstance(adapter, DatabaseHealthProvider)

    def test_default_alias_is_default(self, make_adapter):
        """``list_aliases`` returns the single ``"default"`` alias."""
        adapter, *_ = make_adapter()
        assert adapter.list_aliases() == ["default"]

    def test_close_all_is_noop(self, make_adapter):
        """``close_all`` returns None — no global pool to drain."""
        adapter, *_ = make_adapter()
        assert adapter.close_all() is None


class TestSQLDatabaseHealthAdapterBehavior:
    """``check_connection`` round-trip across dialect × success/failure."""

    @pytest.mark.parametrize(
        "dialect",
        [SQLDialect.POSTGRESQL, SQLDialect.MYSQL, SQLDialect.SQLITE],
        ids=["postgresql", "mysql", "sqlite"],
    )
    def test_check_connection_success_returns_usable_with_dialect_vendor(
        self, make_adapter, dialect
    ):
        """``SELECT 1`` succeeds → ``is_usable=True``, vendor == dialect.value."""
        adapter, get_connection, conn, cursor = make_adapter(dialect=dialect)

        info = adapter.check_connection()

        assert isinstance(info, DatabaseConnectionInfo)
        assert info.alias == "default"
        assert info.vendor == dialect.value
        assert info.is_usable is True
        get_connection.assert_called_once_with()
        cursor.execute.assert_called_once_with("SELECT 1")
        cursor.fetchone.assert_called_once()

    @pytest.mark.parametrize(
        "dialect",
        [SQLDialect.POSTGRESQL, SQLDialect.MYSQL, SQLDialect.SQLITE],
        ids=["postgresql", "mysql", "sqlite"],
    )
    def test_check_connection_execute_failure_returns_unusable(
        self, make_adapter, dialect
    ):
        """``SELECT 1`` raises → ``is_usable=False``, exception swallowed."""
        adapter, *_ = make_adapter(
            dialect=dialect, execute_side_effect=RuntimeError("connection refused")
        )

        info = adapter.check_connection()

        assert info.alias == "default"
        assert info.vendor == dialect.value
        assert info.is_usable is False

    def test_check_connection_get_connection_failure_returns_unusable(
        self, make_adapter
    ):
        """``get_connection`` itself raises → ``is_usable=False``."""
        adapter, *_ = make_adapter(
            conn_side_effect=OSError("dns failure"),
        )

        info = adapter.check_connection()

        assert info.is_usable is False
        assert info.vendor == "postgresql"

    def test_check_connection_alias_is_passed_through(self, make_adapter):
        """The alias argument lands verbatim in ``DatabaseConnectionInfo.alias``."""
        adapter, *_ = make_adapter()

        info = adapter.check_connection("replica")

        assert info.alias == "replica"

    def test_check_connection_closes_cursor_and_conn_on_success(self, make_adapter):
        """Cleanup ordering: cursor.close() then conn.close()."""
        adapter, _gc, conn, cursor = make_adapter()

        adapter.check_connection()

        cursor.close.assert_called_once()
        conn.close.assert_called_once()

    def test_check_connection_closes_cursor_and_conn_on_execute_failure(
        self, make_adapter
    ):
        """Cleanup runs even when ``SELECT 1`` raises."""
        adapter, _gc, conn, cursor = make_adapter(
            execute_side_effect=RuntimeError("net")
        )

        adapter.check_connection()

        cursor.close.assert_called_once()
        conn.close.assert_called_once()

    def test_check_connection_close_failures_are_swallowed(self, make_adapter):
        """Flaky cleanup must not surface — pool-fronted callables sometimes
        raise on ``.close()`` and we still want a clean health-probe result."""
        adapter, _gc, conn, cursor = make_adapter()
        cursor.close.side_effect = RuntimeError("flaky cursor close")
        conn.close.side_effect = RuntimeError("flaky conn close")

        info = adapter.check_connection()

        assert info.is_usable is True
        cursor.close.assert_called_once()
        conn.close.assert_called_once()

    def test_check_connection_each_call_opens_new_connection(self, make_adapter):
        """Open-per-check lifecycle — N invocations call ``get_connection`` N times."""
        adapter, get_connection, *_ = make_adapter()

        adapter.check_connection()
        adapter.check_connection()
        adapter.check_connection()

        assert get_connection.call_count == 3

    def test_check_connection_logs_debug_event_on_failure(self, make_adapter, caplog):
        """Failure path emits a structured ``sql_database_health.check_failed`` event."""
        adapter, *_ = make_adapter(
            execute_side_effect=RuntimeError("connection refused")
        )

        with caplog.at_level("DEBUG"):
            adapter.check_connection("primary")

        assert any(
            "sql_database_health.check_failed" in r.message for r in caplog.records
        )


class TestSQLDatabaseHealthAdapterHealthCheckConvenience:
    """The ABC's ``health_check()`` default consumes ``check_connection`` correctly."""

    def test_health_check_returns_true_on_success(self, make_adapter):
        adapter, *_ = make_adapter()
        assert adapter.health_check() is True

    def test_health_check_returns_false_on_failure(self, make_adapter):
        adapter, *_ = make_adapter(execute_side_effect=RuntimeError("net"))
        assert adapter.health_check() is False
