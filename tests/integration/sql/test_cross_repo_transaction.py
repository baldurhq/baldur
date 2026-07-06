"""
Cross-repo transaction integration tests for 429 PR2.

Two baldur repos (DLQ + CircuitBreakerState) share the same
DB-API 2.0 connection; ``baldur.sql_transaction`` must commit the
whole scope atomically and roll back both repos' writes on
exception. The tests also cover the BALDUR_SQL_AUTOCOMMIT=1 escape
hatch and the zero-config factory resolved from BALDUR_SQL_DSN.

Test Categories:
    A. Cross-repo transaction scope (``sql_transaction``):
        - Commit persists writes from both repos atomically.
        - Exception rolls back both repos.
        - Sequential transactions on the same connection are independent.
        - Nested re-entry is a no-op; outer owns commit/rollback.
    B. Autocommit delegation escape hatch (``BALDUR_SQL_AUTOCOMMIT=1``):
        - Baldur does not call commit/rollback — the user owns them.
    C. Default connection factory:
        - ``build_connection_factory()`` resolves ``BALDUR_SQL_DSN`` from env
          and returns a usable sqlite3 connection.

Note: No infra required — stdlib sqlite3 ``:memory:`` shared between
repos simulates a single pooled connection. Supports parallel execution
with pytest-xdist.
"""

from __future__ import annotations

import sqlite3

import pytest

from baldur.adapters.sql import (
    SQLCircuitBreakerStateRepository,
    SQLFailedOperationRepository,
    sql_transaction,
)
from baldur.adapters.sql.base import SchemaVersionManager
from baldur.adapters.sql.connection import build_connection_factory
from baldur.interfaces.repositories import (
    CircuitBreakerStateEnum,
    FailedOperationStatus,
)
from baldur.settings.sql import reset_sql_settings


@pytest.fixture(autouse=True)
def _sqlite_env(monkeypatch):
    """Pin DSN to sqlite + reset settings/schema cache per test."""
    monkeypatch.setenv("BALDUR_SQL_DSN", "sqlite:///:memory:")
    reset_sql_settings()
    SchemaVersionManager._reset_applied_cache()
    yield
    reset_sql_settings()
    SchemaVersionManager._reset_applied_cache()


@pytest.fixture
def shared_conn():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def repos(shared_conn):
    """Two repos sharing the same connection handle."""
    get_conn = lambda: shared_conn  # noqa: E731
    return (
        SQLFailedOperationRepository(get_conn),
        SQLCircuitBreakerStateRepository(get_conn),
    )


class TestCrossRepoTransactionBehavior:
    """sql_transaction commits / rolls back two repos atomically."""

    def test_commit_persists_writes_from_both_repos(self, repos, shared_conn):
        dlq, cb = repos
        with sql_transaction(shared_conn):
            entry = dlq.create(domain="payment", failure_type="timeout")
            cb.atomic_force_open("openai", reason="cascade")
            # Both writes are still inside the transaction scope.

        # After commit both repos observe the writes.
        assert dlq.get_by_id(entry.id).status == FailedOperationStatus.PENDING.value
        assert (
            cb.get_by_service_name("openai").state == CircuitBreakerStateEnum.OPEN.value
        )

    def test_exception_rolls_back_both_repos(self, repos, shared_conn):
        dlq, cb = repos
        with pytest.raises(RuntimeError):
            with sql_transaction(shared_conn):
                dlq.create(domain="payment", failure_type="timeout")
                cb.atomic_force_open("openai", reason="x")
                raise RuntimeError("simulated failure")

        # Neither repo's write survived.
        stats = dlq.get_statistics()
        assert stats["total"] == 0
        assert cb.get_by_service_name("openai") is None

    def test_sequential_transactions_are_independent(self, repos, shared_conn):
        """A second transaction on the same conn is not polluted by the first."""
        dlq, cb = repos
        with sql_transaction(shared_conn):
            entry_a = dlq.create(domain="payment", failure_type="timeout")

        # Independent second txn — same conn, re-entered cleanly.
        with sql_transaction(shared_conn):
            entry_b = dlq.create(domain="payment", failure_type="http_5xx")
            cb.atomic_force_open("openai", reason="y")

        assert dlq.get_by_id(entry_a.id) is not None
        assert dlq.get_by_id(entry_b.id) is not None
        assert cb.get_by_service_name("openai").state == "open"

    def test_nested_transaction_inner_does_not_commit_early(self, repos, shared_conn):
        """Re-entering sql_transaction is a no-op; outer owns commit/rollback."""
        dlq, cb = repos

        with pytest.raises(RuntimeError):
            with sql_transaction(shared_conn):
                dlq.create(domain="payment", failure_type="timeout")
                with sql_transaction(shared_conn):
                    cb.atomic_force_open("openai", reason="inner")
                # Raising AFTER inner "completes" must still roll everything
                # back — proves the inner did not commit its subset.
                raise RuntimeError("boom")

        assert dlq.get_statistics()["total"] == 0
        assert cb.get_by_service_name("openai") is None


class TestAutocommitDelegatedEscapeHatchBehavior:
    """BALDUR_SQL_AUTOCOMMIT=1 delegates commit/rollback to the user."""

    def test_delegated_autocommit_requires_user_commit(self, monkeypatch, shared_conn):
        monkeypatch.setenv("BALDUR_SQL_AUTOCOMMIT", "1")
        reset_sql_settings()
        get_conn = lambda: shared_conn  # noqa: E731
        dlq = SQLFailedOperationRepository(get_conn)

        entry = dlq.create(domain="payment", failure_type="timeout")

        # Baldur did NOT commit — the row exists in the current transaction
        # but is tied to the user's connection state. Since the same conn
        # is used for reads, visibility works transparently here (single
        # conn sqlite). The contract is that Baldur does not call commit.
        # Confirm by rolling back at the user layer and seeing the row vanish.
        shared_conn.rollback()
        assert dlq.get_by_id(entry.id) is None


class TestDefaultConnectionFactoryBehavior:
    """build_connection_factory reads BALDUR_SQL_DSN (singleton path)."""

    def test_factory_resolves_sqlite_from_env(self):
        """Zero-config path: env DSN → working sqlite connection."""
        factory = build_connection_factory()
        conn = factory()
        try:
            assert isinstance(conn, sqlite3.Connection)
            cur = conn.cursor()
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)
        finally:
            conn.close()
