"""676 — On-recovery auto-replay arming probe.

Target: ``baldur.services.replay_service.arming``

    - ``get_on_recovery_arming_status`` / ``_evaluate`` — the single source of
      truth behind the gauge, the stats block and the console badge. Link
      evaluation order (first missing wins for the headline):
      ``disabled -> celery_missing -> worker_missing ->
      map_unconfigured -> handler_missing``.
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
):
    """Patch all four link seams, defaulting to a fully-armed configuration.

    Overriding a single kwarg isolates exactly one missing link so the
    first-missing-wins ordering can be asserted.
    """
    cfg = _ARMED_CONFIG if config is None else config
    with (
        patch(f"{_MOD}._resolve_replay_config", return_value=cfg),
        patch(f"{_MOD}._celery_task_importable", return_value=celery),
        patch(f"{_MOD}._cached_worker_state", return_value=worker),
        patch(f"{_MOD}._has_registered_handler", return_value=handler),
    ):
        yield


# =============================================================================
# Link-state evaluation — first-missing-wins ordering
# =============================================================================


class TestArmingStatusBehavior:
    """``_evaluate`` walks the dependency chain and stops at the first hard
    prerequisite; leaf links are evaluated together.
    """

    def test_all_links_ok_is_armed(self):
        with _links():
            status = arming._evaluate(check_worker=True)

        assert status.armed is True
        assert status.missing_link is None
        assert status.missing_links == []
        assert status.links == {
            "disabled": "ok",
            "celery_missing": "ok",
            "worker_missing": "ok",
            "map_unconfigured": "ok",
            "handler_missing": "ok",
        }

    def test_disabled_short_circuits_before_celery(self):
        with _links(config={"on_recovery_enabled": False}):
            status = arming._evaluate(check_worker=True)

        assert status.missing_link == "disabled"
        assert "celery_missing" not in status.links

    def test_celery_missing_short_circuits_before_worker(self):
        with _links(celery=False):
            status = arming._evaluate(check_worker=True)

        assert status.missing_link == "celery_missing"
        assert "worker_missing" not in status.links

    def test_worker_missing_is_the_headline_when_only_worker_absent(self):
        with _links(worker="missing"):
            status = arming._evaluate(check_worker=True)

        assert status.armed is False
        assert status.missing_link == "worker_missing"
        assert status.missing_links == ["worker_missing"]

    def test_map_unconfigured_when_map_empty(self):
        with _links(
            config={"on_recovery_enabled": True, "service_failure_type_map": {}}
        ):
            status = arming._evaluate(check_worker=True)

        assert status.missing_link == "map_unconfigured"

    def test_handler_missing_when_no_registered_handler(self):
        with _links(handler=False):
            status = arming._evaluate(check_worker=True)

        assert status.missing_link == "handler_missing"

    def test_multiple_leaf_links_reported_headline_is_first_in_order(self):
        # worker + map + handler all missing at once: leaves are evaluated
        # together, so missing_links carries all three, but the headline is
        # the first in _LINK_ORDER.
        with _links(
            worker="missing",
            config={"on_recovery_enabled": True, "service_failure_type_map": {}},
            handler=False,
        ):
            status = arming._evaluate(check_worker=True)

        assert status.missing_link == "worker_missing"
        assert status.missing_links == [
            "worker_missing",
            "map_unconfigured",
            "handler_missing",
        ]

    def test_worker_unknown_fails_open_and_does_not_disarm(self):
        # A transient broker hiccup resolves the worker link to "unknown"
        # (not "missing"), so it must NOT count against arming.
        with _links(worker="unknown"):
            status = arming._evaluate(check_worker=True)

        assert status.armed is True
        assert status.links["worker_missing"] == "unknown"
        assert "worker_missing" not in status.missing_links

    def test_worker_link_unevaluated_when_check_worker_false(self):
        # Startup / dispatch gauge refresh skips the broker I/O link.
        with _links():
            status = arming._evaluate(check_worker=False)

        assert status.links["worker_missing"] == "unevaluated"
        assert status.armed is True

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
        assert status.missing_link == "probe_failed"
        # armed=None leaves the gauge unchanged (called, early-returns).
        set_gauge.assert_called_once_with(None)


# =============================================================================
# ArmingStatus DTO contract
# =============================================================================


class TestArmingStatusContract:
    """Frozen result DTO + the fail-open sentinel."""

    def test_probe_failed_sentinel_shape(self):
        status = ArmingStatus.probe_failed()
        assert status.armed is None
        assert status.missing_link == "probe_failed"
        assert status.missing_links == ["probe_failed"]
        assert status.links == {}

    def test_default_collections_are_empty(self):
        status = ArmingStatus(armed=True, missing_link=None)
        assert status.missing_links == []
        assert status.links == {}

    def test_frozen_instance_rejects_mutation(self):
        status = ArmingStatus(armed=True, missing_link=None)
        with pytest.raises(Exception):
            status.armed = False  # type: ignore[misc]


# =============================================================================
# Worker-presence TTL cache
# =============================================================================


class TestWorkerCacheBehavior:
    """``_cached_worker_state`` collapses concurrent/periodic polls onto one
    broker round-trip per TTL window.
    """

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        reset_worker_cache()
        yield
        reset_worker_cache()

    def _settings(self, ttl: int = 15):
        settings = MagicMock()
        settings.worker_status_cache_ttl_seconds = ttl
        return settings

    def test_second_call_within_ttl_reuses_cached_broker_result(self):
        probe = MagicMock(return_value="ok")
        # monotonic advances 5s between calls — inside the 15s TTL window.
        with (
            patch(f"{_MOD}._probe_dlq_worker", probe),
            patch(
                "baldur.settings.celery_task.get_celery_task_settings",
                return_value=self._settings(ttl=15),
            ),
            patch(f"{_MOD}.time.monotonic", side_effect=[1000.0, 1005.0]),
        ):
            first = arming._cached_worker_state()
            second = arming._cached_worker_state()

        assert first == "ok"
        assert second == "ok"
        # The broker probe ran exactly once — the second poll hit the cache.
        assert probe.call_count == 1

    def test_call_after_ttl_expiry_refreshes_broker_result(self):
        probe = MagicMock(side_effect=["missing", "ok"])
        # 1000 -> cache(expiry 1015); 1020 is past expiry -> re-probe.
        with (
            patch(f"{_MOD}._probe_dlq_worker", probe),
            patch(
                "baldur.settings.celery_task.get_celery_task_settings",
                return_value=self._settings(ttl=15),
            ),
            patch(f"{_MOD}.time.monotonic", side_effect=[1000.0, 1020.0]),
        ):
            first = arming._cached_worker_state()
            second = arming._cached_worker_state()

        assert first == "missing"
        assert second == "ok"
        assert probe.call_count == 2

    def test_reset_worker_cache_forces_fresh_probe(self):
        probe = MagicMock(return_value="ok")
        with (
            patch(f"{_MOD}._probe_dlq_worker", probe),
            patch(
                "baldur.settings.celery_task.get_celery_task_settings",
                return_value=self._settings(ttl=15),
            ),
            patch(f"{_MOD}.time.monotonic", side_effect=[1000.0, 1001.0]),
        ):
            arming._cached_worker_state()
            reset_worker_cache()
            arming._cached_worker_state()

        # The reset invalidated the cache, so the second call re-probed.
        assert probe.call_count == 2
