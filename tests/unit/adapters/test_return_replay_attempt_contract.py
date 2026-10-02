"""``return_replay_attempt``: give back the attempt an acquisition took (807 D4).

The contract every DLQ adapter holds (memory, SQL, Redis, Redis degraded):
only while the entry is REPLAYING **and** its ``retry_count`` equals the count
this replay's own acquisition returned, set ``retry_count`` to one less; leave
the status (so no index moves) and ``updated_at`` (the stale release's age) as
they are; answer False for a PENDING entry, another count, or a missing entry.
The count is the holder's fence: a replay another acquisition overtook gives
nothing back.

The Redis cases run the adapter's own WATCH / MULTI / EXEC write over a
backend whose raw client models the transaction, so a write landing between
the watched read and the EXEC is a real conflict the adapter must survive.
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from baldur.interfaces.repositories import FailedOperationStatus
from baldur.utils.time import utc_now

DOMAIN = "payment_api"
MAX_REPLAYS = 2
MISSING_ID = "999999"


def _park(store, *, retry_count: int = 0) -> str:
    return store.repo.create(
        domain=DOMAIN,
        failure_type="TIMEOUT",
        retry_count=retry_count,
        max_retries=MAX_REPLAYS,
    ).id


def _acquire(store, dlq_id: str) -> int:
    acquired = store.repo.try_acquire_for_replay(dlq_id, MAX_REPLAYS)
    assert acquired is not None
    return acquired.retry_count


def _reacquire_blob(dlq_id: str, store):
    """A hook: another replay takes the entry over at the next count."""

    def _hook(_key, backend):
        repo = store.repo
        data = repo._decode_entry(backend.blobs[repo._make_key(dlq_id)])
        data["retry_count"] = int(data["retry_count"]) + 1
        data["updated_at"] = utc_now().isoformat()
        backend.set_blob(repo._make_key(dlq_id), repo._encode_entry(data))

    return _hook


class TestReturnReplayAttemptContract:
    """The fenced give-back, identical on every adapter."""

    def test_return_replay_attempt_lowers_the_count_by_one_while_held(self, dlq_store):
        # Given: an entry this replay acquired.
        dlq_id = _park(dlq_store)
        count = _acquire(dlq_store, dlq_id)
        before = dlq_store.repo.get_by_id(dlq_id)

        # When
        returned = dlq_store.repo.return_replay_attempt(dlq_id, count)

        # Then: one attempt back; status and age untouched.
        after = dlq_store.repo.get_by_id(dlq_id)
        assert returned is True
        assert count == 1
        assert after.retry_count == 0
        assert after.status == FailedOperationStatus.REPLAYING.value
        assert after.updated_at == before.updated_at
        assert after.last_retry_at == before.last_retry_at

    def test_return_replay_attempt_refuses_a_pending_entry(self, dlq_store):
        dlq_id = _park(dlq_store, retry_count=1)

        assert dlq_store.repo.return_replay_attempt(dlq_id, 1) is False
        entry = dlq_store.repo.get_by_id(dlq_id)
        assert (entry.status, entry.retry_count) == ("pending", 1)

    @pytest.mark.parametrize("offset", [-1, 1], ids=["lower_count", "higher_count"])
    def test_return_replay_attempt_refuses_another_count(self, dlq_store, offset):
        """A replay overtaken by another acquisition holds a stale count."""
        dlq_id = _park(dlq_store)
        count = _acquire(dlq_store, dlq_id)

        assert dlq_store.repo.return_replay_attempt(dlq_id, count + offset) is False
        assert dlq_store.repo.get_by_id(dlq_id).retry_count == count

    def test_return_replay_attempt_refuses_a_missing_entry(self, dlq_store):
        assert dlq_store.repo.return_replay_attempt(MISSING_ID, 1) is False

    def test_return_replay_attempt_gives_back_once_per_acquisition(self, dlq_store):
        dlq_id = _park(dlq_store)
        count = _acquire(dlq_store, dlq_id)

        first = dlq_store.repo.return_replay_attempt(dlq_id, count)
        second = dlq_store.repo.return_replay_attempt(dlq_id, count)

        assert (first, second) == (True, False)
        assert dlq_store.repo.get_by_id(dlq_id).retry_count == count - 1

    def test_return_replay_attempt_leaves_the_entry_out_of_selection(self, dlq_store):
        """The entry stays REPLAYING — no lane selects it until it completes."""
        dlq_id = _park(dlq_store)
        count = _acquire(dlq_store, dlq_id)

        dlq_store.repo.return_replay_attempt(dlq_id, count)

        page = dlq_store.repo.find_replayable_page(
            max_retries=MAX_REPLAYS, domain=DOMAIN, limit=10
        )
        assert [entry.id for entry in page.entries] == []
        assert dlq_store.repo.try_acquire_for_replay(dlq_id, MAX_REPLAYS) is None

    def test_return_replay_attempt_then_completion_frees_the_attempt(self, dlq_store):
        """Give back, then complete as failed: the entry is acquirable at the
        same attempt number again."""
        dlq_id = _park(dlq_store)
        count = _acquire(dlq_store, dlq_id)

        dlq_store.repo.return_replay_attempt(dlq_id, count)
        dlq_store.repo.complete_replay(dlq_id, success=False, note="still down")

        assert _acquire(dlq_store, dlq_id) == count


@pytest.mark.parametrize("dlq_store", ["redis"], indirect=True)
class TestReturnReplayAttemptRedisWatchBehavior:
    """The Redis give-back is a compare-and-set on the entry key."""

    def test_return_replay_attempt_losing_a_watch_race_gives_nothing_back(
        self, dlq_store
    ):
        # Given: another replay takes the entry over between the give-back's
        # watched read and its write.
        dlq_id = _park(dlq_store)
        count = _acquire(dlq_store, dlq_id)
        dlq_store.backend.after_get = _reacquire_blob(dlq_id, dlq_store)

        # When
        returned = dlq_store.repo.return_replay_attempt(dlq_id, count)

        # Then: the conflict re-read the entry and found it held elsewhere.
        assert returned is False
        assert dlq_store.backend.watch_conflicts == 1
        assert dlq_store.repo.get_by_id(dlq_id).retry_count == count + 1

    def test_return_replay_attempt_after_a_lost_race_still_fenced_on_the_new_count(
        self, dlq_store
    ):
        dlq_id = _park(dlq_store)
        count = _acquire(dlq_store, dlq_id)
        dlq_store.backend.after_watch = _reacquire_blob(dlq_id, dlq_store)

        returned = dlq_store.repo.return_replay_attempt(dlq_id, count)

        assert returned is False
        assert dlq_store.backend.watch_conflicts == 0

    def test_return_replay_attempt_connection_fault_falls_back_to_the_python_path(
        self, dlq_store
    ):
        import redis

        dlq_id = _park(dlq_store)
        count = _acquire(dlq_store, dlq_id)

        def _drop_connection(_key, _backend):
            raise redis.ConnectionError("connection reset")

        dlq_store.backend.after_watch = _drop_connection

        with capture_logs() as logs:
            returned = dlq_store.repo.return_replay_attempt(dlq_id, count)

        assert returned is True
        assert dlq_store.repo.get_by_id(dlq_id).retry_count == count - 1
        degraded = [
            e for e in logs if e["event"] == "dlq.replay_attempt_return_degraded"
        ]
        assert len(degraded) == 1
        assert degraded[0]["log_level"] == "warning"

    def test_return_replay_attempt_moves_no_index(self, dlq_store):
        dlq_id = _park(dlq_store)
        count = _acquire(dlq_store, dlq_id)
        repo = dlq_store.repo
        replaying_key = repo._status_key(FailedOperationStatus.REPLAYING.value)
        before = {key: set(members) for key, members in dlq_store.backend.zsets.items()}

        repo.return_replay_attempt(dlq_id, count)

        after = {key: set(members) for key, members in dlq_store.backend.zsets.items()}
        assert after == before
        assert dlq_id in after[replaying_key]
