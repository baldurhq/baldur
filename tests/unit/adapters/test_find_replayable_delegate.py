"""``find_replayable`` is now the unpaged view of ``find_replayable_page``.

Three adapters implement the page selector and inherit the unpaged one from the
contract, so the risk the rewrite carries is that a caller which never asked for
a page sees a *different selection* than it used to. Every existing consumer —
the operator console's batch replay, the priority sweep, the scheduled lane —
goes through this method.

So the assertions here are the selection contract, not today's exact rows:
PENDING, ``retry_count < max_retries``, the domain and failure-type filters
honoured, no more than ``limit``, oldest first, and no capture-source
narrowing (the source predicate is reachable only through the page selector —
a manual replay must not be silently scoped to one capture layer).

The same population is seeded into all three stores, so a divergence between
adapters shows up as one adapter failing a row the others pass.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.memory.failed_operation import InMemoryFailedOperationRepository
from baldur.adapters.sql.base import SchemaVersionManager
from baldur.adapters.sql.failed_operation import SQLFailedOperationRepository
from baldur.interfaces.repositories import FailedOperationStatus
from baldur.models.dlq import POLICY_CHAIN_CAPTURE_SOURCE
from baldur.settings.sql import reset_sql_settings
from tests.factories.redis import FakeSortedSetBackend
from tests.factories.time_helpers import freeze_time

PENDING = FailedOperationStatus.PENDING.value
REVIEW = FailedOperationStatus.REQUIRES_REVIEW.value
BASE = datetime(2026, 9, 5, 10, 0, 0, tzinfo=UTC)
PAYMENT = "payment_api"
POINT = "point_api"

# (label, domain, failure_type, status, retry_count, source, offset_seconds)
POPULATION = [
    (
        "eligible_timeout",
        PAYMENT,
        "TIMEOUT",
        PENDING,
        0,
        POLICY_CHAIN_CAPTURE_SOURCE,
        1,
    ),
    (
        "eligible_open_circuit",
        PAYMENT,
        "CIRCUIT_BREAKER_OPEN",
        PENDING,
        0,
        "middleware",
        2,
    ),
    ("spent_retries", PAYMENT, "TIMEOUT", PENDING, 5, POLICY_CHAIN_CAPTURE_SOURCE, 3),
    ("escalated", PAYMENT, "TIMEOUT", REVIEW, 0, POLICY_CHAIN_CAPTURE_SOURCE, 4),
    ("other_domain", POINT, "TIMEOUT", PENDING, 0, POLICY_CHAIN_CAPTURE_SOURCE, 5),
]


class _Store:
    """One seeded repository plus the label→id map the assertions read by."""

    def __init__(self, repo):
        self.repo = repo
        self.ids: dict[str, str] = {}

    def labels(self, entries) -> list[str]:
        by_id = {entry_id: label for label, entry_id in self.ids.items()}
        return [by_id[e.id] for e in entries]


def _seed_memory() -> _Store:
    store = _Store(InMemoryFailedOperationRepository())
    for label, domain, failure_type, status, retries, source, offset in POPULATION:
        at = (BASE + timedelta(seconds=offset)).strftime("%Y-%m-%d %H:%M:%S")
        with freeze_time(at):
            entry = store.repo.create(
                domain=domain,
                failure_type=failure_type,
                metadata={"source": source},
                retry_count=retries,
                max_retries=5,
            )
        if status != PENDING:
            store.repo.update_status(entry.id, status)
        store.ids[label] = entry.id
    return store


def _seed_sql(conn) -> _Store:
    store = _Store(SQLFailedOperationRepository(lambda: conn))
    for label, domain, failure_type, status, retries, source, offset in POPULATION:
        at = (BASE + timedelta(seconds=offset)).strftime("%Y-%m-%d %H:%M:%S")
        with freeze_time(at):
            entry = store.repo.create(
                domain=domain,
                failure_type=failure_type,
                metadata={"source": source},
                retry_count=retries,
                max_retries=5,
            )
        if status != PENDING:
            store.repo.update_status(entry.id, status)
        store.ids[label] = entry.id
    return store


def _seed_redis(backend) -> _Store:
    from baldur.adapters.redis.dlq import RedisDLQRepository
    from baldur.adapters.redis.dlq_query import RedisDLQQuery

    with patch.object(RedisDLQRepository, "__init__", lambda self, **kw: None):
        repo = RedisDLQRepository.__new__(RedisDLQRepository)
    repo._backend = backend
    repo._key_prefix = "dlq:"
    repo._pending_key = "dlq:pending"
    repo._entry_prefix = "dlq:entry:"
    repo._by_domain_prefix = "dlq:by_domain:"
    repo._status_prefix = "dlq:status:"
    repo._status_domain_prefix = "dlq:status_domain:"
    repo._all_key = "dlq:all"
    repo._domains_key = "dlq:domains"
    repo._known_domains = set()
    repo.query = RedisDLQQuery(repo)
    backend.is_degraded = False
    repo.query._warm_composite_if_needed = MagicMock(
        wraps=lambda *_args, **_kwargs: True
    )

    store = _Store(repo)
    for label, domain, failure_type, status, retries, source, offset in POPULATION:
        created_at = BASE + timedelta(seconds=offset)
        backend.set_blob(
            f"dlq:entry:{label}",
            json.dumps(
                {
                    "id": label,
                    "domain": domain,
                    "failure_type": failure_type,
                    "status": status,
                    "retry_count": retries,
                    "max_retries": 5,
                    "metadata": {"source": source},
                    "created_at": created_at.isoformat(),
                }
            ).encode("utf-8"),
        )
        score = created_at.timestamp()
        if status == PENDING:
            backend.zadd("dlq:pending", {label: score})
            backend.zadd(f"dlq:status_domain:pending:{domain}", {label: score})
        backend.zadd(f"dlq:by_domain:{domain}", {label: score})
        store.ids[label] = label
    return store


@pytest.fixture(params=["memory", "sql", "redis"])
def store(request, monkeypatch):
    """The same DLQ population, seeded into each backing store in turn."""
    if request.param == "memory":
        yield _seed_memory()
        return
    if request.param == "redis":
        yield _seed_redis(FakeSortedSetBackend())
        return

    monkeypatch.setenv("BALDUR_SQL_DSN", "sqlite:///:memory:")
    reset_sql_settings()
    SchemaVersionManager._reset_applied_cache()
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        yield _seed_sql(conn)
    finally:
        conn.close()
        reset_sql_settings()
        SchemaVersionManager._reset_applied_cache()


class TestFindReplayableDelegateBehavior:
    """The unpaged selection contract, identical on all three adapters."""

    def test_unfiltered_call_returns_every_eligible_entry_oldest_first(self, store):
        found = store.repo.find_replayable(max_retries=5)

        assert store.labels(found) == [
            "eligible_timeout",
            "eligible_open_circuit",
            "other_domain",
        ]

    def test_escalated_and_spent_entries_are_never_returned(self, store):
        found = store.repo.find_replayable(max_retries=5)

        labels = store.labels(found)
        assert "escalated" not in labels
        assert "spent_retries" not in labels

    def test_domain_filter_scopes_the_selection(self, store):
        found = store.repo.find_replayable(max_retries=5, domain=PAYMENT)

        assert store.labels(found) == ["eligible_timeout", "eligible_open_circuit"]

    def test_failure_type_filter_scopes_the_selection(self, store):
        found = store.repo.find_replayable(max_retries=5, failure_type="TIMEOUT")

        assert store.labels(found) == ["eligible_timeout", "other_domain"]

    def test_domain_and_failure_type_compose(self, store):
        found = store.repo.find_replayable(
            max_retries=5, domain=PAYMENT, failure_type="TIMEOUT"
        )

        assert store.labels(found) == ["eligible_timeout"]

    def test_limit_bounds_the_result_from_the_oldest_end(self, store):
        found = store.repo.find_replayable(max_retries=5, limit=1)

        assert store.labels(found) == ["eligible_timeout"]

    def test_lower_max_retries_narrows_eligibility(self, store):
        """The bound is exclusive: an entry at the bound is already spent."""
        found = store.repo.find_replayable(max_retries=0)

        assert found == []

    def test_selection_is_not_narrowed_to_one_capture_source(self, store):
        """The source predicate is reachable only through the page selector; a
        manual replay must still see middleware captures."""
        found = store.repo.find_replayable(max_retries=5, domain=PAYMENT)

        assert "eligible_open_circuit" in store.labels(found)

    def test_repeated_calls_return_the_same_selection(self, store):
        """No hidden cursor state: the unpaged view always starts from the
        oldest eligible entry."""
        first = store.repo.find_replayable(max_retries=5)
        second = store.repo.find_replayable(max_retries=5)

        assert store.labels(first) == store.labels(second)
