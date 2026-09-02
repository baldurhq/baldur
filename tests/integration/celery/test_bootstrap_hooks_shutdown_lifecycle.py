"""Mock-based integration tests for the Celery worker exit lifecycle.

The stop-side receivers are not delegation either. ``_on_worker_shutdown``
composes the real ``GracefulShutdownCoordinator``, the real DLQ outbox teardown
and the real audit flush over **shared process-global state** — the
coordinator's phase, the teardown's once-gate and cached result, the audit
once-flag — and the arithmetic that ties them together only matters when a real
drain does not converge:

- the drain wait subtracts the outbox teardown's own budget, so step 3 still
  runs when step 2 spends its whole window. With a coordinator that never
  drains, that subtraction is the difference between a teardown and a
  ``terminationGracePeriodSeconds`` kill;
- the outbox teardown and the audit flush share one process lifecycle, and the
  outbox's final writes have to land before the WAL closes;
- the pool child's receiver reaches the same idempotent teardown while
  deliberately never touching the coordinator, whose handler list it merely
  inherited from a parent it does not own.

Test Categories:
    A. worker_shutdown (the worker main process):
        - a non-converging drain still leaves the teardown and the flush time
        - the teardown's writes precede the WAL close
        - the coordinator's own drain and the receiver's unconditional steps
          resolve to one teardown, not two
    B. worker_process_shutdown (a pool child, including a recycle):
        - the same teardown runs with no coordinator involvement at all
        - a child's teardown does not initiate a shutdown the parent owns

No infrastructure: the five audit teardown stages and the DLQ store are
stubbed, so no WAL, checkpoint or disk buffer is touched. The coordinator, its
handlers, the outbox and its drainer thread are real.
"""

from __future__ import annotations

import os
from contextlib import ExitStack
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.celery.bootstrap_hooks import (
    _on_worker_process_shutdown,
    _on_worker_shutdown,
)
from baldur.core.shutdown_coordinator import ShutdownHandler, TrackedRequest

_STAGES = (
    "_shutdown_async_logger",
    "_shutdown_sync_worker",
    "_shutdown_wal",
    "_save_final_checkpoint",
    "_shutdown_disk_buffer",
)

# Waits are on threading primitives; these bound the failure mode, a hang.
_HANDOFF_TIMEOUT_SECONDS = 5.0
# Short enough to keep the suite fast, long enough that the reserve
# subtraction below leaves a positive, observable drain wait.
_TEST_DRAIN_TIMEOUT_SECONDS = 1.5
# Below the teardown's two internal floors, so the budget is the floors.
_TEST_OUTBOX_BUDGET_SECONDS = 0.1


class _NeverDrainsHandler(ShutdownHandler):
    """A subsystem whose in-flight work never finishes.

    The reserve arithmetic is invisible against a drain that converges
    immediately — the wait returns before its timeout matters. This handler is
    what makes the subtraction observable.
    """

    def on_shutdown_start(self) -> None: ...

    def is_drain_complete(self) -> bool:
        return False

    def on_drain_complete(self) -> None: ...

    def on_force_shutdown(self, pending_requests: list[TrackedRequest]) -> None: ...


def _wait_until(predicate, timeout: float = _HANDOFF_TIMEOUT_SECONDS) -> bool:
    """Poll a state transition the drainer thread performs."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture(autouse=True)
def _reset_dlq_outbox_module_state():
    """Undo the process-global teardown state the receivers leave behind.

    The teardown sets the producer-coercion flag and caches its terminal result
    so repeat callers are no-ops. Left behind, every later test in this worker
    dispatches DLQ captures synchronously and any teardown they run returns
    this file's cached counts.
    """
    from baldur.services.dlq_outbox import outbox as outbox_module

    def _clear() -> None:
        if outbox_module._outbox is not None:
            try:
                outbox_module._outbox.stop(timeout=1.0)
            except Exception:
                pass
            outbox_module._outbox = None
        outbox_module._outbox_origin_pid = None
        outbox_module._worker_dead = False
        outbox_module._worker_dead_coercions = 0
        outbox_module._shutdown_result = None

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _real_flush_with_stubbed_stages(monkeypatch):
    """Run the real audit flush body against stubbed stages, from a clean flag."""
    from baldur.audit.async_audit_lifecycle import _reset_audit_shutdown_state

    monkeypatch.setenv("BALDUR_TEST_MODE", "false")
    _reset_audit_shutdown_state()
    with ExitStack() as stack:
        stubs = {
            stage: stack.enter_context(
                patch(f"baldur.audit.async_audit_lifecycle.{stage}")
            )
            for stage in _STAGES
        }
        yield stubs
    _reset_audit_shutdown_state()


@pytest.fixture(autouse=True)
def _fresh_coordinator():
    from baldur.core.shutdown_coordinator import reset_shutdown_coordinator

    reset_shutdown_coordinator()
    yield
    reset_shutdown_coordinator()


@pytest.fixture(autouse=True)
def _short_drain_timeout():
    """A real settings object with one field overridden.

    The coordinator reads the same object for its own drain deadline, so a
    stand-in would have to carry every field it touches. The production floor
    exists for operators; what this suite needs is a window a test can outlast.
    """
    from baldur.settings.recovery_shutdown import RecoveryShutdownSettings

    short = RecoveryShutdownSettings().model_copy(
        update={"default_drain_timeout_seconds": _TEST_DRAIN_TIMEOUT_SECONDS}
    )
    with patch(
        "baldur.settings.recovery_shutdown.get_recovery_shutdown_settings",
        return_value=short,
    ):
        yield


@pytest.fixture
def stalled_coordinator():
    """A real coordinator whose drain cannot converge, carrying both handlers."""
    from baldur.audit.shutdown_handler import AuditShutdownHandler
    from baldur.core.shutdown_coordinator import (
        RequestTracker,
        get_shutdown_coordinator,
    )
    from baldur.services.dlq_outbox.shutdown import DLQOutboxShutdownHandler

    coordinator = get_shutdown_coordinator(request_tracker=RequestTracker())
    coordinator.register_handler(_NeverDrainsHandler())
    coordinator.register_handler(DLQOutboxShutdownHandler())
    coordinator.register_handler(AuditShutdownHandler())
    return coordinator


@pytest.fixture
def wedged_outbox():
    """A started outbox whose store never returns, published as the singleton.

    The wedged writer is what makes the teardown observable: one entry is stuck
    in flight, the other two stay on the ring, and the emergency dump is the
    only thing that can rescue any of them. Yields ``(outbox, dumped, order)``.
    """
    import threading

    from baldur.audit.ring_buffer import RingBuffer
    from baldur.services.dlq_outbox import outbox as outbox_module
    from baldur.services.dlq_outbox.outbox import Outbox
    from baldur.services.dlq_outbox.worker import DLQOutboxWorker
    from baldur.settings.backpressure import BackpressureStrategy
    from baldur.settings.dlq_outbox import DLQOutboxSettings

    forever = threading.Event()
    dumped: list[dict] = []
    order: list[str] = []

    def hanging_writer(kwargs):
        forever.wait(timeout=_HANDOFF_TIMEOUT_SECONDS * 2)

    def recording_dump(batch, deadline=None):
        order.append("outbox_dump")
        dumped.extend(batch)
        return len(batch)

    buffer: RingBuffer = RingBuffer(
        capacity=100, strategy=BackpressureStrategy.DROP_OLDEST
    )
    worker = DLQOutboxWorker(
        buffer=buffer,
        sync_writer=hanging_writer,
        batch_size=1,
        flush_interval_seconds=0.01,
        on_emergency_dump=recording_dump,
    )
    outbox = Outbox(buffer=buffer, worker=worker)

    short = DLQOutboxSettings(join_timeout_seconds=_TEST_OUTBOX_BUDGET_SECONDS)
    with patch(
        "baldur.settings.dlq_outbox.get_dlq_outbox_settings", return_value=short
    ):
        outbox.start()
        outbox_module._outbox = outbox
        for i in range(3):
            outbox.put({"domain": "payment", "failure_type": f"PG_TIMEOUT_{i}"})
        assert _wait_until(lambda: worker.in_flight == 1), (
            "the drainer never picked up the first entry"
        )
        try:
            yield outbox, dumped, order
        finally:
            forever.set()


class TestCeleryWorkerMainShutdownLifecycleIntegration:
    """``worker_shutdown`` against a real coordinator and a real outbox."""

    def test_a_drain_that_never_converges_still_leaves_the_teardown_time(
        self, stalled_coordinator, wedged_outbox
    ):
        """The reserve, doing the job it exists for.

        Step 2 waits on other subsystems and step 3 is queued behind it. Unlike
        gunicorn there is no in-process watcher here — billiard joins its
        children with no timeout — so an unreserved wait would make the outbox
        teardown the first thing the platform's stop timeout cuts.
        """
        _, dumped, _ = wedged_outbox

        _on_worker_shutdown()

        assert len(dumped) == 3
        assert {e["failure_type"] for e in dumped} == {
            "PG_TIMEOUT_0",
            "PG_TIMEOUT_1",
            "PG_TIMEOUT_2",
        }

    def test_a_drain_that_never_converges_is_reported_as_incomplete(
        self, stalled_coordinator, wedged_outbox
    ):
        """The drain that needed its last few seconds says so in its own line,
        which is what makes the subtraction an accountable trade."""
        with capture_logs() as cap_logs:
            _on_worker_shutdown()

        incomplete = [
            e for e in cap_logs if e.get("event") == "shutdown.worker_drain_incomplete"
        ]
        assert len(incomplete) == 1
        assert incomplete[0]["log_level"] == "warning"
        assert not [e for e in cap_logs if e.get("event") == "shutdown.worker_drained"]

    def test_the_outbox_writes_land_before_the_wal_is_closed(
        self, stalled_coordinator, wedged_outbox, _real_flush_with_stubbed_stages
    ):
        """The transaction boundary across two subsystems that share only a
        process lifecycle."""
        _, _, order = wedged_outbox
        _real_flush_with_stubbed_stages["_shutdown_wal"].side_effect = lambda *a, **kw: (
            order.append("wal_close")
        )

        _on_worker_shutdown()

        assert order == ["outbox_dump", "wal_close"]

    def test_the_receiver_runs_the_teardown_exactly_once_with_the_drain(
        self, stalled_coordinator, wedged_outbox
    ):
        """Steps 3 and 4 are unconditional, so on a converging drain the
        coordinator's handlers reach them first and the receiver's own calls
        are no-ops. The gate is what makes "unconditional" safe."""
        _, dumped, _ = wedged_outbox

        _on_worker_shutdown()
        _on_worker_shutdown()

        assert len(dumped) == 3

    def test_the_audit_flush_runs_exactly_once_across_the_drain_and_the_receiver(
        self, stalled_coordinator, wedged_outbox, _real_flush_with_stubbed_stages
    ):
        """Serialization must not become duplication: the coordinator's audit
        handler and the receiver's unconditional flush share the once-flag."""
        _on_worker_shutdown()

        for stage, stub in _real_flush_with_stubbed_stages.items():
            assert stub.call_count == 1, f"{stage} ran {stub.call_count} times"

    def test_the_terminal_marker_reports_what_happened_to_the_entries(
        self, stalled_coordinator, wedged_outbox
    ):
        """One line answers both "did the pipeline run" and "where did the
        buffered DLQ entries go"."""
        with capture_logs() as cap_logs:
            _on_worker_shutdown()

        markers = [
            e for e in cap_logs if e.get("event") == "shutdown.worker_exit_completed"
        ]
        assert len(markers) == 1
        assert markers[0]["process_role"] == "celery_worker_main"
        assert markers[0]["worker_id"] == os.getpid()
        # Three pending (one in flight, two on the ring), all rescued by the
        # dump, none lost.
        assert markers[0]["outbox_pending_at_entry"] == 3
        assert markers[0]["outbox_emergency_dumped"] == 3
        assert markers[0]["outbox_residual"] == 0


class TestCeleryPoolChildShutdownLifecycleIntegration:
    """``worker_process_shutdown`` — the child's only exit pipeline."""

    def test_the_child_drains_its_outbox_without_a_coordinator(
        self, stalled_coordinator, wedged_outbox
    ):
        """A ``maxtasksperchild`` recycle is routine operation: it must pay for
        no drain, and still lose no entries."""
        from baldur.core.shutdown_coordinator import ShutdownPhase

        _, dumped, _ = wedged_outbox

        _on_worker_process_shutdown()

        assert len(dumped) == 3
        assert stalled_coordinator.phase == ShutdownPhase.RUNNING

    def test_the_childs_writes_land_before_the_wal_is_closed(
        self, stalled_coordinator, wedged_outbox, _real_flush_with_stubbed_stages
    ):
        _, _, order = wedged_outbox
        _real_flush_with_stubbed_stages["_shutdown_wal"].side_effect = lambda *a, **kw: (
            order.append("wal_close")
        )

        _on_worker_process_shutdown()

        assert order == ["outbox_dump", "wal_close"]

    def test_a_child_teardown_leaves_the_parents_handlers_untouched(
        self, stalled_coordinator, wedged_outbox, _real_flush_with_stubbed_stages
    ):
        """The child inherited the parent's handler list and does not own that
        state; firing it would run leader-election release, exporter teardown
        and the private service handlers in the wrong process."""
        from baldur.core.shutdown_coordinator import ShutdownPhase

        _on_worker_process_shutdown()

        assert stalled_coordinator.phase == ShutdownPhase.RUNNING

    def test_the_childs_teardown_and_a_later_main_exit_resolve_to_one_drain(
        self, stalled_coordinator, wedged_outbox
    ):
        """Both receivers reach the same idempotent teardown, so a process that
        somehow ran both reports one drain rather than two."""
        _, dumped, _ = wedged_outbox

        _on_worker_process_shutdown()
        _on_worker_shutdown()

        assert len(dumped) == 3

    def test_the_childs_terminal_marker_names_the_pool_child_role(
        self, stalled_coordinator, wedged_outbox
    ):
        with capture_logs() as cap_logs:
            _on_worker_process_shutdown()

        markers = [
            e for e in cap_logs if e.get("event") == "shutdown.worker_exit_completed"
        ]
        assert len(markers) == 1
        assert markers[0]["process_role"] == "celery_pool_child"
        assert markers[0]["outbox_emergency_dumped"] == 3
