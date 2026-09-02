"""676 — On-recovery auto-replay arming probe.

Target: ``baldur.services.replay_service.arming``

    - ``get_on_recovery_arming_status`` / ``_evaluate`` — the single source of
      truth behind the gauge, the stats block and the console badge. Shared
      link order (first missing wins for the headline):
      ``disabled -> celery_missing -> worker_missing -> handler_missing``,
      then each lane's own link — ``map_unconfigured`` for the mapped sweep,
      ``open_circuit_capture_disabled`` for the open-circuit sweep.
    - ``ArmingStatus`` / ``LaneStatus`` / ``DispatchRecord`` — the frozen
      result DTOs, the fail-open sentinel (``armed=None``) and the one REST
      shape their ``to_dict`` produces.
    - ``_cached_worker_state`` — the broker-presence probe cached behind a
      short TTL so the console's periodic polling pays at most one broker
      round-trip per TTL window. The round-trip runs on one daemon thread per
      process behind a shared deadline, and is abandoned — sequence-guarded —
      when that deadline passes.
    - ``_probe_dlq_worker`` — the round-trip itself: how a broadcast reply
      folds to ok / missing / unknown, and the dedicated connection it always
      closes.
    - ``record_dispatch_outcome`` / ``get_dispatch_ledger`` — the in-process
      dispatch ledger the operator surfaces read as ``last_dispatch``.
    - ``_set_gauge`` / ``refresh_armed_gauge`` — the single writer of the
      armed gauge, which publishes 1 only for a verified-armed verdict.

Every link check is patched at its module seam, so no live broker / Celery is
touched and no ``baldur_pro`` import is needed (G19/G20/G21 safe). The
probe-thread tests gate their fake round-trip on an ``Event`` the teardown
always releases: a gate left closed strands a live daemon thread inside the
call for the rest of the session.
"""

from __future__ import annotations

import contextlib
import os
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.core import process_utils
from baldur.metrics.recorders.dlq import DLQMetricRecorder
from baldur.services.replay_service import arming
from baldur.services.replay_service.arming import (
    ArmingStatus,
    DispatchRecord,
    LaneStatus,
    get_dispatch_ledger,
    get_on_recovery_arming_status,
    get_worker_cache,
    record_dispatch_outcome,
    refresh_armed_gauge,
    reset_dispatch_ledger,
    reset_worker_cache,
)
from baldur.utils.time import utc_now

_MOD = "baldur.services.replay_service.arming"

# Marks a process that serves Celery tasks — the probe reads it to tell an
# unanswered broadcast it sent from inside a worker from one it sent outside.
_CELERY_SERVING_ENV = process_utils._CELERY_WORKER_SERVING_ENV_VAR

# Ceiling on every fake's wait. A test that reaches it has already failed; the
# bound only keeps a wedged session from hanging.
_GATE_TIMEOUT = 5.0

_ARMED_CONFIG = {
    "on_recovery_enabled": True,
    "service_failure_type_map": {"payment_api": ["TIMEOUT"]},
}


@contextlib.contextmanager
def _links(
    *,
    config=None,
    celery=True,
    worker="ok",
    handler=True,
    capture=True,
):
    """Patch all five link seams, defaulting to a fully-armed configuration.

    Overriding a single kwarg isolates exactly one missing link so the
    first-missing-wins ordering can be asserted.
    """
    cfg = _ARMED_CONFIG if config is None else config
    with (
        patch(f"{_MOD}._resolve_replay_config", return_value=cfg),
        patch(f"{_MOD}._celery_task_importable", return_value=celery),
        patch(f"{_MOD}._cached_worker_state", return_value=worker),
        patch(f"{_MOD}._has_registered_handler", return_value=handler),
        patch(f"{_MOD}._open_circuit_capture_enabled", return_value=capture),
    ):
        yield


def _all_links_ok() -> dict[str, str]:
    """Every link in the global order answering "ok"."""
    return dict.fromkeys(arming._LINK_ORDER, "ok")


def _dispatch_record(
    *,
    outcome: str = "dispatched",
    at: datetime | None = None,
    service_name: str = "payment-api",
    error: str | None = None,
    consecutive_failures: int = 0,
) -> DispatchRecord:
    """A ledger entry carrying this process's pid — what a real one looks like."""
    return DispatchRecord(
        outcome=outcome,
        at=at or datetime(2026, 9, 2, 12, 30, 45, tzinfo=UTC),
        service_name=service_name,
        error=error,
        consecutive_failures=consecutive_failures,
        pid=os.getpid(),
    )


@contextlib.contextmanager
def _dlq_recorder():
    """Stand the DLQ metric recorder up as a spec-bound double.

    The armed gauge and the dispatch counter both resolve through
    ``get_metrics().dlq``, so this is the seam every metric assertion here
    reads.
    """
    recorder = MagicMock(spec=DLQMetricRecorder)
    with patch(
        "baldur.metrics.prometheus.get_metrics",
        return_value=SimpleNamespace(dlq=recorder),
    ):
        yield recorder


def _join_probe_threads(timeout: float = _GATE_TIMEOUT) -> None:
    """Wait out every probe thread still alive in this process."""
    for thread in threading.enumerate():
        if thread.name == arming._PROBE_THREAD_NAME:
            thread.join(timeout=timeout)


@pytest.fixture(autouse=True)
def _clean_probe_state():
    """Reset the probe's per-process state around every test in this module.

    ``log_state=True`` also clears the transition memory that keeps repeated
    probe failures at DEBUG — without it a previous test's verdict decides the
    next one's first log level. The join is thread-leak hygiene: a released
    fake still has to reach its result publication before the next test's
    cache assertions run.
    """
    reset_worker_cache(log_state=True)
    reset_dispatch_ledger()
    yield
    _join_probe_threads()
    reset_worker_cache(log_state=True)
    reset_dispatch_ledger()


# =============================================================================
# Link-state evaluation — first-missing-wins ordering, folded per lane
# =============================================================================


class TestArmingStatusBehavior:
    """``_evaluate`` walks the dependency chain and stops at the first hard
    prerequisite; leaf links are evaluated together and folded per lane.
    """

    def test_all_links_ok_is_armed(self):
        with _links():
            status = arming._evaluate()

        assert status.armed is True
        assert status.missing_link is None
        assert status.missing_links == []
        assert status.unverified_link is None
        assert status.links == {
            "disabled": "ok",
            "celery_missing": "ok",
            "worker_missing": "ok",
            "handler_missing": "ok",
            "map_unconfigured": "ok",
            "open_circuit_capture_disabled": "ok",
        }
        assert all(lane.armed is True for lane in status.lanes.values())

    def test_disabled_short_circuits_before_celery(self):
        with _links(config={"on_recovery_enabled": False}):
            status = arming._evaluate()

        assert status.missing_link == "disabled"
        assert "celery_missing" not in status.links
        # A hard prerequisite blocks every sweep, not just one of them.
        assert [lane.armed for lane in status.lanes.values()] == [False, False]

    def test_celery_missing_short_circuits_before_worker(self):
        with _links(celery=False):
            status = arming._evaluate()

        assert status.missing_link == "celery_missing"
        assert "worker_missing" not in status.links
        # The other hard prerequisite, blocking both sweeps the same way.
        assert [lane.armed for lane in status.lanes.values()] == [False, False]

    def test_worker_missing_is_the_headline_when_only_worker_absent(self):
        with _links(worker="missing"):
            status = arming._evaluate()

        assert status.armed is False
        assert status.missing_link == "worker_missing"
        assert status.missing_links == ["worker_missing"]

    def test_map_unconfigured_disarms_only_the_mapped_lane(self):
        # The shipped default: no failure-type map, open-circuit capture on.
        # That sweep needs no map entry, so the surface stays armed and the map
        # link is reported on its own lane rather than as a headline.
        with _links(
            config={"on_recovery_enabled": True, "service_failure_type_map": {}}
        ):
            status = arming._evaluate()

        assert status.armed is True
        assert status.lanes["open_circuit"].armed is True
        assert status.lanes["mapped"].armed is False
        assert status.lanes["mapped"].link == "map_unconfigured"
        assert status.missing_link is None
        assert status.missing_links == []

    def test_both_lane_links_missing_disarms_with_map_as_headline(self):
        with _links(
            config={"on_recovery_enabled": True, "service_failure_type_map": {}},
            capture=False,
        ):
            status = arming._evaluate()

        assert status.armed is False
        assert status.missing_link == "map_unconfigured"
        assert status.missing_links == [
            "map_unconfigured",
            "open_circuit_capture_disabled",
        ]

    def test_handler_missing_when_no_registered_handler(self):
        with _links(handler=False):
            status = arming._evaluate()

        assert status.missing_link == "handler_missing"

    def test_multiple_leaf_links_reported_headline_is_first_in_order(self):
        # worker + map + handler all missing at once: leaves are evaluated
        # together, so missing_links carries all three, but the headline is the
        # first in _LINK_ORDER — a shared link outranks a lane link.
        with _links(
            worker="missing",
            config={"on_recovery_enabled": True, "service_failure_type_map": {}},
            handler=False,
        ):
            status = arming._evaluate()

        assert status.missing_link == "worker_missing"
        assert status.missing_links == [
            "worker_missing",
            "handler_missing",
            "map_unconfigured",
        ]

    def test_worker_unknown_is_unverified_never_armed(self):
        # A broker the probe could not reach resolves the worker link to
        # "unknown". Nothing was refuted, but nothing was verified either — the
        # verdict is indeterminate, never a positive arming claim.
        with _links(worker="unknown"):
            status = arming._evaluate()

        assert status.armed is None
        assert status.links["worker_missing"] == "unknown"
        assert status.unverified_link == "worker_missing"
        assert status.missing_link is None
        assert status.missing_links == []

    def test_full_probe_sets_gauge_and_never_raises(self):
        with _links(), patch(f"{_MOD}._set_gauge") as set_gauge:
            status = get_on_recovery_arming_status()

        assert status.armed is True
        set_gauge.assert_called_once_with(True)

    def test_full_probe_fails_open_to_probe_failed_when_evaluate_raises(self):
        # The operator surfaces must never 500 on a probe fault.
        with (
            patch(f"{_MOD}._evaluate", side_effect=RuntimeError("boom")),
            patch(f"{_MOD}._set_gauge") as set_gauge,
        ):
            status = get_on_recovery_arming_status()

        assert status.armed is None
        assert status.unverified_link == "probe_failed"
        # A probe fault is an unverified cause, not a missing prerequisite.
        assert status.missing_link is None
        set_gauge.assert_called_once_with(None)


# =============================================================================
# ArmingStatus DTO contract — headline invariants and the one REST shape
# =============================================================================


class TestArmingStatusContract:
    """Frozen result DTOs, the fail-open sentinel, and what ``to_dict`` ships."""

    def test_probe_failed_sentinel_shape(self):
        status = ArmingStatus.probe_failed()
        assert status.armed is None
        assert status.missing_link is None
        assert status.missing_links == []
        assert status.unverified_link == "probe_failed"
        assert status.links == {}
        assert status.lanes == {}
        assert status.last_dispatch is None

    def test_probe_failed_keeps_the_dispatch_the_process_already_observed(self):
        # A probe fault says nothing about what this process dispatched, so the
        # sentinel keeps that evidence rather than dropping it with the verdict.
        record = _dispatch_record(outcome="error", consecutive_failures=2)

        status = ArmingStatus.probe_failed(last_dispatch=record)

        assert status.armed is None
        assert status.unverified_link == "probe_failed"
        assert status.last_dispatch is record

    def test_default_collections_are_empty(self):
        status = ArmingStatus(armed=True, missing_link=None)
        assert status.missing_links == []
        assert status.links == {}
        assert status.lanes == {}
        assert status.unverified_link is None
        assert status.last_dispatch is None

    def test_frozen_instance_rejects_mutation(self):
        status = ArmingStatus(armed=True, missing_link=None)
        with pytest.raises(Exception):
            status.armed = False  # type: ignore[misc]

    def test_finalize_armed_verdict_carries_no_headline(self):
        status = arming._finalize(_all_links_ok())

        assert status.armed is True
        assert status.missing_link is None
        assert status.missing_links == []
        assert status.unverified_link is None

    def test_finalize_disarmed_headline_is_the_first_of_the_missing_links(self):
        links = _all_links_ok()
        links["handler_missing"] = "missing"
        links["open_circuit_capture_disabled"] = "missing"

        status = arming._finalize(links)

        assert status.armed is False
        assert status.missing_links == [
            "handler_missing",
            "open_circuit_capture_disabled",
        ]
        assert status.missing_link == status.missing_links[0]
        assert status.unverified_link is None

    def test_finalize_unverified_verdict_names_the_link_and_reports_no_missing(self):
        links = _all_links_ok()
        links["worker_missing"] = "unknown"

        status = arming._finalize(links)

        assert status.armed is None
        assert status.unverified_link == "worker_missing"
        # Nothing was refuted, so nothing may be reported as missing.
        assert status.missing_link is None
        assert status.missing_links == []

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (
                ArmingStatus(
                    armed=True,
                    missing_link=None,
                    links={"disabled": "ok", "worker_missing": "ok"},
                    lanes={"open_circuit": LaneStatus(armed=True, link=None)},
                ),
                {
                    "armed": True,
                    "missing_link": None,
                    "missing_links": [],
                    "unverified_link": None,
                    "links": {"disabled": "ok", "worker_missing": "ok"},
                    "lanes": {"open_circuit": {"armed": True, "link": None}},
                    "last_dispatch": None,
                },
            ),
            (
                ArmingStatus(
                    armed=False,
                    missing_link="worker_missing",
                    missing_links=["worker_missing"],
                    links={"disabled": "ok", "worker_missing": "missing"},
                    lanes={
                        "open_circuit": LaneStatus(armed=False, link="worker_missing")
                    },
                ),
                {
                    "armed": False,
                    "missing_link": "worker_missing",
                    "missing_links": ["worker_missing"],
                    "unverified_link": None,
                    "links": {"disabled": "ok", "worker_missing": "missing"},
                    "lanes": {
                        "open_circuit": {"armed": False, "link": "worker_missing"}
                    },
                    "last_dispatch": None,
                },
            ),
            (
                ArmingStatus(
                    armed=None,
                    missing_link=None,
                    unverified_link="worker_missing",
                    links={"disabled": "ok", "worker_missing": "unknown"},
                    lanes={
                        "open_circuit": LaneStatus(armed=None, link="worker_missing")
                    },
                ),
                {
                    "armed": None,
                    "missing_link": None,
                    "missing_links": [],
                    "unverified_link": "worker_missing",
                    "links": {"disabled": "ok", "worker_missing": "unknown"},
                    "lanes": {
                        "open_circuit": {"armed": None, "link": "worker_missing"}
                    },
                    "last_dispatch": None,
                },
            ),
            (
                ArmingStatus.probe_failed(),
                {
                    "armed": None,
                    "missing_link": None,
                    "missing_links": [],
                    "unverified_link": "probe_failed",
                    "links": {},
                    "lanes": {},
                    "last_dispatch": None,
                },
            ),
        ],
        ids=["armed", "disarmed", "unverified", "probe_failed"],
    )
    def test_to_dict_ships_the_same_key_set_for_every_verdict(self, status, expected):
        # This dict IS the published ``auto_replay`` block: the console reads it
        # field by field, so a key that appears only on some verdicts is a bug.
        assert status.to_dict() == expected

    def test_to_dict_carries_the_last_dispatch_as_its_own_block(self):
        status = ArmingStatus(
            armed=True,
            missing_link=None,
            last_dispatch=_dispatch_record(outcome="dispatched"),
        )

        assert status.to_dict()["last_dispatch"] == {
            "outcome": "dispatched",
            "at": "2026-09-02T12:30:45+00:00",
            "service_name": "payment-api",
            "error": None,
            "consecutive_failures": 0,
            "pid": os.getpid(),
        }

    def test_to_dict_hands_out_copies_the_caller_cannot_write_back_through(self):
        # The DTO is frozen but its dict/list fields are not, so a serialisation
        # that aliased them would let a handler mutate a shared verdict.
        status = ArmingStatus(
            armed=False,
            missing_link="worker_missing",
            missing_links=["worker_missing"],
            links={"worker_missing": "missing"},
        )

        payload = status.to_dict()
        payload["missing_links"].append("handler_missing")
        payload["links"]["handler_missing"] = "missing"

        assert status.missing_links == ["worker_missing"]
        assert status.links == {"worker_missing": "missing"}

    def test_lane_status_to_dict_carries_the_verdict_and_its_link(self):
        assert LaneStatus(armed=False, link="map_unconfigured").to_dict() == {
            "armed": False,
            "link": "map_unconfigured",
        }

    def test_dispatch_record_to_dict_renders_the_timestamp_as_iso_8601(self):
        record = _dispatch_record(
            outcome="error",
            at=datetime(2026, 9, 2, 12, 30, 45, tzinfo=UTC),
            error="broker down",
            consecutive_failures=3,
        )

        assert record.to_dict() == {
            "outcome": "error",
            "at": "2026-09-02T12:30:45+00:00",
            "service_name": "payment-api",
            "error": "broker down",
            "consecutive_failures": 3,
            "pid": os.getpid(),
        }


# =============================================================================
# Worker-presence probe — scaffolding shared by every probe-thread test
# =============================================================================


class _FakeClock:
    """Controllable ``time.monotonic`` the caller and the probe thread share.

    A ``side_effect`` list cannot serve here: the probe runs on a thread of its
    own and reads the clock too, so the number of reads is not fixed by the
    caller.
    """

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _GatedProbe:
    """Event-gated stand-in for the broker round-trip.

    ``entered`` reports that the probe thread is inside the call and ``release``
    is what lets it return, so a test decides when the round-trip completes
    instead of racing it. Successive calls walk ``results`` and then repeat the
    last one.
    """

    def __init__(self, *results: str):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._results = list(results) or ["ok"]
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            index = self.calls
            self.calls += 1
        self.entered.set()
        self.release.wait(timeout=_GATE_TIMEOUT)
        return self._results[min(index, len(self._results) - 1)]


def _open_probe(*results: str) -> _GatedProbe:
    """A fake round-trip that answers at once — its gate is already open."""
    probe = _GatedProbe(*results)
    probe.release.set()
    return probe


def _celery_settings(*, ttl: int = 15, inspect_timeout: int = 1) -> SimpleNamespace:
    """The two ``CeleryTaskSettings`` fields the probe reads, and nothing else.

    A namespace rather than a mock: a field the probe stops reading, or starts
    misspelling, has to fail here rather than be auto-answered.
    """
    return SimpleNamespace(
        worker_status_cache_ttl_seconds=ttl,
        inspect_timeout=inspect_timeout,
    )


@contextlib.contextmanager
def _probe_env(probe, clock, *, ttl: int = 15, budget: float = 5.0):
    """Drive the probe with a fake clock and a caller budget tests control.

    ``_probe_budget_seconds`` is the patch point that keeps these tests off real
    seconds: it is the only wall-clock wait in the path, because the deadline is
    enforced on the caller's side of the future.
    """
    with (
        patch(f"{_MOD}._probe_dlq_worker", probe),
        patch(
            "baldur.settings.celery_task.get_celery_task_settings",
            return_value=_celery_settings(ttl=ttl),
        ),
        patch(f"{_MOD}._probe_budget_seconds", return_value=budget),
        patch(f"{_MOD}.time.monotonic", clock),
    ):
        yield


class TestWorkerCacheBehavior:
    """``_cached_worker_state`` collapses concurrent/periodic polls onto one
    broker round-trip per TTL window.
    """

    def test_second_call_within_ttl_reuses_cached_broker_result(self):
        probe = _open_probe("ok")
        clock = _FakeClock()
        with _probe_env(probe, clock):
            first = arming._cached_worker_state()
            clock.advance(5.0)  # inside the 15s TTL window
            second = arming._cached_worker_state()

        assert first == "ok"
        assert second == "ok"
        # The broker probe ran exactly once — the second poll hit the cache.
        assert probe.calls == 1

    def test_call_after_ttl_expiry_refreshes_broker_result(self):
        probe = _open_probe("missing", "ok")
        clock = _FakeClock()
        with _probe_env(probe, clock):
            first = arming._cached_worker_state()
            clock.advance(20.0)  # past the 15s expiry
            second = arming._cached_worker_state()

        assert first == "missing"
        assert second == "ok"
        assert probe.calls == 2

    def test_cache_accessor_does_not_hand_out_a_parent_process_entry(self):
        # A forked child inherits the parent's cache verbatim; the accessor
        # must adopt the process like every other reader and answer empty.
        probe = _open_probe("ok")
        clock = _FakeClock()
        with _probe_env(probe, clock):
            arming._cached_worker_state()
            assert get_worker_cache() != {}

            with patch.object(arming, "_probe_state_pid", os.getpid() + 1):
                inherited = get_worker_cache()

        assert inherited == {}

    def test_reset_worker_cache_forces_fresh_probe(self):
        probe = _open_probe("ok")
        clock = _FakeClock()
        with _probe_env(probe, clock):
            arming._cached_worker_state()
            reset_worker_cache()
            clock.advance(1.0)
            arming._cached_worker_state()

        # The reset invalidated the cache, so the second call re-probed.
        assert probe.calls == 2


# =============================================================================
# Worker-presence probe — one bounded round-trip per process
# =============================================================================


class TestWorkerProbeSingleFlightBehavior:
    """One in-flight round-trip per process, bounded for every caller.

    The round-trip runs on a daemon thread because it is not otherwise bounded:
    on a connection that went stale after the process connected, the broker
    client republishes forever rather than raising, so a console poll used to
    hang a serving worker for the whole outage.
    """

    @pytest.fixture
    def gated_probe(self):
        """Hand out Event-gated fakes and release them however a test ends.

        An un-released fake leaves its daemon thread parked inside the fake
        round-trip for the rest of the session — the very leak the bounded
        probe exists to prevent.
        """
        probes: list[_GatedProbe] = []

        def make(*results: str) -> _GatedProbe:
            probe = _GatedProbe(*results)
            probes.append(probe)
            return probe

        yield make

        for probe in probes:
            probe.release.set()
        _join_probe_threads()

    def test_concurrent_callers_share_one_in_flight_round_trip(self, gated_probe):
        # Given: a round-trip that cannot finish until this test says so, with
        # one caller already parked on it.
        probe = gated_probe("ok")
        clock = _FakeClock()
        results: list[str] = []

        with _probe_env(probe, clock):
            first = threading.Thread(
                target=lambda: results.append(arming._cached_worker_state())
            )
            first.start()
            assert probe.entered.wait(timeout=_GATE_TIMEOUT)

            # When: a second caller arrives while that probe is still in flight.
            second = threading.Thread(
                target=lambda: results.append(arming._cached_worker_state())
            )
            second.start()
            probe.release.set()
            first.join(timeout=_GATE_TIMEOUT)
            second.join(timeout=_GATE_TIMEOUT)

        # Then: both callers got the real verdict off one round-trip. The
        # second either joined the in-flight future or read the cache that same
        # probe wrote — it can never have started a round-trip of its own,
        # because nothing could publish before ``release`` was set.
        assert results == ["ok", "ok"]
        assert probe.calls == 1

    def test_a_round_trip_past_the_deadline_resolves_unknown_and_caches_it(
        self, gated_probe
    ):
        # Given: a round-trip that will not answer within the caller's budget.
        probe = gated_probe("ok")
        clock = _FakeClock()

        with _probe_env(probe, clock, ttl=15, budget=0.05):
            timed_out = arming._cached_worker_state()
            cached = get_worker_cache()[arming._WORKER_CACHE_KEY]
            expires_at = clock.now + 15.0
            clock.advance(5.0)  # still inside the abandonment's TTL
            second = arming._cached_worker_state()

        # Then: the caller is released at its deadline with an unverified
        # answer, and that answer is cached for the TTL so the next poll does
        # not queue behind the same wedged attempt.
        assert timed_out == "unknown"
        assert cached == (expires_at, "unknown")
        assert second == "unknown"
        assert probe.calls == 1

    def test_a_wedged_attempt_neither_caches_nor_respawns_until_it_exits(
        self, gated_probe
    ):
        # Given: an abandoned attempt still parked inside the round-trip, and a
        # cache entry that has since expired.
        probe = gated_probe("ok", "missing")
        clock = _FakeClock()

        with _probe_env(probe, clock, ttl=15, budget=0.05):
            arming._cached_worker_state()  # deadline passes -> abandoned
            wedged = arming._probe_thread
            clock.advance(20.0)  # past the abandonment's TTL
            before = get_worker_cache()

            # When: a miss lands while that thread is still alive.
            during = arming._cached_worker_state()

            # Then: a non-observation — nothing spawned, and nothing cached,
            # because the next miss after that thread dies must probe afresh.
            assert during == "unknown"
            assert get_worker_cache() == before
            assert probe.calls == 1

            # When: the wedged attempt finally exits and a miss lands again.
            probe.release.set()
            wedged.join(timeout=_GATE_TIMEOUT)
            fresh = arming._cached_worker_state()

        # Then: the slot was never pinned — a new round-trip ran and answered.
        assert fresh == "missing"
        assert probe.calls == 2

    def test_a_late_answer_after_the_deadline_does_not_overwrite_the_cache(
        self, gated_probe
    ):
        # Given: an attempt abandoned at its deadline, whose verdict arrives
        # only afterwards.
        probe = gated_probe("ok")
        clock = _FakeClock()

        with _probe_env(probe, clock, ttl=15, budget=0.05):
            timed_out = arming._cached_worker_state()
            wedged = arming._probe_thread

            # When: the round-trip finally answers "ok".
            probe.release.set()
            wedged.join(timeout=_GATE_TIMEOUT)

            # Then: the sequence guard drops it — a newer answer already stands,
            # and a late one must never overwrite it.
            assert timed_out == "unknown"
            assert get_worker_cache()[arming._WORKER_CACHE_KEY][1] == "unknown"
            assert arming._cached_worker_state() == "unknown"

    def test_reset_during_a_live_round_trip_hands_the_next_call_its_answer(
        self, gated_probe
    ):
        # Given: a healthy probe parked inside the round-trip with a caller on
        # it.
        probe = gated_probe("ok")
        clock = _FakeClock()
        results: list[str] = []

        def call():
            results.append(arming._cached_worker_state())

        with _probe_env(probe, clock, ttl=15, budget=2.0):
            first = threading.Thread(target=call)
            first.start()
            assert probe.entered.wait(timeout=_GATE_TIMEOUT)

            # When: a dispatch attempt invalidates the cache while that probe
            # is still in flight, and a poll lands right after it.
            reset_worker_cache()
            second = threading.Thread(target=call)
            second.start()
            probe.release.set()
            first.join(timeout=_GATE_TIMEOUT)
            second.join(timeout=_GATE_TIMEOUT)
            cached = get_worker_cache()[arming._WORKER_CACHE_KEY][1]

        # Then: the live round-trip is the re-check the reset asked for. The
        # post-reset poll joined it and got the broker's real answer rather
        # than a non-observation, no second round-trip started, and the
        # answer was cached — a reset must not turn a healthy deployment into
        # "unverified" for the rest of that round-trip.
        assert sorted(results) == ["ok", "ok"]
        assert probe.calls == 1
        assert cached == "ok"


# =============================================================================
# Worker-presence probe — what one broadcast reply actually evidences
# =============================================================================


class _FakeBrokerConnection:
    """Connection stand-in that records the probe closing it."""

    def __init__(self) -> None:
        self.closes = 0

    def close(self) -> None:
        self.closes += 1


class _FakeBroadcast:
    """Stand-in for the reply-collecting broadcast, recording how it was asked.

    ``reply`` is what the broker answered; an exception instance is raised
    instead, the way an unreachable broker fails the round-trip.
    """

    def __init__(self, reply) -> None:
        self.calls: list[tuple] = []
        self._reply = reply

    def __call__(self, connection, timeout):
        self.calls.append((connection, timeout))
        if isinstance(self._reply, BaseException):
            raise self._reply
        return self._reply


class _FakeCeleryApp:
    """``current_app`` stand-in carrying only what the probe touches.

    Hand-written rather than a mock so a call to anything else raises instead
    of silently answering: telling an answer from a silence is this probe's
    whole job, and an auto-generated attribute would erase the difference.
    """

    def __init__(self, *, connect_timeout: float = 4.0) -> None:
        self.connection = _FakeBrokerConnection()
        self.connection_kwargs: dict = {}
        self.conf = SimpleNamespace(broker_connection_timeout=connect_timeout)

    def connection_for_write(self, **kwargs) -> _FakeBrokerConnection:
        self.connection_kwargs = kwargs
        return self.connection


@contextlib.contextmanager
def _celery_app(app, broadcast, *, inspect_timeout: int = 1):
    """Stand the probe's ``current_app``, its settings and its broadcast up."""
    with (
        patch("celery.current_app", app),
        patch(
            "baldur.settings.celery_task.get_celery_task_settings",
            return_value=_celery_settings(inspect_timeout=inspect_timeout),
        ),
        patch(f"{_MOD}._inspect_active_queues", broadcast),
    ):
        yield


class _FakeMailbox:
    """A control mailbox class: records how it was built, answers ``multi_call``.

    The probe builds its own mailbox from the app's via ``type(base)(...)``,
    so a fake class stands in for both the template and the copy.
    """

    instances: list[_FakeMailbox] = []

    def __init__(self, namespace, **kwargs) -> None:
        self.namespace = namespace
        self.kwargs = kwargs
        self.replies: list = []
        self.multi_calls: list[tuple] = []
        for key, value in kwargs.items():
            setattr(self, key, value)
        _FakeMailbox.instances.append(self)

    def multi_call(self, command, kwargs=None, timeout=1, **_):
        self.multi_calls.append((command, timeout))
        return self.replies


def _control_mailbox_template() -> _FakeMailbox:
    """The app's own mailbox — what the probe copies its settings from."""
    return _FakeMailbox(
        "celery",
        type="fanout",
        connection=None,
        clock=object(),
        accept=["json"],
        serializer="json",
        producer_pool=object(),  # the pool the probe must never draw from
        queue_ttl=300.0,
        queue_expires=10.0,
        queue_durable=False,
        queue_exclusive=False,
        reply_queue_ttl=300.0,
        reply_queue_expires=10.0,
    )


class TestWorkerProbeReplyFoldBehavior:
    """``_probe_dlq_worker`` folds one broadcast reply into ok/missing/unknown.

    A broadcast sent from inside a worker always has that worker among its own
    addressees, so silence there means the sender could not hear itself — it
    has learned nothing about anyone else. Outside a worker the same silence is
    a verdict.
    """

    @pytest.mark.parametrize(
        ("reply", "serving", "expected"),
        [
            (None, True, "unknown"),
            ({}, True, "unknown"),
            (None, False, "missing"),
            ({"w1": [{"name": "other_queue"}]}, True, "missing"),
            ({"w1": [{"name": "other_queue"}]}, False, "missing"),
            ({"w1": None}, False, "missing"),
            ({"w1": [{"name": "dlq_processing"}]}, True, "ok"),
            (
                {"w1": [{"name": "other_queue"}], "w2": [{"name": "dlq_processing"}]},
                False,
                "ok",
            ),
        ],
        ids=[
            "silence_inside_a_worker",
            "empty_reply_inside_a_worker",
            "silence_outside_a_worker",
            "wrong_queue_inside_a_worker",
            "wrong_queue_outside_a_worker",
            "worker_replied_with_no_queues",
            "queue_served_inside_a_worker",
            "queue_served_by_one_of_two_workers",
        ],
    )
    def test_reply_folds_to_the_state_it_actually_evidences(
        self, monkeypatch, reply, serving, expected
    ):
        # Given: a broker answering with this reply, probed from this kind of
        # process.
        if serving:
            monkeypatch.setenv(_CELERY_SERVING_ENV, "1")
        else:
            monkeypatch.delenv(_CELERY_SERVING_ENV, raising=False)
        app = _FakeCeleryApp()

        with _celery_app(app, _FakeBroadcast(reply)):
            assert arming._probe_dlq_worker() == expected

    def test_probe_broadcasts_over_its_own_connection_and_closes_it(self, monkeypatch):
        # A probe wedged on an unreachable broker must never hold a pooled
        # connection the dispatch path needs to send the replay task.
        monkeypatch.delenv(_CELERY_SERVING_ENV, raising=False)
        app = _FakeCeleryApp()
        broadcast = _FakeBroadcast({"w1": [{"name": "dlq_processing"}]})

        with _celery_app(app, broadcast, inspect_timeout=1):
            assert arming._probe_dlq_worker() == "ok"

        assert broadcast.calls == [(app.connection, 1)]
        assert app.connection.closes == 1

    def test_probe_closes_its_connection_when_the_broadcast_raises(self, monkeypatch):
        monkeypatch.delenv(_CELERY_SERVING_ENV, raising=False)
        app = _FakeCeleryApp()

        with _celery_app(app, _FakeBroadcast(OSError("broker unreachable"))):
            # A broker error is unverified state, never a refutation.
            assert arming._probe_dlq_worker() == "unknown"

        assert app.connection.closes == 1

    def test_probe_connection_carries_a_deadline_on_both_socket_directions(
        self, monkeypatch
    ):
        # Without socket deadlines a half-open socket has none at all, and the
        # probe thread would outlive the outage that produced it.
        monkeypatch.delenv(_CELERY_SERVING_ENV, raising=False)
        app = _FakeCeleryApp(connect_timeout=4.0)

        with _celery_app(app, _FakeBroadcast({}), inspect_timeout=1):
            budget = arming._probe_budget_seconds()
            arming._probe_dlq_worker()

        assert app.connection_kwargs["connect_timeout"] == 4.0
        # Both transports' key names are passed; each reads only its own.
        assert app.connection_kwargs["transport_options"] == {
            "socket_timeout": budget,
            "socket_connect_timeout": budget,
            "read_timeout": budget,
            "write_timeout": budget,
        }


class TestWorkerProbeBroadcastBehavior:
    """``_inspect_active_queues`` publishes and collects on one connection.

    ``control.inspect(connection=...)`` binds only the reply side to the
    supplied connection; the request goes out through the app's producer pool
    on a pooled connection with no socket deadline. The probe's own mailbox
    has no pool, so a wedged probe can neither hold a pooled connection nor
    republish forever on one.
    """

    @pytest.fixture(autouse=True)
    def _fresh_mailboxes(self):
        _FakeMailbox.instances.clear()
        yield
        _FakeMailbox.instances.clear()

    def test_broadcast_builds_a_pool_less_mailbox_bound_to_the_probe_connection(self):
        # Given: the app's control mailbox, which carries a producer pool.
        template = _control_mailbox_template()
        app = SimpleNamespace(control=SimpleNamespace(mailbox=template))
        connection = _FakeBrokerConnection()

        # When
        with patch("celery.current_app", app):
            built_before = len(_FakeMailbox.instances)
            active = arming._inspect_active_queues(connection, 3)

        # Then: exactly one mailbox was built, on the probe connection, with
        # the app's identity copied and the producer pool dropped.
        built = _FakeMailbox.instances[built_before:]
        assert len(built) == 1
        box = built[0]
        assert box.namespace == template.namespace
        assert box.kwargs["type"] == template.type
        assert box.kwargs["connection"] is connection
        assert box.kwargs["producer_pool"] is None
        assert box.kwargs["accept"] == template.accept
        assert box.kwargs["serializer"] == template.serializer
        assert box.kwargs["queue_ttl"] == template.queue_ttl
        assert box.kwargs["reply_queue_expires"] == template.reply_queue_expires
        assert box.multi_calls == [("active_queues", 3)]
        assert active == {}

    def test_broadcast_flattens_the_per_node_replies(self):
        template = _control_mailbox_template()
        app = SimpleNamespace(control=SimpleNamespace(mailbox=template))

        with (
            patch("celery.current_app", app),
            patch.object(
                _FakeMailbox,
                "multi_call",
                lambda self, command, kwargs=None, timeout=1, **_: [
                    {"w1": [{"name": "other"}]},
                    {"w2": [{"name": "dlq_processing"}]},
                ],
            ),
        ):
            active = arming._inspect_active_queues(_FakeBrokerConnection(), 1)

        assert active == {
            "w1": [{"name": "other"}],
            "w2": [{"name": "dlq_processing"}],
        }

    def test_broadcast_with_no_replies_is_an_empty_map(self):
        template = _control_mailbox_template()
        app = SimpleNamespace(control=SimpleNamespace(mailbox=template))

        with (
            patch("celery.current_app", app),
            patch.object(
                _FakeMailbox,
                "multi_call",
                lambda self, command, kwargs=None, timeout=1, **_: None,
            ),
        ):
            active = arming._inspect_active_queues(_FakeBrokerConnection(), 1)

        assert active == {}


class TestWorkerProbeLoggingBehavior:
    """A stable probe failure is announced once, not once per tick.

    With the Celery extra installed and no broker anywhere, every probe fails
    forever; warning per probe would flood a stable condition.
    """

    def test_repeated_probe_failures_warn_once_then_drop_to_debug(self):
        probe = _open_probe("unknown")
        clock = _FakeClock()

        with _probe_env(probe, clock), capture_logs() as cap:
            for _ in range(3):
                reset_worker_cache()
                arming._cached_worker_state()

        failures = [e for e in cap if e["event"] == "replay_arming.worker_probe_failed"]
        assert [e["log_level"] for e in failures] == ["warning", "debug", "debug"]

    def test_probe_recovery_after_a_failure_is_announced_exactly_once(self):
        probe = _open_probe("unknown", "ok")
        clock = _FakeClock()

        with _probe_env(probe, clock), capture_logs() as cap:
            for _ in range(3):
                reset_worker_cache()
                arming._cached_worker_state()

        recovered = [
            e for e in cap if e["event"] == "replay_arming.worker_probe_recovered"
        ]
        assert len(recovered) == 1
        assert recovered[0]["log_level"] == "info"
        assert recovered[0]["worker_state"] == "ok"
        # The failure that preceded it is the only one that warned.
        failures = [e for e in cap if e["event"] == "replay_arming.worker_probe_failed"]
        assert [e["log_level"] for e in failures] == ["warning"]


# =============================================================================
# Dispatch ledger — the observed past behind ``last_dispatch``
# =============================================================================


class TestDispatchLedgerBehavior:
    """``record_dispatch_outcome`` keeps the evidence a dispatch actually left.

    An evaluation that never called the task is not an attempt: reporting one
    as ``last_dispatch`` would name an attempt that never happened, and both
    such outcomes are already visible as links.
    """

    @pytest.mark.parametrize(
        ("outcome", "recorded"),
        [
            ("dispatched", True),
            ("error", True),
            ("skipped_disabled", False),
            ("celery_missing", False),
        ],
        ids=["dispatched", "error", "skipped_disabled", "celery_missing"],
    )
    def test_only_an_outcome_that_called_the_task_reaches_the_ledger(
        self, outcome, recorded
    ):
        with _dlq_recorder() as recorder:
            record_dispatch_outcome(outcome, service_name="payment-api")

        record = get_dispatch_ledger()
        assert (record is not None) is recorded
        if recorded:
            assert record.outcome == outcome
            assert record.service_name == "payment-api"
            assert record.pid == os.getpid()
        # Every outcome is counted, whether or not it earns a ledger entry.
        recorder.record_replay_dispatch.assert_called_once_with(outcome)

    def test_consecutive_failures_count_the_run_and_a_dispatch_ends_it(self):
        with _dlq_recorder():
            for _ in range(3):
                record_dispatch_outcome(
                    "error", service_name="payment-api", error="broker down"
                )
            after_errors = get_dispatch_ledger()
            record_dispatch_outcome("dispatched", service_name="payment-api")
            after_success = get_dispatch_ledger()

        assert after_errors.consecutive_failures == 3
        assert after_errors.error == "broker down"
        assert after_success.consecutive_failures == 0
        assert after_success.error is None

    @pytest.mark.parametrize(
        ("outcome", "invalidates"),
        [
            ("dispatched", True),
            ("error", True),
            ("skipped_disabled", False),
            ("celery_missing", False),
        ],
        ids=["dispatched", "error", "skipped_disabled", "celery_missing"],
    )
    def test_only_a_real_attempt_invalidates_the_cached_worker_state(
        self, outcome, invalidates
    ):
        # After a failure the cached state is a verdict the attempt just
        # contradicted; after a success a cached "unknown" left by an earlier
        # deadline would otherwise sit beside a successful dispatch in the same
        # answer. An evaluation that never dialled the broker learned nothing
        # about it, so it leaves the entry alone.
        probe = _open_probe("unknown")
        clock = _FakeClock()

        with _probe_env(probe, clock):
            assert arming._cached_worker_state() == "unknown"
            assert get_worker_cache() != {}

            with _dlq_recorder():
                record_dispatch_outcome(outcome, service_name="payment-api")

            assert (get_worker_cache() == {}) is invalidates

    def test_concurrent_error_records_count_every_one_of_them(self):
        # The failure run is a read-modify-write under one lock; without it two
        # threads reading the same prior count would both write prior + 1.
        workers = 20
        start = threading.Barrier(workers)

        def record():
            start.wait(timeout=_GATE_TIMEOUT)
            record_dispatch_outcome(
                "error", service_name="payment-api", error="broker down"
            )

        with _dlq_recorder():
            threads = [threading.Thread(target=record) for _ in range(workers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=_GATE_TIMEOUT)

        assert get_dispatch_ledger().consecutive_failures == workers

    def test_a_process_that_dispatched_nothing_reports_no_last_dispatch(self):
        assert get_dispatch_ledger() is None

    def test_a_record_inherited_across_fork_is_not_this_process_observation(self):
        # A forked child owns none of the parent's dispatches, so reporting the
        # parent's as this process's last one would be a borrowed observation.
        foreign = DispatchRecord(
            outcome="dispatched",
            at=utc_now(),
            service_name="payment-api",
            error=None,
            consecutive_failures=4,
            pid=os.getpid() + 1,
        )

        with patch.object(arming, "_dispatch_record", foreign):
            assert get_dispatch_ledger() is None

    def test_a_foreign_record_does_not_seed_this_process_failure_run(self):
        foreign = DispatchRecord(
            outcome="error",
            at=utc_now(),
            service_name="payment-api",
            error="broker down",
            consecutive_failures=4,
            pid=os.getpid() + 1,
        )

        with patch.object(arming, "_dispatch_record", foreign), _dlq_recorder():
            record_dispatch_outcome(
                "error", service_name="payment-api", error="broker down"
            )
            record = get_dispatch_ledger()

        # The run starts at this process's own first failure, not the parent's.
        assert record.consecutive_failures == 1


# =============================================================================
# Armed gauge — one writer, and 1 only for a verified-armed verdict
# =============================================================================


class TestArmedGaugeBehavior:
    """``baldur_dlq_auto_replay_armed`` is written by the probe alone.

    The recorder keeps a boolean contract, so the tri-state folds at this seam:
    1 only when every prerequisite was verified, 0 for both "a prerequisite is
    missing" and "a prerequisite could not be verified".
    """

    @pytest.mark.parametrize(
        ("armed", "published"),
        [(True, True), (False, False), (None, False)],
        ids=["armed", "disarmed", "unverified"],
    )
    def test_only_a_verified_armed_verdict_publishes_as_true(self, armed, published):
        # An unverifiable guarantee is not a delivered one.
        with _dlq_recorder() as recorder:
            arming._set_gauge(armed)

        recorder.set_auto_replay_armed.assert_called_once_with(published)

    def test_set_gauge_swallows_a_failing_metrics_facade(self):
        # Fail-open: the gauge is diagnostics, never a reason to fault a poll.
        with patch(
            "baldur.metrics.prometheus.get_metrics",
            side_effect=RuntimeError("registry down"),
        ) as get_metrics:
            arming._set_gauge(True)

        # The arm is a silent except, so the fault's own firing is the witness
        # that it was entered rather than skipped.
        assert get_metrics.called

    def test_refresh_publishes_the_verdict_the_evaluation_reached(self):
        with _links(), _dlq_recorder() as recorder:
            refresh_armed_gauge()

        recorder.set_auto_replay_armed.assert_called_once_with(True)

    def test_refresh_publishes_zero_when_the_evaluation_raises(self):
        # A stale positive claim is the one outcome this gauge must never
        # produce, so a failed evaluation writes 0 rather than leaving a 1.
        with (
            patch(f"{_MOD}._evaluate", side_effect=RuntimeError("probe boom")),
            _dlq_recorder() as recorder,
        ):
            refresh_armed_gauge()

        recorder.set_auto_replay_armed.assert_called_once_with(False)
