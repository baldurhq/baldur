"""``release_stale_replaying``: an interrupted replay at its cap goes to review (807 D14).

The contract every DLQ adapter holds (memory, SQL, Redis, Redis degraded): a
REPLAYING entry held past the cutoff leaves REPLAYING — back to PENDING below
its own stored ``max_retries``; to REQUIRES_REVIEW, with the resolution note
saying it was interrupted on its last allowed attempt, at the cap, where a
PENDING entry could never be acquired again. The count returned covers both
destinations. An entry taken more recently than the cutoff is left alone.

The Redis release moves each entry compare-and-set: an entry another replay
acquired between the candidate scan and the write — or between the release's
watched read and its write — stays REPLAYING at its new count, and the
per-status and per-domain indexes always match the stored status.
"""

from __future__ import annotations

import pytest

from baldur.interfaces.repositories import (
    STALE_RELEASE_AT_CAP_NOTE,
    FailedOperationStatus,
)
from baldur.utils.time import utc_now
from tests.factories.time_helpers import freeze_time

DOMAIN = "payment_api"
MAX_REPLAYS = 2
WINDOW_MINUTES = 30
TAKEN_AT = "2026-10-02 10:00:00"
LATER = "2026-10-02 10:31:00"

PENDING = FailedOperationStatus.PENDING.value
REPLAYING = FailedOperationStatus.REPLAYING.value
REQUIRES_REVIEW = FailedOperationStatus.REQUIRES_REVIEW.value


def _taken(store, *, retry_count: int = 0, domain: str = DOMAIN) -> str:
    """Park an entry and acquire it, as a replay whose worker then died."""
    dlq_id = store.repo.create(
        domain=domain,
        failure_type="TIMEOUT",
        retry_count=retry_count,
        max_retries=MAX_REPLAYS,
    ).id
    assert store.repo.try_acquire_for_replay(dlq_id, MAX_REPLAYS) is not None
    return dlq_id


def _release(store, at: str = LATER) -> int:
    with freeze_time(at):
        return store.repo.release_stale_replaying(older_than_minutes=WINDOW_MINUTES)


def _state(store, dlq_id: str) -> tuple[str, int]:
    entry = store.repo.get_by_id(dlq_id)
    return entry.status, entry.retry_count


class TestReleaseStaleReplayingContract:
    """Both destinations, identical on every adapter."""

    def test_release_below_the_cap_returns_the_entry_to_pending(self, dlq_store):
        with freeze_time(TAKEN_AT):
            dlq_id = _taken(dlq_store)

        released = _release(dlq_store)

        assert released == 1
        assert _state(dlq_store, dlq_id) == (PENDING, 1)

    def test_release_at_the_cap_sends_the_entry_to_review_with_the_note(
        self, dlq_store
    ):
        """Interrupted on its last allowed attempt: PENDING could never be
        acquired again, so it goes to the review queue."""
        with freeze_time(TAKEN_AT):
            dlq_id = _taken(dlq_store, retry_count=MAX_REPLAYS - 1)

        released = _release(dlq_store)

        entry = dlq_store.repo.get_by_id(dlq_id)
        assert released == 1
        assert (entry.status, entry.retry_count) == (REQUIRES_REVIEW, MAX_REPLAYS)
        assert entry.resolution_note == STALE_RELEASE_AT_CAP_NOTE

    def test_release_counts_both_destinations(self, dlq_store):
        with freeze_time(TAKEN_AT):
            below = _taken(dlq_store)
            at_cap = _taken(dlq_store, retry_count=MAX_REPLAYS - 1)

        released = _release(dlq_store)

        assert released == 2
        assert _state(dlq_store, below)[0] == PENDING
        assert _state(dlq_store, at_cap)[0] == REQUIRES_REVIEW

    @pytest.mark.parametrize(
        ("taken_at", "released"),
        [("2026-10-02 10:00:59", 1), ("2026-10-02 10:01:01", 0)],
        ids=["just_past_the_cutoff", "just_inside_the_window"],
    )
    def test_release_window_boundary(self, dlq_store, taken_at, released):
        """Released at 10:31:00 with a 30-minute window: the cutoff is 10:01:00."""
        with freeze_time(taken_at):
            dlq_id = _taken(dlq_store)

        assert _release(dlq_store) == released
        assert _state(dlq_store, dlq_id)[0] == (PENDING if released else REPLAYING)

    def test_release_leaves_pending_and_finished_entries_alone(self, dlq_store):
        with freeze_time(TAKEN_AT):
            pending = dlq_store.repo.create(domain=DOMAIN, failure_type="TIMEOUT").id
            resolved = _taken(dlq_store)
            dlq_store.repo.complete_replay(resolved, success=True)

        assert _release(dlq_store) == 0
        assert _state(dlq_store, pending) == (PENDING, 0)
        assert _state(dlq_store, resolved)[0] == FailedOperationStatus.RESOLVED.value

    def test_release_is_idempotent_across_two_runs(self, dlq_store):
        with freeze_time(TAKEN_AT):
            dlq_id = _taken(dlq_store)

        first = _release(dlq_store)
        second = _release(dlq_store)

        assert (first, second) == (1, 0)
        assert _state(dlq_store, dlq_id) == (PENDING, 1)

    def test_release_then_the_entry_is_acquirable_again(self, dlq_store):
        with freeze_time(TAKEN_AT):
            dlq_id = _taken(dlq_store)
        _release(dlq_store)

        reacquired = dlq_store.repo.try_acquire_for_replay(dlq_id, MAX_REPLAYS)

        assert reacquired is not None
        assert reacquired.retry_count == MAX_REPLAYS


def _reacquire(store, dlq_id: str):
    """A hook: another replay takes the released entry at the next count."""

    def _hook(_key, backend):
        repo = store.repo
        key = repo._make_key(dlq_id)
        data = repo._decode_entry(backend.blobs[key])
        data["status"] = REPLAYING
        data["retry_count"] = int(data["retry_count"]) + 1
        data["updated_at"] = utc_now().isoformat()
        data["last_retry_at"] = utc_now().isoformat()
        backend.set_blob(key, repo._encode_entry(data))

    return _hook


def _index_members(store, status: str, domain: str = DOMAIN) -> tuple[bool, bool]:
    """Whether the entry index of ``status`` / of ``(status, domain)`` hold ids."""
    repo = store.repo
    status_key = repo.PENDING_KEY if status == PENDING else repo._status_key(status)
    return (
        store.backend.members(status_key),
        store.backend.members(repo._status_domain_key(status, domain)),
    )


@pytest.mark.parametrize("dlq_store", ["redis", "redis_degraded"], indirect=True)
class TestReleaseStaleReplayingRedisIndexBehavior:
    """The Redis indexes follow the stored status, on either write path."""

    @pytest.mark.parametrize(
        ("retry_count", "destination"),
        [(0, PENDING), (MAX_REPLAYS - 1, REQUIRES_REVIEW)],
        ids=["to_pending", "to_review"],
    )
    def test_release_moves_the_status_and_domain_indexes(
        self, dlq_store, retry_count, destination
    ):
        with freeze_time(TAKEN_AT):
            dlq_id = _taken(dlq_store, retry_count=retry_count)

        _release(dlq_store)

        replaying_status, replaying_domain = _index_members(dlq_store, REPLAYING)
        dest_status, dest_domain = _index_members(dlq_store, destination)
        assert dlq_id not in replaying_status
        assert dlq_id not in replaying_domain
        assert dlq_id in dest_status
        assert dlq_id in dest_domain
        assert _state(dlq_store, dlq_id)[0] == destination


@pytest.mark.parametrize("dlq_store", ["redis"], indirect=True)
class TestReleaseStaleReplayingRedisCompareAndSetBehavior:
    """A release that races an acquisition never hands back a held entry."""

    def test_release_reacquired_between_scan_and_write_is_left_alone(self, dlq_store):
        # Given: the scan lists the entry stale; another replay takes it
        # before the release's watched read.
        with freeze_time(TAKEN_AT):
            dlq_id = _taken(dlq_store)
        dlq_store.backend.after_watch = _reacquire(dlq_store, dlq_id)

        # When
        released = _release(dlq_store)

        # Then
        assert released == 0
        assert _state(dlq_store, dlq_id) == (REPLAYING, 2)
        assert dlq_id in _index_members(dlq_store, REPLAYING)[0]

    def test_release_watch_conflict_after_the_read_is_re_read_and_left_alone(
        self, dlq_store
    ):
        # Given: the acquisition lands between the release's read and its write.
        with freeze_time(TAKEN_AT):
            dlq_id = _taken(dlq_store)
        dlq_store.backend.after_get = _reacquire(dlq_store, dlq_id)

        # When
        released = _release(dlq_store)

        # Then: EXEC failed on the watched key, the re-read saw it held.
        assert dlq_store.backend.watch_conflicts == 1
        assert released == 0
        assert _state(dlq_store, dlq_id) == (REPLAYING, 2)
        replaying_status, replaying_domain = _index_members(dlq_store, REPLAYING)
        assert dlq_id in replaying_status
        assert dlq_id in replaying_domain
        assert dlq_id not in _index_members(dlq_store, PENDING)[0]

    def test_release_marks_the_entry_released_from_stale(self, dlq_store):
        with freeze_time(TAKEN_AT):
            dlq_id = _taken(dlq_store)

        _release(dlq_store)

        assert dlq_store.repo.get_by_id(dlq_id).metadata["released_from_stale"] is True
