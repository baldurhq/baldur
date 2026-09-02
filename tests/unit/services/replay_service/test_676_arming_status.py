"""676 — On-recovery auto-replay arming probe.

Target: ``baldur.services.replay_service.arming``

    - ``get_on_recovery_arming_status`` / ``_evaluate`` — the single source of
      truth behind the gauge, the stats block and the console badge. Shared
      link order (first missing wins for the headline):
      ``disabled -> celery_missing -> worker_missing -> handler_missing``,
      then each lane's own link — ``map_unconfigured`` for the mapped sweep,
      ``open_circuit_capture_disabled`` for the open-circuit sweep.
    - ``ArmingStatus`` / ``ArmingStatus.probe_failed`` — the frozen result
      DTO and its fail-open sentinel (``armed=None``).
    - ``_cached_worker_state`` — the broker-presence probe cached behind a
      short TTL so the console's periodic polling pays at most one broker
      round-trip per TTL window.

Every link check is patched at its module seam, so no live broker / Celery
is touched and no ``baldur_pro`` import is needed (G19/G20/G21 safe).
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

import pytest

from baldur.services.replay_service import arming
from baldur.services.replay_service.arming import (
    ArmingStatus,
    get_on_recovery_arming_status,
    reset_worker_cache,
)

_MOD = "baldur.services.replay_service.arming"

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
# ArmingStatus DTO contract
# =============================================================================


class TestArmingStatusContract:
    """Frozen result DTO + the fail-open sentinel."""

    def test_probe_failed_sentinel_shape(self):
        status = ArmingStatus.probe_failed()
        assert status.armed is None
        assert status.missing_link is None
        assert status.missing_links == []
        assert status.unverified_link == "probe_failed"
        assert status.links == {}
        assert status.lanes == {}
        assert status.last_dispatch is None

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


# =============================================================================
# Worker-presence TTL cache
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


class TestWorkerCacheBehavior:
    """``_cached_worker_state`` collapses concurrent/periodic polls onto one
    broker round-trip per TTL window.
    """

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        reset_worker_cache(log_state=True)
        yield
        reset_worker_cache(log_state=True)

    def _settings(self, ttl: int = 15):
        settings = MagicMock()
        settings.worker_status_cache_ttl_seconds = ttl
        settings.inspect_timeout = 1
        return settings

    @contextlib.contextmanager
    def _probe_env(self, probe, clock, ttl: int = 15):
        """Drive the probe with a fake clock and a budget no test waits out."""
        with (
            patch(f"{_MOD}._probe_dlq_worker", probe),
            patch(
                "baldur.settings.celery_task.get_celery_task_settings",
                return_value=self._settings(ttl=ttl),
            ),
            patch(f"{_MOD}._probe_budget_seconds", return_value=5.0),
            patch(f"{_MOD}.time.monotonic", clock),
        ):
            yield

    def test_second_call_within_ttl_reuses_cached_broker_result(self):
        probe = MagicMock(return_value="ok")
        clock = _FakeClock()
        with self._probe_env(probe, clock):
            first = arming._cached_worker_state()
            clock.advance(5.0)  # inside the 15s TTL window
            second = arming._cached_worker_state()

        assert first == "ok"
        assert second == "ok"
        # The broker probe ran exactly once — the second poll hit the cache.
        assert probe.call_count == 1

    def test_call_after_ttl_expiry_refreshes_broker_result(self):
        probe = MagicMock(side_effect=["missing", "ok"])
        clock = _FakeClock()
        with self._probe_env(probe, clock):
            first = arming._cached_worker_state()
            clock.advance(20.0)  # past the 15s expiry
            second = arming._cached_worker_state()

        assert first == "missing"
        assert second == "ok"
        assert probe.call_count == 2

    def test_reset_worker_cache_forces_fresh_probe(self):
        probe = MagicMock(return_value="ok")
        clock = _FakeClock()
        with self._probe_env(probe, clock):
            arming._cached_worker_state()
            reset_worker_cache()
            clock.advance(1.0)
            arming._cached_worker_state()

        # The reset invalidated the cache, so the second call re-probed.
        assert probe.call_count == 2
