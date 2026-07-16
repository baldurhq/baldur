"""Real-Redis integration tests for OSS ``DLQReadService`` single-entry actions
+ read queries over the Redis adapter (708).

What this tests that the in-memory round-trip cannot:

The core round-trip (``test_dlq_read_slot_empty_roundtrip.py``) runs against the
in-memory fake, which serializes under a Python lock. This module drives the
same OSS ``DLQReadService`` retry / force-redrive / read surface over a REAL
``RedisDLQRepository``, exercising:

- The Redis **atomic acquire** (server-side Lua ``try_acquire_for_replay``) under
  genuine thread concurrency — two simultaneous retries of the same entry must
  see EXACTLY ONE acquire (no double-execution), the property the in-memory
  GIL-serialized fake cannot prove.
- The Redis cap-aware ``complete_replay`` terminal transitions (under-cap →
  PENDING, at-cap → REQUIRES_REVIEW) and the force-redrive fresh-budget acquire
  (REQUIRES_REVIEW → REPLAYING, retry_count reset to 1) end-to-end.
- Read-query adapter parity — ``get_facet_counts`` / ``get_cleanup_stats`` /
  ``list_entries`` through the OSS facade over Redis equal the memory adapter run
  against the same seeded set.

Auto-skips when Redis is unavailable via the conftest ``requires_redis`` marker
autoskip hook.
"""

from __future__ import annotations

import threading

import pytest

pytestmark = pytest.mark.requires_redis


from baldur.adapters.memory.failed_operation import InMemoryFailedOperationRepository
from baldur.core.exceptions import DLQStateConflictError
from baldur.interfaces.repositories import FailedOperationData, FailedOperationStatus
from baldur.models.dlq import DLQConfig
from baldur.services.dlq_read import DLQReadService
from baldur.services.replay_service import ReplayHandler, register_replay_handler
from baldur.services.replay_service.models import ReplayResult

PENDING = FailedOperationStatus.PENDING.value
REQUIRES_REVIEW = FailedOperationStatus.REQUIRES_REVIEW.value
RESOLVED = FailedOperationStatus.RESOLVED.value


@pytest.fixture(autouse=True)
def _reset_redis_unavailable_flag():
    """Reset the runtime-scoped Redis negative cache so backend can init Redis."""
    from baldur.adapters.redis import _redis_state

    state = _redis_state()
    state.unavailable = False
    state.fail_time = 0.0
    yield
    state.unavailable = False
    state.fail_time = 0.0


class _StubReplayHandler(ReplayHandler):
    """Deterministic replay handler so ``_execute_replay`` has a known outcome."""

    def __init__(self, domain: str, *, succeed: bool = True):
        self._domain = domain
        self._succeed = succeed
        self._lock = threading.Lock()
        self.replay_calls: list[str] = []

    @property
    def domain(self) -> str:
        return self._domain

    def can_replay(self, failed_op: FailedOperationData) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op: FailedOperationData) -> ReplayResult:
        with self._lock:
            self.replay_calls.append(failed_op.id)
        if self._succeed:
            return ReplayResult.succeeded(failed_op.id, "ok")
        return ReplayResult.failed(failed_op.id, "fail")


@pytest.fixture
def register_handler():
    """Register stub replay handlers, restoring the registry afterwards."""
    from baldur.services.replay_service import handlers as _handlers

    snapshot = dict(_handlers._replay_handlers)

    def _register(domain: str, *, succeed: bool = True) -> _StubReplayHandler:
        handler = _StubReplayHandler(domain, succeed=succeed)
        register_replay_handler(handler)
        return handler

    yield _register

    _handlers._replay_handlers.clear()
    _handlers._replay_handlers.update(snapshot)


@pytest.fixture
def redis_read_service(redis_dlq_repository):
    """OSS ``DLQReadService`` over a real ``RedisDLQRepository`` (audit stubbed)."""
    service = DLQReadService(
        config=DLQConfig(enabled=True, max_replay_attempts=2),
        repository=redis_dlq_repository,
    )
    service._log_dlq_audit = lambda **kwargs: None  # type: ignore[method-assign]
    return service


def _seed_set(repo) -> None:
    """A fixed status×domain mix reused across the parity assertions."""
    for _ in range(2):
        repo.create(domain="payment", failure_type="timeout")
    entry = repo.create(domain="payment", failure_type="timeout")
    repo.update_status(entry.id, RESOLVED)
    repo.create(domain="inventory", failure_type="timeout")
    resolved = repo.create(domain="inventory", failure_type="timeout")
    repo.update_status(resolved.id, RESOLVED)


# =============================================================================
# Single-entry actions over the Redis atomic acquire / complete primitives
# =============================================================================


class TestRedisSingleEntryActionAtomicity:
    """OSS single-entry actions drive real Redis acquire/complete transitions."""

    def test_retry_pending_to_resolved_on_redis(
        self, redis_read_service, redis_dlq_repository, register_handler
    ):
        domain = "redis_retry_ok"
        handler = register_handler(domain, succeed=True)
        entry = redis_dlq_repository.create(domain=domain, failure_type="x")

        result = redis_read_service.retry_entry(entry.id)

        assert result["success"] is True
        assert result["status"] == RESOLVED
        assert handler.replay_calls == [entry.id]
        assert redis_dlq_repository.get_by_id(entry.id).status == RESOLVED

    def test_retry_handler_failure_under_cap_reverts_pending_on_redis(
        self, redis_read_service, redis_dlq_repository, register_handler
    ):
        domain = "redis_retry_under_cap"
        register_handler(domain, succeed=False)
        entry = redis_dlq_repository.create(
            domain=domain, failure_type="x", retry_count=0, max_retries=2
        )

        result = redis_read_service.retry_entry(entry.id)

        assert result["success"] is False
        assert result["status"] == PENDING
        assert redis_dlq_repository.get_by_id(entry.id).status == PENDING

    def test_retry_at_cap_reconverges_requires_review_on_redis(
        self, redis_read_service, redis_dlq_repository, register_handler
    ):
        """The Redis cap-aware ``complete_replay`` converges to the terminal
        REQUIRES_REVIEW when the acquire pushes retry_count to the cap."""
        domain = "redis_retry_at_cap"
        register_handler(domain, succeed=False)
        # retry_count 1, cap 2 → acquire increments to 2 (==cap) → handler fails
        # → complete_replay sees at-cap → REQUIRES_REVIEW.
        entry = redis_dlq_repository.create(
            domain=domain, failure_type="poison", retry_count=1, max_retries=2
        )

        result = redis_read_service.retry_entry(entry.id)

        assert result["success"] is False
        assert result["status"] == REQUIRES_REVIEW
        assert redis_dlq_repository.get_by_id(entry.id).status == REQUIRES_REVIEW

    def test_force_redrive_at_cap_resolves_fresh_budget_on_redis(
        self, redis_read_service, redis_dlq_repository, register_handler
    ):
        """Force-acquire (force=True) accepts an at-cap REQUIRES_REVIEW entry on
        Redis, grants a fresh budget (retry_count 1), and resolves on success."""
        domain = "redis_force_ok"
        handler = register_handler(domain, succeed=True)
        entry = redis_dlq_repository.create(
            domain=domain, failure_type="poison", retry_count=2, max_retries=2
        )
        redis_dlq_repository.update_status(entry.id, REQUIRES_REVIEW)

        result = redis_read_service.force_redrive_entry(
            entry.id, actor_id="ops", reason="fixed"
        )

        assert result["success"] is True
        assert result["status"] == RESOLVED
        assert result["retry_count"] == 1
        assert result["previous_retry_count"] == 2
        assert handler.replay_calls == [entry.id]
        assert redis_dlq_repository.get_by_id(entry.id).status == RESOLVED

    def test_concurrent_retry_acquires_exactly_once_on_redis(
        self, redis_read_service, redis_dlq_repository, register_handler
    ):
        """Two simultaneous retries of the same entry: the Redis atomic acquire
        admits EXACTLY ONE (no double-execution); the loser gets a state
        conflict. This is the property the GIL-serialized in-memory fake cannot
        prove — the acquire is server-side atomic, not lock-serialized.
        """
        domain = "redis_concurrent"
        handler = register_handler(domain, succeed=True)
        entry = redis_dlq_repository.create(domain=domain, failure_type="x")

        barrier = threading.Barrier(2)
        successes: list[dict] = []
        conflicts: list[Exception] = []
        lock = threading.Lock()

        def _attempt():
            barrier.wait()
            try:
                res = redis_read_service.retry_entry(entry.id)
                with lock:
                    successes.append(res)
            except DLQStateConflictError as exc:
                with lock:
                    conflicts.append(exc)

        threads = [threading.Thread(target=_attempt) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # Exactly one acquired + resolved; the other lost the atomic acquire.
        assert len(successes) == 1
        assert len(conflicts) == 1
        assert successes[0]["status"] == RESOLVED
        # The handler ran for exactly one attempt — no double-execution.
        assert handler.replay_calls == [entry.id]
        assert redis_dlq_repository.get_by_id(entry.id).status == RESOLVED


# =============================================================================
# Read-query adapter parity — OSS facade over Redis == memory adapter
# =============================================================================


class TestRedisReadQueriesParity:
    """The OSS read facade returns identical counts over Redis and memory."""

    def test_facet_counts_match_memory_adapter(
        self, redis_read_service, redis_dlq_repository
    ):
        _seed_set(redis_dlq_repository)
        memory = InMemoryFailedOperationRepository()
        _seed_set(memory)
        memory_service = DLQReadService(
            config=DLQConfig(enabled=True), repository=memory
        )

        assert (
            redis_read_service.get_facet_counts() == memory_service.get_facet_counts()
        )

    def test_cleanup_stats_by_status_match_memory_adapter(
        self, redis_read_service, redis_dlq_repository
    ):
        _seed_set(redis_dlq_repository)
        memory = InMemoryFailedOperationRepository()
        _seed_set(memory)
        memory_service = DLQReadService(
            config=DLQConfig(enabled=True), repository=memory
        )

        redis_stats = redis_read_service.get_cleanup_stats()
        memory_stats = memory_service.get_cleanup_stats()

        assert redis_stats.by_status == memory_stats.by_status

    def test_list_entries_total_count_reflects_seeded_set_on_redis(
        self, redis_read_service, redis_dlq_repository
    ):
        _seed_set(redis_dlq_repository)

        result = redis_read_service.list_entries(page=1, page_size=20)

        # 5 seeded entries span all statuses (no-status filter is cross-status).
        assert result["total_count"] == 5
        assert len(result["results"]) == 5
