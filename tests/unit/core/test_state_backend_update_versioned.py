"""Versioned writes on the state store: ``update_versioned`` and ``get_strict``.

Target: ``baldur.core.state_backend`` — the strict read that tells a failure
from absence, the versioned write every control-state writer goes through
(strict read → mutate on the stored state → conditional write stamped with a
version and a writer token), its read-back classification of a raise, the
pending-change settlement, and the Redis conditional write that answers
``False`` on a lost watch instead of writing a second time.

Verification techniques applied (§8):
  - §8.12 Branch outcome — every ``update_versioned`` exit (committed, already
    satisfied by value and by token, declined, conflict then committed,
    exhausted, raise read back as committed / not applied / unknown, a failed
    strict read before and after a sent write)
  - §8.8 State transition — the mutate re-runs on each attempt's fresh state
  - §8.3 Idempotency — a change whose token is stored is never written twice
  - §8.2 Exception/edge cases — ``get_strict`` raises where ``get`` swallows
  - §8.5 Dependency interaction — the Redis pipeline sends one ``EXEC`` round
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import os
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from baldur.core.state_backend import (
    ALREADY_SATISFIED,
    OCC_VERSION_FIELD,
    OCC_WRITER_FIELD,
    Declined,
    FileStateBackend,
    MemoryStateBackend,
    PendingChange,
    RedisStateBackend,
    StateBackend,
    VersionedWriteOutcome,
    carries_writer_token,
    settle_pending_changes,
    stored_version,
    update_versioned,
)
from baldur.settings.redis import DEFAULT_REDIS_URL
from tests.factories.state_backend_doubles import (
    CAS_LAND_THEN_RAISE,
    CAS_LOSE,
    CAS_PEER,
    CAS_RAISE,
    ScriptedStateBackend,
)

KEY = "system_control"
TOKEN = "change-token"


# =============================================================================
# Helpers
# =============================================================================


def _stamped(value: dict[str, Any], version: int, token: str) -> dict[str, Any]:
    stamped = dict(value)
    stamped[OCC_VERSION_FIELD] = version
    stamped[OCC_WRITER_FIELD] = token
    return stamped


def _set_enabled(enabled: bool) -> Callable[[dict[str, Any] | None], Any]:
    """A mutate that writes ``enabled`` over whatever is stored."""

    def mutate(stored: dict[str, Any] | None) -> dict[str, Any]:
        current = {k: v for k, v in (stored or {}).items() if not k.startswith("__")}
        current["enabled"] = enabled
        return current

    return mutate


# =============================================================================
# Contract — the fields a versioned write stamps
# =============================================================================


class TestVersionedWriteContract:
    """The store-visible field names a versioned write stamps (D8)."""

    def test_version_and_writer_field_names_are_the_design_names(self):
        """A previous release's reader and a hand edit find these exact names."""
        assert OCC_VERSION_FIELD == "__occ_version__"
        assert OCC_WRITER_FIELD == "__occ_writer__"

    def test_committed_write_stamps_version_and_token_beside_the_value(self):
        """The stored value carries the replaced version + 1 and the change's token."""
        backend = MemoryStateBackend()
        backend.set(KEY, _stamped({"enabled": True}, 4, "older-change"))

        update_versioned(backend, KEY, _set_enabled(False), token=TOKEN)

        assert backend.get(KEY) == {
            "enabled": False,
            "__occ_version__": 5,
            "__occ_writer__": TOKEN,
        }


# =============================================================================
# Behavior — get_strict tells a failure from absence
# =============================================================================


class _WatchingPipeline:
    """Redis pipeline double supporting the WATCH / MULTI / EXEC round."""

    def __init__(self, client: _WatchingRedisClient) -> None:
        self._client = client
        self._queued: list[tuple[str, str]] = []
        self._in_multi = False

    def __enter__(self) -> _WatchingPipeline:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def watch(self, key: str) -> None:
        self._client.calls.append(("watch", key))

    def unwatch(self) -> None:
        self._client.calls.append(("unwatch",))

    def get(self, key: str) -> str | None:
        return self._client.store.get(key)

    def multi(self) -> None:
        self._client.calls.append(("multi",))
        self._in_multi = True

    def set(self, key: str, value: str) -> None:
        self._queued.append((key, value))

    def execute(self) -> list[bool]:
        self._client.calls.append(("execute", len(self._queued)))
        if self._client.watch_error_on_execute:
            from redis import WatchError

            raise WatchError("watched key changed")
        for key, value in self._queued:
            self._client.store[key] = value
        return [True for _ in self._queued]


class _WatchingRedisClient:
    """Minimal decoded-response Redis client for the state backend's calls."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.watch_error_on_execute = False
        self.get_error: BaseException | None = None

    def get(self, key: str) -> str | None:
        if self.get_error is not None:
            raise self.get_error
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value

    def pipeline(self) -> _WatchingPipeline:
        return _WatchingPipeline(self)


def _redis_backend(client: _WatchingRedisClient) -> RedisStateBackend:
    """A RedisStateBackend bound to ``client`` without a server admission."""
    backend = RedisStateBackend.__new__(RedisStateBackend)
    backend._key_prefix = "baldur:state:"
    backend._client = client
    return backend


def _file_backend_and_failure(tmp_path) -> tuple[StateBackend, Callable[[], None]]:
    backend = FileStateBackend(tmp_path / "state")

    def corrupt() -> None:
        (tmp_path / "state" / f"{KEY}.json").write_text("{not json")

    return backend, corrupt


def _redis_backend_and_failure() -> tuple[StateBackend, Callable[[], None]]:
    client = _WatchingRedisClient()

    def fail() -> None:
        client.get_error = ConnectionError("redis down")

    return _redis_backend(client), fail


@pytest.fixture(params=["memory", "file", "redis"])
def strict_backend(request, tmp_path) -> StateBackend:
    """Each shipped backend."""
    if request.param == "memory":
        return MemoryStateBackend()
    if request.param == "file":
        return _file_backend_and_failure(tmp_path)[0]
    return _redis_backend_and_failure()[0]


@pytest.fixture(params=["file", "redis"])
def failing_backend(request, tmp_path) -> tuple[StateBackend, Callable[[], None]]:
    """Each backend whose read can fail, with a hook that makes the next one fail."""
    if request.param == "file":
        return _file_backend_and_failure(tmp_path)
    return _redis_backend_and_failure()


class TestGetStrictBehavior:
    """``get_strict`` returns ``None`` for absence and raises on a failed read (D7)."""

    def test_get_strict_absent_key_returns_none(self, strict_backend):
        """Absence is ``None`` on every shipped backend."""
        assert strict_backend.get_strict(KEY) is None

    def test_get_strict_present_key_returns_the_stored_value(self, strict_backend):
        """A stored value round-trips through the strict read."""
        strict_backend.set(KEY, {"enabled": False})
        assert strict_backend.get_strict(KEY) == {"enabled": False}

    def test_get_strict_failed_read_raises_where_get_returns_default(
        self, failing_backend
    ):
        """The failure the tolerant ``get`` hides as a default surfaces here."""
        # Given
        backend, make_next_read_fail = failing_backend
        backend.set(KEY, {"enabled": False})
        make_next_read_fail()

        # When / Then
        assert backend.get(KEY, {"sentinel": True}) == {"sentinel": True}
        with pytest.raises((ValueError, ConnectionError)):
            backend.get_strict(KEY)

    def test_get_strict_default_on_a_custom_backend_delegates_to_get(self):
        """A backend that does not override it reads through its own ``get``."""

        class _GetOnlyBackend(MemoryStateBackend):
            get_strict = StateBackend.get_strict

            def get(self, key, default=None):
                self.asked = (key, default)
                return {"from": "get"}

        backend = _GetOnlyBackend()

        assert backend.get_strict(KEY) == {"from": "get"}
        assert backend.asked == (KEY, None)


# =============================================================================
# Behavior — update_versioned outcome matrix
# =============================================================================


class TestUpdateVersionedBehavior:
    """Every exit of the versioned write, and what it leaves in the store (D8)."""

    def test_update_versioned_first_attempt_commits_over_the_stored_state(self):
        """Committed: ``before`` is the replaced state, ``after`` the stored one."""
        # Given
        backend = ScriptedStateBackend()
        replaced = _stamped({"enabled": True}, 2, "older-change")
        backend.set(KEY, replaced)

        # When
        result = update_versioned(backend, KEY, _set_enabled(False), token=TOKEN)

        # Then
        assert result.outcome is VersionedWriteOutcome.COMMITTED
        assert result.committed is True
        assert result.before == replaced
        assert result.after == backend.get(KEY)
        assert backend.cas_calls[0][0] == stored_version(replaced)

    def test_update_versioned_absent_key_writes_version_one(self):
        """An absent key is version 0, so the first write stamps 1."""
        backend = ScriptedStateBackend()

        result = update_versioned(backend, KEY, _set_enabled(True), token=TOKEN)

        assert result.committed is True
        assert result.before is None
        assert stored_version(backend.get(KEY)) == 1

    def test_update_versioned_fresh_token_is_generated_when_omitted(self):
        """Two changes without a caller token never share one."""
        backend = ScriptedStateBackend()

        first = update_versioned(backend, KEY, _set_enabled(True))
        second = update_versioned(backend, KEY, _set_enabled(False))

        assert first.token != second.token
        assert carries_writer_token(backend.get(KEY), second.token)

    def test_update_versioned_already_satisfied_commits_without_writing(self):
        """The mutate's ``ALREADY_SATISFIED`` answer writes nothing."""
        backend = ScriptedStateBackend()
        stored = _stamped({"enabled": False}, 3, "other")
        backend.set(KEY, stored)

        result = update_versioned(
            backend, KEY, lambda current: ALREADY_SATISFIED, token=TOKEN
        )

        assert result.outcome is VersionedWriteOutcome.COMMITTED
        assert result.before == stored
        assert result.after == stored
        assert backend.cas_calls == []

    def test_update_versioned_stored_token_commits_without_calling_the_mutate(self):
        """A change already in the store (a reply lost earlier) is not re-applied."""
        # Given: the store carries this change's token
        backend = ScriptedStateBackend()
        backend.set(KEY, _stamped({"enabled": False}, 7, TOKEN))
        mutate = MagicMock(spec=lambda stored: None)

        # When
        result = update_versioned(backend, KEY, mutate, token=TOKEN)

        # Then
        assert result.committed is True
        mutate.assert_not_called()
        assert backend.cas_calls == []

    def test_update_versioned_declined_on_the_fresh_state_writes_nothing(self):
        """The mutate's own precondition, evaluated on the stored state, declines."""
        backend = ScriptedStateBackend()
        stored = _stamped({"enabled": False}, 1, "other")
        backend.set(KEY, stored)

        result = update_versioned(
            backend, KEY, lambda current: Declined("already_disabled"), token=TOKEN
        )

        assert result.outcome is VersionedWriteOutcome.DECLINED
        assert result.decline_reason == "already_disabled"
        assert result.after == stored
        assert backend.cas_calls == []
        assert backend.get(KEY) == stored

    def test_update_versioned_conflict_reruns_the_mutate_on_the_peer_state(self):
        """A lost conditional write re-reads and re-evaluates on what the peer wrote."""
        # Given: a peer commits between this change's read and its write
        backend = ScriptedStateBackend()
        backend.set(KEY, _stamped({"enabled": True, "level": 1}, 1, "a"))
        backend.peer_value = _stamped({"enabled": True, "level": 3}, 2, "peer")
        backend.cas_script = [CAS_PEER]
        seen: list[dict[str, Any] | None] = []

        def mutate(stored):
            seen.append(stored)
            return _set_enabled(False)(stored)

        # When
        result = update_versioned(backend, KEY, mutate, token=TOKEN)

        # Then: the second attempt saw the peer's level and kept it
        assert result.committed is True
        assert [s["level"] for s in seen] == [1, 3]
        assert result.before == backend.peer_value
        assert backend.get(KEY)["level"] == 3
        assert stored_version(backend.get(KEY)) == 3

    def test_update_versioned_conflict_then_decline_writes_nothing(self):
        """A peer's write that the mutate rejects ends the change as declined."""
        backend = ScriptedStateBackend()
        backend.set(KEY, _stamped({"level": 1}, 1, "a"))
        backend.peer_value = _stamped({"level": 3}, 2, "peer")
        backend.cas_script = [CAS_PEER]

        def mutate(stored):
            if stored["level"] >= 2:
                return Declined("already_at_or_above")
            return {"level": 2}

        result = update_versioned(backend, KEY, mutate, token=TOKEN)

        assert result.outcome is VersionedWriteOutcome.DECLINED
        assert backend.get(KEY) == backend.peer_value

    def test_update_versioned_exhausted_attempts_with_unchanged_version_is_not_applied(
        self,
    ):
        """Every attempt lost and the stored version never moved → not applied."""
        backend = ScriptedStateBackend()
        backend.set(KEY, _stamped({"enabled": True}, 5, "other"))
        backend.cas_script = [CAS_LOSE, CAS_LOSE]

        result = update_versioned(
            backend, KEY, _set_enabled(False), token=TOKEN, max_attempts=2
        )

        assert result.outcome is VersionedWriteOutcome.NOT_APPLIED
        assert len(backend.cas_calls) == 2
        assert backend.get(KEY)["enabled"] is True

    def test_update_versioned_exhausted_attempts_with_moved_version_is_unknown(self):
        """A read-back that shows another version cannot tell → unknown."""
        backend = ScriptedStateBackend()
        backend.set(KEY, _stamped({"enabled": True}, 5, "other"))
        backend.peer_value = _stamped({"enabled": True}, 6, "peer")
        backend.cas_script = [CAS_PEER]

        result = update_versioned(
            backend, KEY, _set_enabled(False), token=TOKEN, max_attempts=1
        )

        assert result.outcome is VersionedWriteOutcome.UNKNOWN

    def test_update_versioned_raise_after_landing_reads_back_as_committed(self):
        """A write that landed and then raised is committed, never not applied."""
        backend = ScriptedStateBackend()
        replaced = _stamped({"enabled": True}, 1, "other")
        backend.set(KEY, replaced)
        backend.cas_script = [CAS_LAND_THEN_RAISE]

        result = update_versioned(backend, KEY, _set_enabled(False), token=TOKEN)

        assert result.outcome is VersionedWriteOutcome.COMMITTED
        assert result.before == replaced
        assert carries_writer_token(result.after, TOKEN)

    def test_update_versioned_raise_before_landing_reads_back_as_not_applied(self):
        """The version still expected on read-back → not applied, error kept."""
        backend = ScriptedStateBackend()
        backend.set(KEY, _stamped({"enabled": True}, 1, "other"))
        backend.cas_script = [CAS_RAISE]

        result = update_versioned(backend, KEY, _set_enabled(False), token=TOKEN)

        assert result.outcome is VersionedWriteOutcome.NOT_APPLIED
        assert isinstance(result.error, ConnectionError)
        assert result.read_stored is True

    def test_update_versioned_raise_with_failed_read_back_is_unknown(self):
        """A read-back that fails cannot decide the write → unknown."""
        # Given: the attempt's read succeeds, the write raises, the read-back fails
        backend = ScriptedStateBackend()
        backend.set(KEY, _stamped({"enabled": True}, 1, "other"))
        backend.cas_script = [CAS_RAISE]
        backend.read_errors = [None, TimeoutError("read-back timed out")]

        # When
        result = update_versioned(backend, KEY, _set_enabled(False), token=TOKEN)

        # Then
        assert result.outcome is VersionedWriteOutcome.UNKNOWN
        assert isinstance(result.error, ConnectionError)

    def test_update_versioned_first_strict_read_failure_is_not_applied(self):
        """Nothing was sent before the first read failed → not applied."""
        backend = ScriptedStateBackend()
        backend.read_errors = [ConnectionError("store down")]

        result = update_versioned(backend, KEY, _set_enabled(False), token=TOKEN)

        assert result.outcome is VersionedWriteOutcome.NOT_APPLIED
        assert result.read_stored is False
        assert backend.cas_calls == []

    def test_update_versioned_strict_read_failure_after_a_lost_write_is_unknown(self):
        """A lost write may have landed, so a failed re-read cannot tell → unknown."""
        backend = ScriptedStateBackend()
        backend.set(KEY, _stamped({"enabled": True}, 1, "other"))
        backend.cas_script = [CAS_LOSE]
        backend.read_errors = [None, ConnectionError("store down")]

        result = update_versioned(backend, KEY, _set_enabled(False), token=TOKEN)

        assert result.outcome is VersionedWriteOutcome.UNKNOWN

    def test_update_versioned_mutate_value_is_not_mutated_in_place(self):
        """The stamped copy is written; the mutate's own dict keeps no version fields."""
        backend = ScriptedStateBackend()
        produced: dict[str, Any] = {"enabled": False}

        update_versioned(backend, KEY, lambda stored: produced, token=TOKEN)

        assert produced == {"enabled": False}


# =============================================================================
# Behavior — pending changes decided by the stored token
# =============================================================================


class TestSettlePendingChangesBehavior:
    """An unknown change is decided by whether the next read carries its token."""

    @staticmethod
    def _pending(token: str) -> PendingChange:
        return PendingChange(
            token=token, description=token, on_committed=lambda stored: None
        )

    def test_settle_pending_changes_splits_by_the_stored_writer_token(self):
        """Only the change whose token is stored reads as committed."""
        landed, lost = self._pending("landed"), self._pending("lost")

        committed, not_applied = settle_pending_changes(
            [landed, lost], _stamped({"enabled": False}, 2, "landed")
        )

        assert committed == [landed]
        assert not_applied == [lost]

    @pytest.mark.parametrize(
        "stored",
        [None, {"enabled": False}, _stamped({"enabled": False}, 9, "overwriter")],
        ids=["absent", "unstamped", "overwritten"],
    )
    def test_settle_pending_changes_without_the_token_is_not_applied(self, stored):
        """An overwritten or absent change reads as not applied (disclosed limit)."""
        change = self._pending("landed")

        committed, not_applied = settle_pending_changes([change], stored)

        assert committed == []
        assert not_applied == [change]

    def test_settle_pending_changes_drops_a_record_inherited_across_fork(self):
        """A record another pid made is in neither list: its maker decides it."""
        inherited = dataclasses.replace(
            self._pending("landed"), origin_pid=os.getpid() + 1
        )

        committed, not_applied = settle_pending_changes(
            [inherited], _stamped({"enabled": False}, 2, "landed")
        )

        assert (committed, not_applied) == ([], [])


class TestStoredVersionBehavior:
    """``stored_version`` reads 0 for anything that carries no usable version."""

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            (None, 0),
            ([1, 2], 0),
            ({"enabled": True}, 0),
            ({OCC_VERSION_FIELD: "not-a-number"}, 0),
            ({OCC_VERSION_FIELD: None}, 0),
            ({OCC_VERSION_FIELD: 7}, 7),
        ],
        ids=["absent", "not_a_dict", "unstamped", "garbage", "null", "stamped"],
    )
    def test_stored_version_reads_the_stamp_or_zero(self, stored, expected):
        """A previous release's blind write (no stamp) reads as version 0."""
        assert stored_version(stored) == expected


# =============================================================================
# Behavior — Redis conditional write and construction
# =============================================================================


class TestRedisStateBackendCasBehavior:
    """One WATCH/MULTI/EXEC round, and the canonical default URL (D8, D14)."""

    def test_compare_and_set_watch_error_answers_false_without_a_second_round(self):
        """A lost watch never re-checks and writes its precomputed value again."""
        # Given: the key changes under the watch
        client = _WatchingRedisClient()
        client.store["baldur:state:k"] = json.dumps({OCC_VERSION_FIELD: 0})
        client.watch_error_on_execute = True
        backend = _redis_backend(client)

        # When
        ok = backend.compare_and_set("k", 0, {"v": 1, OCC_VERSION_FIELD: 1})

        # Then
        assert ok is False
        assert [c for c in client.calls if c[0] == "execute"] == [("execute", 1)]
        assert json.loads(client.store["baldur:state:k"]) == {OCC_VERSION_FIELD: 0}

    def test_compare_and_set_version_mismatch_unwatches_without_multi(self):
        """A stale expected version answers False before any transaction."""
        client = _WatchingRedisClient()
        client.store["baldur:state:k"] = json.dumps({OCC_VERSION_FIELD: 4})
        backend = _redis_backend(client)

        ok = backend.compare_and_set("k", 3, {"v": 1, OCC_VERSION_FIELD: 4})

        assert ok is False
        assert ("unwatch",) in client.calls
        assert ("multi",) not in client.calls

    def test_compare_and_set_matching_version_writes_in_one_transaction(self):
        """A matching version queues the SET inside MULTI and executes once."""
        client = _WatchingRedisClient()
        backend = _redis_backend(client)

        ok = backend.compare_and_set("k", 0, {"v": 1, OCC_VERSION_FIELD: 1})

        assert ok is True
        assert client.calls == [
            ("watch", "baldur:state:k"),
            ("multi",),
            ("execute", 1),
        ]
        assert json.loads(client.store["baldur:state:k"]) == {
            "v": 1,
            OCC_VERSION_FIELD: 1,
        }

    def test_default_url_is_the_canonical_redis_default(self):
        """The constructor's default is the one canonical spelling (no inline URL)."""
        default = (
            inspect.signature(RedisStateBackend.__init__)
            .parameters["redis_url"]
            .default
        )

        assert default == DEFAULT_REDIS_URL
