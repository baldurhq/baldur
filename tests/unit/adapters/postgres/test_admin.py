"""
Unit tests for :class:`baldur.adapters.postgres.admin.PgAdmin` (515 D4).

Source: ``src/baldur/adapters/postgres/admin.py``

``PgAdmin`` holds every PG-admin SQL string and injects backend access
through two callables — ``get_session`` (context-managed cursor for
the 19 methods whose SQL emission fits one ``with`` block) and
``get_connection`` (raw connection used only by ``create_cursor``).
Tests run across both backend pairs:

- **Django pair**: stubbed ``django.db.connections[alias].cursor()`` via
  ``django_session_factory`` + ``django_connection_factory``.
- **DB-API pair**: ``dbapi_session_factory(get_connection)`` and the
  same ``get_connection`` directly. A captured-call mock cursor records
  every SQL string emitted so assertions can pin the exact statement
  shape without a live psycopg2.

The ``backend_pair`` parametrize covers both pairs uniformly so the
single-class design holds across backends per D4.

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.5 Dependency interaction (cursor.execute call list captures SQL).
- §8.3 Idempotency (advisory lock acquire+release sequence).
- §8.8 State transition (timeout_context restoration in finally).
- §8.2 Exception/edge cases (release_lock_failed log on flaky cleanup).
- §6.7 parametrize for backend pairs and lock matrix.
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest

from baldur.adapters.postgres.admin import PgAdmin
from baldur.adapters.postgres.sessions import (
    dbapi_session_factory,
    django_connection_factory,
    django_session_factory,
)
from baldur.interfaces.pg_admin import ConnectionStats

# =============================================================================
# Backend-pair fixtures
# =============================================================================


class _RecordingCursor:
    """Minimal DB-API cursor recording every execute() call.

    Sequential ``fetchone()`` returns are queued via ``set_fetchone_values``;
    when the queue is exhausted, returns ``None`` (mirrors empty resultset).
    """

    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self._fetchone_values: list[Any] = []
        self.closed = False

    def set_fetchone_values(self, values: list[Any]) -> None:
        self._fetchone_values = list(values)

    def execute(self, query: str, params: Any | None = None) -> None:
        self.executed.append((query, params))

    def fetchone(self) -> Any:
        if not self._fetchone_values:
            return None
        return self._fetchone_values.pop(0)

    def close(self) -> None:
        self.closed = True


class _DjangoCursorContext:
    """Mimics Django's ``connection.cursor()`` context manager."""

    def __init__(self, cursor: _RecordingCursor) -> None:
        self.cursor = cursor

    def __enter__(self) -> _RecordingCursor:
        return self.cursor

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


@pytest.fixture
def recording_cursor() -> _RecordingCursor:
    return _RecordingCursor()


@pytest.fixture
def django_backend_pair(monkeypatch, recording_cursor):
    """Install a fake ``django.db.connections`` and return a (PgAdmin, cursor) pair."""
    fake_db = types.ModuleType("django.db")
    fake_conn = MagicMock(name="django_connection[default]")
    fake_conn.cursor.return_value = _DjangoCursorContext(recording_cursor)

    class _ConnectionsProxy:
        def __getitem__(self, alias):
            return fake_conn

    fake_db.connections = _ConnectionsProxy()
    saved_django = sys.modules.get("django")
    saved_db = sys.modules.get("django.db")
    if saved_django is None:
        sys.modules["django"] = types.ModuleType("django")
    sys.modules["django.db"] = fake_db
    try:
        admin = PgAdmin(
            get_session=django_session_factory("default"),
            get_connection=django_connection_factory("default"),
            label="django:default",
        )
        yield admin, recording_cursor, fake_conn
    finally:
        if saved_django is None:
            sys.modules.pop("django", None)
        else:
            sys.modules["django"] = saved_django
        if saved_db is None:
            sys.modules.pop("django.db", None)
        else:
            sys.modules["django.db"] = saved_db


@pytest.fixture
def dbapi_backend_pair(recording_cursor):
    """Wire ``dbapi_session_factory`` over a fake DB-API connection."""
    fake_conn = MagicMock(name="dbapi_conn")
    fake_conn.cursor.return_value = recording_cursor

    def _get_connection() -> Any:
        return fake_conn

    admin = PgAdmin(
        get_session=dbapi_session_factory(_get_connection),
        get_connection=_get_connection,
        label="sql:default",
    )
    return admin, recording_cursor, fake_conn


@pytest.fixture(params=["django", "dbapi"])
def backend_pair(request, django_backend_pair, dbapi_backend_pair):
    """Parametrized fixture that yields (PgAdmin, cursor, conn) for both backends.

    Pytest evaluates both dependency fixtures, but only one of them
    monkeypatches sys.modules at a time (the django pair only patches
    during its own teardown context).
    """
    if request.param == "django":
        return django_backend_pair
    return dbapi_backend_pair


# =============================================================================
# A. Basic SQL-emission contracts
# =============================================================================


class TestPgAdminBasicSqlBehavior:
    """SQL strings emitted for the simple per-method primitives.

    Parametrized over both backend pairs so the single-class D4 design is
    verified to hold for every call site.
    """

    def test_is_available_returns_true(self, backend_pair):
        admin, _, _ = backend_pair
        assert admin.is_available() is True

    def test_ping_success(self, backend_pair):
        admin, cursor, _ = backend_pair
        cursor.set_fetchone_values([(1,)])

        assert admin.ping() is True
        assert cursor.executed[0][0] == "SELECT 1"

    def test_ping_swallow_exception_logs_and_returns_false(self, backend_pair, caplog):
        """``ping`` must NOT propagate — connectivity probes can't crash callers."""
        admin, cursor, _ = backend_pair

        def _raise(query: str, params=None) -> None:
            raise RuntimeError("connection refused")

        cursor.execute = _raise  # type: ignore[assignment]

        with caplog.at_level("ERROR"):
            assert admin.ping() is False

        assert any("pg_admin.ping_failed" in r.message for r in caplog.records)

    def test_get_connection_stats_returns_typed_struct(self, backend_pair):
        admin, cursor, _ = backend_pair
        cursor.set_fetchone_values([(50, 12, 30, 8)])

        stats = admin.get_connection_stats()

        assert stats == ConnectionStats(
            total_connections=50, active=12, idle=30, idle_in_transaction=8
        )
        emitted_sql = cursor.executed[0][0]
        assert "pg_stat_activity" in emitted_sql
        assert "current_database()" in emitted_sql

    def test_get_active_connection_count_returns_first_column(self, backend_pair):
        admin, cursor, _ = backend_pair
        cursor.set_fetchone_values([(17,)])

        assert admin.get_active_connection_count() == 17
        emitted_sql = cursor.executed[0][0]
        assert "state = 'active'" in emitted_sql

    def test_get_active_connection_count_returns_zero_when_no_row(self, backend_pair):
        """Defensive zero when the result set is empty."""
        admin, cursor, _ = backend_pair
        # fetchone queue empty → returns None
        assert admin.get_active_connection_count() == 0

    @pytest.mark.parametrize("seconds", [0.01, 0.5, 5])
    def test_pg_sleep_emits_pg_sleep_with_duration(self, backend_pair, seconds):
        admin, cursor, _ = backend_pair
        cursor.set_fetchone_values([(None,)])

        admin.pg_sleep(seconds)

        assert cursor.executed[0][0] == f"SELECT pg_sleep({seconds})"

    def test_get_backend_pid_with_delay_returns_pid(self, backend_pair):
        admin, cursor, _ = backend_pair
        cursor.set_fetchone_values([(12345, None)])

        pid = admin.get_backend_pid_with_delay(delay_seconds=0.05)

        assert pid == 12345
        assert "pg_backend_pid()" in cursor.executed[0][0]
        assert "pg_sleep(0.05)" in cursor.executed[0][0]


# =============================================================================
# B. Advisory lock matrix
# =============================================================================


class TestPgAdminAdvisoryLockBehavior:
    """Acquire/release SQL across the wait × exclusive matrix.

    Each acquire family pairs with a release family; the test verifies
    both the SQL string and the parameter list, and confirms the
    parametrized wait-True path yields ``True`` unconditionally per the
    contract (``pg_advisory_lock`` blocks rather than returning a bool).
    """

    @pytest.mark.parametrize(
        ("wait", "expected_sql"),
        [
            (True, "SELECT pg_advisory_lock(%s)"),
            (False, "SELECT pg_try_advisory_lock(%s)"),
        ],
    )
    def test_acquire_advisory_lock_exclusive(self, backend_pair, wait, expected_sql):
        admin, cursor, _ = backend_pair
        cursor.set_fetchone_values([(True,)])

        result = admin.acquire_advisory_lock(lock_id=42, wait=wait)

        assert result is True
        assert cursor.executed[-1] == (expected_sql, [42])

    @pytest.mark.parametrize(
        ("wait", "expected_sql"),
        [
            (True, "SELECT pg_advisory_lock_shared(%s)"),
            (False, "SELECT pg_try_advisory_lock_shared(%s)"),
        ],
    )
    def test_acquire_advisory_lock_shared(self, backend_pair, wait, expected_sql):
        admin, cursor, _ = backend_pair
        cursor.set_fetchone_values([(True,)])

        result = admin.acquire_advisory_lock_shared(lock_id=42, wait=wait)

        assert result is True
        assert cursor.executed[-1] == (expected_sql, [42])

    def test_try_advisory_lock_delegates_to_acquire_non_waiting(self, backend_pair):
        """``try_advisory_lock`` == ``acquire_advisory_lock(wait=False)``."""
        admin, cursor, _ = backend_pair
        cursor.set_fetchone_values([(True,)])

        assert admin.try_advisory_lock(lock_id=99) is True
        assert cursor.executed[-1][0] == "SELECT pg_try_advisory_lock(%s)"

    @pytest.mark.parametrize("returned", [True, False])
    def test_acquire_returns_fetchone_value_for_non_waiting(
        self, backend_pair, returned
    ):
        """Non-waiting acquire returns whatever ``pg_try_advisory_lock`` says."""
        admin, cursor, _ = backend_pair
        cursor.set_fetchone_values([(returned,)])

        assert admin.acquire_advisory_lock(lock_id=42, wait=False) is returned

    def test_release_advisory_lock_emits_unlock_sql(self, backend_pair):
        admin, cursor, _ = backend_pair
        cursor.set_fetchone_values([(True,)])

        assert admin.release_advisory_lock(lock_id=42) is True
        assert cursor.executed[-1] == ("SELECT pg_advisory_unlock(%s)", [42])

    def test_release_advisory_lock_shared_emits_shared_unlock_sql(self, backend_pair):
        admin, cursor, _ = backend_pair
        cursor.set_fetchone_values([(True,)])

        assert admin.release_advisory_lock_shared(lock_id=42) is True
        assert cursor.executed[-1] == (
            "SELECT pg_advisory_unlock_shared(%s)",
            [42],
        )


# =============================================================================
# C. advisory_lock_context matrix (exclusive × wait)
# =============================================================================


class TestPgAdminAdvisoryLockContextBehavior:
    """4-case matrix for ``advisory_lock_context`` — acquire + finally-release.

    Acquire and release run inside the same ``with self._get_session()``
    block so they hit the same backend session. The test asserts the
    cursor saw acquire-then-release on every successful path.
    """

    @pytest.mark.parametrize(
        ("exclusive", "wait", "acquire_sql", "unlock_sql"),
        [
            (
                True,
                True,
                "SELECT pg_advisory_lock(%s)",
                "SELECT pg_advisory_unlock(%s)",
            ),
            (
                True,
                False,
                "SELECT pg_try_advisory_lock(%s)",
                "SELECT pg_advisory_unlock(%s)",
            ),
            (
                False,
                True,
                "SELECT pg_advisory_lock_shared(%s)",
                "SELECT pg_advisory_unlock_shared(%s)",
            ),
            (
                False,
                False,
                "SELECT pg_try_advisory_lock_shared(%s)",
                "SELECT pg_advisory_unlock_shared(%s)",
            ),
        ],
    )
    def test_acquire_then_release_in_finally(
        self, backend_pair, exclusive, wait, acquire_sql, unlock_sql
    ):
        admin, cursor, _ = backend_pair
        # Non-waiting variants need a truthy fetchone for "acquired".
        cursor.set_fetchone_values([(True,)])

        with admin.advisory_lock_context(
            lock_id=42, exclusive=exclusive, wait=wait
        ) as acquired:
            assert acquired is True

        executed_sql = [row[0] for row in cursor.executed]
        assert acquire_sql in executed_sql
        assert unlock_sql in executed_sql
        # Release happens after acquire.
        assert executed_sql.index(unlock_sql) > executed_sql.index(acquire_sql)

    def test_non_waiting_failure_yields_false_and_skips_release(self, backend_pair):
        """``pg_try_advisory_lock`` returned False → no release attempted."""
        admin, cursor, _ = backend_pair
        cursor.set_fetchone_values([(False,)])

        with admin.advisory_lock_context(
            lock_id=42, exclusive=True, wait=False
        ) as acquired:
            assert acquired is False

        executed_sql = [row[0] for row in cursor.executed]
        assert "SELECT pg_advisory_unlock(%s)" not in executed_sql

    def test_release_failure_logs_warning_does_not_propagate(
        self, backend_pair, caplog
    ):
        """Flaky cleanup must surface as ``release_lock_failed`` log, not raise."""
        admin, cursor, _ = backend_pair
        cursor.set_fetchone_values([(True,)])

        # Make only the unlock raise.
        original_execute = cursor.execute

        def _execute(query: str, params=None) -> None:
            if "unlock" in query:
                raise RuntimeError("unlock blew up")
            return original_execute(query, params)

        cursor.execute = _execute  # type: ignore[assignment]

        with caplog.at_level("WARNING"):
            with admin.advisory_lock_context(lock_id=42, exclusive=True) as acquired:
                assert acquired is True
            # No exception escaped the with block.

        assert any("pg_admin.release_lock_failed" in r.message for r in caplog.records)


# =============================================================================
# D. Session settings + timeout_context
# =============================================================================


class TestPgAdminSessionTimeoutBehavior:
    """``set_lock_timeout`` / ``set_statement_timeout`` / ``reset_timeouts``."""

    @pytest.mark.parametrize(
        ("timeout_ms", "expected_sql"),
        [
            (0, "SET lock_timeout = '0'"),
            (1500, "SET lock_timeout = '1500ms'"),
        ],
    )
    def test_set_lock_timeout_branches_on_zero(
        self, backend_pair, timeout_ms, expected_sql
    ):
        admin, cursor, _ = backend_pair
        admin.set_lock_timeout(timeout_ms)
        assert cursor.executed[-1][0] == expected_sql

    @pytest.mark.parametrize(
        ("timeout_ms", "expected_sql"),
        [
            (0, "SET statement_timeout = '0'"),
            (5000, "SET statement_timeout = '5000ms'"),
        ],
    )
    def test_set_statement_timeout_branches_on_zero(
        self, backend_pair, timeout_ms, expected_sql
    ):
        admin, cursor, _ = backend_pair
        admin.set_statement_timeout(timeout_ms)
        assert cursor.executed[-1][0] == expected_sql

    def test_reset_timeouts_emits_both_zero_settings(self, backend_pair):
        admin, cursor, _ = backend_pair
        admin.reset_timeouts()
        executed_sql = [row[0] for row in cursor.executed]
        assert "SET lock_timeout = '0'" in executed_sql
        assert "SET statement_timeout = '0'" in executed_sql


class TestPgAdminTimeoutContextBehavior:
    """``timeout_context`` applies + restores; DeadlineContext-aware."""

    def test_timeout_context_applies_and_resets_on_exit(self, backend_pair):
        admin, cursor, _ = backend_pair

        with admin.timeout_context(lock_timeout_ms=500, statement_timeout_ms=3000):
            mid_sql = [row[0] for row in cursor.executed]

        end_sql = [row[0] for row in cursor.executed]

        assert "SET lock_timeout = '500ms'" in mid_sql
        assert "SET statement_timeout = '3000ms'" in mid_sql
        # Reset runs in finally — both timeouts set back to 0 after exit.
        assert end_sql.count("SET lock_timeout = '0'") >= 1
        assert end_sql.count("SET statement_timeout = '0'") >= 1

    def test_timeout_context_resets_even_when_body_raises(self, backend_pair):
        admin, cursor, _ = backend_pair

        with pytest.raises(RuntimeError, match="body-error"):
            with admin.timeout_context(statement_timeout_ms=1000):
                raise RuntimeError("body-error")

        executed_sql = [row[0] for row in cursor.executed]
        assert "SET statement_timeout = '0'" in executed_sql

    def test_timeout_context_skips_apply_when_value_is_zero(self, backend_pair):
        """When the requested values are zero, only the reset on exit runs."""
        admin, cursor, _ = backend_pair

        with admin.timeout_context(lock_timeout_ms=0, statement_timeout_ms=0):
            mid_count = len(cursor.executed)

        end_count = len(cursor.executed)

        # No SET commands inside the body (the deadline path may still set
        # statement_timeout when a deadline is active; in unit tests there
        # is no deadline set, so the body should be empty).
        assert mid_count == 0
        # Reset emits both SET ... = '0' commands.
        assert end_count >= 2


# =============================================================================
# E. Stress test primitives
# =============================================================================


class TestPgAdminStressPrimitiveBehavior:
    """SQL emitted for the 4 stress-test primitives (execute_*)."""

    def test_execute_aggregate_query_table_interpolated_with_fixed_columns(
        self, backend_pair
    ):
        admin, cursor, _ = backend_pair
        cursor.set_fetchone_values([(100, 25.5, 99.9, 1.1)])

        result = admin.execute_aggregate_query("products")

        assert result == (100, 25.5, 99.9, 1.1)
        emitted_sql = cursor.executed[0][0]
        assert "FROM products" in emitted_sql
        assert "COUNT(*)" in emitted_sql
        assert "AVG(price)" in emitted_sql
        assert "is_active = true" in emitted_sql

    def test_execute_aggregate_query_handles_null_aggregates(self, backend_pair):
        """Empty table → AVG/MAX/MIN are NULL → coerced to 0.0."""
        admin, cursor, _ = backend_pair
        cursor.set_fetchone_values([(0, None, None, None)])

        assert admin.execute_aggregate_query("empty_table") == (0, 0.0, 0.0, 0.0)

    def test_execute_nonexistent_table_query_emits_predictable_sql(self, backend_pair):
        admin, cursor, _ = backend_pair
        admin.execute_nonexistent_table_query()
        assert (
            cursor.executed[0][0] == "SELECT * FROM __nonexistent_table_for_cb_test__"
        )

    def test_execute_timeout_query_sets_timeout_then_sleeps(self, backend_pair):
        admin, cursor, _ = backend_pair
        admin.execute_timeout_query(timeout_ms=10, sleep_seconds=2)
        assert cursor.executed[0][0] == "SET statement_timeout = '10ms'"
        assert cursor.executed[1][0] == "SELECT pg_sleep(2)"

    def test_execute_slow_query_emits_pg_sleep(self, backend_pair):
        admin, cursor, _ = backend_pair
        cursor.set_fetchone_values([(None,)])
        admin.execute_slow_query(seconds=3)
        assert cursor.executed[0][0] == "SELECT pg_sleep(3)"


# =============================================================================
# F. Cursor escape hatches — create_cursor / execute_with_cursor
# =============================================================================


class TestPgAdminCursorEscapeHatchBehavior:
    """``create_cursor`` returns a caller-owned cursor; ``execute_with_cursor`` uses it."""

    def test_create_cursor_returns_cursor_from_get_connection(self, backend_pair):
        """Cursor lifecycle is owned by the caller (pool-exhaustion path).

        ``create_cursor()`` calls ``self._get_connection().cursor()`` and
        returns that cursor object directly — no ``with`` block.
        """
        admin, _, fake_conn = backend_pair

        cursor = admin.create_cursor()

        assert cursor is fake_conn.cursor.return_value or cursor == (
            fake_conn.cursor.return_value
        )
        fake_conn.cursor.assert_called()

    def test_create_cursor_called_multiple_times_each_uses_get_connection(
        self, backend_pair
    ):
        """Each call independently asks ``get_connection`` for a cursor."""
        admin, _, fake_conn = backend_pair

        admin.create_cursor()
        admin.create_cursor()

        assert fake_conn.cursor.call_count == 2

    def test_execute_with_cursor_runs_on_supplied_cursor(self, backend_pair):
        """Caller-supplied cursor: routes through neither get_session nor
        get_connection — the cursor is just used."""
        admin, _, _ = backend_pair

        external_cursor = MagicMock(name="external")
        external_cursor.fetchone.return_value = ("payload",)

        result = admin.execute_with_cursor(external_cursor, "SELECT 42", params=[1])

        external_cursor.execute.assert_called_once_with("SELECT 42", [1])
        external_cursor.fetchone.assert_called_once()
        assert result == ("payload",)

    def test_execute_with_cursor_no_params_branch(self, backend_pair):
        """When ``params is None``, ``execute()`` is called without params."""
        admin, _, _ = backend_pair

        external_cursor = MagicMock(name="external")
        external_cursor.fetchone.return_value = None

        admin.execute_with_cursor(external_cursor, "SELECT NOW()", params=None)

        external_cursor.execute.assert_called_once_with("SELECT NOW()")


# =============================================================================
# G. DeadlineContext compatibility (timeout_context import branch)
# =============================================================================


class TestPgAdminTimeoutContextDeadlineIntegration:
    """``timeout_context`` consults ``get_deadline_aware_statement_timeout``."""

    def test_deadline_shorter_than_requested_wins(
        self, monkeypatch, dbapi_backend_pair
    ):
        """When the deadline-aware helper returns a shorter timeout, it wins."""
        admin, cursor, _ = dbapi_backend_pair

        @contextmanager
        def _noop_session():
            yield cursor

        # Replace the deadline helper used inside timeout_context.
        fake_module = types.SimpleNamespace(
            get_deadline_aware_statement_timeout=lambda default_db_timeout_ms: 500,
        )
        monkeypatch.setitem(sys.modules, "baldur.scaling.deadline_context", fake_module)

        with admin.timeout_context(statement_timeout_ms=5000):
            pass

        executed_sql = [row[0] for row in cursor.executed]
        assert "SET statement_timeout = '500ms'" in executed_sql

    def test_import_error_falls_through_to_requested_value(
        self, monkeypatch, dbapi_backend_pair
    ):
        """If the deadline module is absent, requested values are used as-is."""
        admin, cursor, _ = dbapi_backend_pair

        # Make the import fail by setting None.
        monkeypatch.setitem(sys.modules, "baldur.scaling.deadline_context", None)

        with admin.timeout_context(statement_timeout_ms=2500):
            pass

        executed_sql = [row[0] for row in cursor.executed]
        assert "SET statement_timeout = '2500ms'" in executed_sql
