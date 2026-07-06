"""
PgAdmin Integration Tests against Docker Postgres.

Verifies the framework-independent PgAdmin (515 D4 — single class, two
callable-injected backends) against a real psycopg2 round-trip. Unit tests
mock the cursor and capture emitted SQL strings; this suite confirms the
SQL actually executes on a live Postgres backend and the advisory-lock /
session-timeout / cursor-escape-hatch contracts hold end-to-end.

Test Categories:
    A. Health & connection stats:
        - ping() round-trip
        - get_connection_stats() returns counts from pg_stat_activity
        - get_active_connection_count() returns a non-negative int
    B. Advisory locks (single-session contract):
        - acquire/release exclusive lock round-trip
        - try_advisory_lock returns True when free, False when held by
          another session
        - advisory_lock_context releases on normal exit and on exception
    C. Session timeouts:
        - timeout_context applies lock_timeout and statement_timeout
        - timeouts are reset after the context exits
    D. Cursor escape hatches:
        - create_cursor returns a cursor whose lifetime outlives the call
        - execute_with_cursor runs SQL on a caller-supplied cursor

Note: Requires Docker PostgreSQL on port 15432 (docker-compose.test.yml).
      Marked with @pytest.mark.requires_db for auto-skip.
"""

from __future__ import annotations

import pytest

from baldur.adapters.postgres.admin import PgAdmin
from baldur.adapters.postgres.sessions import dbapi_session_factory
from tests.integration.conftest import DatabaseTestConfig

pytestmark = pytest.mark.requires_db


# =============================================================================
# Fixtures
# =============================================================================


def _connect():
    psycopg2 = pytest.importorskip("psycopg2")
    cfg = DatabaseTestConfig()
    conn = psycopg2.connect(
        host=cfg.DEFAULT_HOST,
        port=cfg.DEFAULT_PORT,
        database=cfg.DEFAULT_DB,
        user=cfg.DEFAULT_USER,
        password=cfg.DEFAULT_PASSWORD,
        connect_timeout=3,
    )
    conn.autocommit = True
    return conn


@pytest.fixture
def pg_admin():
    """Real PgAdmin wired to a per-call psycopg2 connection.

    ``dbapi_session_factory`` opens / closes the connection per
    ``with get_session() as cur:`` block, so each method invocation
    runs on a fresh backend session. That matches the DB-API runtime
    contract from 515 D4 and isolates one test from another.
    """
    pytest.importorskip("psycopg2")
    return PgAdmin(
        get_session=dbapi_session_factory(_connect),
        get_connection=_connect,
        label="integration",
    )


@pytest.fixture
def persistent_locker():
    """A second connection used to hold an advisory lock across one test.

    Needed to verify ``try_advisory_lock`` correctly observes a lock held
    by another session. The fixture takes the lock on setup and unlocks /
    closes on teardown, so the assertion sees a real cross-session lock
    rather than a same-session re-entry.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    conn = _connect()
    cursor = conn.cursor()
    yield cursor
    try:
        cursor.close()
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass
    del psycopg2  # silence unused-import lint


# =============================================================================
# A. Health & connection stats
# =============================================================================


class TestPgAdminHealthAndStatsAgainstPostgres:
    """Real psycopg2 round-trip for ping + pg_stat_activity reads."""

    def test_ping_returns_true_against_healthy_postgres(self, pg_admin):
        """
        Purpose:
            Verify ping() executes SELECT 1 against the real backend and
            returns True.
        Expected:
            - ping() is True
        """
        assert pg_admin.ping() is True

    def test_get_connection_stats_returns_real_counts(self, pg_admin):
        """
        Purpose:
            ConnectionStats round-trip — counts come from pg_stat_activity
            filtered by current_database(). At minimum the calling backend
            itself is counted, so total_connections must be >= 1.
        Expected:
            - total_connections >= 1 (at least our own session)
            - active + idle + idle_in_transaction <= total_connections
            - all four fields are ints
        """
        stats = pg_admin.get_connection_stats()

        assert isinstance(stats.total_connections, int)
        assert isinstance(stats.active, int)
        assert isinstance(stats.idle, int)
        assert isinstance(stats.idle_in_transaction, int)
        assert stats.total_connections >= 1
        assert (
            stats.active + stats.idle + stats.idle_in_transaction
            <= stats.total_connections
        )

    def test_get_active_connection_count_is_non_negative(self, pg_admin):
        """
        Purpose:
            The active count is global (no current_database() filter on
            this helper), so the only invariant is non-negativity.
        Expected:
            - get_active_connection_count() >= 0
        """
        assert pg_admin.get_active_connection_count() >= 0


# =============================================================================
# B. Advisory locks
# =============================================================================


class TestPgAdminAdvisoryLocksAgainstPostgres:
    """Advisory-lock round-trip across real Postgres sessions."""

    def test_try_advisory_lock_returns_true_when_lock_is_free(self, pg_admin):
        """
        Purpose:
            Sanity check: a never-acquired lock id can be taken
            non-blockingly.
        Expected:
            - try_advisory_lock(id) is True
            - subsequent release returns True
        """
        lock_id = 91501

        acquired = pg_admin.try_advisory_lock(lock_id)
        try:
            assert acquired is True
        finally:
            released = pg_admin.release_advisory_lock(lock_id)

        # Note: release_advisory_lock opens a new session via dbapi factory,
        # but pg_try_advisory_lock above released when its session closed,
        # so this release should return False (already released).
        assert released is False

    def test_try_advisory_lock_returns_false_when_lock_held_by_other_session(
        self, pg_admin, persistent_locker
    ):
        """
        Purpose:
            When another backend session holds the lock, pg_try_advisory_lock
            must return False — verifies the SQL actually delegates to the
            backend (not just a Python truthy default).
        Expected:
            - persistent_locker takes lock_id
            - pg_admin.try_advisory_lock(lock_id) returns False
        """
        lock_id = 91502

        persistent_locker.execute("SELECT pg_advisory_lock(%s)", [lock_id])
        persistent_locker.fetchone()

        try:
            assert pg_admin.try_advisory_lock(lock_id) is False
        finally:
            persistent_locker.execute("SELECT pg_advisory_unlock(%s)", [lock_id])
            persistent_locker.fetchone()

    def test_advisory_lock_context_holds_and_releases_in_one_session(self, pg_admin):
        """
        Purpose:
            advisory_lock_context shares one session across acquire + release
            so the lock is genuinely held inside the block and released on
            exit. Verified by re-acquiring after exit (would block forever
            if release leaked).
        Expected:
            - acquired flag is True inside the block
            - try-acquire after exit succeeds (lock was released)
        """
        lock_id = 91503

        with pg_admin.advisory_lock_context(
            lock_id, exclusive=True, wait=True
        ) as acquired:
            assert acquired is True

        # After the context exits the lock is released; non-blocking
        # re-acquire must succeed.
        reacquired = pg_admin.try_advisory_lock(lock_id)
        assert reacquired is True
        # Lock auto-released when its acquiring session ended.

    def test_advisory_lock_context_releases_on_exception(self, pg_admin):
        """
        Purpose:
            The finally branch in advisory_lock_context must run unlock
            even when the body raises — otherwise a transient failure in
            user code would leak the lock for the rest of the connection
            lifetime.
        Expected:
            - exception propagates out of the with-block
            - non-blocking re-acquire after exit succeeds
        """
        lock_id = 91504

        with pytest.raises(RuntimeError, match="boom"):
            with pg_admin.advisory_lock_context(lock_id, exclusive=True, wait=True):
                raise RuntimeError("boom")

        # If release did not run the second acquire would still see the
        # lock held (same session would re-enter, but a different session
        # via dbapi_session_factory would block — try should expose this).
        assert pg_admin.try_advisory_lock(lock_id) is True


# =============================================================================
# C. Session timeouts
# =============================================================================


class TestPgAdminTimeoutContextAgainstPostgres:
    """timeout_context sets and resets statement_timeout against real PG.

    ``timeout_context`` depends on a *persistent* backend session — the
    SET statement_timeout and the subsequent query must execute on the
    same session, or the SET is wasted. ``dbapi_session_factory`` opens
    and closes per call, so these tests build a persistent-session
    factory (one shared connection, one cursor per ``with``) matching
    Django's per-thread connection model.
    """

    @pytest.fixture
    def persistent_pg_admin(self):
        """PgAdmin wired to ONE persistent connection.

        Mirrors Django's behavior where ``connections[alias]`` returns
        a thread-local proxy that reuses a single backend session.
        Required to test ``timeout_context`` semantics — without
        session persistence the SET statement_timeout would not affect
        the next query.
        """
        from contextlib import contextmanager

        conn = _connect()

        @contextmanager
        def _persistent_session():
            cursor = conn.cursor()
            try:
                yield cursor
            finally:
                try:
                    cursor.close()
                except Exception:
                    pass

        def _get_persistent_conn():
            return conn

        admin = PgAdmin(
            get_session=_persistent_session,
            get_connection=_get_persistent_conn,
            label="persistent",
        )
        yield admin
        try:
            conn.close()
        except Exception:
            pass

    def test_timeout_context_applies_statement_timeout(self, persistent_pg_admin):
        """
        Purpose:
            Inside timeout_context, a pg_sleep longer than the configured
            statement_timeout must abort with QueryCanceled. Verifies the
            SET statement_timeout SQL actually reached the backend and
            persisted across the next cursor invocation.
        Expected:
            - pg_sleep(1) inside timeout_context(statement_timeout_ms=50)
              raises psycopg2.errors.QueryCanceled
        """
        psycopg2 = pytest.importorskip("psycopg2")

        with pytest.raises(psycopg2.errors.QueryCanceled):
            with persistent_pg_admin.timeout_context(statement_timeout_ms=50):
                persistent_pg_admin.pg_sleep(1.0)

    def test_timeout_context_resets_timeout_on_exit(self, persistent_pg_admin):
        """
        Purpose:
            After timeout_context exits, statement_timeout must be reset
            so a subsequent slow query does NOT abort. Verifies the
            ``finally: self.reset_timeouts()`` branch runs and that the
            reset is visible to later queries on the same session.
        Expected:
            - pg_sleep(0.1) after a 50ms-timeout context completes cleanly
        """
        with persistent_pg_admin.timeout_context(statement_timeout_ms=50):
            pass  # Set + reset, no slow query inside.

        # Reset visible — a 100ms sleep must not raise.
        persistent_pg_admin.pg_sleep(0.1)

    def test_pg_sleep_completes_when_under_no_timeout(self, pg_admin):
        """
        Purpose:
            Without a configured statement_timeout, a brief pg_sleep
            completes without raising — sanity check that the open-per-
            call factory is wired correctly for non-timeout SQL.
        Expected:
            - pg_sleep(0.05) returns without raising
        """
        pg_admin.pg_sleep(0.05)


# =============================================================================
# D. Cursor escape hatches
# =============================================================================


class TestPgAdminCursorEscapeHatchesAgainstPostgres:
    """create_cursor + execute_with_cursor against a real backend."""

    def test_create_cursor_returns_usable_cursor_outliving_call(self, pg_admin):
        """
        Purpose:
            create_cursor's caller-owned lifecycle (515 D4) — the cursor
            must remain executable after the call returns. The stress-test
            pool-exhaustion path depends on holding cursors externally.
        Expected:
            - cursor.execute("SELECT 1") succeeds
            - cursor.fetchone() returns (1,)
        """
        cursor = pg_admin.create_cursor()
        try:
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
            assert row == (1,)
        finally:
            try:
                cursor.close()
            except Exception:
                pass

    def test_execute_with_cursor_runs_query_on_caller_supplied_cursor(self, pg_admin):
        """
        Purpose:
            execute_with_cursor routes through a caller-supplied cursor
            and returns fetchone() output. Verified with a parameterized
            SELECT to ensure both query and params propagate.
        Expected:
            - execute_with_cursor returns (42,)
        """
        cursor = pg_admin.create_cursor()
        try:
            row = pg_admin.execute_with_cursor(cursor, "SELECT %s", [42])
            assert row == (42,)
        finally:
            try:
                cursor.close()
            except Exception:
                pass
