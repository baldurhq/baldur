"""RedisAuditBuffer._graceful_shutdown() re-runnability pin.

Pins the property both production coverage paths rely on (atexit flush
on polite exits, chained signal re-delivery flush on defer-exit): a
second _graceful_shutdown() call flushes entries added to the fallback
buffer AFTER the first call's flush+clear — the buffer remains usable
across runs, it is not torn down.

FakeRedis pattern precedent: test_redis_batch.py; this fake applies
pipeline commands to its store on execute() so receipt is assertable.
"""
# traceability: 598 D6 / SC4 (status-quo coverage pin)

from __future__ import annotations

from typing import Any

import pytest


class _ApplyingPipeline:
    """Pipeline fake that applies lpush into the shared store on execute()."""

    def __init__(self, store: dict[str, list[str]]) -> None:
        self._store = store
        self._ops: list[tuple[Any, ...]] = []

    def lpush(self, key: str, *values: str) -> _ApplyingPipeline:
        self._ops.append(("lpush", key, values))
        return self

    def expire(self, key: str, ttl: int) -> _ApplyingPipeline:
        self._ops.append(("expire", key, ttl))
        return self

    def sadd(self, key: str, *values: str) -> _ApplyingPipeline:
        self._ops.append(("sadd", key, values))
        return self

    def execute(self) -> list[Any]:
        results: list[Any] = []
        for op in self._ops:
            if op[0] == "lpush":
                bucket = self._store.setdefault(op[1], [])
                for value in op[2]:
                    bucket.insert(0, value)
                results.append(len(bucket))
            else:
                results.append(True)
        self._ops = []
        return results


class _FakeRedis:
    """Minimal Redis fake covering the _log_batch_chunk surface."""

    def __init__(self) -> None:
        self.data: dict[str, list[str]] = {}

    def pipeline(self, transaction: bool = False) -> _ApplyingPipeline:
        return _ApplyingPipeline(self.data)

    def llen(self, key: str) -> int:
        return len(self.data.get(key, []))


@pytest.fixture
def fake_redis() -> _FakeRedis:
    """Fake Redis whose store records flushed payloads."""
    return _FakeRedis()


@pytest.fixture
def redis_buffer(fake_redis: _FakeRedis):
    """RedisAuditBuffer without shutdown hooks (atexit hygiene).

    Since 600 D4 ``_graceful_shutdown`` saves and restores
    ``logging.raiseExceptions`` itself (fix-356 mirror), so no manual
    teardown restore is needed for isolation.
    """
    from baldur.adapters.audit.redis_buffer import RedisAuditBuffer

    return RedisAuditBuffer(
        redis_client=fake_redis,
        fallback_adapter=None,
        enable_graceful_shutdown=False,
    )


class TestRedisBufferGracefulShutdownRerunBehavior:
    """_graceful_shutdown() is re-runnable: the fallback buffer stays
    usable after flush+clear, so a later run flushes new arrivals."""

    def test_second_shutdown_flushes_entries_added_after_first(
        self, redis_buffer, fake_redis
    ) -> None:
        """Entries stored after the first flush+clear reach Redis on re-run."""
        # Given — a first flush that drains and clears the fallback buffer
        redis_buffer._store_in_fallback_buffer([{"event": "first"}], "default")
        redis_buffer._graceful_shutdown()

        key = f"{redis_buffer._key_prefix}{{default}}:buffer"
        assert fake_redis.llen(key) == 1
        assert redis_buffer.get_fallback_buffer_size() == 0

        # When — new entries arrive after the clear, then a second run
        redis_buffer._store_in_fallback_buffer([{"event": "second"}], "default")
        redis_buffer._graceful_shutdown()

        # Then — the post-clear entry reached Redis and the buffer drained
        assert fake_redis.llen(key) == 2
        assert any("second" in payload for payload in fake_redis.data[key])
        assert redis_buffer.get_fallback_buffer_size() == 0

    def test_rerun_with_empty_fallback_buffer_is_noop(
        self, redis_buffer, fake_redis
    ) -> None:
        """Repeated runs on an empty fallback buffer write nothing."""
        redis_buffer._graceful_shutdown()
        redis_buffer._graceful_shutdown()

        assert fake_redis.data == {}
        assert redis_buffer.get_fallback_buffer_size() == 0
