"""ExecutorMetricRecorder unit tests (487 D11/D12).

Test targets:
- baldur.metrics.recorders.executor.register_executor / unregister_executor
- baldur.metrics.recorders.executor._ExecutorCollector.collect()
- ExecutorMetricRecorder facade slot in BaldurMetrics
- Cross-cutting: TimeoutPolicy and BaldurEventBus both register on first
  ``_get_executor()`` call and unregister on shutdown.

UNIT_TEST_GUIDELINES.md compliance:
- Contract verification: hardcoded metric names + label
- Behavior verification: source-referenced register/unregister flow,
  scrape gauge values from live ThreadPoolExecutor private attributes
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.fixture(autouse=True)
def _reset_executor_registry():
    """Snapshot the registry before each test, restore on teardown.

    Other tests in the suite may register executors as a side effect
    of importing TimeoutPolicy / BaldurEventBus. Snapshotting keeps
    those registrations intact while letting this file's tests start
    from a known empty-or-known state.
    """
    from baldur.metrics.recorders import executor as mod

    with mod._registry_lock:
        snapshot = dict(mod._executor_registry)
        mod._executor_registry.clear()
    yield
    with mod._registry_lock:
        mod._executor_registry.clear()
        mod._executor_registry.update(snapshot)


# =============================================================================
# Contract — exports + metric names
# =============================================================================


class TestExecutorRecorderContract:
    """487 D11: ExecutorMetricRecorder public surface contract."""

    def test_module_exports(self):
        """__all__ exposes class + register/unregister/get_registered."""
        from baldur.metrics.recorders.executor import __all__

        assert "ExecutorMetricRecorder" in __all__
        assert "register_executor" in __all__
        assert "unregister_executor" in __all__
        assert "get_registered_executors" in __all__

    def test_collector_emits_three_named_gauges(self):
        """_ExecutorCollector yields queue_size / active_threads / max_workers gauges."""
        from baldur.metrics.recorders.executor import (
            PROMETHEUS_AVAILABLE,
            _ExecutorCollector,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

        collector = _ExecutorCollector()
        names = {fam.name for fam in collector.collect()}
        assert names == {
            "baldur_executor_queue_size",
            "baldur_executor_active_threads",
            "baldur_executor_max_workers",
        }

    def test_each_gauge_carries_name_label(self):
        """Each yielded gauge declares the 'name' label."""
        from baldur.metrics.recorders.executor import (
            PROMETHEUS_AVAILABLE,
            _ExecutorCollector,
            register_executor,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-label")
        try:
            register_executor("test-label", executor)
            collector = _ExecutorCollector()
            for fam in collector.collect():
                # GaugeMetricFamily exposes ``samples``; each sample's labels
                # is a dict — assert ``name`` is present on every emitted
                # sample.
                for sample in fam.samples:
                    assert "name" in sample.labels
                    assert sample.labels["name"] == "test-label"
        finally:
            executor.shutdown(wait=True)


# =============================================================================
# Behavior — registry idempotency
# =============================================================================


class TestExecutorRegistryBehavior:
    """487 D11: register / unregister / get_registered idempotency."""

    def test_register_then_get_returns_same_executor(self):
        """register_executor adds the entry visible through get_registered_executors."""
        from baldur.metrics.recorders.executor import (
            get_registered_executors,
            register_executor,
        )

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            register_executor("alpha", executor)
            assert get_registered_executors()["alpha"] is executor
        finally:
            executor.shutdown(wait=True)

    def test_double_register_same_name_replaces(self):
        """Second register_executor under the same name silently replaces."""
        from baldur.metrics.recorders.executor import (
            get_registered_executors,
            register_executor,
        )

        first = ThreadPoolExecutor(max_workers=1)
        second = ThreadPoolExecutor(max_workers=2)
        try:
            register_executor("dup", first)
            register_executor("dup", second)
            assert get_registered_executors()["dup"] is second
        finally:
            first.shutdown(wait=True)
            second.shutdown(wait=True)

    def test_unregister_removes_entry(self):
        """unregister_executor removes the registry slot."""
        from baldur.metrics.recorders.executor import (
            get_registered_executors,
            register_executor,
            unregister_executor,
        )

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            register_executor("beta", executor)
            assert "beta" in get_registered_executors()
            unregister_executor("beta")
            assert "beta" not in get_registered_executors()
        finally:
            executor.shutdown(wait=True)

    def test_unregister_absent_name_is_noop(self):
        """unregister_executor of an unknown name is a silent no-op (D11 dual-invocation safety)."""
        from baldur.metrics.recorders.executor import unregister_executor

        # Must not raise — covers reset_protect_caches() ↔ reset_event_bus_settings()
        # double-call against an already-cleared registry.
        unregister_executor("never-registered")
        unregister_executor("never-registered")

    def test_get_registered_returns_snapshot_copy(self):
        """get_registered_executors returns an isolated dict snapshot."""
        from baldur.metrics.recorders.executor import (
            get_registered_executors,
            register_executor,
        )

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            register_executor("gamma", executor)
            snap = get_registered_executors()
            snap.clear()
            # Mutating the returned dict must NOT empty the real registry.
            assert "gamma" in get_registered_executors()
        finally:
            executor.shutdown(wait=True)


# =============================================================================
# Behavior — scrape-time gauges
# =============================================================================


class TestExecutorCollectorScrapeBehavior:
    """487 D11: _ExecutorCollector.collect() reads live executor state."""

    def test_max_workers_reflects_constructor_argument(self):
        """max_workers gauge equals the executor's _max_workers."""
        from baldur.metrics.recorders.executor import (
            PROMETHEUS_AVAILABLE,
            _ExecutorCollector,
            register_executor,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

        executor = ThreadPoolExecutor(max_workers=7, thread_name_prefix="sized")
        try:
            register_executor("sized", executor)
            collector = _ExecutorCollector()

            samples = [
                s
                for fam in collector.collect()
                if fam.name == "baldur_executor_max_workers"
                for s in fam.samples
                if s.labels.get("name") == "sized"
            ]
            assert samples, "expected a sample for the registered executor"
            assert samples[0].value == 7
        finally:
            executor.shutdown(wait=True)

    def test_queue_and_active_gauges_reflect_runtime_state(self):
        """queue_size + active_threads gauges reflect executor private attrs."""
        import threading

        from baldur.metrics.recorders.executor import (
            PROMETHEUS_AVAILABLE,
            _ExecutorCollector,
            register_executor,
        )

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="runtime-state")
        block = threading.Event()
        release = threading.Event()
        try:
            register_executor("runtime-state", executor)

            def slow():
                block.set()
                release.wait(timeout=5.0)

            future = executor.submit(slow)
            assert block.wait(timeout=5.0)

            collector = _ExecutorCollector()
            samples = {
                fam.name: [
                    s for s in fam.samples if s.labels.get("name") == "runtime-state"
                ]
                for fam in collector.collect()
            }

            # When the worker has picked the task up, qsize is 0 and we
            # have one live thread. We verify the gauges are reading
            # exactly those private attrs (consistency, not fixed values
            # — the private attrs themselves are the source of truth).
            assert samples["baldur_executor_active_threads"][0].value == len(
                executor._threads
            )
            assert samples["baldur_executor_queue_size"][0].value == (
                executor._work_queue.qsize()
            )
        finally:
            release.set()
            future.result(timeout=5.0)
            executor.shutdown(wait=True)


# =============================================================================
# Behavior — DCL classmethods register/unregister both executor singletons
# =============================================================================


class TestSharedExecutorRegistrationBehavior:
    """487 D12: TimeoutPolicy + BaldurEventBus register/unregister via DCL."""

    def setup_method(self) -> None:
        from baldur.resilience.policies.timeout import TimeoutPolicy
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        TimeoutPolicy.shutdown_executor()
        BaldurEventBus.shutdown_dispatch_executor()

    def teardown_method(self) -> None:
        from baldur.resilience.policies.timeout import TimeoutPolicy
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        TimeoutPolicy.shutdown_executor()
        BaldurEventBus.shutdown_dispatch_executor()

    def test_timeout_policy_registers_on_first_get(self):
        """TimeoutPolicy._get_executor() registers under 'baldur-timeout'."""
        from baldur.metrics.recorders.executor import get_registered_executors
        from baldur.resilience.policies.timeout import TimeoutPolicy

        TimeoutPolicy._get_executor()
        registry = get_registered_executors()
        assert "baldur-timeout" in registry
        assert registry["baldur-timeout"] is TimeoutPolicy._executor

    def test_event_bus_registers_on_first_get(self):
        """BaldurEventBus._get_executor() registers under 'baldur-eventbus-dispatch'."""
        from baldur.metrics.recorders.executor import get_registered_executors
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        BaldurEventBus._get_executor()
        registry = get_registered_executors()
        assert "baldur-eventbus-dispatch" in registry
        assert registry["baldur-eventbus-dispatch"] is BaldurEventBus._executor

    def test_timeout_policy_unregisters_on_shutdown(self):
        """TimeoutPolicy.shutdown_executor() removes 'baldur-timeout'."""
        from baldur.metrics.recorders.executor import get_registered_executors
        from baldur.resilience.policies.timeout import TimeoutPolicy

        TimeoutPolicy._get_executor()
        assert "baldur-timeout" in get_registered_executors()
        TimeoutPolicy.shutdown_executor()
        assert "baldur-timeout" not in get_registered_executors()

    def test_event_bus_unregisters_on_shutdown(self):
        """BaldurEventBus.shutdown_dispatch_executor() removes the EventBus name."""
        from baldur.metrics.recorders.executor import get_registered_executors
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        BaldurEventBus._get_executor()
        assert "baldur-eventbus-dispatch" in get_registered_executors()
        BaldurEventBus.shutdown_dispatch_executor()
        assert "baldur-eventbus-dispatch" not in get_registered_executors()


# =============================================================================
# Facade — BaldurMetrics slot
# =============================================================================


class TestExecutorRecorderFacadeContract:
    """487 D11: BaldurMetrics exposes ``executor`` slot."""

    def test_executor_recorder_slot_present(self):
        """BaldurMetrics() exposes .executor (ExecutorMetricRecorder)."""
        from baldur.metrics.prometheus import BaldurMetrics
        from baldur.metrics.recorders.executor import ExecutorMetricRecorder

        metrics = BaldurMetrics()
        assert hasattr(metrics, "executor")
        assert isinstance(metrics.executor, ExecutorMetricRecorder)
