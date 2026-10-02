"""Fixtures shared by the DLQ repository contract suites in this directory.

``dlq_store`` is one ``FailedOperationRepository`` per adapter the contract
holds on: the in-memory store, the SQL store over a hermetic sqlite database,
and the Redis store over a backend whose raw client honours WATCH / MULTI /
EXEC (``tests.factories.redis_watch``) — once with Redis answering and once
degraded, where every lifecycle write takes its read-modify-write path.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from baldur.interfaces.repositories import FailedOperationRepository
from tests.factories.redis_watch import WatchingRedisBackend

DLQ_ADAPTERS = ("memory", "sql", "redis", "redis_degraded")


@dataclass
class DLQStore:
    """One adapter under the contract; ``backend`` is set for the Redis ones."""

    name: str
    repo: FailedOperationRepository
    backend: WatchingRedisBackend | None = None


@pytest.fixture(params=DLQ_ADAPTERS)
def dlq_store(request, monkeypatch) -> Iterator[DLQStore]:
    """A fresh DLQ repository of each adapter kind."""
    name = request.param
    if name == "memory":
        from baldur.adapters.memory import InMemoryFailedOperationRepository

        yield DLQStore(name, InMemoryFailedOperationRepository())
        return

    if name == "sql":
        from baldur.adapters.sql.base import SchemaVersionManager
        from baldur.adapters.sql.failed_operation import SQLFailedOperationRepository
        from baldur.settings.sql import reset_sql_settings

        monkeypatch.setenv("BALDUR_SQL_DSN", "sqlite:///:memory:")
        reset_sql_settings()
        SchemaVersionManager._reset_applied_cache()
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        try:
            yield DLQStore(name, SQLFailedOperationRepository(lambda: conn))
        finally:
            conn.close()
            reset_sql_settings()
            SchemaVersionManager._reset_applied_cache()
        return

    from baldur.adapters.redis.dlq import RedisDLQRepository

    backend = WatchingRedisBackend()
    backend.redis_up = name == "redis"
    repo = RedisDLQRepository(backend, pod_id="pod", pid=1, run_nonce="nonce")
    yield DLQStore(name, repo, backend)
