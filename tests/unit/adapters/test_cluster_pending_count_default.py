"""The interface default of ``get_cluster_pending_count_by_domain()`` (809 D3).

The strict pending count must never answer from a substituted view. The
interface default simply delegates to ``get_pending_count_by_domain()``, which
is right only where this process's view IS the store: the in-memory adapter
(one process is the store) and the SQL adapter (the database is, and a failed
query raises). These tests pin both halves of that claim — the same answer as
the ordinary count, and a driver failure that propagates instead of reading
as "nothing parked". The Redis override, which does hold a local view in front
of a shared store, has its own suite.

Verification techniques applied:
- Behavior parity: the strict and ordinary counts agree per domain, including
  a domain with nothing in it and entries that left pending
- Exception propagation: a SQL driver error reaches the caller
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from baldur.adapters.memory.failed_operation import InMemoryFailedOperationRepository
from baldur.adapters.sql.base import SchemaVersionManager
from baldur.adapters.sql.failed_operation import SQLFailedOperationRepository
from baldur.interfaces.repositories import FailedOperationStatus
from baldur.settings.sql import SQLDialect

DOMAINS = ["payment_api", "catalog_api", "absent_api"]


@pytest.fixture(autouse=True)
def _pristine_schema_cache() -> Iterator[None]:
    """A fresh sqlite handle must get its DDL, not inherit "already applied"."""
    SchemaVersionManager._reset_applied_cache()
    yield
    SchemaVersionManager._reset_applied_cache()


@pytest.fixture
def sqlite_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(params=["memory", "sqlite"])
def repository(request, sqlite_conn):
    """Each adapter that inherits the interface default."""
    if request.param == "memory":
        return InMemoryFailedOperationRepository()
    return SQLFailedOperationRepository(lambda: sqlite_conn, dialect=SQLDialect.SQLITE)


def _seed(repository) -> None:
    """Pending and non-pending entries across two domains."""
    for _ in range(3):
        repository.create(domain="payment_api", failure_type="TIMEOUT")
    left_pending = repository.create(domain="payment_api", failure_type="TIMEOUT")
    repository.update_status(left_pending.id, FailedOperationStatus.RESOLVED.value)
    repository.create(domain="catalog_api", failure_type="TIMEOUT")


class TestClusterPendingCountDefaultBehavior:
    """Where this process's view is the store, the strict count is the count."""

    @pytest.mark.parametrize("domain", DOMAINS)
    def test_cluster_pending_count_default_equals_the_pending_count(
        self, repository, domain
    ):
        """Same answer as the ordinary count for every domain."""
        _seed(repository)

        strict = repository.get_cluster_pending_count_by_domain(domain)

        assert strict == repository.get_pending_count_by_domain(domain)

    def test_cluster_pending_count_default_excludes_entries_that_left_pending(
        self, repository
    ):
        """Three of the four payment entries are still pending."""
        _seed(repository)

        assert repository.get_cluster_pending_count_by_domain("payment_api") == 3

    def test_cluster_pending_count_sql_driver_error_propagates(self, sqlite_conn):
        """A failed query raises; it never reads as "nothing is parked"."""
        # Given: a repository whose schema exists, then a dead connection.
        repository = SQLFailedOperationRepository(
            lambda: sqlite_conn, dialect=SQLDialect.SQLITE
        )
        repository.create(domain="payment_api", failure_type="TIMEOUT")
        sqlite_conn.close()

        # When / Then
        with pytest.raises(sqlite3.Error):
            repository.get_cluster_pending_count_by_domain("payment_api")
