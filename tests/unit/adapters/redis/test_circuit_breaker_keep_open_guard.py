"""The keep-open guard on the Redis circuit-breaker repository's guarded write.

793 D3. ``update_state(keep_open=True)`` routes through the conditional Lua
script with two explicit guard flags — ``ARGV[2]`` for the pin guard,
``ARGV[3]`` for the keep-open guard, fields from ``ARGV[4]`` — so the store
declines a CLOSED write against a stored ``open`` / ``half_open`` row in the
same invocation that would perform it. Every route that cannot run the script
falls back to a read-check-write through the backend with both guards applied
against the row it answers. The one route that neither checks nor writes: a
keep-open CLOSED write while the backend is degraded and a Redis was named —
declined outright, before and after the guard read, so it never becomes a WAL
record that replays CLOSED over a peer's trip. A guarded OPEN write still
reaches the backend; on the unreached-default route the process memory is the
store and the local read-check-write stays exact.

The Lua body is not executed here (the client's ``eval`` is a double); the
decline the script performs is pinned by the ``requires_redis`` integration
suite. What this file asserts is the wiring: the flags, the script text, the
fallback and the degraded decline.

Verification techniques applied:
- Contract: ARGV positions and values, the script's state check
- Error path: a raising ``eval`` falls back and declines on a stored OPEN row;
  ``skip_if_pinned=False`` leaves the pin check out of the fallback
- Degraded: the CLOSED decline before and after the guard read; an OPEN write
  still reaches ``hset``; the unreached-default route keeps the local check
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, PropertyMock

import pytest
import redis

from baldur.adapters.redis.circuit_breaker import (
    _LUA_UPDATE_STATE_SKIP_IF_PINNED,
    RedisCircuitBreakerStateRepository,
)
from baldur.adapters.resilient.backend import ResilientStorageBackend
from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)
from baldur.utils.time import utc_now

SVC = "payment"
CLOSED = CircuitBreakerStateEnum.CLOSED.value
OPEN = CircuitBreakerStateEnum.OPEN.value
HALF_OPEN = CircuitBreakerStateEnum.HALF_OPEN.value

# Python-side positions in the ``eval`` call: script, numkeys, key, then ARGV.
_ARGV_PIN_FLAG = 4
_ARGV_KEEP_OPEN_FLAG = 5
_ARGV_FIELDS_START = 6


def _make_repo(
    eval_return=1, *, degraded: bool = False, unreached_default: bool = False
) -> tuple[RedisCircuitBreakerStateRepository, MagicMock]:
    """A repo over a spec'd backend with its posture terms pinned.

    ``is_degraded`` is pinned explicitly: a spec'd double answers a property
    with a truthy mock, which would read as "degraded" on every arm.
    """
    backend = MagicMock(spec=ResilientStorageBackend)
    backend._get_full_key.side_effect = lambda key: f"baldur:{key}"
    backend.is_degraded = degraded
    backend.hset.return_value = True
    client = MagicMock(spec=redis.Redis)
    client.eval.return_value = eval_return
    backend.raw_redis_client = client
    if unreached_default:
        backend.has_reached_redis = False
        backend._probing_unconfigured_default.return_value = True
    return RedisCircuitBreakerStateRepository(backend=backend), backend


def _row(state: str, **overrides) -> CircuitBreakerStateData:
    return CircuitBreakerStateData(service_name=SVC, state=state, **overrides)


def _closed_reset(repo, **directives) -> bool:
    """The consecutive-count reset's write, with the given guard directives."""
    return repo.update_state(
        service_name=SVC, state=CLOSED, failure_count=0, **directives
    )


class TestRedisKeepOpenGuardBehavior:
    """The flags the script receives, and every route around the script."""

    # ------------------------------------------------------------ contract

    def test_keep_open_routes_through_the_conditional_script_with_the_flag(self):
        repo, backend = _make_repo()

        result = _closed_reset(repo, keep_open=True)

        assert result is True
        backend.hset.assert_not_called()
        args = backend.raw_redis_client.eval.call_args.args
        assert args[0] is _LUA_UPDATE_STATE_SKIP_IF_PINNED
        assert args[_ARGV_PIN_FLAG] == "0"
        assert args[_ARGV_KEEP_OPEN_FLAG] == "1"

    @pytest.mark.parametrize(
        ("directives", "expected_flags"),
        [
            ({"skip_if_pinned": True}, ("1", "0")),
            ({"keep_open": True}, ("0", "1")),
            ({"skip_if_pinned": True, "keep_open": True}, ("1", "1")),
        ],
        ids=["pin_only", "keep_open_only", "both"],
    )
    def test_each_guard_is_its_own_flag(self, directives, expected_flags):
        """``keep_open`` alone never adds the pin guard the caller did not ask for."""
        repo, backend = _make_repo()

        _closed_reset(repo, **directives)

        args = backend.raw_redis_client.eval.call_args.args
        assert (args[_ARGV_PIN_FLAG], args[_ARGV_KEEP_OPEN_FLAG]) == expected_flags

    def test_fields_follow_the_two_flags(self):
        repo, backend = _make_repo()

        repo.update_state(
            service_name=SVC, state=CLOSED, failure_count=3, keep_open=True
        )

        argv = backend.raw_redis_client.eval.call_args.args[_ARGV_FIELDS_START:]
        pairs = dict(zip(argv[::2], argv[1::2], strict=True))
        assert pairs["state"] == CLOSED
        assert pairs["failure_count"] == "3"

    def test_script_text_carries_the_keep_open_state_check(self):
        """The Lua reads the stored state and declines on the two non-CLOSED values."""
        script = _LUA_UPDATE_STATE_SKIP_IF_PINNED

        assert "'state'" in script
        assert "guard_keep_open" in script
        assert "stored_state == 'open'" in script
        assert "stored_state == 'half_open'" in script
        assert "for i = 4, #ARGV" in script

    def test_declined_script_verdict_is_reported_as_success(self):
        repo, _backend = _make_repo(eval_return=0)

        assert _closed_reset(repo, keep_open=True) is True

    def test_without_either_directive_the_plain_hset_path_is_used(self):
        repo, backend = _make_repo()

        _closed_reset(repo)

        backend.hset.assert_called_once()
        backend.raw_redis_client.eval.assert_not_called()

    # ----------------------------------------------------------- error path

    @pytest.mark.parametrize(
        "stored_state", [OPEN, HALF_OPEN], ids=["open", "half_open"]
    )
    def test_script_failure_falls_back_and_declines_on_a_stored_non_closed_row(
        self, stored_state
    ):
        repo, backend = _make_repo()
        backend.raw_redis_client.eval.side_effect = ConnectionError("blip")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "get_state", lambda service_name: _row(stored_state))
            result = _closed_reset(repo, keep_open=True)

        assert result is True
        backend.hset.assert_not_called()

    def test_script_failure_falls_back_and_writes_on_a_stored_closed_row(self):
        repo, backend = _make_repo()
        backend.raw_redis_client.eval.side_effect = ConnectionError("blip")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                repo, "get_state", lambda service_name: _row(CLOSED, failure_count=3)
            )
            result = _closed_reset(repo, keep_open=True)

        assert result is True
        backend.hset.assert_called_once()

    def test_fallback_without_the_pin_directive_skips_the_pin_check(self):
        """``skip_if_pinned=False, keep_open=True``: a pinned CLOSED row is written."""
        repo, backend = _make_repo()
        backend.raw_redis_client.eval.side_effect = ConnectionError("blip")
        pinned_closed = _row(
            CLOSED,
            manually_controlled=True,
            manual_override_expires_at=utc_now() + timedelta(minutes=10),
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "get_state", lambda service_name: pinned_closed)
            result = _closed_reset(repo, keep_open=True)

        assert result is True
        backend.hset.assert_called_once()

    def test_fallback_with_the_pin_directive_still_declines_a_pinned_row(self):
        """Control: the pin guard is applied when asked for, alongside keep-open."""
        repo, backend = _make_repo()
        backend.raw_redis_client.eval.side_effect = ConnectionError("blip")
        pinned_closed = _row(
            CLOSED,
            manually_controlled=True,
            manual_override_expires_at=utc_now() + timedelta(minutes=10),
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "get_state", lambda service_name: pinned_closed)
            result = _closed_reset(repo, skip_if_pinned=True, keep_open=True)

        assert result is True
        backend.hset.assert_not_called()

    # ------------------------------------------------------------- degraded

    def test_degraded_backend_declines_a_guarded_closed_write_before_any_read(self):
        """The guard read would answer from process memory; the write would hit the WAL."""
        repo, backend = _make_repo(degraded=True)

        result = _closed_reset(repo, keep_open=True)

        assert result is True
        backend.hset.assert_not_called()
        backend.hgetall.assert_not_called()
        backend.raw_redis_client.eval.assert_not_called()

    def test_backend_that_degrades_on_the_guard_read_is_declined_after_it(self):
        """The guard read itself is the ``hgetall`` that flips the backend.

        Not degraded at entry; the script fails; the fallback's read flips the
        backend to degraded and answers nothing — the write is still declined.
        """
        repo, backend = _make_repo()
        backend.raw_redis_client.eval.side_effect = ConnectionError("blip")
        degraded = PropertyMock(side_effect=[False, False, True, True])
        type(backend).is_degraded = degraded
        try:

            def _flipping_read(key):
                # The read that degrades the backend answers from local memory.
                return {}

            backend.hgetall.side_effect = _flipping_read

            result = _closed_reset(repo, keep_open=True)
        finally:
            del type(backend).is_degraded

        assert result is True
        backend.hgetall.assert_called_once()
        backend.hset.assert_not_called()

    def test_degraded_backend_still_takes_a_guarded_open_write(self):
        """An OPEN write can only make the store more restrictive: it reaches the WAL."""
        repo, backend = _make_repo(degraded=True)
        backend.raw_redis_client.eval.side_effect = ConnectionError("blip")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "get_state", lambda service_name: _row(CLOSED))
            result = repo.update_state(
                service_name=SVC, state=OPEN, opened_at=utc_now(), keep_open=True
            )

        assert result is True
        backend.hset.assert_called_once()

    def test_degraded_backend_without_keep_open_still_writes_a_closed_row(self):
        """Control: only the guarded CLOSED write is declined on a degraded backend."""
        repo, backend = _make_repo(degraded=True)
        backend.raw_redis_client.eval.side_effect = ConnectionError("blip")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "get_state", lambda service_name: _row(CLOSED))
            result = _closed_reset(repo, skip_if_pinned=True)

        assert result is True
        backend.hset.assert_called_once()

    @pytest.mark.parametrize(
        ("stored_state", "expect_write"),
        [(OPEN, False), (HALF_OPEN, False), (CLOSED, True)],
        ids=["open_declines", "half_open_declines", "closed_writes"],
    )
    def test_unreached_default_route_keeps_the_local_read_check_write(
        self, stored_state, expect_write
    ):
        """Nobody named a store: the process memory is the store, the check is exact."""
        repo, backend = _make_repo(degraded=True, unreached_default=True)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "get_state", lambda service_name: _row(stored_state))
            result = _closed_reset(repo, keep_open=True)

        assert result is True
        backend.raw_redis_client.eval.assert_not_called()
        assert backend.hset.called is expect_write
