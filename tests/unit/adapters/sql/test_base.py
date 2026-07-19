"""
Unit tests for baldur.adapters.sql.base.

Coverage:
- GenericSQLRepository._prepare() placeholder translation (sqlite '?')
- JSON / datetime serialization helpers
- _execute() commits on success, rolls back on exception
- sql_transaction suspends per-call commits
- SchemaVersionManager idempotency + bookkeeping table DDL
- _execute_returning_id POSTGRESQL branch (mocked)
- Lazy schema bootstrap (PR2 review fix #7)
- dialect_upsert_clause helper (PR2 review fix #8)
- Public dialect_* helper aliases (PR2 review fix #12)

All tests use stdlib sqlite3 in-memory — no infra required.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from baldur.adapters.sql.base import (
    GenericSQLRepository,
    SchemaVersionManager,
    _suspended_conns,
    dialect_bigserial,
    dialect_json_type,
    dialect_timestamp_type,
    dialect_upsert_clause,
    sql_transaction,
)
from baldur.settings.sql import SQLDialect

# ---------------------------------------------------------------------------
# Placeholder + serializer contract
# ---------------------------------------------------------------------------


class TestGenericSQLRepositoryPlaceholderContract:
    """_prepare() translates %s to the dialect's placeholder."""

    def test_sqlite_replaces_percent_s_with_question_mark(self, get_sqlite_conn):
        repo = GenericSQLRepository(get_sqlite_conn, dialect=SQLDialect.SQLITE)
        assert repo._prepare("SELECT %s, %s") == "SELECT ?, ?"

    def test_postgres_leaves_percent_s_intact(self, get_sqlite_conn):
        repo = GenericSQLRepository(get_sqlite_conn, dialect=SQLDialect.POSTGRESQL)
        assert repo._prepare("SELECT %s") == "SELECT %s"


class TestGenericSQLRepositorySerializerBehavior:
    """JSON / datetime helpers round-trip and handle edge cases."""

    def test_dumps_json_serializes_datetime_as_isoformat(self):
        dt = datetime(2026, 4, 14, 12, 0, 0, tzinfo=UTC)
        result = GenericSQLRepository._dumps_json({"ts": dt})
        assert dt.isoformat() in result

    def test_loads_json_returns_none_for_none_input(self):
        assert GenericSQLRepository._loads_json(None) is None

    def test_loads_json_passes_through_preparsed_dict(self):
        # psycopg2 with JSONB returns already-decoded dicts.
        assert GenericSQLRepository._loads_json({"a": 1}) == {"a": 1}

    def test_loads_json_returns_none_for_invalid_payload(self):
        assert GenericSQLRepository._loads_json("not valid json") is None

    def test_dumps_json_raises_for_unsupported_type(self):
        class Unsupported:
            pass

        with pytest.raises(TypeError):
            GenericSQLRepository._dumps_json({"x": Unsupported()})

    def test_dt_from_db_parses_iso_string(self):
        iso = "2026-04-14T12:00:00+00:00"
        parsed = GenericSQLRepository._dt_from_db(iso)
        assert parsed == datetime(2026, 4, 14, 12, 0, 0, tzinfo=UTC)

    def test_dt_from_db_returns_none_for_malformed_string(self):
        assert GenericSQLRepository._dt_from_db("garbage") is None

    def test_dt_from_db_returns_datetime_passthrough(self):
        dt = datetime(2026, 4, 14, tzinfo=UTC)
        assert GenericSQLRepository._dt_from_db(dt) is dt


# ---------------------------------------------------------------------------
# _execute / _cursor commit/rollback
# ---------------------------------------------------------------------------


class TestGenericSQLRepositoryExecuteBehavior:
    """_execute() lifecycle: commit on success, rollback on exception."""

    def test_execute_commits_on_success(self, get_sqlite_conn, sqlite_conn):
        repo = GenericSQLRepository(get_sqlite_conn, dialect=SQLDialect.SQLITE)
        repo._execute("CREATE TABLE t (v INTEGER)")
        repo._execute("INSERT INTO t VALUES (%s)", (42,))

        # Read back via a fresh cursor — committed rows must be visible.
        cur = sqlite_conn.cursor()
        cur.execute("SELECT v FROM t")
        assert cur.fetchone() == (42,)

    def test_execute_rolls_back_on_exception(self, get_sqlite_conn, sqlite_conn):
        repo = GenericSQLRepository(get_sqlite_conn, dialect=SQLDialect.SQLITE)
        repo._execute("CREATE TABLE t (v INTEGER PRIMARY KEY)")
        repo._execute("INSERT INTO t VALUES (%s)", (1,))

        # Second insert of the same PK triggers an IntegrityError mid-txn.
        with pytest.raises(sqlite3.IntegrityError):
            repo._execute("INSERT INTO t VALUES (%s)", (1,))

        # The initial row must still be present; the failed statement rolled back.
        cur = sqlite_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM t")
        assert cur.fetchone()[0] == 1

    def test_autocommit_delegated_skips_baldur_commit(self):
        """When autocommit_delegated=True the base never calls conn.commit()."""
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = MagicMock()
        repo = GenericSQLRepository(
            lambda: mock_conn,
            dialect=SQLDialect.POSTGRESQL,
            autocommit_delegated=True,
        )
        repo._execute("SELECT 1")
        mock_conn.commit.assert_not_called()
        mock_conn.rollback.assert_not_called()


# ---------------------------------------------------------------------------
# sql_transaction scope
# ---------------------------------------------------------------------------


class TestSqlTransactionStateTransitionBehavior:
    """sql_transaction brackets the connection with a single commit/rollback."""

    def test_enter_marks_connection_as_suspended(self, sqlite_conn):
        assert id(sqlite_conn) not in _suspended_conns()
        with sql_transaction(sqlite_conn):
            assert id(sqlite_conn) in _suspended_conns()
        assert id(sqlite_conn) not in _suspended_conns()

    def test_suspension_skips_repo_auto_commit(self, get_sqlite_conn, sqlite_conn):
        repo = GenericSQLRepository(get_sqlite_conn, dialect=SQLDialect.SQLITE)
        repo._execute("CREATE TABLE t (v INTEGER)")

        with sql_transaction(sqlite_conn):
            repo._execute("INSERT INTO t VALUES (%s)", (1,))
            repo._execute("INSERT INTO t VALUES (%s)", (2,))
        cur = sqlite_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM t")
        assert cur.fetchone()[0] == 2

    def test_exception_rolls_back_the_whole_scope(self, get_sqlite_conn, sqlite_conn):
        """On exception inside the scope, no writes survive."""
        repo = GenericSQLRepository(get_sqlite_conn, dialect=SQLDialect.SQLITE)
        repo._execute("CREATE TABLE t (v INTEGER)")

        with pytest.raises(RuntimeError):
            with sql_transaction(sqlite_conn):
                repo._execute("INSERT INTO t VALUES (%s)", (1,))
                repo._execute("INSERT INTO t VALUES (%s)", (2,))
                raise RuntimeError("boom")

        cur = sqlite_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM t")
        assert cur.fetchone()[0] == 0

    def test_reentrant_transaction_is_noop_on_inner(self, sqlite_conn):
        """Nested sql_transaction on the same conn doesn't double-commit."""
        commits: list[None] = []
        rollbacks: list[None] = []
        mock = MagicMock()
        mock.commit.side_effect = lambda: commits.append(None)
        mock.rollback.side_effect = lambda: rollbacks.append(None)

        with sql_transaction(mock):
            with sql_transaction(mock):
                pass
            assert len(commits) == 0  # Inner did not commit.
        assert len(commits) == 1  # Outer committed once.

    def test_suspended_cleaned_up_on_exception(self, sqlite_conn):
        """Exception still clears the suspension marker — no leakage."""
        with pytest.raises(ValueError):
            with sql_transaction(sqlite_conn):
                raise ValueError("fail")
        assert id(sqlite_conn) not in _suspended_conns()


# ---------------------------------------------------------------------------
# SchemaVersionManager
# ---------------------------------------------------------------------------


class TestSchemaVersionManagerBehavior:
    """SchemaVersionManager ensures DDL runs exactly once per (repo, version)."""

    def test_ensure_applies_ddl_on_first_call(
        self, get_sqlite_conn, sqlite_conn, sqlite_dialect
    ):
        manager = SchemaVersionManager(get_sqlite_conn, dialect=sqlite_dialect)
        manager.ensure(
            "repo_x",
            1,
            ["CREATE TABLE repo_x (id INTEGER PRIMARY KEY)"],
        )
        cur = sqlite_conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='repo_x'"
        )
        assert cur.fetchone() == ("repo_x",)

    def test_ensure_is_idempotent(self, get_sqlite_conn, sqlite_conn, sqlite_dialect):
        manager = SchemaVersionManager(get_sqlite_conn, dialect=sqlite_dialect)
        ddl = ["CREATE TABLE repo_y (id INTEGER PRIMARY KEY)"]
        manager.ensure("repo_y", 1, ddl)
        # Second call must not re-run DDL — a raw DROP + second ensure
        # proves the cache short-circuits.
        cur = sqlite_conn.cursor()
        cur.execute("DROP TABLE repo_y")
        sqlite_conn.commit()
        manager.ensure("repo_y", 1, ddl)  # Should be no-op (cached).

        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='repo_y'"
        )
        assert cur.fetchone() is None, (
            "DDL should not re-run once the (repo, version) was recorded"
        )

    def test_ensure_records_version_in_bookkeeping_table(
        self, get_sqlite_conn, sqlite_conn, sqlite_dialect
    ):
        manager = SchemaVersionManager(get_sqlite_conn, dialect=sqlite_dialect)
        manager.ensure(
            "repo_z",
            3,
            ["CREATE TABLE repo_z (id INTEGER PRIMARY KEY)"],
        )
        cur = sqlite_conn.cursor()
        cur.execute(
            "SELECT version FROM baldur_schema_version WHERE repo_name = ?",
            ("repo_z",),
        )
        assert cur.fetchone() == (3,)

    def test_ensure_upgrades_when_version_advances(
        self, get_sqlite_conn, sqlite_conn, sqlite_dialect
    ):
        """Higher version triggers re-application of DDL."""
        manager = SchemaVersionManager(get_sqlite_conn, dialect=sqlite_dialect)
        manager.ensure("repo_w", 1, ["CREATE TABLE repo_w (a INTEGER)"])

        # Simulate process restart: clear in-process cache.
        SchemaVersionManager._reset_applied_cache()

        manager.ensure("repo_w", 2, ["CREATE TABLE IF NOT EXISTS repo_w2 (b INTEGER)"])

        cur = sqlite_conn.cursor()
        cur.execute(
            "SELECT version FROM baldur_schema_version WHERE repo_name = ?",
            ("repo_w",),
        )
        assert cur.fetchone() == (2,)

    def test_ensure_is_skipped_when_schema_not_managed(
        self, get_sqlite_conn, sqlite_conn, sqlite_dialect, monkeypatch
    ):
        """schema_managed=False means DDL is the user's responsibility."""
        from baldur.settings import sql as sql_settings_mod

        # Force a fresh SQLSettings with schema_managed=False.
        sql_settings_mod.reset_sql_settings()
        monkeypatch.setenv("BALDUR_SQL_SCHEMA_MANAGED", "0")
        # A fresh repo reads the env-overridden setting.
        repo = GenericSQLRepository(get_sqlite_conn, dialect=sqlite_dialect)
        repo._ensure_schema("some_repo", 1, ["CREATE TABLE should_not_exist (a INT)"])

        cur = sqlite_conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='should_not_exist'"
        )
        assert cur.fetchone() is None

    def test_concurrent_ensure_results_in_single_application(
        self, get_sqlite_conn, sqlite_conn, sqlite_dialect
    ):
        """Multi-thread ensure() on the same repo runs DDL at most once."""
        manager = SchemaVersionManager(get_sqlite_conn, dialect=sqlite_dialect)
        ddl = ["CREATE TABLE IF NOT EXISTS concurrent_repo (id INTEGER PRIMARY KEY)"]

        errors: list[Exception] = []

        def worker():
            try:
                manager.ensure("concurrent_repo", 1, ddl)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        cur = sqlite_conn.cursor()
        cur.execute(
            "SELECT version FROM baldur_schema_version WHERE repo_name = ?",
            ("concurrent_repo",),
        )
        assert cur.fetchone() == (1,)


# ---------------------------------------------------------------------------
# PR2 review fix #3 — _execute_returning_id POSTGRESQL branch
# ---------------------------------------------------------------------------


class TestExecuteReturningIdPostgresBranchBehavior:
    """PG path appends ``RETURNING id`` and reads via ``fetchone()``."""

    def _build_pg_repo(self, mock_cursor):
        """Compose a POSTGRESQL repo over a mocked DB-API connection."""
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        repo = GenericSQLRepository(
            lambda: mock_conn,
            dialect=SQLDialect.POSTGRESQL,
        )
        return repo, mock_conn

    def test_appends_returning_id_when_absent(self):
        """Bare INSERT gains ``RETURNING id`` suffix on PG."""
        cursor = MagicMock()
        cursor.fetchone.return_value = (123,)
        repo, _ = self._build_pg_repo(cursor)

        new_id = repo._execute_returning_id("INSERT INTO t (x) VALUES (%s)", (1,))

        assert new_id == 123
        executed_sql = cursor.execute.call_args[0][0]
        assert executed_sql.endswith(" RETURNING id")

    def test_skips_returning_when_already_present(self):
        """Statement already containing ``RETURNING`` is left untouched."""
        cursor = MagicMock()
        cursor.fetchone.return_value = (7,)
        repo, _ = self._build_pg_repo(cursor)

        repo._execute_returning_id("INSERT INTO t (x) VALUES (%s) RETURNING pk", (1,))

        executed_sql = cursor.execute.call_args[0][0]
        assert executed_sql.count("RETURNING") == 1

    def test_skips_returning_when_already_present_case_insensitive(self):
        """Case-insensitive ``returning`` detection — no double-append."""
        cursor = MagicMock()
        cursor.fetchone.return_value = (8,)
        repo, _ = self._build_pg_repo(cursor)

        repo._execute_returning_id("insert into t (x) values (%s) returning pk", (1,))

        executed_sql = cursor.execute.call_args[0][0].upper()
        assert executed_sql.count("RETURNING") == 1

    def test_returns_none_when_pg_fetchone_returns_none(self):
        """Empty cursor result on PG → returned id is None."""
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        repo, _ = self._build_pg_repo(cursor)

        new_id = repo._execute_returning_id("INSERT INTO t DEFAULT VALUES")

        assert new_id is None


# ---------------------------------------------------------------------------
# PR2 review fix #7 — Lazy schema bootstrap
# ---------------------------------------------------------------------------


class TestLazySchemaBootstrapBehavior:
    """Schema DDL runs on first connection borrow, not on __init__."""

    def test_init_does_not_borrow_connection(self):
        """Constructing the repo must not call get_connection."""
        get_conn = MagicMock()

        repo = GenericSQLRepository(
            get_conn,
            dialect=SQLDialect.SQLITE,
            schema=("lazy_repo", 1, lambda d: ["CREATE TABLE lazy_repo (id INTEGER)"]),
        )

        assert get_conn.call_count == 0
        assert repo._schema_ready is False

    def test_first_borrow_runs_ddl_once(self, get_sqlite_conn, sqlite_conn):
        """The first DB-touching call applies DDL exactly once."""
        ddl_calls: list[SQLDialect] = []

        def ddl_factory(dialect):
            ddl_calls.append(dialect)
            return ["CREATE TABLE lazy_repo (id INTEGER PRIMARY KEY)"]

        repo = GenericSQLRepository(
            get_sqlite_conn,
            dialect=SQLDialect.SQLITE,
            schema=("lazy_repo", 1, ddl_factory),
        )
        assert repo._schema_ready is False

        # First borrow — schema should be applied.
        repo._fetch_one("SELECT COUNT(*) FROM lazy_repo")

        assert repo._schema_ready is True
        assert ddl_calls == [SQLDialect.SQLITE]
        cur = sqlite_conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='lazy_repo'"
        )
        assert cur.fetchone() == ("lazy_repo",)

    def test_subsequent_borrow_does_not_re_run_ddl(self, get_sqlite_conn):
        """Once ready, the ddl_factory is not invoked again."""
        ddl_calls: list[SQLDialect] = []

        def ddl_factory(dialect):
            ddl_calls.append(dialect)
            return ["CREATE TABLE lazy_repo2 (id INTEGER PRIMARY KEY)"]

        repo = GenericSQLRepository(
            get_sqlite_conn,
            dialect=SQLDialect.SQLITE,
            schema=("lazy_repo2", 1, ddl_factory),
        )

        repo._fetch_one("SELECT COUNT(*) FROM lazy_repo2")
        repo._fetch_one("SELECT COUNT(*) FROM lazy_repo2")
        repo._fetch_one("SELECT COUNT(*) FROM lazy_repo2")

        # ddl_factory invoked exactly once across multiple ops.
        assert ddl_calls == [SQLDialect.SQLITE]

    def test_schema_none_keeps_ready_true_from_start(self):
        """A repo declared without schema is ready immediately (legacy path)."""
        repo = GenericSQLRepository(
            lambda: MagicMock(),
            dialect=SQLDialect.SQLITE,
            schema=None,
        )
        assert repo._schema_ready is True


# ---------------------------------------------------------------------------
# PR2 review fix #8 — dialect_upsert_clause helper
# ---------------------------------------------------------------------------


class TestDialectUpsertClauseContract:
    """Per-dialect upsert tail strings (design contract)."""

    def test_postgresql_uses_on_conflict_excluded(self):
        clause = dialect_upsert_clause(
            SQLDialect.POSTGRESQL,
            conflict_cols=["id"],
            update_cols=["count", "status"],
        )
        assert clause == (
            "ON CONFLICT (id) DO UPDATE SET "
            "count = EXCLUDED.count, status = EXCLUDED.status"
        )

    def test_mysql_uses_on_duplicate_key_values(self):
        clause = dialect_upsert_clause(
            SQLDialect.MYSQL,
            conflict_cols=["id"],
            update_cols=["count", "status"],
        )
        assert clause == (
            "ON DUPLICATE KEY UPDATE count = VALUES(count), status = VALUES(status)"
        )

    def test_sqlite_uses_on_conflict_excluded_lowercase(self):
        clause = dialect_upsert_clause(
            SQLDialect.SQLITE,
            conflict_cols=["id"],
            update_cols=["count", "status"],
        )
        assert clause == (
            "ON CONFLICT(id) DO UPDATE SET "
            "count = excluded.count, status = excluded.status"
        )

    def test_postgresql_supports_composite_conflict_cols(self):
        clause = dialect_upsert_clause(
            SQLDialect.POSTGRESQL,
            conflict_cols=["tenant", "id"],
            update_cols=["count"],
        )
        assert "ON CONFLICT (tenant, id)" in clause


# ---------------------------------------------------------------------------
# PR2 review fix #12 — Public dialect_* helper aliases
# ---------------------------------------------------------------------------


class TestPublicDialectHelpersContract:
    """Renamed dialect helpers are part of the public surface."""

    def test_public_helpers_listed_in_all(self):
        """``__all__`` exposes the four public dialect helpers."""
        from baldur.adapters.sql import base as base_mod

        for name in (
            "dialect_bigserial",
            "dialect_json_type",
            "dialect_timestamp_type",
            "dialect_upsert_clause",
        ):
            assert name in base_mod.__all__

    def test_public_helpers_are_importable(self):
        """The renamed names import cleanly from base."""
        from baldur.adapters.sql.base import (
            dialect_bigserial as _b,
        )
        from baldur.adapters.sql.base import (
            dialect_json_type as _j,
        )
        from baldur.adapters.sql.base import (
            dialect_timestamp_type as _t,
        )
        from baldur.adapters.sql.base import (
            dialect_upsert_clause as _u,
        )

        # Smoke: each helper produces a non-empty string for sqlite.
        assert _b(SQLDialect.SQLITE)
        assert _j(SQLDialect.SQLITE)
        assert _t(SQLDialect.SQLITE)
        assert _u(SQLDialect.SQLITE, conflict_cols=["id"], update_cols=["x"])

    def test_dialect_helpers_round_trip_smoke(self):
        """Per-dialect type fragments include expected dialect-specific tokens."""
        # PG-specific tokens
        assert "JSONB" in dialect_json_type(SQLDialect.POSTGRESQL)
        assert "TIMESTAMPTZ" in dialect_timestamp_type(SQLDialect.POSTGRESQL)
        assert "BIGSERIAL" in dialect_bigserial(SQLDialect.POSTGRESQL)
        # MySQL-specific tokens
        assert "JSON" in dialect_json_type(SQLDialect.MYSQL)
        assert "AUTO_INCREMENT" in dialect_bigserial(SQLDialect.MYSQL)
        # sqlite fallbacks
        assert "TEXT" in dialect_json_type(SQLDialect.SQLITE)
        assert "AUTOINCREMENT" in dialect_bigserial(SQLDialect.SQLITE)


# ---------------------------------------------------------------------------
# Timestamp read normalization
# ---------------------------------------------------------------------------


class TestDatetimeFromDbContract:
    """_dt_from_db always yields a timezone-aware UTC datetime.

    MySQL/MariaDB store DATETIME(6), which carries no zone, and sqlite may
    hold a naive ISO string. Both used to come back naive while the memory
    and Redis adapters returned aware values for the same DTO field, so a
    caller comparing one against utc_now() raised TypeError on those
    backends only.
    """

    def test_naive_datetime_is_stamped_utc(self):
        result = GenericSQLRepository._dt_from_db(datetime(2026, 4, 1, 12, 0))
        assert result == datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
        assert result.tzinfo is not None

    def test_aware_datetime_is_returned_unchanged(self):
        value = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
        assert GenericSQLRepository._dt_from_db(value) == value

    def test_naive_iso_string_is_stamped_utc(self):
        result = GenericSQLRepository._dt_from_db("2026-04-01T12:00:00")
        assert result == datetime(2026, 4, 1, 12, 0, tzinfo=UTC)

    def test_rfc3339_string_keeps_its_offset(self):
        result = GenericSQLRepository._dt_from_db("2026-04-01T12:00:00+00:00")
        assert result == datetime(2026, 4, 1, 12, 0, tzinfo=UTC)

    def test_bytes_are_decoded_then_stamped(self):
        result = GenericSQLRepository._dt_from_db(b"2026-04-01T12:00:00")
        assert result == datetime(2026, 4, 1, 12, 0, tzinfo=UTC)

    def test_result_is_comparable_against_utc_now(self):
        """The regression this normalization exists to prevent.

        A DATETIME(6) read compared against an aware cutoff used to raise
        "can't compare offset-naive and offset-aware datetimes" inside the
        compressed-entry lifecycle sweep.
        """
        from baldur.utils.time import utc_now

        stale_at = GenericSQLRepository._dt_from_db(datetime(2026, 4, 1, 12, 0))
        assert stale_at < utc_now()

    def test_none_and_garbage_return_none(self):
        assert GenericSQLRepository._dt_from_db(None) is None
        assert GenericSQLRepository._dt_from_db("not-a-timestamp") is None
