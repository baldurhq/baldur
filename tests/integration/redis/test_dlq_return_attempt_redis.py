"""The Redis DLQ's give-back and stale release, compare-and-set on a real server (807).

``return_replay_attempt`` and ``release_stale_replaying`` are WATCH / MULTI /
EXEC writes on the entry key. Against a real Redis this suite drives both
through the shipped repository, and injects the race each one exists to lose
safely: a second client writes the entry key between the transaction's watched
read and its EXEC — as another replay acquiring the entry would.

Test Categories:
    A. The give-back:
        - one attempt back while REPLAYING at the acquired count; status, age
          and indexes untouched
        - refused for a PENDING entry, another count, a missing entry
        - a write landing inside the watch makes EXEC fail; the re-read finds
          the entry held at another count and gives nothing back
    B. The stale release:
        - below the cap to PENDING, at the cap to REQUIRES_REVIEW with the note;
          the per-status and per-domain indexes follow
        - an acquisition landing inside the release's watch keeps the entry
          REPLAYING at its new count, in the REPLAYING indexes

Note: All tests require a running Redis instance.
      Marked with @pytest.mark.requires_redis for auto-skip.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from unittest.mock import PropertyMock, patch

import pytest

from baldur.adapters.redis.dlq import RedisDLQRepository
from baldur.interfaces.repositories import (
    STALE_RELEASE_AT_CAP_NOTE,
    FailedOperationStatus,
)
from baldur.utils.time import utc_now

pytestmark = pytest.mark.requires_redis

_LIFECYCLE_CLOCK = "baldur.adapters.redis.dlq_lifecycle.utc_now"
MAX_REPLAYS = 2
WINDOW_MINUTES = 30

PENDING = FailedOperationStatus.PENDING.value
REPLAYING = FailedOperationStatus.REPLAYING.value
REQUIRES_REVIEW = FailedOperationStatus.REQUIRES_REVIEW.value


@pytest.fixture(autouse=True)
def _reset_redis_unavailable_flag():
    """Reset runtime-scoped Redis negative cache so the backend can reach Redis."""
    from baldur.adapters.redis import _redis_state

    state = _redis_state()
    state.unavailable = False
    state.fail_time = 0.0
    yield
    state.unavailable = False
    state.fail_time = 0.0


@pytest.fixture
def raw_redis(redis_client):
    """A second, byte-level client on the same server: the other replay."""
    import redis

    params = redis_client.connection_pool.connection_kwargs
    client = redis.Redis(
        host=params.get("host", "localhost"),
        port=params.get("port", 6379),
        db=params.get("db", 0),
    )
    yield client
    client.close()


class _InterferingPipeline:
    """A real pipeline whose next watched read is followed by another client's write."""

    def __init__(self, pipe, hook: Callable[[str], None] | None):
        self._pipe = pipe
        self._hook = hook

    def __enter__(self):
        self._pipe.__enter__()
        return self

    def __exit__(self, *exc):
        return self._pipe.__exit__(*exc)

    def get(self, key):
        value = self._pipe.get(key)
        hook, self._hook = self._hook, None
        if hook is not None:
            hook(key)
        return value

    def __getattr__(self, name):
        return getattr(self._pipe, name)


class _InterferingClient:
    def __init__(self, real, hook: Callable[[str], None]):
        self._real = real
        self._hook: Callable[[str], None] | None = hook

    def pipeline(self, transaction: bool = True):
        hook, self._hook = self._hook, None
        return _InterferingPipeline(self._real.pipeline(transaction=transaction), hook)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _interfere(repo: RedisDLQRepository, hook: Callable[[str], None]):
    """Route the repository's next transaction through an interfering pipeline."""
    real = repo._backend.raw_redis_client
    return patch.object(
        RedisDLQRepository,
        "_raw_redis_client",
        new_callable=PropertyMock,
        return_value=_InterferingClient(real, hook),
    )


def _reacquired_by_another_replay(repo, raw_redis, *, at):
    """The other replay: takes the entry at the next count, from its own client."""

    def _hook(full_key: str) -> None:
        data = repo._decode_entry(raw_redis.get(full_key))
        data["status"] = REPLAYING
        data["retry_count"] = int(data["retry_count"]) + 1
        data["updated_at"] = at.isoformat()
        data["last_retry_at"] = at.isoformat()
        raw_redis.set(full_key, repo._encode_entry(data))

    return _hook


def _park_and_take(repo, *, retry_count: int = 0, at=None) -> str:
    dlq_id = repo.create(
        domain="payment",
        failure_type="PG_TIMEOUT",
        retry_count=retry_count,
        max_retries=MAX_REPLAYS,
    ).id
    with patch(_LIFECYCLE_CLOCK, return_value=at or utc_now()):
        assert repo.try_acquire_for_replay(dlq_id, MAX_REPLAYS) is not None
    return dlq_id


def _in_index(repo, raw_redis, status: str, dlq_id: str) -> tuple[bool, bool]:
    backend = repo._backend
    status_key = repo.PENDING_KEY if status == PENDING else repo._status_key(status)
    composite = repo._status_domain_key(status, "payment")
    return (
        raw_redis.zscore(backend._get_full_key(status_key), dlq_id) is not None,
        raw_redis.zscore(backend._get_full_key(composite), dlq_id) is not None,
    )


# =============================================================================
# A. The give-back
# =============================================================================


class TestReturnReplayAttemptRedis:
    """The fenced give-back on a real server."""

    def test_return_replay_attempt_gives_one_back_and_moves_nothing_else(
        self, redis_dlq_repository, raw_redis
    ):
        """
        Purpose:
            A replay gives back the attempt its own acquisition took.
        Expected:
            - True, and ``retry_count`` one lower
            - still REPLAYING, in the REPLAYING indexes, ``updated_at`` unchanged
        """
        repo = redis_dlq_repository
        dlq_id = _park_and_take(repo)
        before = repo.get_by_id(dlq_id)

        returned = repo.return_replay_attempt(dlq_id, before.retry_count)

        after = repo.get_by_id(dlq_id)
        assert returned is True
        assert (after.status, after.retry_count) == (REPLAYING, before.retry_count - 1)
        assert after.updated_at == before.updated_at
        assert _in_index(repo, raw_redis, REPLAYING, dlq_id) == (True, True)

    @pytest.mark.parametrize(
        "case", ["pending", "another_count", "missing"], ids=lambda c: c
    )
    def test_return_replay_attempt_refuses_what_this_replay_does_not_hold(
        self, redis_dlq_repository, case
    ):
        """
        Purpose:
            The count is the holder's fence.
        Expected:
            - False for a PENDING entry, a different count, a missing entry
        """
        repo = redis_dlq_repository
        if case == "pending":
            dlq_id = repo.create(domain="payment", failure_type="PG_TIMEOUT").id
            count = 0
        elif case == "another_count":
            dlq_id = _park_and_take(repo)
            count = 2
        else:
            dlq_id, count = "pod:1:nonce:404", 1

        assert repo.return_replay_attempt(dlq_id, count) is False

    def test_return_replay_attempt_losing_a_watch_race_gives_nothing_back(
        self, redis_dlq_repository, raw_redis
    ):
        """
        Purpose:
            Another replay takes the entry between the give-back's watched read
            and its EXEC.
        Expected:
            - EXEC fails; the re-read finds the entry at the next count
            - False; the other replay's count stands
        """
        repo = redis_dlq_repository
        dlq_id = _park_and_take(repo)
        count = repo.get_by_id(dlq_id).retry_count
        hook = _reacquired_by_another_replay(repo, raw_redis, at=utc_now())

        with _interfere(repo, hook):
            returned = repo.return_replay_attempt(dlq_id, count)

        assert returned is False
        assert repo.get_by_id(dlq_id).retry_count == count + 1


# =============================================================================
# B. The stale release
# =============================================================================


class TestReleaseStaleReplayingRedis:
    """Compare-and-set per entry; the indexes follow the stored status."""

    def test_release_routes_by_the_cap_and_moves_the_indexes(
        self, redis_dlq_repository, raw_redis
    ):
        """
        Purpose:
            Two replays died holding entries — one on its last allowed attempt.
        Expected:
            - both leave REPLAYING: below the cap to PENDING, at the cap to
              REQUIRES_REVIEW with the note
            - each is in its destination's status and per-domain index, in
              neither REPLAYING index
        """
        repo = redis_dlq_repository
        taken_at = utc_now() - timedelta(minutes=WINDOW_MINUTES + 5)
        below = _park_and_take(repo, at=taken_at)
        at_cap = _park_and_take(repo, retry_count=MAX_REPLAYS - 1, at=taken_at)

        released = repo.release_stale_replaying(older_than_minutes=WINDOW_MINUTES)

        assert released == 2
        assert repo.get_by_id(below).status == PENDING
        review = repo.get_by_id(at_cap)
        assert (review.status, review.resolution_note) == (
            REQUIRES_REVIEW,
            STALE_RELEASE_AT_CAP_NOTE,
        )
        assert _in_index(repo, raw_redis, PENDING, below) == (True, True)
        assert _in_index(repo, raw_redis, REQUIRES_REVIEW, at_cap) == (True, True)
        for dlq_id in (below, at_cap):
            assert _in_index(repo, raw_redis, REPLAYING, dlq_id) == (False, False)

    def test_release_racing_an_acquisition_leaves_the_entry_held(
        self, redis_dlq_repository, raw_redis
    ):
        """
        Purpose:
            The stale release reads a stale entry; before its EXEC another
            replay takes it at the next count.
        Expected:
            - nothing released; the entry stays REPLAYING at the new count
            - it is still in the REPLAYING indexes, and not in PENDING
        """
        repo = redis_dlq_repository
        taken_at = utc_now() - timedelta(minutes=WINDOW_MINUTES + 5)
        dlq_id = _park_and_take(repo, at=taken_at)
        hook = _reacquired_by_another_replay(repo, raw_redis, at=utc_now())

        with _interfere(repo, hook):
            released = repo.release_stale_replaying(older_than_minutes=WINDOW_MINUTES)

        entry = repo.get_by_id(dlq_id)
        assert released == 0
        assert (entry.status, entry.retry_count) == (REPLAYING, 2)
        assert _in_index(repo, raw_redis, REPLAYING, dlq_id) == (True, True)
        assert _in_index(repo, raw_redis, PENDING, dlq_id) == (False, False)

    def test_release_leaves_a_fresh_replaying_entry_alone(self, redis_dlq_repository):
        repo = redis_dlq_repository
        dlq_id = _park_and_take(repo, at=utc_now() - timedelta(minutes=5))

        released = repo.release_stale_replaying(older_than_minutes=WINDOW_MINUTES)

        assert released == 0
        assert repo.get_by_id(dlq_id).status == REPLAYING
