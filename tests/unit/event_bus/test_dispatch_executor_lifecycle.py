"""BaldurEventBus dispatch-executor lifecycle tests (487 D1, D3).

Test targets:
- BaldurEventBus._get_executor() — DCL singleton (concurrent-construct → 1)
- BaldurEventBus.shutdown_dispatch_executor() — drain + clear classvar
- BALDUR_EVENT_BUS_DISPATCH_WORKERS env-var roundtrip via reset cascade

UNIT_TEST_GUIDELINES.md compliance:
- Concurrency / state-transition / idempotency techniques (§8)
- Behavior verification — source-referenced assertions
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

# =============================================================================
# Shared setup — every test starts with the executor cleared so a prior
# test cannot leak the cached singleton into the assertions below.
# =============================================================================


@pytest.fixture(autouse=True)
def _clean_dispatch_executor():
    from baldur.services.event_bus.bus.event_bus import BaldurEventBus

    BaldurEventBus.shutdown_dispatch_executor()
    yield
    BaldurEventBus.shutdown_dispatch_executor()


# =============================================================================
# DCL singleton behavior
# =============================================================================


class TestBaldurEventBusExecutorLifecycleBehavior:
    """487 D1: ``_get_executor()`` is a process-shared DCL singleton."""

    def test_get_executor_returns_threadpool_executor(self):
        """First call constructs a ThreadPoolExecutor."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        executor = BaldurEventBus._get_executor()
        assert isinstance(executor, ThreadPoolExecutor)
        assert BaldurEventBus._executor is executor

    def test_executor_reused_across_calls(self):
        """Second + third call returns the cached instance (no rebuild)."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        first = BaldurEventBus._get_executor()
        second = BaldurEventBus._get_executor()
        third = BaldurEventBus._get_executor()
        assert first is second is third

    def test_thread_name_prefix_is_baldur_eventbus_dispatch(self):
        """Worker threads use the documented ``baldur-eventbus-dispatch`` prefix."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        executor = BaldurEventBus._get_executor()
        assert executor._thread_name_prefix == "baldur-eventbus-dispatch"

    def test_dcl_first_call_race_constructs_once(self):
        """Concurrent first-call from N threads triggers exactly 1 constructor.

        DCL fast path is the unlocked classvar read; only one thread
        wins the lock and constructs the ``ThreadPoolExecutor``.
        """
        from baldur.services.event_bus.bus import event_bus as event_bus_module
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        construct_count = 0
        original_cls = ThreadPoolExecutor

        def counting_constructor(*args: object, **kwargs: object) -> object:
            nonlocal construct_count
            construct_count += 1
            return original_cls(*args, **kwargs)

        n_threads = 8
        barrier = threading.Barrier(n_threads)
        instances: list[object] = []
        instances_lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            inst = BaldurEventBus._get_executor()
            with instances_lock:
                instances.append(inst)

        with patch.object(
            event_bus_module,
            "ThreadPoolExecutor",
            side_effect=counting_constructor,
        ):
            threads = [threading.Thread(target=worker) for _ in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)

        assert construct_count == 1
        assert len(instances) == n_threads
        assert all(inst is instances[0] for inst in instances)


# =============================================================================
# shutdown_dispatch_executor — drain + state transition
# =============================================================================


class TestBaldurEventBusExecutorShutdownBehavior:
    """487 D1/D3: ``shutdown_dispatch_executor()`` clears the slot."""

    def test_shutdown_clears_classvar(self):
        """``shutdown_dispatch_executor()`` drains and nulls ``_executor``."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        BaldurEventBus._get_executor()
        assert BaldurEventBus._executor is not None
        BaldurEventBus.shutdown_dispatch_executor()
        assert BaldurEventBus._executor is None

    def test_shutdown_idempotent_when_uninitialized(self):
        """Calling shutdown without prior _get_executor is a no-op (no error)."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        assert BaldurEventBus._executor is None
        BaldurEventBus.shutdown_dispatch_executor()  # must not raise
        assert BaldurEventBus._executor is None

    def test_post_shutdown_get_executor_rebuilds(self):
        """After shutdown, the next ``_get_executor()`` returns a NEW instance."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        first = BaldurEventBus._get_executor()
        BaldurEventBus.shutdown_dispatch_executor()
        second = BaldurEventBus._get_executor()

        assert first is not second
        assert BaldurEventBus._executor is second

    def test_shutdown_drains_in_flight_handlers(self):
        """``wait=True`` blocks until in-flight tasks complete (D3 contract)."""
        import time

        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        executor = BaldurEventBus._get_executor()

        completed: list[bool] = []
        started = threading.Event()

        def slow_task() -> None:
            started.set()
            time.sleep(0.1)
            completed.append(True)

        executor.submit(slow_task)
        assert started.wait(timeout=5.0)

        BaldurEventBus.shutdown_dispatch_executor()
        # ``wait=True`` guarantees the in-flight task ran to completion
        # before shutdown returned.
        assert completed == [True]
        assert BaldurEventBus._executor is None


# =============================================================================
# Settings roundtrip — env var → executor max_workers via reset cascade
# =============================================================================


class TestBaldurEventBusExecutorSettingsRoundtripBehavior:
    """487 D2/D3: ``dispatch_workers`` env var observable after reset cascade."""

    def setup_method(self) -> None:
        from baldur.settings.event_bus import reset_event_bus_settings

        reset_event_bus_settings()

    def teardown_method(self) -> None:
        from baldur.settings.event_bus import reset_event_bus_settings

        reset_event_bus_settings()

    def test_executor_max_workers_matches_settings_default(self):
        """Default ``dispatch_workers=32`` reaches the executor's ``_max_workers``."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import get_event_bus_settings

        expected = get_event_bus_settings().dispatch_workers
        executor = BaldurEventBus._get_executor()
        assert executor._max_workers == expected

    def test_env_override_observable_after_reset(self, monkeypatch):
        """BALDUR_EVENT_BUS_DISPATCH_WORKERS=4 → executor _max_workers=4 after reset."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        # Construct an initial executor, then change the env var. Without
        # the reset cascade, the running executor would still hold the
        # original value because dispatch_workers is read once on first
        # _get_executor() call.
        BaldurEventBus._get_executor()
        monkeypatch.setenv("BALDUR_EVENT_BUS_DISPATCH_WORKERS", "4")
        reset_event_bus_settings()

        executor = BaldurEventBus._get_executor()
        assert executor._max_workers == 4

    def test_reset_event_bus_settings_drains_executor(self):
        """``reset_event_bus_settings()`` triggers the dispatch-executor drain."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        BaldurEventBus._get_executor()
        assert BaldurEventBus._executor is not None
        reset_event_bus_settings()
        assert BaldurEventBus._executor is None

    def test_reset_protect_caches_drains_executor(self):
        """``reset_protect_caches()`` also drains the EventBus executor (487 D3)."""
        from baldur.protect_facade import reset_protect_caches
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        BaldurEventBus._get_executor()
        assert BaldurEventBus._executor is not None
        reset_protect_caches()
        assert BaldurEventBus._executor is None
