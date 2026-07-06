"""DaemonWorkerMetricRecorder unit tests (impl 489 D2 + D8).

Test targets:
    - baldur.metrics.recorders.daemon_worker.register_daemon_worker
    - unregister_daemon_worker / get_registered_daemon_workers
    - _DaemonWorkerCollector.collect()
    - record_daemon_worker_restart (lifetime counter)
    - DaemonWorkerMetricRecorder facade slot in BaldurMetrics

UNIT_TEST_GUIDELINES.md compliance:
- Contract: hardcoded metric names, gauge labels
- Behavior: register/unregister flow, observer injection side-effect,
  scrape gauge values reflect handle state
"""

from __future__ import annotations

import threading

import pytest

from baldur.meta.daemon_worker import DaemonWorkerHandle


@pytest.fixture(autouse=True)
def _reset_handle_registry():
    """Snapshot the registry before each test, restore on teardown.

    Other tests in the suite may register handles as a side effect of
    importing/initializing workers. Snapshotting keeps unrelated entries
    intact while letting this file's tests start from a known state.
    """
    from baldur.metrics.recorders import daemon_worker as mod

    with mod._registry_lock:
        snapshot = dict(mod._handle_registry)
        mod._handle_registry.clear()
    yield
    with mod._registry_lock:
        mod._handle_registry.clear()
        mod._handle_registry.update(snapshot)


def _dummy_thread() -> threading.Thread:
    return threading.Thread(target=lambda: None, daemon=True)


def _live_thread(stop_event: threading.Event) -> threading.Thread:
    """A daemon thread that stays alive until ``stop_event`` is set."""
    t = threading.Thread(target=lambda: stop_event.wait(timeout=5.0), daemon=True)
    t.start()
    return t


# =============================================================================
# Contract — module exports + metric names
# =============================================================================


class TestDaemonWorkerRecorderContract:
    """impl 489 D2: public surface contract."""

    def test_module_exports(self):
        from baldur.metrics.recorders.daemon_worker import __all__

        assert "DaemonWorkerMetricRecorder" in __all__
        assert "register_daemon_worker" in __all__
        assert "unregister_daemon_worker" in __all__
        assert "get_registered_daemon_workers" in __all__

    def test_collector_emits_three_named_gauges(self):
        """``_DaemonWorkerCollector.collect`` yields the documented gauges."""
        from baldur.metrics.recorders.daemon_worker import (
            PROMETHEUS_AVAILABLE,
            _DaemonWorkerCollector,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

        collector = _DaemonWorkerCollector()
        names = {fam.name for fam in collector.collect()}
        assert "baldur_daemon_worker_alive" in names
        assert "baldur_daemon_worker_last_heartbeat_age_seconds" in names
        assert "baldur_daemon_worker_processing_delay_seconds" in names

    def test_each_gauge_carries_name_label(self):
        """Each yielded gauge declares the ``name`` label."""
        from baldur.metrics.recorders.daemon_worker import (
            PROMETHEUS_AVAILABLE,
            _DaemonWorkerCollector,
            register_daemon_worker,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

        handle = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=1.0)
        register_daemon_worker("test-label", handle)

        collector = _DaemonWorkerCollector()
        for fam in collector.collect():
            for sample in fam.samples:
                assert "name" in sample.labels
                assert sample.labels["name"] == "test-label"


# =============================================================================
# Behavior — registry idempotency
# =============================================================================


class TestDaemonWorkerRegistryBehavior:
    """impl 489 D1: register / unregister / get_registered idempotency."""

    def test_register_then_get_returns_same_handle(self):
        from baldur.metrics.recorders.daemon_worker import (
            get_registered_daemon_workers,
            register_daemon_worker,
        )

        handle = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=1.0)
        register_daemon_worker("alpha", handle)

        assert get_registered_daemon_workers()["alpha"] is handle

    def test_double_register_same_name_replaces(self):
        """Second ``register_daemon_worker`` under the same name silently replaces."""
        from baldur.metrics.recorders.daemon_worker import (
            get_registered_daemon_workers,
            register_daemon_worker,
        )

        first = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=1.0)
        second = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=2.0)
        register_daemon_worker("dup", first)
        register_daemon_worker("dup", second)

        assert get_registered_daemon_workers()["dup"] is second

    def test_unregister_removes_entry(self):
        from baldur.metrics.recorders.daemon_worker import (
            get_registered_daemon_workers,
            register_daemon_worker,
            unregister_daemon_worker,
        )

        handle = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=1.0)
        register_daemon_worker("beta", handle)
        assert "beta" in get_registered_daemon_workers()

        unregister_daemon_worker("beta")
        assert "beta" not in get_registered_daemon_workers()

    def test_unregister_absent_name_is_noop(self):
        """``unregister_daemon_worker`` on absent name does not raise (mirrors executor)."""
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

        unregister_daemon_worker("never-registered")
        unregister_daemon_worker("never-registered")

    def test_get_registered_returns_snapshot_copy(self):
        """``get_registered_daemon_workers`` returns an isolated dict snapshot."""
        from baldur.metrics.recorders.daemon_worker import (
            get_registered_daemon_workers,
            register_daemon_worker,
        )

        handle = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=1.0)
        register_daemon_worker("gamma", handle)

        snap = get_registered_daemon_workers()
        snap.clear()
        # Mutating the returned dict must NOT empty the real registry.
        assert "gamma" in get_registered_daemon_workers()


# =============================================================================
# Behavior — observer injection on register
# =============================================================================


class TestDaemonWorkerHistogramWiringBehavior:
    """impl 489 D2: register injects ``_iteration_duration_observer`` onto the handle."""

    def test_register_injects_iteration_duration_observer(self):
        """After ``register_daemon_worker``, the handle has an observer wired."""
        from baldur.metrics.recorders.daemon_worker import (
            PROMETHEUS_AVAILABLE,
            register_daemon_worker,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

        handle = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=1.0)
        assert handle._iteration_duration_observer is None

        register_daemon_worker("obs-wire", handle)

        assert callable(handle._iteration_duration_observer)

    def test_observe_iteration_increments_histogram_buckets(self):
        """Calling ``handle.observe_iteration(d)`` flows into the histogram."""
        from baldur.metrics.recorders import daemon_worker as mod
        from baldur.metrics.recorders.daemon_worker import (
            PROMETHEUS_AVAILABLE,
            _ensure_persistent_metrics,
            register_daemon_worker,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")
        _ensure_persistent_metrics()

        handle = DaemonWorkerHandle(thread=_dummy_thread(), tick_interval_seconds=1.0)
        register_daemon_worker("hist-test", handle)

        # Read the per-name labelled histogram's count BEFORE observing.
        labelled = mod._iteration_histogram.labels(name="hist-test")
        before = labelled._sum.get()

        handle.observe_iteration(0.05)
        handle.observe_iteration(2.0)

        after = labelled._sum.get()
        assert after - before == pytest.approx(0.05 + 2.0)


# =============================================================================
# Behavior — scrape-time gauges
# =============================================================================


class TestDaemonWorkerCollectorContract:
    """impl 489 D2: empty registry yields zero rows for the gauges."""

    def test_empty_registry_emits_zero_samples_per_gauge(self):
        from baldur.metrics.recorders.daemon_worker import (
            PROMETHEUS_AVAILABLE,
            _DaemonWorkerCollector,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

        collector = _DaemonWorkerCollector()
        for fam in collector.collect():
            assert fam.samples == [], (
                f"expected empty samples for {fam.name} when registry is empty"
            )


class TestDaemonWorkerCollectorScrapeBehavior:
    """impl 489 D2: ``_DaemonWorkerCollector.collect`` reads live handle state."""

    def test_alive_gauge_reflects_thread_is_alive(self):
        """``baldur_daemon_worker_alive`` matches ``handle.thread.is_alive()``."""
        from baldur.metrics.recorders.daemon_worker import (
            PROMETHEUS_AVAILABLE,
            _DaemonWorkerCollector,
            register_daemon_worker,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

        stop = threading.Event()
        try:
            live = _live_thread(stop)
            live_handle = DaemonWorkerHandle(thread=live, tick_interval_seconds=1.0)
            register_daemon_worker("alive-yes", live_handle)

            dead_handle = DaemonWorkerHandle(
                thread=_dummy_thread(), tick_interval_seconds=1.0
            )
            register_daemon_worker("alive-no", dead_handle)

            collector = _DaemonWorkerCollector()
            samples = {
                s.labels.get("name"): s.value
                for fam in collector.collect()
                if fam.name == "baldur_daemon_worker_alive"
                for s in fam.samples
            }
            assert samples["alive-yes"] == 1.0
            assert samples["alive-no"] == 0.0
        finally:
            stop.set()

    def test_processing_delay_gauge_only_for_buffer_workers(self):
        """``processing_delay_provider=None`` → no row in the delay gauge."""
        from baldur.metrics.recorders.daemon_worker import (
            PROMETHEUS_AVAILABLE,
            _DaemonWorkerCollector,
            register_daemon_worker,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

        no_provider = DaemonWorkerHandle(
            thread=_dummy_thread(), tick_interval_seconds=1.0
        )
        with_provider = DaemonWorkerHandle(
            thread=_dummy_thread(),
            tick_interval_seconds=1.0,
            processing_delay_provider=lambda: 1.5,
        )
        register_daemon_worker("no-buffer", no_provider)
        register_daemon_worker("buffer", with_provider)

        collector = _DaemonWorkerCollector()
        delay_samples = {
            s.labels.get("name"): s.value
            for fam in collector.collect()
            if fam.name == "baldur_daemon_worker_processing_delay_seconds"
            for s in fam.samples
        }
        assert "no-buffer" not in delay_samples
        assert delay_samples["buffer"] == 1.5

    def test_heartbeat_age_is_non_negative(self):
        """The age gauge clamps to ``>= 0`` even when the heartbeat is "in the future"."""
        from baldur.metrics.recorders.daemon_worker import (
            PROMETHEUS_AVAILABLE,
            _DaemonWorkerCollector,
            register_daemon_worker,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

        # Set last_heartbeat_at to a value far in the future so the age
        # would be negative without clamping.
        handle = DaemonWorkerHandle(
            thread=_dummy_thread(),
            tick_interval_seconds=1.0,
            last_heartbeat_at=1e18,
        )
        register_daemon_worker("future-beat", handle)

        collector = _DaemonWorkerCollector()
        ages = [
            s.value
            for fam in collector.collect()
            if fam.name == "baldur_daemon_worker_last_heartbeat_age_seconds"
            for s in fam.samples
            if s.labels.get("name") == "future-beat"
        ]
        assert ages
        assert ages[0] == 0.0


# =============================================================================
# Behavior — lifetime restart counter
# =============================================================================


class TestDaemonWorkerRestartCounterBehavior:
    """impl 489 D2 + D7: ``baldur_daemon_worker_restarts_total`` is monotonic."""

    def test_record_daemon_worker_restart_increments_counter(self):
        from baldur.metrics.recorders import daemon_worker as mod
        from baldur.metrics.recorders.daemon_worker import (
            PROMETHEUS_AVAILABLE,
            _ensure_persistent_metrics,
            record_daemon_worker_restart,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")
        _ensure_persistent_metrics()

        labelled = mod._restarts_counter.labels(name="counter-test")
        before = labelled._value.get()

        record_daemon_worker_restart("counter-test")
        record_daemon_worker_restart("counter-test")
        record_daemon_worker_restart("counter-test")

        after = labelled._value.get()
        assert after - before == 3


# =============================================================================
# Facade — BaldurMetrics slot
# =============================================================================


class TestDaemonWorkerRecorderFacadeContract:
    """impl 489 D2: ``BaldurMetrics`` exposes the ``daemon_workers`` slot."""

    def test_daemon_workers_recorder_slot_present(self):
        from baldur.metrics.prometheus import BaldurMetrics
        from baldur.metrics.recorders.daemon_worker import DaemonWorkerMetricRecorder

        metrics = BaldurMetrics()
        assert hasattr(metrics, "daemon_workers")
        assert isinstance(metrics.daemon_workers, DaemonWorkerMetricRecorder)
