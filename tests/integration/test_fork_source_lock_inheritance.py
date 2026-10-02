"""Real-``fork()`` integration tests: a forked worker serves from its first call.

``fork()`` copies the parent's memory but keeps only the forking thread. A lock
another parent thread holds at that instant arrives held in the child with no
thread left to release it, a thread pool arrives with no live threads, and a
buffered stream a parent thread is writing arrives with its internal lock
held. A pre-fork server's master runs such threads by design (the leader
scheduler, the admin server), so each of these hangs a worker on its first
call. None of it is observable without a real fork, which is what this module
does.

Test Categories:
    A. Held locks: a parent thread blocked inside a runtime singleton's
       construction, inside a provider-registry factory, or holding a lock from
       the factories — the child's operation returns. A lock built without the
       factories stays held (the control that proves the setup reproduces the
       hazard).
    B. The measured chain (Redis): the circuit-breaker store's warm-up is
       blocked in the parent with its pool threads running; the child's first
       protected call returns and its pool runs what is submitted.
    C. Daemon-worker reporting across the fork: a parent-only worker is not
       reported by the child (the leader scheduler included, and it is never
       respawned there); a worker the child re-owns and then loses, and a
       parent-started worker the child never re-owns, are reported DEAD.
    D. Log streams: parent threads write through a stream handler and through a
       handler chained to it while the parent forks; every child logs its first
       record, and no fork waits long.
    E. Imports in progress: a parent thread is inside a module body when the
       parent forks. The fork waits for an import that finishes within its
       wait and gives up, bounded, on one that does not; a child that inherited
       such an import forks again without waiting; parent threads importing
       modules that create loggers, or importing under logging's own lock
       (``dictConfig``), neither stall the forks nor deadlock them.

Children report through a pipe (or their exit status) and always leave through
``os._exit`` so they never run pytest's exit handlers or flush the parent's
buffers. Each child arms its own ``SIGALRM`` so a hang fails the test instead
of hanging the run.

Infrastructure: POSIX ``fork()``; category B also needs Redis.
"""

from __future__ import annotations

import contextlib
import contextvars
import importlib
import json
import logging
import logging.config
import logging.handlers
import os
import signal
import sys
import threading
import time
import types
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from baldur.core import process_utils
from baldur.core.process_utils import fork_safe_lock, fork_safe_rlock

pytestmark = pytest.mark.skipif(
    not hasattr(os, "fork"), reason="fork() is POSIX-only (Windows dev host)"
)

# The Goal's bound on a child's first operation.
_CHILD_OPERATION_SECONDS = 5.0
# Backstop: a child still running after this is killed by its own SIGALRM.
_CHILD_KILL_SECONDS = 20
# How long the parent waits for its own helper threads to reach their hold.
_PARENT_SETUP_SECONDS = 10.0
# (D) forks while two parent threads write, the per-child bound on its first
# record, and the bound on each fork's own duration.
_LOG_STREAM_FORKS = 100
_CHILD_FIRST_RECORD_SECONDS = 3
_FORK_DURATION_SECONDS = 0.5
# (E) the bound on a child's import of a module its parent was importing; when
# a held parent import is let go after the fork call starts; the slack a fork
# past the import wait may take over the wait itself; forks while a parent
# thread imports in bursts (burst size, pause between bursts); forks while a
# parent thread re-runs ``dictConfig`` back to back, and the bound on each of
# those forks.
_CHILD_IMPORT_SECONDS = 3.0
_PARENT_IMPORT_RELEASE_SECONDS = 0.1
_FORK_PAST_THE_WAIT_SLACK_SECONDS = 0.5
_IMPORT_BURST_FORKS = 100
_IMPORT_BURST_SIZE = 5
_IMPORT_BURST_PAUSE_SECONDS = 0.01
_DICT_CONFIG_FORKS = 30
_DICT_CONFIG_FORK_SECONDS = 2.0


# =============================================================================
# Fork harness
# =============================================================================


def _arm_child_backstop(seconds: int) -> None:
    """Kill this (child) process after ``seconds`` unless it exits first.

    The disposition is reset first: a SIGALRM handler inherited from the test
    runner would turn the kill into an exception inside the code under test.
    """
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(seconds)


def _run_in_child(
    body: Callable[[], dict[str, Any] | None],
    *,
    kill_after: int = _CHILD_KILL_SECONDS,
    after_fork_in_parent: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Fork, run ``body()`` in the child, and return the child's report.

    The child writes one JSON object to a pipe: ``ok`` plus whatever ``body``
    returned, or the exception it raised. A child killed by its backstop
    writes nothing, which is reported as a failure. ``after_fork_in_parent``
    runs in the parent the moment ``fork()`` returns there.
    """
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs only in the forked child
        os.close(read_fd)
        _arm_child_backstop(kill_after)
        try:
            report: dict[str, Any] = {"ok": True, **(body() or {})}
        except BaseException as e:
            report = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        try:
            os.write(write_fd, json.dumps(report, default=str).encode())
        finally:
            os._exit(0)

    if after_fork_in_parent is not None:
        after_fork_in_parent()
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as reader:
        payload = reader.read()
    _, status = os.waitpid(pid, 0)
    if not payload:
        return {"ok": False, "error": f"child wrote no report (wait status {status})"}
    return json.loads(payload)


def _wait_until(predicate: Callable[[], bool], timeout: float) -> bool:
    """Poll ``predicate`` on a short interval; True once it holds."""
    deadline = time.monotonic() + timeout
    pause = threading.Event()
    while time.monotonic() < deadline:
        if predicate():
            return True
        pause.wait(0.01)
    return predicate()


class _ParentThreadHolding:
    """Run ``hold(entered, release)`` on a parent thread that will not survive
    the fork; ``hold`` sets ``entered`` once it holds what the test is about
    and returns once ``release`` is set.
    """

    def __init__(self, hold: Callable[[threading.Event, threading.Event], None]):
        self.entered = threading.Event()
        self.release = threading.Event()
        self._thread = threading.Thread(
            target=hold, args=(self.entered, self.release), daemon=True
        )

    def __enter__(self) -> _ParentThreadHolding:
        self._thread.start()
        assert self.entered.wait(_PARENT_SETUP_SECONDS), "parent thread never held"
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release.set()
        self._thread.join(timeout=_PARENT_SETUP_SECONDS)


def _hold_lock(lock) -> Callable[[threading.Event, threading.Event], None]:
    def hold(entered: threading.Event, release: threading.Event) -> None:
        with lock:
            entered.set()
            release.wait(timeout=60)

    return hold


# =============================================================================
# A. Held locks
# =============================================================================


class TestHeldLocksAreFreeInTheChild:
    """A parent thread inside a lock-held section at the fork instant does not
    block the child's use of the same lock.
    """

    def test_runtime_singleton_under_construction_is_built_by_the_child(self):
        """
        Purpose:
            The fork source's scheduler builds process-global singletons while
            holding the runtime lock; a child forked mid-build must build its own.
        Expected:
            - The child's ``get_singleton`` returns within the Goal's bound
            - It returns the child's own value (the parent's build never
              published into the slot the child sees)
        """
        from baldur.runtime import get_runtime

        runtime = get_runtime()
        slot = "fork_797_runtime_singleton"

        def hold(entered: threading.Event, release: threading.Event) -> None:
            def build_in_parent() -> str:
                entered.set()
                release.wait(timeout=60)
                return "built-by-parent"

            runtime.get_singleton(slot, build_in_parent)

        def child() -> dict[str, Any]:
            started = time.monotonic()
            value = runtime.get_singleton(slot, lambda: "built-by-child")
            return {"value": value, "seconds": time.monotonic() - started}

        try:
            with _ParentThreadHolding(hold):
                report = _run_in_child(child)
        finally:
            runtime.reset_singleton(slot)

        assert report["ok"], report
        assert report["value"] == "built-by-child"
        assert report["seconds"] < _CHILD_OPERATION_SECONDS

    def test_provider_registry_mid_construction_serves_the_child(self):
        """
        Purpose:
            A registry factory runs under the registry lock; the measured hang
            had the scheduler inside such a factory at the fork instant.
        Expected:
            - The child resolves another provider of the same registry within
              the Goal's bound
        """
        from baldur.factory.base import GenericProviderRegistry

        registry: GenericProviderRegistry[str] = GenericProviderRegistry(
            "fork_797_registry"
        )
        registry.register("child_side", lambda: "resolved-in-child")

        def hold(entered: threading.Event, release: threading.Event) -> None:
            def build_in_parent() -> str:
                entered.set()
                release.wait(timeout=60)
                return "built-by-parent"

            registry.register("parent_side", build_in_parent)
            registry.get("parent_side")

        def child() -> dict[str, Any]:
            started = time.monotonic()
            value = registry.get("child_side")
            return {"value": value, "seconds": time.monotonic() - started}

        with _ParentThreadHolding(hold):
            report = _run_in_child(child)

        assert report["ok"], report
        assert report["value"] == "resolved-in-child"
        assert report["seconds"] < _CHILD_OPERATION_SECONDS

    @pytest.mark.parametrize(
        "factory", [fork_safe_lock, fork_safe_rlock], ids=["lock", "rlock"]
    )
    def test_factory_lock_held_by_a_parent_thread_is_free_in_the_child(self, factory):
        """
        Purpose:
            Any lock from the factories, whoever holds it at the fork instant.
        Expected:
            - The child acquires it within the Goal's bound
        """
        lock = factory()

        def child() -> dict[str, Any]:
            acquired = lock.acquire(timeout=_CHILD_OPERATION_SECONDS)
            return {"acquired": acquired}

        with _ParentThreadHolding(_hold_lock(lock)):
            report = _run_in_child(child)

        assert report == {"ok": True, "acquired": True}

    def test_lock_built_without_the_factory_stays_held_in_the_child(self):
        """
        Purpose:
            Control: the same setup with a bare ``threading.Lock`` reproduces
            the hazard, so the cases above pass because of the repair and not
            because the setup never held anything at the fork instant.
        Expected:
            - The child's timed acquire fails
        """
        bare = threading.Lock()

        def child() -> dict[str, Any]:
            return {"acquired": bare.acquire(timeout=1.0)}

        with _ParentThreadHolding(_hold_lock(bare)):
            report = _run_in_child(child)

        assert report == {"ok": True, "acquired": False}


# =============================================================================
# B. The measured chain: circuit-breaker store warm-up blocked in the parent
# =============================================================================


def _test_redis_url() -> str:
    """The integration suite's Redis (``REDIS_URL`` first, as its fixtures do)."""
    return os.environ.get("REDIS_URL", "redis://localhost:16379/1")


@pytest.fixture
def layered_store_reset(monkeypatch):
    """A process with Redis configured and no circuit-breaker store built yet.

    Four warm-up workers instead of the default sixteen: the number does not
    change what a fork inherits, and it keeps the two processes' pools small.
    """
    from baldur.adapters.memory.layered_repository import (
        reset_layered_repository_executor,
    )
    from baldur.adapters.memory.layered_repository.base import LayeredRepositoryBase
    from baldur.adapters.redis.connection_factory import (
        reset_redis_connection_factory,
    )
    from baldur.adapters.resilient.backend import reset_storage_backend
    from baldur.factory.registry import ProviderRegistry
    from baldur.protect_facade import reset_protect_caches
    from baldur.services.circuit_breaker.convenience import (
        reset_circuit_breaker_service,
    )
    from baldur.settings.l2_storage import reset_l2_storage_settings
    from baldur.settings.redis import reset_redis_settings
    from baldur.settings.resilient_storage import reset_resilient_storage_settings

    def reset_all() -> None:
        reset_protect_caches()
        reset_circuit_breaker_service()
        ProviderRegistry.circuit_breaker_repo.clear_instances()
        # A plain thread reads the process-shared cache, not this test's
        # context: a store an earlier test built on a worker thread would be
        # handed back without a construction (and without a warm-up).
        contextvars.Context().run(ProviderRegistry.circuit_breaker_repo.clear_instances)
        LayeredRepositoryBase._reset_warmup_state()
        reset_layered_repository_executor()
        reset_storage_backend()
        reset_redis_connection_factory()
        reset_redis_settings()
        reset_resilient_storage_settings()
        reset_l2_storage_settings()

    monkeypatch.setenv("BALDUR_REDIS_URL", _test_redis_url())
    monkeypatch.setenv("BALDUR_L2_STORAGE_EXECUTOR_MAX_WORKERS", "4")
    reset_all()
    yield 4
    reset_all()


@pytest.mark.requires_redis
class TestBlockedStoreWarmupDoesNotBlockTheChild:
    """The measured hang, made deterministic.

    On the Celery quickstart the fork source's scheduler was inside the
    circuit-breaker store's construction — holding the provider-registry lock
    and the warm-up lock, with the warm-up's pool threads in flight — when the
    pool forked, and every child hung on its first protected call.
    """

    def test_first_protected_call_in_the_child_returns_and_its_pool_runs(
        self, monkeypatch, layered_store_reset
    ):
        """
        Purpose:
            Reproduce that instant: a parent thread builds the store and its
            warm-up workers are parked inside their Redis call when the parent
            forks. The workers park only in the parent — the child inherits the
            unset release event, so an unconditional park would block the
            child's own warm-up and fail the test for the test's reason.
        Expected:
            - The child's first ``@baldur.protected`` call returns its result
              within the Goal's bound
            - A task submitted to the store's pool in the child runs
        """
        # Given — the protected function exists before anything is held
        import baldur
        from baldur.adapters.memory.layered_repository.base import (
            LayeredRepositoryBase,
        )
        from baldur.adapters.redis import RedisCircuitBreakerStateRepository
        from baldur.factory.registry import ProviderRegistry

        warmup_workers = layered_store_reset
        parent_pid = os.getpid()
        parked: list[int] = []
        parked_lock = threading.Lock()
        original_slot = RedisCircuitBreakerStateRepository.try_acquire_half_open_slot

        def build_store(entered: threading.Event, release: threading.Event) -> None:
            entered.set()
            ProviderRegistry.get_circuit_breaker_repo(name="layered")

        # Leaving the ``with`` block below releases the parked workers too.
        builder = _ParentThreadHolding(build_store)

        def park_in_parent(self, *args: Any, **kwargs: Any) -> Any:
            if os.getpid() == parent_pid:
                with parked_lock:
                    parked.append(threading.get_ident())
                builder.release.wait(timeout=60)
            return original_slot(self, *args, **kwargs)

        monkeypatch.setattr(
            RedisCircuitBreakerStateRepository,
            "try_acquire_half_open_slot",
            park_in_parent,
        )

        @baldur.protected("fork_797_first_call")
        def charge() -> str:
            return "charged"

        def child() -> dict[str, Any]:
            started = time.monotonic()
            result = charge()
            call_seconds = time.monotonic() - started
            pool = LayeredRepositoryBase._get_executor()
            submitted = pool.submit(lambda: "ran").result(
                timeout=_CHILD_OPERATION_SECONDS
            )
            return {
                "result": result,
                "call_seconds": call_seconds,
                "submitted": submitted,
            }

        with builder:
            assert _wait_until(
                lambda: len(parked) == warmup_workers, _PARENT_SETUP_SECONDS
            ), f"warm-up never parked all workers ({len(parked)} parked)"

            # When
            report = _run_in_child(child)

        # Then
        assert report["ok"], report
        assert report["result"] == "charged"
        assert report["call_seconds"] < _CHILD_OPERATION_SECONDS
        assert report["submitted"] == "ran"


# =============================================================================
# C. Daemon-worker reporting across the fork
# =============================================================================


@pytest.fixture
def isolated_daemon_registry():
    """Only the handles a test registers are in the registry during the test."""
    from baldur.metrics.recorders import daemon_worker as registry_module

    with registry_module._registry_lock:
        saved = dict(registry_module._handle_registry)
        registry_module._handle_registry.clear()
    yield
    with registry_module._registry_lock:
        registry_module._handle_registry.clear()
        registry_module._handle_registry.update(saved)


def _live_parent_thread(stop: threading.Event) -> threading.Thread:
    thread = threading.Thread(target=stop.wait, args=(60,), daemon=True)
    thread.start()
    return thread


def _probe_statuses() -> dict[str, str]:
    """One probe tick in this process: worker name -> reported status."""
    from baldur.meta.health_probe import DaemonWorkerProbe

    result = DaemonWorkerProbe().probe()
    return {
        name: detail["status"] for name, detail in result.details["workers"].items()
    }


class _StubElector:
    """The smallest elector a ``LeaderScheduler`` runs against."""

    def __init__(self) -> None:
        self._on_become: list[Callable[[], None]] = []

    def on_become_leader(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._on_become.append(callback)
        return callback

    def on_lose_leader(self, callback: Callable[[], None]) -> Callable[[], None]:
        return callback

    def start(self) -> None:
        for callback in self._on_become:
            callback()

    def stop(self) -> None:
        return None

    def is_leader(self) -> bool:
        return True


@pytest.mark.usefixtures("isolated_daemon_registry")
class TestDaemonWorkerReportingAcrossTheFork:
    """What a child's watchdog reports about workers it inherited."""

    def test_parent_only_worker_is_not_reported_by_the_child(self):
        """
        Purpose:
            A worker flagged ``fork_source_only`` runs only in the parent; the
            child inherits its registry entry, never its thread.
        Expected:
            - The child's registry snapshot lacks it
            - The child's probe lists no worker at all, so nothing is DEAD
            - The child's scrape yields no sample for it
        """
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import (
            PROMETHEUS_AVAILABLE,
            _DaemonWorkerCollector,
            get_registered_daemon_workers,
            register_daemon_worker,
        )

        stop = threading.Event()
        register_daemon_worker(
            "ParentOnly",
            DaemonWorkerHandle(
                thread=_live_parent_thread(stop),
                tick_interval_seconds=1.0,
                fork_source_only=True,
            ),
        )

        def child() -> dict[str, Any]:
            scraped = None
            if PROMETHEUS_AVAILABLE:
                scraped = sorted(
                    {
                        sample.labels.get("name")
                        for family in _DaemonWorkerCollector().collect()
                        for sample in family.samples
                    }
                )
            return {
                "registered": sorted(get_registered_daemon_workers()),
                "probe": _probe_statuses(),
                "scraped": scraped,
            }

        try:
            parent_view = sorted(get_registered_daemon_workers())
            report = _run_in_child(child)
        finally:
            stop.set()

        assert parent_view == ["ParentOnly"]
        assert report["ok"], report
        assert report["registered"] == []
        assert report["probe"] == {}
        assert report["scraped"] in (None, [])

    def test_parent_started_worker_the_child_never_reowns_is_reported_dead(self):
        """
        Purpose:
            The true alarm: a component the child uses but whose thread the
            child never re-owns is dead there, and the probe must say so.
        Expected:
            - The child's probe reports it DEAD
        """
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        stop = threading.Event()
        register_daemon_worker(
            "ParentStarted",
            DaemonWorkerHandle(
                thread=_live_parent_thread(stop), tick_interval_seconds=1.0
            ),
        )

        try:
            report = _run_in_child(lambda: {"probe": _probe_statuses()})
        finally:
            stop.set()

        assert report["ok"], report
        assert report["probe"] == {"ParentStarted": "DEAD"}

    def test_reowned_outbox_writer_that_dies_in_the_child_is_reported_dead(self):
        """
        Purpose:
            The re-own shape: the child re-owns the DLQ outbox through its fork
            repair, rebinding the inherited handle to its own writer; when that
            writer dies the child's probe reports it, and the outbox coerces
            captures to the synchronous path.
        Expected:
            - The re-owned writer is alive in the child before it is stopped
            - The child's probe then reports ``DLQOutboxWorker`` DEAD
            - The outbox's worker-dead flag flips in the child
        """
        from baldur.services.dlq_outbox import outbox as outbox_module

        outbox_module.reset_dlq_outbox()
        with patch.object(outbox_module, "_default_sync_writer", lambda kwargs: None):
            outbox_module.setup_dlq_outbox()

        def child() -> dict[str, Any]:
            outbox_module.setup_dlq_outbox()  # the per-worker starter re-entry
            worker = outbox_module.get_outbox().worker
            writer = worker._thread
            reowned_alive = writer is not None and writer.is_alive()
            worker._stop_event.set()
            writer.join(timeout=_CHILD_OPERATION_SECONDS)
            statuses = _probe_statuses()
            flagged = _wait_until(outbox_module.is_worker_dead, 5.0)
            return {
                "reowned_alive": reowned_alive,
                "status": statuses.get(outbox_module._DLQ_OUTBOX_WORKER_NAME),
                "worker_dead": flagged,
            }

        try:
            report = _run_in_child(child)
        finally:
            outbox_module.reset_dlq_outbox()

        assert report == {
            "ok": True,
            "reowned_alive": True,
            "status": "DEAD",
            "worker_dead": True,
        }

    def test_leader_scheduler_is_neither_reported_nor_respawned_in_the_child(
        self, monkeypatch
    ):
        """
        Purpose:
            The leader scheduler stays in the fork source; with respawn
            enabled, a child that reported it DEAD would also start a second
            leader loop. A parent-started, unflagged worker is the control that
            proves respawn really is live in the child.
        Expected:
            - Over three probe ticks the child never lists the scheduler
            - No scheduler thread exists in the child
            - The control worker's restart callback did run in the child
        """
        # Given — a running leader scheduler and an unflagged control worker
        from baldur.coordination.scheduler import LeaderScheduler
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker
        from baldur.settings.daemon_worker import reset_daemon_worker_settings

        with patch(
            "baldur.coordination.scheduler.register_for_graceful_shutdown",
            return_value=None,
        ):
            scheduler = LeaderScheduler(
                resource_name="fork-797", elector=_StubElector()
            )
        scheduler_name = "Scheduler-fork-797"
        control_respawns: list[int] = []
        stop = threading.Event()
        register_daemon_worker(
            "RespawnControl",
            DaemonWorkerHandle(
                thread=_live_parent_thread(stop),
                tick_interval_seconds=1.0,
                restart_callback=lambda: control_respawns.append(os.getpid()),
            ),
        )
        monkeypatch.setenv("BALDUR_DAEMON_WORKER_RESPAWN_ENABLED", "true")
        reset_daemon_worker_settings()

        def child() -> dict[str, Any]:
            ticks = [_probe_statuses() for _ in range(3)]
            scheduler_threads = [
                t.name for t in threading.enumerate() if t.name == scheduler_name
            ]
            return {
                "ticks": ticks,
                "scheduler_threads": scheduler_threads,
                "control_respawns": len(control_respawns),
            }

        scheduler.start()
        try:
            assert _wait_until(
                lambda: (
                    scheduler._scheduler_thread is not None
                    and scheduler._scheduler_thread.is_alive()
                ),
                _PARENT_SETUP_SECONDS,
            )

            # When
            report = _run_in_child(child)
        finally:
            scheduler.stop()
            stop.set()
            reset_daemon_worker_settings()

        # Then
        assert report["ok"], report
        assert all(scheduler_name not in tick for tick in report["ticks"])
        assert report["scheduler_threads"] == []
        assert report["control_respawns"] >= 1


# =============================================================================
# D. Log streams written by parent threads at the fork instant
# =============================================================================


@contextlib.contextmanager
def _parent_threads_writing(log_path: Path) -> Iterator[logging.Logger]:
    """Two parent threads write continuously to one buffered log file.

    One writes through a ``StreamHandler`` directly, the other through a
    ``MemoryHandler(capacity=1)`` chained to it. Yields the direct logger, the
    one a child writes its own first record through.
    """
    with open(log_path, "w", encoding="utf-8") as stream:
        target = logging.StreamHandler(stream)
        chained = logging.handlers.MemoryHandler(
            capacity=1, target=target, flushLevel=logging.INFO
        )
        direct_logger = logging.getLogger("fork_797.direct")
        chained_logger = logging.getLogger("fork_797.chained")
        wiring = ((direct_logger, target), (chained_logger, chained))
        for logger, handler in wiring:
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False
        stop_writing = threading.Event()

        def write_until_stopped(logger: logging.Logger) -> None:
            while not stop_writing.is_set():
                logger.info("parent record written while the parent forks")

        writers = [
            threading.Thread(target=write_until_stopped, args=(logger,), daemon=True)
            for logger, _ in wiring
        ]
        for writer in writers:
            writer.start()
        try:
            yield direct_logger
        finally:
            stop_writing.set()
            for writer in writers:
                writer.join(timeout=5)
            for logger, handler in wiring:
                logger.removeHandler(handler)
            chained.close()
            target.close()


class TestLogStreamsAcrossTheFork:
    """No parent thread is inside a stream write when the child is created."""

    def test_children_log_their_first_record_while_parent_threads_write(self, tmp_path):
        """
        Purpose:
            A buffered stream's internal lock is not re-initialized in a fork
            child; a parent thread inside ``StreamHandler.emit`` at the fork
            instant left the child blocked on its first log line. The parent
            forks repeatedly while its threads write through a stream handler
            and through a handler chained to it.
        Expected:
            - Every child writes its first record through the same handler
              within the per-child bound
            - No fork takes longer than the per-fork bound (the chained-handler
              stall of holding every handler's lock does not occur)
        """
        fork_seconds: list[float] = []
        failed_children = 0

        with _parent_threads_writing(tmp_path / "stream.log") as direct_logger:
            for _ in range(_LOG_STREAM_FORKS):
                started = time.monotonic()
                pid = os.fork()
                if pid == 0:  # pragma: no cover - runs only in the forked child
                    _arm_child_backstop(_CHILD_FIRST_RECORD_SECONDS)
                    try:
                        direct_logger.info("child first record")
                    finally:
                        os._exit(0)
                fork_seconds.append(time.monotonic() - started)
                _, status = os.waitpid(pid, 0)
                if os.waitstatus_to_exitcode(status) != 0:
                    failed_children += 1

        assert failed_children == 0
        assert max(fork_seconds) < _FORK_DURATION_SECONDS, max(fork_seconds)


# =============================================================================
# E. Module imports in progress at the fork instant
# =============================================================================

# A module body a parent thread is held inside. It reaches its events through a
# gate module placed in ``sys.modules``, so it makes no import of its own.
_HELD_MODULE_BODY = """\
import sys

_gate = sys.modules[{gate!r}]
_gate.entered.set()
_gate.release.wait(timeout=60)
DONE = True
"""

# A module body that creates a logger, as nearly every module does; that takes
# logging's module lock, which the before-fork step's hold owns.
_LOGGER_MODULE_BODY = """\
import logging

_LOG = logging.getLogger(__name__)
"""

# A handler class that ``dictConfig`` imports by name.
_DICT_CONFIG_HANDLER_BODY = """\
import logging


class Handler(logging.NullHandler):
    pass
"""


@pytest.fixture
def module_dir(tmp_path, monkeypatch) -> Path:
    """A directory on ``sys.path`` for the module files a test writes."""
    monkeypatch.syspath_prepend(str(tmp_path))
    return tmp_path


def _write_module(directory: Path, name: str, body: str) -> None:
    (directory / f"{name}.py").write_text(body, encoding="utf-8")
    importlib.invalidate_caches()


class _ParentImportHeld:
    """A parent thread held inside the body of a fresh module it is importing.

    While the body waits, the thread owns the module's import lock — the window
    a fork must not land in. ``completed()`` lets the body finish; leaving the
    ``with`` block does too, joins the thread and removes the module and its
    gate from ``sys.modules``.
    """

    def __init__(self, directory: Path, purpose: str) -> None:
        self.name = f"x800_{purpose}_{uuid.uuid4().hex[:12]}"
        self._gate = types.ModuleType(f"{self.name}_gate")
        self._gate.entered = threading.Event()
        self._gate.release = threading.Event()
        _write_module(
            directory, self.name, _HELD_MODULE_BODY.format(gate=self._gate.__name__)
        )
        self._thread = threading.Thread(
            target=importlib.import_module, args=(self.name,), daemon=True
        )

    @property
    def released(self) -> bool:
        return self._gate.release.is_set()

    def release(self) -> None:
        self._gate.release.set()

    def completed(self) -> bool:
        """Let the body finish; True once the import ran it to its end."""
        self.release()
        self._thread.join(timeout=_PARENT_SETUP_SECONDS)
        return getattr(sys.modules.get(self.name), "DONE", False) is True

    def __enter__(self) -> _ParentImportHeld:
        sys.modules[self._gate.__name__] = self._gate
        self._thread.start()
        assert self._gate.entered.wait(_PARENT_SETUP_SECONDS), (
            "the parent import never reached its module body"
        )
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
        self._thread.join(timeout=_PARENT_SETUP_SECONDS)
        sys.modules.pop(self.name, None)
        sys.modules.pop(self._gate.__name__, None)


class _ParentThreadImportingBursts:
    """A parent thread that imports fresh modules in bursts until stopped.

    Each burst writes ``_IMPORT_BURST_SIZE`` module files whose bodies create a
    logger, publishes their names as ``current``, imports them back to back,
    then pauses. A child forked at any instant can import the burst its parent
    was on: a module the parent finished is already loaded, one it had not
    started loads from its file, and one it was inside at the fork — its
    import lock inherited held — would block the child for good.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._prefix = f"x800_burst_{uuid.uuid4().hex[:8]}"
        self.current: list[str] = []
        self.imported: list[str] = []
        self.errors: list[BaseException] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._import_bursts, daemon=True)

    def _import_bursts(self) -> None:
        burst = 0
        try:
            while not self._stop.is_set():
                names = [
                    f"{self._prefix}_{burst}_{i}" for i in range(_IMPORT_BURST_SIZE)
                ]
                for name in names:
                    _write_module(self._directory, name, _LOGGER_MODULE_BODY)
                self.current = names
                for name in names:
                    importlib.import_module(name)
                    self.imported.append(name)
                burst += 1
                self._stop.wait(_IMPORT_BURST_PAUSE_SECONDS)
        except BaseException as e:  # pragma: no cover - asserted by the test
            self.errors.append(e)

    def __enter__(self) -> _ParentThreadImportingBursts:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join(timeout=_PARENT_SETUP_SECONDS)
        for name in [*self.imported, *self.current]:
            sys.modules.pop(name, None)
            logging.Logger.manager.loggerDict.pop(name, None)


class _ParentThreadReconfiguringLogging:
    """A parent thread that re-runs ``dictConfig`` back to back until stopped,
    importing its handler class afresh on every run.

    ``dictConfig`` holds logging's module lock while it resolves a class name,
    so each run imports under the lock the before-fork step's hold takes; with
    no pause between runs, a fork almost always meets a run in progress. It
    also closes and forgets every handler registered before it, so the runs
    happen against a private handler registry, and the ``disabled`` flags it
    rewrites on existing loggers are put back afterwards.
    """

    def __init__(self, directory: Path) -> None:
        self._module = f"x800_dictconfig_{uuid.uuid4().hex[:12]}"
        _write_module(directory, self._module, _DICT_CONFIG_HANDLER_BODY)
        self._logger_name = f"{self._module}.configured"
        self._config = {
            "version": 1,
            "disable_existing_loggers": False,
            "handlers": {"reimported": {"class": f"{self._module}.Handler"}},
            "loggers": {
                self._logger_name: {"handlers": ["reimported"], "propagate": False}
            },
        }
        self.runs = 0
        self.errors: list[BaseException] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._reconfigure, daemon=True)

    def _reconfigure(self) -> None:
        try:
            while not self._stop.is_set():
                sys.modules.pop(self._module, None)
                logging.config.dictConfig(self._config)
                self.runs += 1
        except BaseException as e:  # pragma: no cover - asserted by the test
            self.errors.append(e)

    def __enter__(self) -> _ParentThreadReconfiguringLogging:
        manager = logging.Logger.manager
        self._disabled_before = {
            name: logger.disabled
            for name, logger in list(manager.loggerDict.items())
            if isinstance(logger, logging.Logger)
        }
        self._registry_before = (logging._handlerList, logging._handlers)
        logging._handlerList, logging._handlers = [], {}
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join(timeout=_PARENT_SETUP_SECONDS)
        configured = logging.getLogger(self._logger_name)
        for handler in list(configured.handlers):
            configured.removeHandler(handler)
            handler.close()
        logging._handlerList, logging._handlers = self._registry_before
        manager = logging.Logger.manager
        for name, disabled in self._disabled_before.items():
            logger = manager.loggerDict.get(name)
            if isinstance(logger, logging.Logger):
                logger.disabled = disabled
        manager.loggerDict.pop(self._logger_name, None)
        sys.modules.pop(self._module, None)


class TestImportsAcrossTheFork:
    """No child is born holding a module import lock a parent thread owned.

    Python guards each module under import with a lock the importing thread
    owns until the body has run; a fork inside that window hands the child the
    lock with no thread left to release it, and the child's import of that
    module blocks forever. The fork waits, bounded, for such imports.
    """

    def test_fork_waits_for_a_parent_import_and_the_child_imports_the_module(
        self, module_dir
    ):
        """
        Purpose:
            A parent thread is inside a module body when the parent forks, and
            finishes it shortly after the fork call starts.
        Expected:
            - The fork returns only after the import was let go, and before the
              wait's budget ran out
            - The child imports the module, complete, within its bound
        """
        # Given
        budget = process_utils._FORK_IMPORT_WAIT_SECONDS
        observed: dict[str, Any] = {}
        held = _ParentImportHeld(module_dir, "waited_for")

        def child() -> dict[str, Any]:
            started = time.monotonic()
            module = importlib.import_module(held.name)
            return {
                "done": getattr(module, "DONE", False),
                "seconds": time.monotonic() - started,
            }

        with held:
            let_go = threading.Timer(_PARENT_IMPORT_RELEASE_SECONDS, held.release)
            started = time.monotonic()

            def fork_returned() -> None:
                observed["seconds"] = time.monotonic() - started
                observed["released"] = held.released

            let_go.start()
            try:
                # When
                report = _run_in_child(child, after_fork_in_parent=fork_returned)
            finally:
                let_go.cancel()
                let_go.join(timeout=_PARENT_SETUP_SECONDS)

        # Then
        assert observed["released"] is True
        assert observed["seconds"] < budget
        assert report["ok"], report
        assert report["done"] is True
        assert report["seconds"] < _CHILD_IMPORT_SECONDS

    def test_fork_past_a_parent_import_that_outlives_the_wait_is_bounded(
        self, module_dir
    ):
        """
        Purpose:
            A parent import that outlives the wait (network in a module body)
            must not turn into a stuck fork; the child inherits that import's
            lock held, and its own forks must not wait for it.
        Expected:
            - The fork returns after the wait's budget and within its slack
            - The parent's import then completes
            - The child's own fork returns within the per-fork bound
        """
        # Given
        budget = process_utils._FORK_IMPORT_WAIT_SECONDS
        observed: dict[str, float] = {}

        def child() -> dict[str, Any]:
            started = time.monotonic()
            pid = os.fork()
            if pid == 0:  # pragma: no cover - runs only in the grandchild
                os._exit(0)
            seconds = time.monotonic() - started
            os.waitpid(pid, 0)
            return {"fork_seconds": seconds}

        with _ParentImportHeld(module_dir, "outlives_wait") as held:
            started = time.monotonic()

            # When
            report = _run_in_child(
                child,
                after_fork_in_parent=lambda: observed.setdefault(
                    "seconds", time.monotonic() - started
                ),
            )
            completed = held.completed()

        # Then
        assert (
            budget <= observed["seconds"] < budget + _FORK_PAST_THE_WAIT_SLACK_SECONDS
        )
        assert completed
        assert report["ok"], report
        assert report["fork_seconds"] < _FORK_DURATION_SECONDS

    def test_forks_while_a_parent_thread_imports_logging_modules_stall_no_one(
        self, module_dir
    ):
        """
        Purpose:
            A parent thread imports bursts of modules that create loggers while
            another writes through a stream handler, and the parent forks
            repeatedly. The hold owns logging's module lock, which each such
            body needs: waiting before the hold lets the body finish, and
            the re-check after it catches one that started meanwhile.
        Expected:
            - Every child imports the burst its parent was on and logs its
              first record within the per-child bound
            - No fork takes longer than the per-fork bound
        """
        # Given
        fork_seconds: list[float] = []
        failed_children = 0

        with (
            _parent_threads_writing(module_dir / "stream.log") as direct_logger,
            _ParentThreadImportingBursts(module_dir) as importer,
        ):
            # When
            for _ in range(_IMPORT_BURST_FORKS):
                started = time.monotonic()
                pid = os.fork()
                if pid == 0:  # pragma: no cover - runs only in the forked child
                    _arm_child_backstop(_CHILD_FIRST_RECORD_SECONDS)
                    try:
                        for name in importer.current:
                            importlib.import_module(name)
                        direct_logger.info("child first record")
                    except BaseException:
                        os._exit(1)
                    os._exit(0)
                fork_seconds.append(time.monotonic() - started)
                _, status = os.waitpid(pid, 0)
                if os.waitstatus_to_exitcode(status) != 0:
                    failed_children += 1
            imported = len(importer.imported)

        # Then — the bursts really ran while the parent forked
        assert importer.errors == []
        assert imported >= _IMPORT_BURST_SIZE
        assert failed_children == 0
        assert max(fork_seconds) < _FORK_DURATION_SECONDS, max(fork_seconds)

    def test_forks_while_a_parent_thread_imports_under_loggings_lock_never_deadlock(
        self, module_dir
    ):
        """
        Purpose:
            ``dictConfig`` imports the handler classes it names while it holds
            logging's module lock. A fork that held the interpreter's import
            lock and then waited for logging's lock would deadlock against it;
            the before-fork step never takes the import lock.
        Expected:
            - Every fork returns within the per-fork bound
        """
        # Given
        fork_seconds: list[float] = []

        with _ParentThreadReconfiguringLogging(module_dir) as reconfigurer:
            # When
            for _ in range(_DICT_CONFIG_FORKS):
                started = time.monotonic()
                pid = os.fork()
                if pid == 0:  # pragma: no cover - runs only in the forked child
                    os._exit(0)
                fork_seconds.append(time.monotonic() - started)
                os.waitpid(pid, 0)
            runs = reconfigurer.runs

        # Then — the reconfiguration really ran while the parent forked
        assert reconfigurer.errors == []
        assert runs > 0
        assert max(fork_seconds) < _DICT_CONFIG_FORK_SECONDS, max(fork_seconds)
