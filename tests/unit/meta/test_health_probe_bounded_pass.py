"""Unit tests for the bounded health-probe pass — ``probe_all(deadline=...)``.

Target: ``baldur.meta.health_probe.HealthProbeManager``.

A pass given a deadline fans its probes out concurrently and stops collecting at
that instant: a probe still running yields ``UNKNOWN`` with ``observed=False``
instead of extending the pass. A component whose feature is inactive stays
absent from the results, and a component whose previous invocation is still in
flight is skipped rather than invoked twice.

Verification techniques applied:
  - Side effects — the truncation WARNING and both fail-open metric recorders
  - State transition — single-flight skip -> reset once the stuck call returns
  - Idempotency — the worker-spawn alive guard
  - Exception/edge cases — a raising probe, an inapplicable probe
  - Negative assertions — a nominal pass is never truncated; a replaced probe
    instance is not entered while the old one is still parked

Synchronization is on test-controlled events only: no sleeps, and no assertion
on a wall-clock duration tighter than the (much larger) per-probe timeout the
same pass carries.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from baldur.meta.config import MetaWatchdogSettings
from baldur.meta.health_probe import (
    PROBE_SINGLE_FLIGHT_REASON,
    PROBE_TRUNCATED_REASON,
    HealthProbe,
    HealthProbeManager,
    HealthStatus,
    ProbeResult,
)
from baldur.utils.time import utc_now

# Generous: every wait below is released by the test itself, so these bound a
# hang, they do not pace a pass.
_EVENT_WAIT_SECONDS = 10.0
# A truncated pass must return far inside the per-probe timeout the same pass
# carries (30 s in these fixtures) — that gap is the signal, not the value.
_TRUNCATED_PASS_UPPER_BOUND_SECONDS = 5.0


class _LogSpy:
    """Recording stand-in for the module-level structlog logger."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.entries: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        def _log(event: str, **kwargs: Any) -> None:
            with self._lock:
                self.entries.append((level, event, kwargs))

        return _log

    def events(self, event: str) -> list[tuple[str, str, dict]]:
        with self._lock:
            return [entry for entry in self.entries if entry[1] == event]


class _ControlledProbe(HealthProbe):
    """Probe whose entry, blocking and verdict are driven by the test."""

    def __init__(
        self,
        component_name: str,
        *,
        status: HealthStatus = HealthStatus.HEALTHY,
        block: threading.Event | None = None,
        raises: Exception | None = None,
        applicable: bool = True,
    ) -> None:
        self._component_name = component_name
        self._status = status
        self._block = block
        self._raises = raises
        self._applicable = applicable
        self.entered = threading.Event()
        self.returned = threading.Event()
        self.call_count = 0

    @property
    def component_name(self) -> str:
        return self._component_name

    def is_applicable(self) -> bool:
        return self._applicable

    def probe(self) -> ProbeResult:
        self.call_count += 1
        self.entered.set()
        try:
            if self._block is not None:
                self._block.wait(_EVENT_WAIT_SECONDS)
            if self._raises is not None:
                raise self._raises
            return ProbeResult(
                component=self._component_name,
                status=self._status,
                latency_ms=1.0,
                timestamp=utc_now(),
            )
        finally:
            self.returned.set()


def _settings(**overrides: Any) -> MetaWatchdogSettings:
    """Settings whose per-probe timeout is far larger than any deadline used here.

    That ordering is deliberate: the collector waits until
    ``min(pass_start + probe_timeout, deadline)``, so a truncation observed with
    these settings can only have come from the deadline.
    """
    base: dict[str, Any] = {
        "enabled": True,
        "probe_interval_seconds": 30.0,
        "probe_timeout_seconds": 30.0,
    }
    base.update(overrides)
    return MetaWatchdogSettings(**base)


@pytest.fixture
def released():
    """An event every parked probe waits on, released before the test ends."""
    event = threading.Event()
    yield event
    event.set()


@pytest.fixture
def log_spy(monkeypatch):
    from baldur.meta import health_probe

    spy = _LogSpy()
    monkeypatch.setattr(health_probe, "logger", spy)
    return spy


@pytest.fixture
def recorded_metrics(monkeypatch):
    """Capture both fail-open recorder calls the bounded pass makes."""
    from baldur.metrics.recorders import watchdog as recorders

    truncated: list[int] = []
    skipped: list[str] = []
    monkeypatch.setattr(
        recorders, "record_watchdog_pass_truncated", lambda: truncated.append(1)
    )
    monkeypatch.setattr(
        recorders,
        "record_watchdog_single_flight_skipped",
        lambda component: skipped.append(component),
    )
    return truncated, skipped


class TestBoundedProbePassBehavior:
    """probe_all(deadline=...) bounds the pass and marks what it never observed."""

    def test_probe_all_returns_at_the_deadline_while_a_probe_is_still_running(
        self, released
    ):
        # Given: a probe that will not return until the test releases it
        stuck = _ControlledProbe("stuck", block=released)
        manager = HealthProbeManager(settings=_settings(), probes=[stuck])

        # When: the pass carries a deadline far shorter than the probe timeout
        start = time.monotonic()
        results = manager.probe_all(deadline=time.monotonic() + 0.2)
        elapsed = time.monotonic() - start

        # Then: the pass ended without waiting for the probe
        assert stuck.entered.wait(_EVENT_WAIT_SECONDS)
        assert not stuck.returned.is_set()
        assert elapsed < _TRUNCATED_PASS_UPPER_BOUND_SECONDS
        assert "stuck" in results

    def test_truncated_component_reports_unknown_unobserved_with_the_budget_reason(
        self, released
    ):
        stuck = _ControlledProbe("stuck", block=released)
        manager = HealthProbeManager(settings=_settings(), probes=[stuck])

        results = manager.probe_all(deadline=time.monotonic() + 0.2)

        result = results["stuck"]
        assert result.status == HealthStatus.UNKNOWN
        assert result.reason == PROBE_TRUNCATED_REASON
        assert result.observed is False

    def test_truncation_does_not_cost_the_completed_probes_their_verdicts(
        self, released
    ):
        # Given: one stalled probe beside two that answer immediately
        stuck = _ControlledProbe("stuck", block=released)
        healthy = _ControlledProbe("healthy", status=HealthStatus.HEALTHY)
        unhealthy = _ControlledProbe("unhealthy", status=HealthStatus.UNHEALTHY)
        manager = HealthProbeManager(
            settings=_settings(), probes=[stuck, healthy, unhealthy]
        )

        results = manager.probe_all(deadline=time.monotonic() + 0.3)

        assert results["healthy"].status == HealthStatus.HEALTHY
        assert results["healthy"].observed is True
        assert results["unhealthy"].status == HealthStatus.UNHEALTHY
        assert results["unhealthy"].observed is True
        assert results["stuck"].observed is False

    def test_probes_run_concurrently_under_one_deadline(self):
        # Given: "first" cannot finish until "second" has entered its probe.
        # Serial execution in list order would park on "first" until the
        # deadline truncated it, and "second" would never run at all.
        second = _ControlledProbe("second")
        first = _ControlledProbe("first", block=second.entered)
        manager = HealthProbeManager(settings=_settings(), probes=[first, second])

        # When
        results = manager.probe_all(deadline=time.monotonic() + 5.0)

        # Then: both produced real verdicts inside the one deadline
        assert results["second"].observed is True
        assert results["first"].observed is True
        assert results["first"].status == HealthStatus.HEALTHY

    def test_overall_status_after_truncation_folds_to_degraded(self, released):
        # Given: every completed probe is HEALTHY and one is truncated
        stuck = _ControlledProbe("stuck", block=released)
        healthy = _ControlledProbe("healthy", status=HealthStatus.HEALTHY)
        manager = HealthProbeManager(settings=_settings(), probes=[stuck, healthy])

        manager.probe_all(deadline=time.monotonic() + 0.2)

        # Then: truncation alone never reports the system UNHEALTHY
        assert manager.get_overall_status() == HealthStatus.DEGRADED

    def test_truncation_preserves_a_real_unhealthy_verdict(self, released):
        stuck = _ControlledProbe("stuck", block=released)
        broken = _ControlledProbe("broken", status=HealthStatus.UNHEALTHY)
        manager = HealthProbeManager(settings=_settings(), probes=[stuck, broken])

        manager.probe_all(deadline=time.monotonic() + 0.3)

        assert manager.get_overall_status() == HealthStatus.UNHEALTHY

    def test_inapplicable_component_is_absent_from_a_bounded_pass(self):
        # Given: a probe whose backing feature is off
        disabled = _ControlledProbe("disabled_feature", applicable=False)
        active = _ControlledProbe("active")
        manager = HealthProbeManager(settings=_settings(), probes=[disabled, active])

        results = manager.probe_all(deadline=time.monotonic() + 5.0)

        # Then: absent entirely — never reported UNKNOWN
        assert "disabled_feature" not in results
        assert disabled.call_count == 0
        assert results["active"].status == HealthStatus.HEALTHY

    def test_inapplicable_component_is_not_named_in_the_truncation_warning(
        self, released, log_spy
    ):
        disabled = _ControlledProbe("disabled_feature", applicable=False)
        stuck = _ControlledProbe("stuck", block=released)
        manager = HealthProbeManager(settings=_settings(), probes=[disabled, stuck])

        manager.probe_all(deadline=time.monotonic() + 0.3)

        warnings = log_spy.events("watchdog.pass_budget_exhausted")
        assert len(warnings) == 1
        level, _, payload = warnings[0]
        assert level == "warning"
        assert payload["truncated_components"] == ["stuck"]

    def test_truncated_pass_counts_the_pass_truncated_metric(
        self, released, recorded_metrics
    ):
        truncated_metric, _ = recorded_metrics
        stuck = _ControlledProbe("stuck", block=released)
        manager = HealthProbeManager(settings=_settings(), probes=[stuck])

        manager.probe_all(deadline=time.monotonic() + 0.2)

        # One pass-level count, regardless of how many components were cut off
        assert len(truncated_metric) == 1

    def test_nominal_pass_is_never_truncated(self, log_spy, recorded_metrics):
        # Given: every probe answers immediately, with a whole budget available
        truncated_metric, _ = recorded_metrics
        probes = [_ControlledProbe(f"fast_{i}") for i in range(3)]
        manager = HealthProbeManager(settings=_settings(), probes=probes)

        results = manager.probe_all(deadline=time.monotonic() + 5.0)

        assert all(result.observed is True for result in results.values())
        assert all(result.reason == "" for result in results.values())
        assert log_spy.events("watchdog.pass_budget_exhausted") == []
        assert truncated_metric == []

    def test_raising_probe_yields_an_observed_unknown_carrying_the_error(self):
        # Given: a probe that raises rather than stalls — a verdict WAS produced
        broken = _ControlledProbe("broken", raises=RuntimeError("probe boom"))
        manager = HealthProbeManager(settings=_settings(), probes=[broken])

        results = manager.probe_all(deadline=time.monotonic() + 5.0)

        assert results["broken"].status == HealthStatus.UNKNOWN
        assert results["broken"].error == "probe boom"
        assert results["broken"].observed is True

    def test_deadline_none_keeps_the_serial_path(self):
        # Given/When: no pass budget to protect
        probes = [_ControlledProbe("a"), _ControlledProbe("b")]
        manager = HealthProbeManager(settings=_settings(), probes=probes)

        results = manager.probe_all()

        assert set(results) == {"a", "b"}
        assert all(result.observed is True for result in results.values())


class TestProbeSingleFlightBehavior:
    """One component never has two invocations in flight across passes."""

    def test_second_pass_skips_a_component_whose_invocation_still_runs(
        self, released, recorded_metrics
    ):
        # Given: pass 1 left the probe running
        _, skipped_metric = recorded_metrics
        stuck = _ControlledProbe("stuck", block=released)
        manager = HealthProbeManager(settings=_settings(), probes=[stuck])
        manager.probe_all(deadline=time.monotonic() + 0.2)
        assert stuck.entered.wait(_EVENT_WAIT_SECONDS)

        # When: a second pass runs while it is still parked
        results = manager.probe_all(deadline=time.monotonic() + 0.2)

        # Then: skipped, not invoked a second time
        assert stuck.call_count == 1
        assert results["stuck"].status == HealthStatus.UNKNOWN
        assert results["stuck"].reason == PROBE_SINGLE_FLIGHT_REASON
        assert results["stuck"].observed is False
        assert skipped_metric == ["stuck"]

    def test_component_probes_again_once_the_stuck_invocation_returns(self, released):
        # Given: a component skipped while its previous invocation was parked
        stuck = _ControlledProbe("stuck", block=released)
        manager = HealthProbeManager(settings=_settings(), probes=[stuck])
        manager.probe_all(deadline=time.monotonic() + 0.2)
        assert stuck.entered.wait(_EVENT_WAIT_SECONDS)
        manager.probe_all(deadline=time.monotonic() + 0.2)

        # When: the stuck invocation finally returns
        released.set()
        assert stuck.returned.wait(_EVENT_WAIT_SECONDS)
        results = manager.probe_all(deadline=time.monotonic() + 5.0)

        # Then: the next pass observes a real verdict again
        assert stuck.call_count == 2
        assert results["stuck"].status == HealthStatus.HEALTHY
        assert results["stuck"].observed is True

    def test_third_consecutive_skip_warns_that_the_component_is_stuck(
        self, released, log_spy
    ):
        # Given: an invocation that never returns
        stuck = _ControlledProbe("stuck", block=released)
        manager = HealthProbeManager(settings=_settings(), probes=[stuck])
        manager.probe_all(deadline=time.monotonic() + 0.2)
        assert stuck.entered.wait(_EVENT_WAIT_SECONDS)

        # When: two skips, then a third
        manager.probe_all(deadline=time.monotonic() + 0.2)
        manager.probe_all(deadline=time.monotonic() + 0.2)
        assert log_spy.events("health_probe_manager.probe_single_flight_stuck") == []
        manager.probe_all(deadline=time.monotonic() + 0.2)

        # Then: exactly the third skip warns, naming the component and the count
        warnings = log_spy.events("health_probe_manager.probe_single_flight_stuck")
        assert len(warnings) == 1
        level, _, payload = warnings[0]
        assert level == "warning"
        assert payload["probe"] == "stuck"
        assert payload["consecutive_skips"] == 3

    def test_replacing_the_probe_under_the_same_name_does_not_double_invoke(
        self, released
    ):
        # Given: instance A of "shared" is parked mid-probe
        first = _ControlledProbe("shared", block=released)
        manager = HealthProbeManager(settings=_settings(), probes=[first])
        manager.probe_all(deadline=time.monotonic() + 0.2)
        assert first.entered.wait(_EVENT_WAIT_SECONDS)

        # When: the public remove/add pair installs a fresh instance for the
        # SAME component name, and a pass runs
        assert manager.remove_probe("shared") is True
        second = _ControlledProbe("shared")
        manager.add_probe(second)
        results = manager.probe_all(deadline=time.monotonic() + 0.3)

        # Then: the new instance was never entered — single-flight is keyed by
        # component name, so the component's external state stays untouched
        assert second.call_count == 0
        assert not second.entered.is_set()
        assert results["shared"].reason == PROBE_SINGLE_FLIGHT_REASON


class TestProbeManagerSpawnGuardBehavior:
    """_spawn_worker_thread refuses to start a second live loop."""

    def test_spawn_is_a_noop_while_the_current_worker_is_alive(self):
        # Given: a running manager
        manager = HealthProbeManager(settings=_settings(), probes=[])
        manager.start()
        try:
            original = manager._worker
            assert original is not None
            assert original.is_alive()

            # When: a stale respawn observation fires the spawn again
            manager._spawn_worker_thread()

            # Then: the same single worker keeps the loop
            assert manager._worker is original
        finally:
            manager.stop()

    def test_spawn_replaces_a_worker_that_is_no_longer_alive(self):
        # Given: a manager whose worker handle refers to a dead thread
        manager = HealthProbeManager(settings=_settings(), probes=[])
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        manager._worker = dead
        manager._running = True
        try:
            # When: the restart callback fires
            manager._spawn_worker_thread()

            # Then: a genuinely dead worker still respawns
            assert manager._worker is not dead
            assert manager._worker.is_alive()
        finally:
            manager.stop()
