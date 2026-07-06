"""SQL adapter force-acquire parity tests for DLQ force-redrive (607 D2/D3/G5).

Exercises ``SQLFailedOperationRepository.try_acquire_for_replay(force=True)``
against a real DB-API driver (stdlib sqlite3 ``:memory:`` — no infra), so the
widened conditional ``UPDATE ... WHERE status IN (pending, requires_review)``
plus the JSON ``metadata`` history stamp inside one transaction are verified
end-to-end, not just against the in-memory adapter.

The conditional-UPDATE / blob round-trip path cannot be reached by the
``InMemoryFailedOperationRepository``, hence an integration-tier test. sqlite
``:memory:`` keeps it infra-free and xdist-safe (the pattern
``test_cross_repo_transaction.py`` already relies on).
"""

from __future__ import annotations

import sqlite3

import pytest

from baldur.adapters.sql import SQLFailedOperationRepository
from baldur.adapters.sql.base import SchemaVersionManager
from baldur.interfaces.repositories import FailedOperationStatus
from baldur.settings.sql import reset_sql_settings

PENDING = FailedOperationStatus.PENDING.value
REPLAYING = FailedOperationStatus.REPLAYING.value
REQUIRES_REVIEW = FailedOperationStatus.REQUIRES_REVIEW.value
RESOLVED = FailedOperationStatus.RESOLVED.value


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
def repo():
    """SQLFailedOperationRepository over a shared in-memory sqlite connection."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        yield SQLFailedOperationRepository(lambda: conn)
    finally:
        conn.close()


def _at_cap_requires_review(repo, *, cap=2):
    """Create an at-cap entry parked in REQUIRES_REVIEW (the 606 terminal)."""
    entry = repo.create(
        domain="payment", failure_type="PG_TIMEOUT", retry_count=cap, max_retries=cap
    )
    repo.update_status(entry.id, status=REQUIRES_REVIEW)
    refreshed = repo.get_by_id(entry.id)
    assert refreshed.status == REQUIRES_REVIEW  # setup guard
    return refreshed


class TestSQLForceAcquire:
    """force=True conditional-UPDATE: widened status set + budget reset + scar."""

    def test_force_acquire_requires_review_resets_and_stamps(self, repo):
        entry = _at_cap_requires_review(repo, cap=2)

        acquired = repo.try_acquire_for_replay(entry.id, max_retries=2, force=True)

        assert acquired is not None
        assert acquired.status == REPLAYING
        assert acquired.retry_count == 1
        assert acquired.metadata["previous_total_retries"] == 2
        assert acquired.metadata["force_redrive_count"] == 1
        # The stamp is durable (read back through the JSON blob column).
        persisted = repo.get_by_id(entry.id)
        assert persisted.status == REPLAYING
        assert persisted.metadata["force_redrive_count"] == 1

    def test_force_acquire_pending_resets_budget(self, repo):
        entry = repo.create(
            domain="payment", failure_type="x", retry_count=1, max_retries=2
        )

        acquired = repo.try_acquire_for_replay(entry.id, max_retries=2, force=True)

        assert acquired is not None
        assert acquired.status == REPLAYING
        assert acquired.retry_count == 1
        assert acquired.metadata["previous_total_retries"] == 1

    def test_force_acquire_rejects_resolved(self, repo):
        entry = repo.create(domain="payment", failure_type="x", max_retries=2)
        repo.update_status(entry.id, status=RESOLVED)

        assert repo.try_acquire_for_replay(entry.id, max_retries=2, force=True) is None

    def test_force_acquire_missing_entry_returns_none(self, repo):
        assert repo.try_acquire_for_replay("999999", max_retries=2, force=True) is None


class TestSQLNormalAcquireUnchanged:
    """force=False contract is behaviour-identical (regression)."""

    def test_normal_acquire_pending_under_cap_increments(self, repo):
        entry = repo.create(
            domain="payment", failure_type="x", retry_count=0, max_retries=2
        )

        acquired = repo.try_acquire_for_replay(entry.id, max_retries=2)

        assert acquired is not None
        assert acquired.status == REPLAYING
        assert acquired.retry_count == 1
        assert "force_redrive_count" not in (acquired.metadata or {})

    def test_normal_acquire_at_cap_returns_none(self, repo):
        entry = repo.create(
            domain="payment", failure_type="x", retry_count=2, max_retries=2
        )

        assert repo.try_acquire_for_replay(entry.id, max_retries=2) is None

    def test_normal_acquire_requires_review_returns_none(self, repo):
        """force=False never acquires a REQUIRES_REVIEW entry (PENDING-only)."""
        entry = _at_cap_requires_review(repo, cap=2)

        assert repo.try_acquire_for_replay(entry.id, max_retries=2) is None
