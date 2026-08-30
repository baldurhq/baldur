"""Redis ``trip_to_open`` wrapper + the pin-guarded ``update_state`` write.

The Lua scripts fold HMGET-decide-HSET into one Redis command, so concurrent
workers that each decided to trip produce exactly one ``did_open=True`` and
the durable row is no longer decided by whichever mirror finished last. The
scripts' own state machine is verified against real Redis in
``tests/integration/redis/test_cb_trip_to_open_lua.py`` — fakeredis cannot
reproduce ``EVAL`` semantics under concurrency.

What is pinned here is the Python half: the return-array parsing into a
``CircuitBreakerOpenAttempt`` (including the declined-by-pin sentinel), the
eval dispatch shape, the exception envelope, and the routing of
``update_state``'s two new directives — the ``skip_if_pinned`` fallback in
particular, whose whole purpose is to keep a Redis blip from being counted
against L2's quarantine budget.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
import redis
from structlog.testing import capture_logs

from baldur.adapters.redis.circuit_breaker import (
    _LUA_TRIP_TO_OPEN,
    _LUA_UPDATE_STATE_SKIP_IF_PINNED,
    RedisCircuitBreakerStateRepository,
)
from baldur.adapters.resilient.backend import ResilientStorageBackend
from baldur.interfaces.repositories import (
    CIRCUIT_BREAKER_PINNED_TOKEN,
    CircuitBreakerOpenAttempt,
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)
from baldur.utils.time import utc_now

FAILURE_COUNT = 5
OPENED_AT_ISO = b"2026-08-25T10:00:00+00:00"
EXPIRES_AT_ISO = b"2026-08-25T11:00:00+00:00"


def _make_repo(eval_return) -> tuple[RedisCircuitBreakerStateRepository, MagicMock]:
    """Construct a repo whose Redis ``eval`` returns the given Lua array.

    Same seam as the shipped open-check tests: the wrapper reaches Redis
    through the backend's public ``raw_redis_client``.
    """
    backend = MagicMock(spec=ResilientStorageBackend)
    backend._get_full_key.side_effect = lambda key: f"baldur:{key}"
    redis_client = MagicMock(spec=redis.Redis)
    redis_client.eval.return_value = eval_return
    backend.raw_redis_client = redis_client
    repo = RedisCircuitBreakerStateRepository(backend=backend)
    return repo, backend


# =============================================================================
# Contract — Lua return-array shape -> CircuitBreakerOpenAttempt mapping
# =============================================================================


class TestRedisTripToOpenContract:
    """Each Lua branch's return array maps to a specific attempt shape."""

    def test_trip_winner_branch_returns_did_open_true(self):
        # Lua write branch: {1, 'open', now_iso, ''}.
        repo, _backend = _make_repo([1, b"open", OPENED_AT_ISO, b""])

        attempt = repo.trip_to_open("svc", FAILURE_COUNT)

        assert isinstance(attempt, CircuitBreakerOpenAttempt)
        assert attempt.did_open is True
        assert attempt.state.state == CircuitBreakerStateEnum.OPEN.value
        # The writeback depends on opened_at, so it must survive parsing.
        assert attempt.state.opened_at is not None
        # The winner reports the count it asked the store to write.
        assert attempt.state.failure_count == FAILURE_COUNT

    def test_open_race_loser_carries_the_existing_opened_at(self):
        # Lua race-loser branch: {0, 'open', <existing opened_at>, ''}.
        repo, _backend = _make_repo([0, b"open", OPENED_AT_ISO, b""])

        attempt = repo.trip_to_open("svc", FAILURE_COUNT)

        assert attempt.did_open is False
        assert attempt.state.state == CircuitBreakerStateEnum.OPEN.value
        assert attempt.state.opened_at is not None
        # A loser wrote nothing, so it must not report the count it wanted.
        assert attempt.state.failure_count == 0

    def test_half_open_branch_returns_no_write_verdict(self):
        # Lua recency branch: {0, 'half_open', '', ''}.
        repo, _backend = _make_repo([0, b"half_open", b"", b""])

        attempt = repo.trip_to_open("svc", FAILURE_COUNT)

        assert attempt.did_open is False
        assert attempt.state.state == CircuitBreakerStateEnum.HALF_OPEN.value
        assert attempt.state.opened_at is None

    def test_corrupted_state_branch_is_returned_verbatim(self):
        # The layered wrapper routes anything outside {open, half_open,
        # pinned} into degraded mode, so the token has to survive intact.
        repo, _backend = _make_repo([0, b"corrupted", b"", b""])

        attempt = repo.trip_to_open("svc", FAILURE_COUNT)

        assert attempt.did_open is False
        assert attempt.state.state == "corrupted"

    def test_pinned_branch_returns_the_sentinel_with_the_expiry(self):
        # Lua declined branch: {0, 'pinned', '', <expires_at>}.
        repo, _backend = _make_repo([0, b"pinned", b"", EXPIRES_AT_ISO])

        attempt = repo.trip_to_open("svc", FAILURE_COUNT)

        assert attempt.did_open is False
        assert attempt.state.state == CIRCUIT_BREAKER_PINNED_TOKEN
        assert attempt.state.manually_controlled is True
        assert attempt.state.manual_override_expires_at is not None

    def test_pinned_branch_without_expiry_reports_an_open_ended_override(self):
        repo, _backend = _make_repo([0, b"pinned", b"", b""])

        attempt = repo.trip_to_open("svc", FAILURE_COUNT)

        assert attempt.state.state == CIRCUIT_BREAKER_PINNED_TOKEN
        assert attempt.state.manual_override_expires_at is None

    def test_returned_state_data_uses_synthetic_defaults(self):
        # Auxiliary fields are synthesized rather than fetched in a second
        # RTT — callers read did_open, state.state, state.opened_at, and the
        # pin expiry on the declined branch.
        repo, _backend = _make_repo([1, b"open", OPENED_AT_ISO, b""])

        attempt = repo.trip_to_open("svc", FAILURE_COUNT)

        assert isinstance(attempt.state, CircuitBreakerStateData)
        assert attempt.state.service_name == "svc"
        assert attempt.state.success_count == 0
        assert attempt.state.half_open_request_count == 0
        assert attempt.state.manually_controlled is False
        assert attempt.state.metadata == {}


# =============================================================================
# Behavior — eval() dispatch shape + exception envelope
# =============================================================================


class TestRedisTripToOpenBehavior:
    """Wrapper-level behavior around the Lua eval call."""

    def test_eval_receives_the_trip_script_and_the_full_key(self):
        repo, backend = _make_repo([1, b"open", OPENED_AT_ISO, b""])

        repo.trip_to_open("payment", FAILURE_COUNT)

        eval_mock = backend.raw_redis_client.eval
        eval_mock.assert_called_once()
        args = eval_mock.call_args.args
        assert args[0] is _LUA_TRIP_TO_OPEN
        assert args[1] == 1  # numkeys
        assert args[2] == "baldur:cb:payment"

    def test_eval_receives_the_failure_count_as_a_string(self):
        # Lua ARGV is a string vector; an int would reach redis-py as one
        # anyway, but the HSET writes the value verbatim, so the wrapper
        # normalizes it rather than depending on the driver.
        repo, backend = _make_repo([1, b"open", OPENED_AT_ISO, b""])

        repo.trip_to_open("svc", FAILURE_COUNT)

        failure_argv = backend.raw_redis_client.eval.call_args.args[4]
        assert failure_argv == str(FAILURE_COUNT)

    def test_eval_now_stamp_is_plain_isoformat(self):
        # The pin-expiry guard compares isoformat strings lexicographically,
        # which only holds while every writer stamps `utc_now().isoformat()`
        # verbatim — no timespec argument, no 'Z' normalization.
        repo, backend = _make_repo([1, b"open", OPENED_AT_ISO, b""])

        repo.trip_to_open("svc", FAILURE_COUNT)

        now_iso = backend.raw_redis_client.eval.call_args.args[3]
        assert isinstance(now_iso, str)
        assert not now_iso.endswith("Z")
        # Round-trips through the same parser the adapter reads stamps with.
        from datetime import datetime

        assert datetime.fromisoformat(now_iso).tzinfo is not None

    def test_eval_failure_propagates_after_warning_log(self):
        # The override re-raises so the layered wrapper records degraded mode
        # and falls back to L1 — a swallowed error would report a trip that
        # never reached the store.
        repo, backend = _make_repo([1, b"open", b"", b""])
        backend.raw_redis_client.eval.side_effect = ConnectionError("redis down")

        with capture_logs() as caplog:
            with pytest.raises(ConnectionError, match="redis down"):
                repo.trip_to_open("svc", FAILURE_COUNT)

        assert any(
            entry.get("event") == "redis_cb_repo.trip_to_open_failed"
            and entry.get("log_level") == "warning"
            for entry in caplog
        )

    def test_absent_redis_client_propagates_to_the_degraded_path(self):
        # Degraded mode (no live client) has no atomic substrate, so the
        # primitive must fail rather than answer from the memory + WAL path
        # as though the cluster had decided.
        repo, backend = _make_repo([1, b"open", b"", b""])
        backend.raw_redis_client = None

        with pytest.raises(Exception):  # noqa: B017 — any failure reaches the fallback
            repo.trip_to_open("svc", FAILURE_COUNT)


# =============================================================================
# Behavior — update_state write directives
# =============================================================================


class TestRedisUpdateStateClearOpenedAtBehavior:
    """``clear_opened_at`` writes an empty field rather than skipping it."""

    def test_directive_writes_an_empty_opened_at(self):
        repo, backend = _make_repo(None)

        repo.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.CLOSED.value,
            clear_opened_at=True,
        )

        updates = backend.hset.call_args.args[1]
        assert updates["opened_at"] == ""

    def test_without_the_directive_a_none_timestamp_is_omitted(self):
        # The keep semantics the directive exists to override: an absent
        # opened_at must not appear in the HSET field set at all.
        repo, backend = _make_repo(None)

        repo.update_state(
            service_name="svc", state=CircuitBreakerStateEnum.CLOSED.value
        )

        assert "opened_at" not in backend.hset.call_args.args[1]

    def test_directive_wins_over_a_supplied_timestamp(self):
        repo, backend = _make_repo(None)

        repo.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.CLOSED.value,
            opened_at=utc_now(),
            clear_opened_at=True,
        )

        assert backend.hset.call_args.args[1]["opened_at"] == ""


class TestRedisUpdateStateSkipIfPinnedBehavior:
    """The store-side pin guard, and what happens when Lua is unavailable."""

    def test_directive_routes_the_write_through_the_conditional_script(self):
        repo, backend = _make_repo(1)

        result = repo.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.CLOSED.value,
            failure_count=0,
            skip_if_pinned=True,
        )

        assert result is True
        # The plain HSET path is bypassed entirely: check and write are one
        # script invocation, so an override taken between them cannot be
        # overwritten.
        backend.hset.assert_not_called()
        args = backend.raw_redis_client.eval.call_args.args
        assert args[0] is _LUA_UPDATE_STATE_SKIP_IF_PINNED
        assert args[1] == 1
        assert args[2] == "baldur:cb:svc"

    def test_directive_flattens_the_field_set_into_argv(self):
        repo, backend = _make_repo(1)

        repo.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.CLOSED.value,
            failure_count=3,
            skip_if_pinned=True,
        )

        # ARGV[1] is the now stamp; the field/value pairs follow it flattened.
        argv = backend.raw_redis_client.eval.call_args.args[4:]
        pairs = dict(zip(argv[::2], argv[1::2], strict=True))
        assert pairs["state"] == CircuitBreakerStateEnum.CLOSED.value
        assert pairs["failure_count"] == "3"

    def test_declined_write_is_reported_as_success(self):
        # The Lua returns 0 when the pin declined the write. The caller asked
        # for a write it authorized the store to elide, so quarantine
        # accounting must see a healthy answer.
        repo, _backend = _make_repo(0)

        result = repo.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.CLOSED.value,
            skip_if_pinned=True,
        )

        assert result is True

    def test_script_failure_falls_back_to_read_check_write(self):
        # A Redis blip must not become a raise: the backend write owns the
        # degrade-and-WAL response, and the mirror's caller would otherwise
        # count a survivable blip toward quarantining L2.
        repo, backend = _make_repo(1)
        backend.raw_redis_client.eval.side_effect = ConnectionError("blip")
        backend.hset.return_value = True
        unpinned_row = CircuitBreakerStateData(
            service_name="svc", state=CircuitBreakerStateEnum.OPEN.value
        )

        with capture_logs() as caplog:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(repo, "get_state", lambda service_name: unpinned_row)
                result = repo.update_state(
                    service_name="svc",
                    state=CircuitBreakerStateEnum.CLOSED.value,
                    skip_if_pinned=True,
                )

        assert result is True
        backend.hset.assert_called_once()
        assert any(
            entry.get("event") == "redis_cb_repo.pin_guarded_update_failed"
            and entry.get("log_level") == "warning"
            for entry in caplog
        )

    def test_unnamed_default_address_reports_the_failure_at_debug(self):
        # Nobody named a Redis, so an unreachable one is the expected state of
        # a zero-config run rather than an incident. Before this write grew a
        # pin guard it reported nothing at all here; at WARNING it fills a
        # first run's console at the background refresh cadence.
        #
        # The reach is pinned True rather than left to the spec'd double's
        # truthy default: this is the blip window, where the dial goes through
        # and the report is what splits on posture.
        repo, backend = _make_repo(1)
        backend.raw_redis_client.eval.side_effect = ConnectionError("blip")
        backend.hset.return_value = True
        backend.has_reached_redis = True
        backend._probing_unconfigured_default.return_value = True
        unpinned_row = CircuitBreakerStateData(
            service_name="svc", state=CircuitBreakerStateEnum.OPEN.value
        )

        with capture_logs() as caplog:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(repo, "get_state", lambda service_name: unpinned_row)
                result = repo.update_state(
                    service_name="svc",
                    state=CircuitBreakerStateEnum.CLOSED.value,
                    skip_if_pinned=True,
                )

        assert result is True
        reports = [
            entry
            for entry in caplog
            if entry.get("event") == "redis_cb_repo.pin_guarded_update_failed"
        ]
        assert len(reports) == 1
        assert reports[0].get("log_level") == "debug"

    def test_a_named_redis_keeps_the_failure_at_warning(self):
        # The other half of the split, pinned explicitly rather than left to
        # the spec'd double's truthiness: a store someone configured that
        # cannot take the write is a real incident.
        repo, backend = _make_repo(1)
        backend.raw_redis_client.eval.side_effect = ConnectionError("blip")
        backend.hset.return_value = True
        backend._probing_unconfigured_default.return_value = False
        unpinned_row = CircuitBreakerStateData(
            service_name="svc", state=CircuitBreakerStateEnum.OPEN.value
        )

        with capture_logs() as caplog:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(repo, "get_state", lambda service_name: unpinned_row)
                repo.update_state(
                    service_name="svc",
                    state=CircuitBreakerStateEnum.CLOSED.value,
                    skip_if_pinned=True,
                )

        assert any(
            entry.get("event") == "redis_cb_repo.pin_guarded_update_failed"
            and entry.get("log_level") == "warning"
            for entry in caplog
        )

    def test_a_backend_without_the_probe_stays_loud(self):
        # Silence is the dangerous direction, so anything that is not exactly
        # True keeps the WARNING — here a backend carrying no probe at all.
        repo, backend = _make_repo(1)
        backend.raw_redis_client.eval.side_effect = ConnectionError("blip")
        backend.hset.return_value = True
        del backend._probing_unconfigured_default
        unpinned_row = CircuitBreakerStateData(
            service_name="svc", state=CircuitBreakerStateEnum.OPEN.value
        )

        with capture_logs() as caplog:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(repo, "get_state", lambda service_name: unpinned_row)
                repo.update_state(
                    service_name="svc",
                    state=CircuitBreakerStateEnum.CLOSED.value,
                    skip_if_pinned=True,
                )

        assert any(
            entry.get("event") == "redis_cb_repo.pin_guarded_update_failed"
            and entry.get("log_level") == "warning"
            for entry in caplog
        )

    def test_a_never_reached_unnamed_store_is_not_dialed_at_all(self):
        # The write that owns its fallback outright: rather than raise, it
        # skips the script and takes the read-check-write below, where the
        # "store" is the same process-local memory + WAL the script would
        # have been mirroring. Nothing failed, so nothing is reported —
        # not even the DEBUG the blip window keeps.
        repo, backend = _make_repo(1)
        backend.hset.return_value = True
        backend.has_reached_redis = False
        backend._probing_unconfigured_default.return_value = True
        unpinned_row = CircuitBreakerStateData(
            service_name="svc", state=CircuitBreakerStateEnum.OPEN.value
        )

        with capture_logs() as caplog:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(repo, "get_state", lambda service_name: unpinned_row)
                result = repo.update_state(
                    service_name="svc",
                    state=CircuitBreakerStateEnum.CLOSED.value,
                    skip_if_pinned=True,
                )

        assert result is True
        backend.raw_redis_client.eval.assert_not_called()
        backend.hset.assert_called_once()
        assert [
            entry
            for entry in caplog
            if entry.get("event") == "redis_cb_repo.pin_guarded_update_failed"
        ] == []

    def test_a_never_reached_unnamed_store_still_honors_an_active_pin(self):
        # The skip is a change of route, not of verdict: the local re-check
        # is the same pin neutrality the script would have enforced.
        repo, backend = _make_repo(1)
        backend.has_reached_redis = False
        backend._probing_unconfigured_default.return_value = True
        pinned_row = CircuitBreakerStateData(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=True,
            manual_override_expires_at=utc_now() + timedelta(minutes=10),
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "get_state", lambda service_name: pinned_row)
            result = repo.update_state(
                service_name="svc",
                state=CircuitBreakerStateEnum.CLOSED.value,
                skip_if_pinned=True,
            )

        assert result is True
        backend.hset.assert_not_called()

    def test_no_live_client_declines_on_an_actively_pinned_row(self):
        # Degraded mode: the "store" is process-local memory + WAL, so a
        # check-then-write gap costs nothing — but the guard must still hold.
        repo, backend = _make_repo(None)
        backend.raw_redis_client = None
        pinned_row = CircuitBreakerStateData(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=True,
            manual_override_expires_at=utc_now() + timedelta(minutes=10),
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "get_state", lambda service_name: pinned_row)
            result = repo.update_state(
                service_name="svc",
                state=CircuitBreakerStateEnum.CLOSED.value,
                skip_if_pinned=True,
            )

        assert result is True
        backend.hset.assert_not_called()

    def test_no_live_client_writes_through_a_lapsed_pin(self):
        repo, backend = _make_repo(None)
        backend.raw_redis_client = None
        backend.hset.return_value = True
        lapsed_row = CircuitBreakerStateData(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=True,
            manual_override_expires_at=utc_now() - timedelta(seconds=1),
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "get_state", lambda service_name: lapsed_row)
            result = repo.update_state(
                service_name="svc",
                state=CircuitBreakerStateEnum.CLOSED.value,
                skip_if_pinned=True,
            )

        assert result is True
        backend.hset.assert_called_once()

    def test_without_the_directive_the_plain_hset_path_is_used(self):
        repo, backend = _make_repo(None)

        repo.update_state(
            service_name="svc", state=CircuitBreakerStateEnum.CLOSED.value
        )

        backend.hset.assert_called_once()
        backend.raw_redis_client.eval.assert_not_called()
