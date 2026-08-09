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

import os
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


# =============================================================================
# Fork safety — the inherited pool is abandoned, never shut down (747 D13)
# =============================================================================


class TestDispatchExecutorForkBehavior:
    """747 D13: a ``ThreadPoolExecutor`` does not survive ``fork()``.

    The child inherits the worker ``Thread`` objects — all dead — together with
    the idle-worker semaphore permits they were counted against, so
    ``_adjust_thread_count`` consumes a permit, spawns nothing, and every
    submitted dispatch queues forever. The pid check runs at the point of use so
    it covers every fork shape (celery prefork, uWSGI, hookless gunicorn,
    ``multiprocessing``), and ``async_pool`` is the default dispatch mode on the
    memory backend too.

    No real ``fork()`` here: the pid stamp is mutated to the shape a child
    inherits, which is exactly what the guard reads.
    """

    # Bounds a hang only — no assertion depends on wall clock.
    _JOIN_TIMEOUT_SECONDS = 5.0

    def test_first_call_stamps_the_constructing_process(self):
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        BaldurEventBus._get_executor()

        assert BaldurEventBus._executor_pid == os.getpid()

    def test_matched_pid_returns_the_same_pool_and_keeps_the_lock(self):
        """The guard must be a fork guard, not a rebuild on every call."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        first = BaldurEventBus._get_executor()
        lock_before = BaldurEventBus._executor_lock

        second = BaldurEventBus._get_executor()

        assert second is first
        assert BaldurEventBus._executor_lock is lock_before

    def test_pid_mismatch_builds_a_new_pool_and_a_new_lock(self):
        """Both the pool and the lock are replaced — the lock is held across a
        pool construction plus a settings load, wide enough for a fork to inherit
        it owned by a thread that no longer exists."""
        # Given a pool this process built, then a simulated fork.
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        inherited_pool = BaldurEventBus._get_executor()
        inherited_lock = BaldurEventBus._executor_lock
        BaldurEventBus._executor_pid = os.getpid() + 1

        # When the first dispatch in the child asks for the executor.
        new_pool = BaldurEventBus._get_executor()

        # Then nothing inherited is reused.
        assert new_pool is not inherited_pool
        assert BaldurEventBus._executor_lock is not inherited_lock
        assert BaldurEventBus._executor_pid == os.getpid()

    def test_pid_mismatch_never_shuts_down_the_inherited_pool(self):
        """``shutdown()`` would acquire the inherited shutdown lock and enqueue a
        wake-up sentinel against dead threads. Dropping the reference is enough —
        the copied thread objects and work queue are plain memory."""
        # Given a pool whose shutdown is spied on.
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        inherited_pool = BaldurEventBus._get_executor()
        BaldurEventBus._executor_pid = os.getpid() + 1

        # When the child rebuilds.
        with patch.object(inherited_pool, "shutdown", autospec=True) as m_shutdown:
            BaldurEventBus._get_executor()

        # Then the parent's pool was abandoned untouched.
        m_shutdown.assert_not_called()

    def test_pid_mismatch_completes_while_the_inherited_lock_is_held(self):
        """The lock is replaced *before* any acquisition. An inherited lock can be
        owned by a thread that did not survive the fork; acquiring it would hang
        the first dispatch forever.
        """
        # Given an inherited lock held by a live thread that never releases it.
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        BaldurEventBus._get_executor()
        inherited_lock = BaldurEventBus._executor_lock
        BaldurEventBus._executor_pid = os.getpid() + 1

        acquired = threading.Event()
        release = threading.Event()

        def _hold_forever() -> None:
            with inherited_lock:
                acquired.set()
                release.wait(self._JOIN_TIMEOUT_SECONDS)

        holder = threading.Thread(target=_hold_forever, daemon=True)
        holder.start()
        assert acquired.wait(timeout=self._JOIN_TIMEOUT_SECONDS)

        try:
            # When the child asks for the executor.
            done = threading.Event()
            result: list[ThreadPoolExecutor] = []

            def _first_dispatch() -> None:
                result.append(BaldurEventBus._get_executor())
                done.set()

            caller = threading.Thread(target=_first_dispatch, daemon=True)
            caller.start()

            # Then it did not block on the orphaned lock.
            assert done.wait(timeout=self._JOIN_TIMEOUT_SECONDS)
            assert result[0] is BaldurEventBus._executor
            assert BaldurEventBus._executor_lock is not inherited_lock
        finally:
            release.set()
            holder.join(timeout=self._JOIN_TIMEOUT_SECONDS)

    def test_rebuilt_pool_actually_dispatches(self):
        """The point of abandoning the pool: submitted handlers run again."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        BaldurEventBus._get_executor()
        BaldurEventBus._executor_pid = os.getpid() + 1

        executor = BaldurEventBus._get_executor()
        future = executor.submit(lambda: "dispatched")

        assert future.result(timeout=self._JOIN_TIMEOUT_SECONDS) == "dispatched"

    def test_rebuilt_pool_replaces_the_registered_executor_slot(self):
        """The scrape-time gauges must observe the live pool, not the inherited
        one — the registry is keyed by name, so registration replaces the slot."""
        from baldur.metrics.recorders.executor import get_registered_executors
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        BaldurEventBus._get_executor()
        BaldurEventBus._executor_pid = os.getpid() + 1

        new_pool = BaldurEventBus._get_executor()

        assert get_registered_executors()["baldur-eventbus-dispatch"] is new_pool
