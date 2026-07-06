"""Redis adapter force-acquire parity tests for DLQ force-redrive (607 D2/D3/G5).

Verifies ``RedisDLQRepository.try_acquire_for_replay(force=True)`` against a
real Redis instance: the atomic block must derive the ZREM **source-status
index** from the entry's actual status (so a ``REQUIRES_REVIEW -> REPLAYING``
move clears the right per-status + composite index), reset retry_count to a
fresh budget, and stamp the ``previous_total_retries`` / ``force_redrive_count``
metadata scar — all under WATCH/MULTI atomicity. The normal ``force=False``
path stays behaviour-identical.

This WATCH/MULTI + source-status-index generalization cannot be exercised by
the in-memory adapter, hence a real-infra parity test. Marked
``requires_redis`` for auto-skip when Redis is unavailable.
"""

from __future__ import annotations

import pytest

from baldur.interfaces.repositories import FailedOperationStatus

pytestmark = pytest.mark.requires_redis

PENDING = FailedOperationStatus.PENDING.value
REPLAYING = FailedOperationStatus.REPLAYING.value
REQUIRES_REVIEW = FailedOperationStatus.REQUIRES_REVIEW.value
RESOLVED = FailedOperationStatus.RESOLVED.value


@pytest.fixture(autouse=True)
def _reset_redis_unavailable_flag():
    """Reset runtime-scoped Redis negative cache so backend can init Redis."""
    from baldur.adapters.redis import _redis_state

    state = _redis_state()
    state.unavailable = False
    state.fail_time = 0.0
    yield
    state.unavailable = False
    state.fail_time = 0.0


def _at_cap_requires_review(repo, *, cap=2):
    """Drive an entry to the at-cap REQUIRES_REVIEW terminal via the real
    lifecycle (acquire -> failed complete at cap), then assert the precondition.
    """
    entry = repo.create(
        domain="payment",
        failure_type="PG_TIMEOUT",
        retry_count=cap - 1,
        max_retries=cap,
    )
    acquired = repo.try_acquire_for_replay(id=entry.id, max_retries=cap)
    assert acquired is not None
    assert acquired.retry_count == cap
    repo.complete_replay(id=entry.id, success=False, note="poison")
    refreshed = repo.get_by_id(entry.id)
    assert refreshed.status == REQUIRES_REVIEW  # setup guard (606 terminal)
    return refreshed


class TestRedisForceAcquire:
    """force=True over WATCH/MULTI: source-status index + budget reset + scar."""

    def test_force_acquire_requires_review_resets_and_stamps(
        self, redis_dlq_repository
    ):
        repo = redis_dlq_repository
        entry = _at_cap_requires_review(repo, cap=2)

        acquired = repo.try_acquire_for_replay(id=entry.id, max_retries=2, force=True)

        assert acquired is not None
        assert acquired.status == REPLAYING
        assert acquired.retry_count == 1
        assert acquired.metadata["previous_total_retries"] == 2
        assert acquired.metadata["force_redrive_count"] == 1

    def test_force_acquire_clears_source_status_index(self, redis_dlq_repository):
        """The REQUIRES_REVIEW source index is ZREM'd; the entry now indexes
        under REPLAYING (find by source status no longer returns it)."""
        repo = redis_dlq_repository
        entry = _at_cap_requires_review(repo, cap=2)

        repo.try_acquire_for_replay(id=entry.id, max_retries=2, force=True)

        review_ids = {e.id for e in repo.find(status=REQUIRES_REVIEW)}
        replaying_ids = {e.id for e in repo.find(status=REPLAYING)}
        assert entry.id not in review_ids
        assert entry.id in replaying_ids

    def test_force_acquire_pending_resets_budget(self, redis_dlq_repository):
        repo = redis_dlq_repository
        entry = repo.create(
            domain="payment", failure_type="x", retry_count=1, max_retries=2
        )

        acquired = repo.try_acquire_for_replay(id=entry.id, max_retries=2, force=True)

        assert acquired is not None
        assert acquired.status == REPLAYING
        assert acquired.retry_count == 1
        assert acquired.metadata["previous_total_retries"] == 1

    def test_force_acquire_rejects_replaying(self, redis_dlq_repository):
        """A second force-acquire on an already-REPLAYING entry returns None."""
        repo = redis_dlq_repository
        entry = _at_cap_requires_review(repo, cap=2)

        first = repo.try_acquire_for_replay(id=entry.id, max_retries=2, force=True)
        assert first is not None

        second = repo.try_acquire_for_replay(id=entry.id, max_retries=2, force=True)
        assert second is None

    def test_force_acquire_missing_entry_returns_none(self, redis_dlq_repository):
        result = redis_dlq_repository.try_acquire_for_replay(
            id=999999, max_retries=2, force=True
        )
        assert result is None


class TestRedisNormalAcquireUnchanged:
    """force=False contract is behaviour-identical (regression)."""

    def test_normal_acquire_pending_under_cap_increments(self, redis_dlq_repository):
        repo = redis_dlq_repository
        entry = repo.create(domain="payment", failure_type="x", max_retries=2)

        acquired = repo.try_acquire_for_replay(id=entry.id, max_retries=2)

        assert acquired is not None
        assert acquired.status == REPLAYING
        assert acquired.retry_count == 1
        assert "force_redrive_count" not in (acquired.metadata or {})

    def test_normal_acquire_requires_review_returns_none(self, redis_dlq_repository):
        repo = redis_dlq_repository
        entry = _at_cap_requires_review(repo, cap=2)

        assert repo.try_acquire_for_replay(id=entry.id, max_retries=2) is None
