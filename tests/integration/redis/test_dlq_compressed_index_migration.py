"""Real-Redis integration tests for the compressed-index migration (723).

What these cover that the in-process fake cannot:

- **The completion criterion's primitive.** The migration concludes on a walk
  that added nothing, and "nothing" is the new-element count ``ZADD`` returns.
  Only real Redis decides that, and the whole design rests on it: a client
  that reported the mapping size instead would conclude on the first walk.
- **The routing switch end to end** — a real sorted-set index that a
  newest-first page genuinely truncates, so the archived listing really is
  unreachable before the marker and reachable after it.
- **The marker as persisted state.** In-process the adapter caches a positive
  observation, so a fresh repository over the same Redis is what proves the
  marker is a stored fact rather than a process one.
- **The sweep's walk over the per-status key**, where the archived prefix is
  not merely filtered out — it is not in the walked key at all.

Auto-skips when Redis is unavailable via the conftest ``requires_redis``
marker autoskip hook.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

pytestmark = pytest.mark.requires_redis


from baldur.adapters.redis.dlq_compression import (
    _COMPRESSED_MARKER_KEY,
    _COMPRESSED_STATUS_PREFIX,
    _COMPRESSED_WATERMARK_KEY,
)
from baldur.interfaces.repositories import DLQCompressedEntry
from baldur.utils.time import utc_now


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


def _entry(
    entry_id: str,
    *,
    days_old: int = 0,
    status: str = "active",
    domain: str = "payment",
) -> DLQCompressedEntry:
    now = utc_now()
    return DLQCompressedEntry(
        id=entry_id,
        domain=domain,
        failure_type="timeout",
        error_code="E001",
        count=3,
        first_seen=now - timedelta(days=days_old + 1),
        last_seen=now,
        sample_error_message="Connection timeout",
        status=status,
        compressed_at=now - timedelta(days=days_old),
    )


def _store_pre_migration(repo, entry) -> DLQCompressedEntry:
    """Store an entry, then strip the memberships that postdate it.

    Entries compressed before the per-status family existed live only in the
    all-statuses index — the population the migration exists to reach.
    """
    repo.store_compressed_entry(entry)
    backend = repo._backend
    backend.zrem(f"{_COMPRESSED_STATUS_PREFIX}{entry.status}", entry.id)
    backend.zrem(
        f"dlq:compressed:status_domain:{entry.status}:{entry.domain}", entry.id
    )
    return entry


def _fresh_repository(repo):
    """A second repository over the same backend, with no cached marker."""
    from baldur.adapters.redis.dlq import RedisDLQRepository

    return RedisDLQRepository(backend=repo._backend)


class TestCompressedBackfillOverRedis:
    """The migration walk against real sorted sets and real ZADD returns."""

    def test_full_walk_files_pre_migration_entries_under_their_status(
        self, redis_dlq_repository
    ):
        """
        Purpose:
            Entries that exist only in the all-statuses index are filed under
            the per-status keys the filtered reads use.
        Expected:
            - the walk reports the members it added
            - both the global and the composite key hold them afterwards
        """
        repo = redis_dlq_repository
        for i in range(3):
            _store_pre_migration(
                repo, _entry(f"compressed:payment:timeout:E001:{i}", days_old=100 + i)
            )

        result = repo.backfill_compressed_status_index(operator_initiated=True)

        assert result["verified"] is True
        assert result["walked"] == 3
        # Global + composite membership for each member.
        assert result["added"] == 6
        assert repo._backend.zcard(f"{_COMPRESSED_STATUS_PREFIX}active") == 3
        assert repo._backend.zcard("dlq:compressed:status_domain:active:payment") == 3

    def test_a_repeated_walk_adds_nothing(self, redis_dlq_repository):
        """
        Purpose:
            The completion criterion is a walk that adds nothing, measured by
            what ZADD reports as new. Re-adding a member with its own score
            must count as zero, or the migration could never conclude.
        Expected:
            - the second walk over the same index reports added == 0
            - it still walked every member
        """
        repo = redis_dlq_repository
        for i in range(3):
            _store_pre_migration(
                repo, _entry(f"compressed:payment:timeout:E001:{i}", days_old=100 + i)
            )
        repo.backfill_compressed_status_index(operator_initiated=True)

        result = repo.backfill_compressed_status_index(operator_initiated=True)

        assert result["walked"] == 3
        assert result["added"] == 0

    def test_marker_and_watermark_outlive_the_repository_that_wrote_them(
        self, redis_dlq_repository
    ):
        """
        Purpose:
            Completion is a stored fact, not a process one: a worker that did
            not run the migration must still route its reads through the
            per-status keys.
        Expected:
            - both control blobs exist in Redis after an operator run
            - a fresh repository over the same backend reports ready
        """
        repo = redis_dlq_repository
        _store_pre_migration(repo, _entry("compressed:payment:timeout:E001:1"))
        repo.backfill_compressed_status_index(operator_initiated=True)

        backend = repo._backend
        assert backend.get_blob(_COMPRESSED_MARKER_KEY) is not None
        assert backend.get_blob(_COMPRESSED_WATERMARK_KEY) is not None
        assert _fresh_repository(repo).compression._is_status_index_ready() is True


class TestCompressedListingRoutingOverRedis:
    """The archived view: unreachable before the migration, reachable after."""

    def test_archived_listing_is_empty_before_and_complete_after(
        self, redis_dlq_repository
    ):
        """
        Purpose:
            The archive is the oldest population, so a newest-first slice of
            the all-statuses index never reaches it once live entries fill the
            page — the defect the routing switch exists to fix.
        Expected:
            - before the migration a status=archived page returns nothing
            - after it, the archived entries are returned
        """
        repo = redis_dlq_repository
        archived = [
            _store_pre_migration(
                repo,
                _entry(
                    f"compressed:payment:timeout:E001:arch{i}",
                    days_old=300 + i,
                    status="archived",
                ),
            )
            for i in range(3)
        ]
        for i in range(50):
            repo.store_compressed_entry(
                _entry(f"compressed:payment:timeout:E001:live{i:03d}", days_old=i + 1)
            )

        before = repo.get_compressed_entries(status="archived", limit=5)
        repo.backfill_compressed_status_index(operator_initiated=True)
        after = repo.get_compressed_entries(status="archived", limit=5)

        assert before == []
        assert {e.id for e in after} == {e.id for e in archived}


class TestCompressedSweepWalkOverRedis:
    """The lifecycle sweep's walk after the switch."""

    def test_stale_lane_walks_only_the_stale_key(self, redis_dlq_repository):
        """
        Purpose:
            The unbounded term in the pre-migration sweep was the terminal
            archived prefix, re-read on every run forever. Archived entries
            are not members of the stale key, so the walk cannot reach them.
        Expected:
            - the walk returns exactly the stale population
            - the archived members are absent from the walked key
        """
        repo = redis_dlq_repository
        for i in range(30):
            repo.store_compressed_entry(
                _entry(
                    f"compressed:payment:timeout:E001:arch{i:03d}",
                    days_old=300,
                    status="archived",
                )
            )
        for i in range(5):
            repo.store_compressed_entry(
                _entry(
                    f"compressed:payment:timeout:E001:stale{i}",
                    days_old=200,
                    status="stale",
                )
            )
        repo.backfill_compressed_status_index(operator_initiated=True)

        rows = repo.get_compressed_entries_before(
            status="stale", before=utc_now() - timedelta(days=100), limit=100
        )

        assert len(rows) == 5
        assert repo._backend.zcard(f"{_COMPRESSED_STATUS_PREFIX}stale") == 5

    def test_walk_relocates_a_stray_whose_payload_moved_ahead(
        self, redis_dlq_repository
    ):
        """
        Purpose:
            A transition writes its payload first, so a crash leaves the
            payload naming the new status while the membership still names the
            old one. The sweep's walk over the old key is what discovers it.
        Expected:
            - the stray is not returned as transitionable
            - after the walk it holds membership of the key its payload names
        """
        from baldur.utils.serialization import fast_dumps, fast_loads

        repo = redis_dlq_repository
        stray = _entry("compressed:payment:timeout:E001:stray", days_old=200)
        repo.store_compressed_entry(stray)
        repo.backfill_compressed_status_index(operator_initiated=True)

        backend = repo._backend
        payload = fast_loads(backend.get_blob(f"dlq:compressed:{stray.id}"))
        payload["status"] = "stale"
        backend.set_blob(f"dlq:compressed:{stray.id}", fast_dumps(payload))

        rows = repo.get_compressed_entries_before(
            status="active", before=utc_now() - timedelta(days=100), limit=100
        )

        assert rows == []
        assert backend.zcard(f"{_COMPRESSED_STATUS_PREFIX}active") == 0
        assert backend.zcard(f"{_COMPRESSED_STATUS_PREFIX}stale") == 1
