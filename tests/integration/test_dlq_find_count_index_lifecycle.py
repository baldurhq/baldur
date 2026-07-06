"""
Integration test for the Redis DLQ find/count index lifecycle (541 D6).

Composition under test: RedisDLQRepository over a *real* ResilientStorageBackend
in degraded (in-memory ZSET) mode — no external infra. This validates what a
per-method mock cannot: that create() writes the global/per-status/by_domain
indexes that find()/count() later read, and that the created_at re-score in
_update keeps cross-status find() ordering exact through status transitions.

The degraded backend exercises the same zadd/zrevrange/zcard/zrem primitives as
the normal Redis path (the index maintenance code is mode-uniform); only the
atomic-MULTI REPLAYING write in dlq_lifecycle is normal-mode-only and is left to
a requires_redis test. The degraded acquire path (_try_acquire_python → _update)
still indexes REPLAYING, so acquire→find(status=replaying) is covered here.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from baldur.adapters.redis.dlq import RedisDLQRepository
from baldur.interfaces.repositories import FailedOperationStatus

PF = FailedOperationStatus.PERMANENTLY_FAILED.value
RR = FailedOperationStatus.REQUIRES_REVIEW.value
RESOLVED = FailedOperationStatus.RESOLVED.value
REPLAYING = FailedOperationStatus.REPLAYING.value


@pytest.fixture
def repo():
    """RedisDLQRepository over a degraded (in-memory) ResilientStorageBackend."""
    from baldur.adapters.resilient.backend import (
        ResilientStorageBackend,
        ResilientStorageMode,
        reset_storage_backend,
    )
    from baldur.settings.resilient_storage import ResilientStorageSettings

    reset_storage_backend()
    with tempfile.TemporaryDirectory() as wal_dir:
        config = ResilientStorageSettings(
            redis_url="redis://nonexistent:6379/0",
            wal_dir=wal_dir,
            allow_memory_only=True,
        )
        with patch("baldur.adapters.cache.RedisCacheAdapter") as MockAdapter:
            MockAdapter.side_effect = Exception("Redis unavailable")
            backend = ResilientStorageBackend(config)
        assert backend.mode == ResilientStorageMode.DEGRADED
        repo = RedisDLQRepository(backend)
        try:
            yield repo
        finally:
            backend.close()
            reset_storage_backend()


def _create_at(repo, when: datetime, **kwargs):
    """create() with a deterministic created_at (patches the module utc_now)."""
    with patch("baldur.adapters.redis.dlq.utc_now", return_value=when):
        return repo.create(**kwargs)


class TestRedisDLQIndexLifecycle:
    """create → transition → find/count over the real degraded backend."""

    def test_no_filter_find_orders_all_statuses_by_created_at_desc(self, repo):
        """create writes the global index; find reads it newest-first."""
        oldest = _create_at(
            repo,
            datetime(2026, 1, 1, 10, tzinfo=UTC),
            domain="payment",
            failure_type="timeout",
        )
        middle = _create_at(
            repo,
            datetime(2026, 1, 2, 10, tzinfo=UTC),
            domain="auth",
            failure_type="http_5xx",
        )
        newest = _create_at(
            repo,
            datetime(2026, 1, 3, 10, tzinfo=UTC),
            domain="inventory",
            failure_type="timeout",
        )

        results = repo.find()

        assert [e.id for e in results] == [newest.id, middle.id, oldest.id]
        assert repo.count() == 3

    def test_escalated_terminal_statuses_visible_by_default(self, repo):
        """The triage blind spot is fixed: permanently_failed / requires_review
        appear in the no-filter listing (541 D5)."""
        e1 = _create_at(
            repo, datetime(2026, 1, 1, 10, tzinfo=UTC), domain="d", failure_type="t"
        )
        e2 = _create_at(
            repo, datetime(2026, 1, 2, 10, tzinfo=UTC), domain="d", failure_type="t"
        )
        repo.update_status(e1.id, PF)
        repo.update_status(e2.id, RR)

        listed = {(e.id, e.status) for e in repo.find()}

        assert (e1.id, PF) in listed
        assert (e2.id, RR) in listed
        assert repo.count(status=PF) == 1
        assert repo.count(status=RR) == 1

    def test_cross_status_ordering_survives_a_transition(self, repo):
        """After moving the middle entry off PENDING, the no-filter find() is
        still created_at DESC — the global index is untouched by _update."""
        oldest = _create_at(
            repo, datetime(2026, 1, 1, 10, tzinfo=UTC), domain="d", failure_type="t"
        )
        middle = _create_at(
            repo, datetime(2026, 1, 2, 10, tzinfo=UTC), domain="d", failure_type="t"
        )
        newest = _create_at(
            repo, datetime(2026, 1, 3, 10, tzinfo=UTC), domain="d", failure_type="t"
        )

        repo.update_status(middle.id, RESOLVED)

        assert [e.id for e in repo.find()] == [newest.id, middle.id, oldest.id]

    def test_per_status_find_is_created_at_ordered_after_transition(self, repo):
        """The per-status index is created_at-scored (not transition-scored):
        two entries transitioned in reverse creation order still come back
        newest-created first."""
        early = _create_at(
            repo, datetime(2026, 1, 1, 10, tzinfo=UTC), domain="d", failure_type="t"
        )
        late = _create_at(
            repo, datetime(2026, 1, 5, 10, tzinfo=UTC), domain="d", failure_type="t"
        )

        # Transition the LATER-created entry first, then the earlier one. A
        # transition-time score would order them [early, late]; a created_at
        # score orders them [late, early].
        repo.update_status(late.id, RESOLVED)
        repo.update_status(early.id, RESOLVED)

        resolved = repo.find(status=RESOLVED)

        assert [e.id for e in resolved] == [late.id, early.id]

    def test_delete_drops_entry_from_global_index(self, repo):
        e1 = _create_at(
            repo, datetime(2026, 1, 1, 10, tzinfo=UTC), domain="d", failure_type="t"
        )
        _create_at(
            repo, datetime(2026, 1, 2, 10, tzinfo=UTC), domain="d", failure_type="t"
        )
        assert repo.count() == 2

        repo.delete(e1.id)

        assert repo.count() == 1
        assert e1.id not in {e.id for e in repo.find()}

    def test_acquire_indexes_replaying_so_status_find_serves_it(self, repo):
        """The degraded acquire path (_try_acquire_python → _update) indexes
        REPLAYING, so find(status=replaying) returns the acquired entry."""
        entry = _create_at(
            repo,
            datetime(2026, 1, 1, 10, tzinfo=UTC),
            domain="payment",
            failure_type="timeout",
        )

        acquired = repo.try_acquire_for_replay(entry.id, max_retries=3)

        assert acquired is not None
        assert acquired.status == REPLAYING
        replaying = repo.find(status=REPLAYING)
        assert [e.id for e in replaying] == [entry.id]
        assert repo.count(status=REPLAYING) == 1

    def test_sparse_status_domain_intersection_pages_over_filtered_set(self, repo):
        """A rare domain scattered through a larger status index pages over the
        filtered intersection — SB-016's combo path (a windowed fetch would
        under-return)."""
        ids_by_domain_rare = []
        for i in range(6):
            dom = "rare" if i % 2 == 0 else "common"
            e = _create_at(
                repo,
                datetime(2026, 1, 1 + i, 10, tzinfo=UTC),
                domain=dom,
                failure_type="timeout",
            )
            repo.update_status(e.id, RESOLVED)
            if dom == "rare":
                ids_by_domain_rare.append(e.id)

        # rare entries created at days 1,3,5 -> DESC newest-first.
        rare_desc = list(reversed(ids_by_domain_rare))

        # count over the filtered intersection is exact.
        assert repo.count(status=RESOLVED, domain="rare") == 3

        # page offset=1, limit=1 over the filtered (DESC) set.
        page = repo.find(status=RESOLVED, domain="rare", offset=1, limit=1)
        assert [e.id for e in page] == [rare_desc[1]]
