"""
SQLDatabaseHealthAdapter Integration Tests against Docker Postgres.

Verifies the framework-independent DB-API 2.0 health adapter introduced in
doc 515 (G1) actually round-trips ``SELECT 1`` against a real PostgreSQL
instance and reports the correct vendor / is_usable values.

Test Categories:
    A. SELECT 1 round-trip against healthy Postgres:
        - is_usable=True when connection succeeds
        - vendor matches injected SQLDialect
    B. Failure handling:
        - is_usable=False when get_connection raises
        - is_usable=False when underlying server is unreachable
    C. health_check() convenience method:
        - Returns True/False mirroring check_connection().is_usable

Note: Requires Docker PostgreSQL on port 15432 (docker-compose.test.yml).
      Marked with @pytest.mark.requires_db for auto-skip.
"""

from __future__ import annotations

import pytest

from baldur.adapters.database.sql_health import SQLDatabaseHealthAdapter
from baldur.settings.sql import SQLDialect
from tests.integration.conftest import DatabaseTestConfig

pytestmark = pytest.mark.requires_db


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def pg_connect():
    """Open a fresh psycopg2 connection to the Docker test Postgres.

    The returned callable is the ``get_connection`` argument the adapter
    expects — each invocation opens a new connection so the adapter's
    open-per-check lifecycle (515 D2) is exercised against a real backend.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    cfg = DatabaseTestConfig()

    def _get_connection():
        return psycopg2.connect(
            host=cfg.DEFAULT_HOST,
            port=cfg.DEFAULT_PORT,
            database=cfg.DEFAULT_DB,
            user=cfg.DEFAULT_USER,
            password=cfg.DEFAULT_PASSWORD,
            connect_timeout=3,
        )

    return _get_connection


# =============================================================================
# A. SELECT 1 round-trip against healthy Postgres
# =============================================================================


class TestSQLDatabaseHealthAdapterHealthyRoundtrip:
    """Adapter reports healthy state when the SELECT 1 round-trip succeeds."""

    def test_check_connection_returns_is_usable_true(self, pg_connect):
        """
        Purpose:
            Verify check_connection() opens a real psycopg2 connection,
            executes SELECT 1, and reports is_usable=True.
        Expected:
            - is_usable is True
            - alias matches the passed-in default
        """
        adapter = SQLDatabaseHealthAdapter(
            get_connection=pg_connect,
            dialect=SQLDialect.POSTGRESQL,
        )

        info = adapter.check_connection()

        assert info.is_usable is True
        assert info.alias == "default"

    def test_check_connection_reports_postgresql_vendor(self, pg_connect):
        """
        Purpose:
            Confirm vendor reflects the injected SQLDialect (per 515 D8 —
            mirrors Django ``conn.vendor`` semantics, vendor name only).
        Expected:
            - vendor == "postgresql"
        """
        adapter = SQLDatabaseHealthAdapter(
            get_connection=pg_connect,
            dialect=SQLDialect.POSTGRESQL,
        )

        info = adapter.check_connection()

        assert info.vendor == "postgresql"

    def test_check_connection_respects_caller_supplied_alias(self, pg_connect):
        """
        Purpose:
            The alias argument is propagated unchanged into the returned
            DatabaseConnectionInfo (used by HealthCheckService to report
            per-alias state).
        Expected:
            - returned alias equals the alias passed in
        """
        adapter = SQLDatabaseHealthAdapter(
            get_connection=pg_connect,
            dialect=SQLDialect.POSTGRESQL,
        )

        info = adapter.check_connection(alias="primary")

        assert info.alias == "primary"
        assert info.is_usable is True

    def test_repeated_checks_each_open_fresh_connection(self, pg_connect):
        """
        Purpose:
            Open-per-check lifecycle (515 D2): consecutive check_connection
            invocations must each succeed independently — there is no shared
            connection that a prior failure could poison.
        Expected:
            - 3 sequential checks all return is_usable=True
        """
        adapter = SQLDatabaseHealthAdapter(
            get_connection=pg_connect,
            dialect=SQLDialect.POSTGRESQL,
        )

        results = [adapter.check_connection().is_usable for _ in range(3)]

        assert results == [True, True, True]


# =============================================================================
# B. Failure handling
# =============================================================================


class TestSQLDatabaseHealthAdapterFailureHandling:
    """Adapter degrades gracefully when the backend is unreachable."""

    def test_get_connection_raises_returns_is_usable_false(self):
        """
        Purpose:
            When the injected callable raises (DSN typo, server down,
            credentials wrong), the adapter must catch and report
            is_usable=False rather than propagate the exception. The
            HealthCheckService relies on this fail-safe behavior.
        Expected:
            - check_connection() returns is_usable=False
            - vendor still reflects the configured dialect
            - no exception propagates
        """

        def _bad_connect():
            raise RuntimeError("connection refused")

        adapter = SQLDatabaseHealthAdapter(
            get_connection=_bad_connect,
            dialect=SQLDialect.POSTGRESQL,
        )

        info = adapter.check_connection()

        assert info.is_usable is False
        assert info.vendor == "postgresql"

    def test_unreachable_postgres_returns_is_usable_false(self):
        """
        Purpose:
            Real psycopg2 failure path — pointing at an unbound port
            triggers a genuine OperationalError, which the adapter
            must convert to is_usable=False.
        Expected:
            - is_usable=False (not a raised exception)
        """
        psycopg2 = pytest.importorskip("psycopg2")

        def _connect_to_unbound_port():
            return psycopg2.connect(
                host="127.0.0.1",
                port=1,
                database="baldur_test",
                user="postgres",
                password="postgres",
                connect_timeout=2,
            )

        adapter = SQLDatabaseHealthAdapter(
            get_connection=_connect_to_unbound_port,
            dialect=SQLDialect.POSTGRESQL,
        )

        info = adapter.check_connection()

        assert info.is_usable is False


# =============================================================================
# C. health_check() convenience method
# =============================================================================


class TestSQLDatabaseHealthAdapterConvenience:
    """The inherited ``health_check()`` mirrors ``check_connection().is_usable``."""

    def test_health_check_returns_true_when_postgres_reachable(self, pg_connect):
        """
        Purpose:
            ConnectionHealthMonitor registers ``health_check`` as a callback;
            verify it returns True when the underlying SELECT 1 succeeds.
        Expected:
            - health_check() is True
        """
        adapter = SQLDatabaseHealthAdapter(
            get_connection=pg_connect,
            dialect=SQLDialect.POSTGRESQL,
        )

        assert adapter.health_check() is True

    def test_health_check_returns_false_when_get_connection_fails(self):
        """
        Purpose:
            health_check() short-circuits on adapter-internal failure so
            registered callbacks never raise into the monitor.
        Expected:
            - health_check() is False
        """

        def _bad_connect():
            raise OSError("no route to host")

        adapter = SQLDatabaseHealthAdapter(
            get_connection=_bad_connect,
            dialect=SQLDialect.POSTGRESQL,
        )

        assert adapter.health_check() is False

    def test_list_aliases_default_single_alias(self, pg_connect):
        """
        Purpose:
            The DB-API adapter exposes a single alias (matches the
            ``check_connection`` default). Multi-alias support is out of
            scope (515 OOS — Django ORM owns multi-alias).
        Expected:
            - list_aliases() == ["default"]
        """
        adapter = SQLDatabaseHealthAdapter(
            get_connection=pg_connect,
            dialect=SQLDialect.POSTGRESQL,
        )

        assert adapter.list_aliases() == ["default"]

    def test_close_all_is_noop(self, pg_connect):
        """
        Purpose:
            Open-per-check lifecycle has no long-lived connection to close,
            so close_all() returns None without side effects.
        Expected:
            - close_all() returns None
            - subsequent check_connection() still succeeds
        """
        adapter = SQLDatabaseHealthAdapter(
            get_connection=pg_connect,
            dialect=SQLDialect.POSTGRESQL,
        )

        assert adapter.close_all() is None
        assert adapter.check_connection().is_usable is True
