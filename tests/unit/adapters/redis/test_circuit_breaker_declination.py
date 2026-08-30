"""The declination guard in front of every raw-client dial this adapter makes.

Five circuit-breaker paths run their Lua scripts against the raw Redis client,
bypassing the storage backend's availability gate and its probe cooldown, so
each invocation opens a connection of its own, fails on its own and reports on
its own. The guard reproduces the gate the dial went around — but only for the
one posture where dialing can never succeed and nobody asked for it: the
backend has **never** reached a Redis, and nobody named one.

The reach half is load-bearing and is deliberately not ``is_degraded``. That
one is also True for the whole degraded and recovering window of a process
whose Redis is answering again, where the raw client stays usable; gating on
it would drop the store-side pin guard and the cluster-wide single-winner trip
on every blip. ``TestBlipWindowKeepsDialing`` is the arm that separates the
two implementations — an ``is_degraded``-based guard passes everything else in
this module and fails only there.

The four lanes whose fallback lives in their caller signal the decline with a
typed ``UnconfiguredStoreError`` rather than a return value, so the layered
wrapper can tell "the store declined" from "the store failed" and keep the
decline off its quarantine counter. The fifth path — the pin-guarded write —
owns its fallback outright and is covered next to the rest of that write, in
``test_circuit_breaker_trip_to_open.py``.
"""

from __future__ import annotations

import tempfile
from unittest.mock import MagicMock, patch

import pytest
import redis
from structlog.testing import capture_logs

from baldur.adapters.cache.redis_adapter import RedisCacheAdapter
from baldur.adapters.redis.circuit_breaker import (
    _LUA_TRIP_TO_OPEN,
    _LUA_UPDATE_STATE_SKIP_IF_PINNED,
    RedisCircuitBreakerStateRepository,
)
from baldur.adapters.resilient.backend import ResilientStorageBackend
from baldur.core.exceptions import AdapterError, UnconfiguredStoreError
from baldur.interfaces.repositories import CircuitBreakerStateEnum
from baldur.settings.resilient_storage import ResilientStorageSettings

SVC = "payment"
OPENED_AT_ISO = b"2026-08-25T10:00:00+00:00"

_ABSENT = object()


def _make_repo(
    eval_return=None,
    *,
    reached=_ABSENT,
    unconfigured=_ABSENT,
) -> tuple[RedisCircuitBreakerStateRepository, MagicMock]:
    """A repo over a spec'd backend whose two guard terms are pinned.

    Both terms default to *unpinned* — the shape every other suite in the
    tree constructs — so the arms that leave them alone assert what a spec'd
    double already does today.
    """
    backend = MagicMock(spec=ResilientStorageBackend)
    backend._get_full_key.side_effect = lambda key: f"baldur:{key}"
    client = MagicMock(spec=redis.Redis)
    client.eval.return_value = eval_return
    backend.raw_redis_client = client
    if reached is _ABSENT:
        del backend.has_reached_redis
    else:
        backend.has_reached_redis = reached
    if unconfigured is _ABSENT:
        del backend._probing_unconfigured_default
    else:
        backend._probing_unconfigured_default.return_value = unconfigured
    return RedisCircuitBreakerStateRepository(backend=backend), backend


# =============================================================================
# Behavior — the predicate every governed lane consults
# =============================================================================


class TestDeclinationPredicateBehavior:
    """``_declining_unreached_default()`` over both of its terms.

    One cell of the table is True. Every other cell — including every shape
    the predicate does not recognise — keeps dialing, which is the direction
    that preserves today's behavior for anything this repository was not
    written against.
    """

    def test_never_reached_and_never_named_is_the_one_declining_cell(self):
        repo, _backend = _make_repo(reached=False, unconfigured=True)

        assert repo._declining_unreached_default() is True

    @pytest.mark.parametrize(
        ("reached", "unconfigured"),
        [
            (True, True),
            (_ABSENT, True),
            (None, True),
            (False, False),
            (False, _ABSENT),
            (False, 1),
            (False, "yes"),
        ],
        ids=[
            "once_reached_unnamed_store",
            "backend_without_the_reach_property",
            "reach_answering_something_unrecognised",
            "never_reached_but_named_store",
            "backend_without_the_posture_probe",
            "posture_answering_one",
            "posture_answering_a_string",
        ],
    )
    def test_every_other_cell_keeps_dialing(self, reached, unconfigured):
        repo, _backend = _make_repo(reached=reached, unconfigured=unconfigured)

        assert repo._declining_unreached_default() is False

    def test_a_spec_bounded_double_keeps_dialing(self):
        """Why no existing suite in the tree changed behavior.

        ``MagicMock(spec=ResilientStorageBackend)`` answers the reach property
        with a truthy mock rather than False, so the guard admits it — which
        is what keeps every construction built on a spec'd double dialing its
        mocked client exactly as before.
        """
        backend = MagicMock(spec=ResilientStorageBackend)
        repo = RedisCircuitBreakerStateRepository(backend=backend)

        assert backend.has_reached_redis is not False
        assert repo._declining_unreached_default() is False

    def test_the_posture_probe_is_not_consulted_once_a_redis_has_answered(self):
        """Short-circuit: reach alone settles the admitted case.

        The store-side guarantees of a process that has reached its Redis do
        not depend on who named the address, so the second term is never
        reached — and cannot cost a settings resolution on the hot path.
        """
        repo, backend = _make_repo(reached=True, unconfigured=True)

        assert repo._declining_unreached_default() is False
        backend._probing_unconfigured_default.assert_not_called()


# =============================================================================
# Behavior — the four lanes whose fallback lives in their caller
# =============================================================================


def _lane_try_acquire(repo):
    return repo.try_acquire_half_open_slot(SVC, 3, 60)


def _lane_close_check(repo):
    return repo.record_success_with_close_check(SVC, 2)


def _lane_open_check(repo):
    return repo.record_failure_with_open_check(SVC)


def _lane_trip(repo):
    return repo.trip_to_open(SVC, 5)


LANES = [
    pytest.param(
        _lane_try_acquire,
        [1, b"open", b"half_open", b"transition"],
        "try_acquire_half_open_slot",
        id="try_acquire_half_open_slot",
    ),
    pytest.param(
        _lane_close_check,
        [1, b"closed", 0],
        "record_success_with_close_check",
        id="record_success_with_close_check",
    ),
    pytest.param(
        _lane_open_check,
        [1, b"open", OPENED_AT_ISO],
        "record_failure_with_open_check",
        id="record_failure_with_open_check",
    ),
    pytest.param(
        _lane_trip,
        [1, b"open", OPENED_AT_ISO, b""],
        "trip_to_open",
        id="trip_to_open",
    ),
]


class TestUnconfiguredLaneDeclinationBehavior:
    """Each governed lane, across the three postures it can be called in."""

    @pytest.mark.parametrize(("call", "eval_return", "operation"), LANES)
    def test_never_reached_unnamed_store_raises_without_dialing(
        self, call, eval_return, operation
    ):
        repo, backend = _make_repo(eval_return, reached=False, unconfigured=True)

        with pytest.raises(UnconfiguredStoreError) as excinfo:
            call(repo)

        backend.raw_redis_client.eval.assert_not_called()
        assert excinfo.value.operation == operation
        assert excinfo.value.service == SVC

    @pytest.mark.parametrize(("call", "eval_return", "operation"), LANES)
    def test_a_decline_reports_nothing_at_any_level(self, call, eval_return, operation):
        """The whole point: a zero-config run's console stays empty.

        Asserted over every record the lane could emit rather than over its
        own failure event alone — the decline is raised above the ``try`` so
        that the method's own handler never sees it, and a guard placed one
        line lower would log at WARNING here and still raise.
        """
        repo, _backend = _make_repo(eval_return, reached=False, unconfigured=True)

        with capture_logs() as logs:
            with pytest.raises(UnconfiguredStoreError):
                call(repo)

        assert logs == []

    @pytest.mark.parametrize(("call", "eval_return", "operation"), LANES)
    def test_a_store_that_answered_once_still_dials(self, call, eval_return, operation):
        """The blip window: unnamed address, but this process reached it."""
        repo, backend = _make_repo(eval_return, reached=True, unconfigured=True)

        call(repo)

        backend.raw_redis_client.eval.assert_called_once()

    @pytest.mark.parametrize(("call", "eval_return", "operation"), LANES)
    def test_a_named_store_still_dials_before_it_has_answered(
        self, call, eval_return, operation
    ):
        """A configured store's outage keeps its loud, counted failure."""
        repo, backend = _make_repo(eval_return, reached=False, unconfigured=False)

        call(repo)

        backend.raw_redis_client.eval.assert_called_once()

    def test_the_decline_is_an_adapter_error_the_wrapper_can_catch(self):
        """The layered wrapper's clause is typed on the adapter hierarchy."""
        repo, _backend = _make_repo(reached=False, unconfigured=True)

        with pytest.raises(AdapterError):
            _lane_trip(repo)

    def test_a_declined_acquire_clears_the_stale_marker(self):
        """Marker hygiene, identical to the lane's own ``except`` branch.

        The layered wrapper reads ``_last_acquire_marker`` after the call. A
        decline that left the previous call's marker standing would report an
        old ``stuck_recovery`` against a transition that never happened.
        """
        repo, _backend = _make_repo(reached=False, unconfigured=True)
        repo._last_acquire_marker = "stuck_recovery"

        with pytest.raises(UnconfiguredStoreError):
            _lane_try_acquire(repo)

        assert repo._last_acquire_marker == ""


# =============================================================================
# Behavior — the window an ``is_degraded`` guard would have swallowed
# =============================================================================


class TestBlipWindowKeepsDialing:
    """A process whose Redis answered once, and is now between connections.

    Driven through a real ``ResilientStorageBackend`` rather than a double:
    the distinction under test is between two of the backend's own
    properties, and a double would let the test assert whichever one it was
    written against. The posture fixture makes the address an unnamed one, so
    the *only* thing separating this from the declining case is that a probe
    succeeded first.
    """

    @pytest.fixture(autouse=True)
    def clear_redis_negative_cache(self):
        """The shared negative cache would short-circuit the probe path."""
        from baldur.adapters.redis import _redis_state

        state = _redis_state()
        previous = (state.unavailable, state.fail_time)
        state.unavailable = False
        state.fail_time = 0.0
        yield
        state.unavailable, state.fail_time = previous

    @pytest.fixture
    def blipping(self, no_redis_posture):
        """A repo over a backend that connected once and then degraded."""
        with tempfile.TemporaryDirectory() as wal_dir:
            backend = ResilientStorageBackend(
                settings=ResilientStorageSettings(wal_dir=wal_dir)
            )
            client = MagicMock(spec=redis.Redis)
            adapter = MagicMock(spec=RedisCacheAdapter)
            adapter.raw_client = client
            with patch("baldur.adapters.cache.RedisCacheAdapter", return_value=adapter):
                assert backend._ensure_redis() is True
            backend._switch_to_degraded()

            # The premise: nobody named this Redis, and the backend no longer
            # reports itself available — yet it has reached one.
            assert backend._probing_unconfigured_default() is True
            assert backend.is_redis_available is False
            assert backend.has_reached_redis is True

            yield RedisCircuitBreakerStateRepository(backend=backend), client
            backend.close()

    def test_the_single_winner_trip_still_reaches_the_script(self, blipping):
        repo, client = blipping
        client.eval.return_value = [1, b"open", OPENED_AT_ISO, b""]

        attempt = repo.trip_to_open(SVC, 5)

        assert client.eval.call_args.args[0] is _LUA_TRIP_TO_OPEN
        assert attempt.did_open is True

    def test_the_store_side_pin_guard_still_reaches_the_script(self, blipping):
        repo, client = blipping
        client.eval.return_value = 1

        assert (
            repo.update_state(
                service_name=SVC,
                state=CircuitBreakerStateEnum.CLOSED.value,
                skip_if_pinned=True,
            )
            is True
        )

        assert client.eval.call_args.args[0] is _LUA_UPDATE_STATE_SKIP_IF_PINNED

    def test_a_failed_pin_guarded_write_still_reports_at_debug(self, blipping):
        """The level split survives the narrowing to this window.

        Deleting the DEBUG half would have made the mirror louder here than
        it is today, not quieter — the address is still one nobody named.
        """
        repo, client = blipping
        client.eval.side_effect = ConnectionError("blip")

        with capture_logs() as logs:
            repo.update_state(
                service_name=SVC,
                state=CircuitBreakerStateEnum.CLOSED.value,
                skip_if_pinned=True,
            )

        reports = [
            entry
            for entry in logs
            if entry.get("event") == "redis_cb_repo.pin_guarded_update_failed"
        ]
        assert len(reports) == 1
        assert reports[0]["log_level"] == "debug"
