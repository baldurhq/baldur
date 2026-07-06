"""Redis integration: ``StateBackend.compare_and_set`` atomicity (666 D1).

Memory/File CAS is lock-based (single-process) and cannot represent the
cross-process, transactional CAS that the multi-pod PRO config-write path
actually relies on. This exercises the real ``RedisStateBackend.compare_and_set``
against a live server: the ``WATCH``/``MULTI``/``EXEC`` optimistic transaction,
the version compare-in-Python, and the ``WatchError``-retry contention path that
the in-process backends structurally cannot exercise.

Requires a running Redis (``requires_redis`` auto-skip).
"""

from __future__ import annotations

import threading

import pytest

from baldur.core.state_backend import RedisStateBackend

pytestmark = pytest.mark.requires_redis

OCC = "__occ_version__"


@pytest.fixture
def redis_state_backend(redis_url):
    """A real RedisStateBackend on the test DB (flushed between tests)."""
    backend = RedisStateBackend(redis_url=redis_url, key_prefix="baldur:state:cas:")
    yield backend
    backend.close()


class TestRedisCompareAndSetConformance:
    """The Redis backing honors the same D1 contract as Memory/File, plus the
    real WATCH/MULTI atomicity under genuine concurrency."""

    def test_absent_key_is_version_zero_sets(self, redis_state_backend):
        ok = redis_state_backend.compare_and_set(
            "cfg", expected_version=0, new_value={"a": 1, OCC: 1}
        )
        assert ok is True
        assert redis_state_backend.get("cfg") == {"a": 1, OCC: 1}

    def test_set_on_match_then_false_on_stale(self, redis_state_backend):
        redis_state_backend.set("cfg", {"a": 1, OCC: 2})

        assert (
            redis_state_backend.compare_and_set(
                "cfg", expected_version=2, new_value={"a": 9, OCC: 3}
            )
            is True
        )
        assert redis_state_backend.get("cfg")[OCC] == 3

        # A stale expected version → False, value untouched.
        assert (
            redis_state_backend.compare_and_set(
                "cfg", expected_version=2, new_value={"a": 0, OCC: 3}
            )
            is False
        )
        assert redis_state_backend.get("cfg")["a"] == 9

    def test_concurrent_cas_exactly_one_winner(self, redis_state_backend):
        """N threads racing CAS from the same expected version: exactly one wins
        (WATCH/MULTI atomicity); the losers return False, never a clobber."""
        redis_state_backend.set("cfg", {"v": "seed", OCC: 0})
        results: list[bool] = []
        results_lock = threading.Lock()
        start = threading.Barrier(8)

        def _writer(idx: int) -> None:
            start.wait()
            won = redis_state_backend.compare_and_set(
                "cfg", expected_version=0, new_value={"v": f"w{idx}", OCC: 1}
            )
            with results_lock:
                results.append(won)

        threads = [threading.Thread(target=_writer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(True) == 1  # exactly one winner survives the race
        assert redis_state_backend.get("cfg")[OCC] == 1
