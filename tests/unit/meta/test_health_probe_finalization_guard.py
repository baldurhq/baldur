"""
Regression guard for HealthProbeManager interpreter-shutdown race.

Background:
    `HealthProbeManager._run_loop` runs on a daemon thread and calls
    `probe_all()`, which creates a per-probe `ThreadPoolExecutor`. Without
    a shutdown guard, a test that forgot to stop the manager would race the
    interpreter finalizer and emit `cannot schedule new futures after
    interpreter shutdown` once Python tears down the runtime.

    `src/baldur/meta/health_probe.py` guards both `_run_loop` (loop top) and
    `probe_all` (before creating the executor and per-probe) with
    `sys.is_finalizing()`. These regressions pin that contract: once Python
    reports finalization, the loop exits without executing probes or
    scheduling futures.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class _CountingProbe:
    """Minimal HealthProbe stub that records how often `probe()` runs."""

    def __init__(self, name: str = "unit_test_probe") -> None:
        self._name = name
        self.call_count = 0

    @property
    def component_name(self) -> str:
        return self._name

    def probe(self):
        from baldur.meta.health_probe import HealthStatus, ProbeResult
        from baldur.utils.time import utc_now

        self.call_count += 1
        return ProbeResult(
            component=self._name,
            status=HealthStatus.HEALTHY,
            latency_ms=0.0,
            timestamp=utc_now(),
        )


class TestHealthProbeFinalizationGuardBehavior:
    """`sys.is_finalizing()` guard must short-circuit the probe loop."""

    def test_run_loop_exits_without_invoking_probe_all_when_finalizing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Given: `sys.is_finalizing()` returns True (simulating shutdown).
        When:  `_run_loop` is invoked directly on a running manager.
        Then:  it breaks out immediately, so `probe_all` is never called.
        """
        # Given
        from baldur.meta import health_probe as health_probe_module
        from baldur.meta.health_probe import HealthProbeManager

        manager = HealthProbeManager(probes=[_CountingProbe()])
        manager._running = True  # satisfy the `while self._running:` precondition
        probe_all_spy = MagicMock()
        monkeypatch.setattr(manager, "probe_all", probe_all_spy)
        monkeypatch.setattr(health_probe_module.sys, "is_finalizing", lambda: True)

        # When
        manager._run_loop()

        # Then
        probe_all_spy.assert_not_called()

    def test_probe_all_returns_empty_and_skips_executor_when_finalizing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Given: `sys.is_finalizing()` returns True before `probe_all()` runs.
        When:  `probe_all()` is called directly.
        Then:  it returns an empty dict without importing or instantiating
               `TimeoutExecutor` — i.e. no future is scheduled.
        """
        # Given
        from baldur.meta import health_probe as health_probe_module
        from baldur.meta.health_probe import HealthProbeManager

        probe = _CountingProbe()
        manager = HealthProbeManager(probes=[probe])
        monkeypatch.setattr(health_probe_module.sys, "is_finalizing", lambda: True)

        # Sentinel: if the guard fails, TimeoutExecutor import would occur.
        executor_ctor = MagicMock(
            side_effect=AssertionError(
                "TimeoutExecutor must not be invoked during finalization"
            )
        )
        monkeypatch.setattr(
            "baldur.core.timeout_executor.TimeoutExecutor", executor_ctor
        )

        # When
        results = manager.probe_all()

        # Then
        assert results == {}
        assert probe.call_count == 0
        executor_ctor.assert_not_called()

    def test_probe_all_executes_normally_when_not_finalizing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Given: `sys.is_finalizing()` returns False (normal runtime).
        When:  `probe_all()` runs against a single stub probe.
        Then:  the probe executes and its result is returned — confirming
               the guard is a pure short-circuit, not a permanent disable.
        """
        # Given
        from baldur.meta import health_probe as health_probe_module
        from baldur.meta.health_probe import HealthProbeManager, HealthStatus

        probe = _CountingProbe(name="guard_not_tripped")
        manager = HealthProbeManager(probes=[probe])
        monkeypatch.setattr(health_probe_module.sys, "is_finalizing", lambda: False)

        # When
        results = manager.probe_all()

        # Then
        assert probe.call_count == 1
        assert "guard_not_tripped" in results
        assert results["guard_not_tripped"].status is HealthStatus.HEALTHY
