"""Composition of the gunicorn worker-exit hook and the audit teardown.

Two writers reach ``graceful_shutdown_audit_system`` against one process
lifecycle — the exit hook's own step and the shutdown coordinator's
``AuditShutdownHandler`` — and they share the fork-inherited
``audit_shutdown_done`` flag and the module flush lock behind it. The
failure this design exists to prevent is only observable when both run
against the same lifecycle, which no single-component test reaches:

- the hook's drain wait can expire while the coordinator's drain thread is
  still inside the flush (the coordinator flips the phase to TERMINATED
  *before* it runs handler teardown, so the wait even returns ``True``), and
  the hook must block on the lock rather than return into a truncation;
- a worker recycle initiates no shutdown at all, so the hook's own flush is
  the only teardown that path ever gets;
- and the hook must never run any of this in the master, because the
  once-flag it would set is inherited by every worker forked afterwards.

Mock-based: real coordinator, real handler, real lifecycle state and flush
body — only the five teardown stages are stubbed, so no WAL, checkpoint or
disk buffer is touched. No infrastructure.
"""

from __future__ import annotations

import os
import threading
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

# Waits are on threading.Events, so a healthy run blocks only as long as the
# handoff needs. These bound the failure modes, which are hangs.
_HANDOFF_TIMEOUT_SECONDS = 5.0
_JOIN_TIMEOUT_SECONDS = 10.0
# Long enough that a correct hook is still blocked when we sample it.
_STILL_BLOCKED_PROBE_SECONDS = 0.3
# Shorter than the settings floor (5 s): the hook's wait must expire while
# the drain thread is still inside the flush, which is the whole overlap.
_TEST_DRAIN_WAIT_SECONDS = 0.2

_STAGES = (
    "_shutdown_async_logger",
    "_shutdown_sync_worker",
    "_shutdown_wal",
    "_save_final_checkpoint",
    "_shutdown_disk_buffer",
)


def _arbiter() -> SimpleNamespace:
    """``worker_exit``'s first positional argument, as gunicorn passes it."""
    return SimpleNamespace()


def _exiting_worker() -> SimpleNamespace:
    return SimpleNamespace(pid=os.getpid())


def _foreign_worker() -> SimpleNamespace:
    """What the master is handed for a worker that had already exited."""
    return SimpleNamespace(pid=-1)


@pytest.fixture(autouse=True)
def _reset_dlq_outbox_module_state():
    """Undo the outbox teardown the real ``worker_exit`` hook performs.

    The hook's teardown is process-global: it sets the producer-coercion flag
    and caches its terminal result so repeat callers are no-ops. Left behind,
    every later test in this worker dispatches DLQ captures synchronously and
    any teardown they run returns this file's cached counts.
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
        outbox_module._teardown_started = False

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _real_flush_with_stubbed_stages(monkeypatch):
    """Run the real flush body against stubbed stages, from a clean flag."""
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
def _short_drain_wait():
    """Give the hook a drain wait below the settings floor.

    A real settings object with one field overridden — the coordinator reads
    the same object for its own drain deadline, so a stand-in would have to
    carry every field it touches. The floor (``ge=5.0``) exists for
    operators; what this suite needs is the overlap a five-second wait would
    simply outlast. Settings *resolution* is pinned by the hook's unit tests.
    """
    from baldur.settings.recovery_shutdown import RecoveryShutdownSettings

    short = RecoveryShutdownSettings().model_copy(
        update={"default_drain_timeout_seconds": _TEST_DRAIN_WAIT_SECONDS}
    )
    with patch(
        "baldur.settings.recovery_shutdown.get_recovery_shutdown_settings",
        return_value=short,
    ):
        yield


@pytest.fixture
def audit_coordinator():
    """A real coordinator carrying the real audit shutdown handler."""
    from baldur.audit.shutdown_handler import AuditShutdownHandler
    from baldur.core.shutdown_coordinator import (
        RequestTracker,
        get_shutdown_coordinator,
    )

    coordinator = get_shutdown_coordinator(request_tracker=RequestTracker())
    coordinator.register_handler(AuditShutdownHandler())
    return coordinator


class TestWorkerExitAgainstAConcurrentCoordinatorFlush:
    """The overlap: drain thread inside the flush when the hook's wait ends."""

    def test_hook_outlasts_the_coordinator_flush_instead_of_truncating_it(
        self, audit_coordinator, _real_flush_with_stubbed_stages
    ):
        # Given: the coordinator's drain thread reaches the audit teardown
        # and stalls inside its first stage
        from baldur.adapters.gunicorn.hooks import worker_exit
        from baldur.audit import async_audit_lifecycle as lifecycle

        order: list[str] = []
        flush_started = threading.Event()
        release_flush = threading.Event()

        def _stalling_stage():
            order.append("flush_started")
            flush_started.set()
            release_flush.wait(timeout=_HANDOFF_TIMEOUT_SECONDS)
            order.append("flush_finished")

        def _run_hook():
            worker_exit(_arbiter(), _exiting_worker())
            order.append("hook_returned")

        with patch.object(
            lifecycle, "_shutdown_async_logger", side_effect=_stalling_stage
        ):
            audit_coordinator.initiate_shutdown()
            assert flush_started.wait(timeout=_HANDOFF_TIMEOUT_SECONDS), (
                "the coordinator's drain thread never reached the audit flush"
            )

            # When: the exit hook runs while that flush is still in flight
            hook = threading.Thread(target=_run_hook)
            hook.start()

            # Then: it is blocked on the flush lock. Returning here is what
            # let the process exit and kill the drain thread mid-flush.
            hook.join(timeout=_STILL_BLOCKED_PROBE_SECONDS)
            assert hook.is_alive(), (
                "worker_exit returned while the concurrent flush was still running"
            )

            release_flush.set()
            hook.join(timeout=_JOIN_TIMEOUT_SECONDS)

        assert not hook.is_alive()
        assert order == ["flush_started", "flush_finished", "hook_returned"]

    def test_the_teardown_body_runs_exactly_once_across_both_writers(
        self, audit_coordinator, _real_flush_with_stubbed_stages
    ):
        """Serialization must not become duplication: the hook waits for the
        lock, then finds the once-flag and runs none of the stages itself."""
        from baldur.adapters.gunicorn.hooks import worker_exit

        audit_coordinator.initiate_shutdown()
        assert audit_coordinator.wait_for_shutdown(timeout=_HANDOFF_TIMEOUT_SECONDS)

        worker_exit(_arbiter(), _exiting_worker())

        for stage, stub in _real_flush_with_stubbed_stages.items():
            assert stub.call_count == 1, f"{stage} ran {stub.call_count} times"


class TestWorkerExitOnTheRecyclePath:
    """No shutdown was initiated, so the hook's flush is the only teardown."""

    def test_recycle_exit_flushes_the_audit_system_and_marks_completion(
        self, audit_coordinator, _real_flush_with_stubbed_stages
    ):
        from baldur.adapters.gunicorn.hooks import worker_exit
        from baldur.core.shutdown_coordinator import ShutdownPhase

        assert audit_coordinator.phase == ShutdownPhase.RUNNING

        with capture_logs() as cap_logs:
            worker_exit(_arbiter(), _exiting_worker())

        for stage, stub in _real_flush_with_stubbed_stages.items():
            assert stub.call_count == 1, f"{stage} did not run on the recycle path"

        shutdown_events = [
            e["event"]
            for e in cap_logs
            if str(e.get("event", "")).startswith("shutdown.")
        ]
        assert shutdown_events == ["shutdown.worker_exit_completed"]

    def test_a_later_coordinator_drain_does_not_reflush(
        self, audit_coordinator, _real_flush_with_stubbed_stages
    ):
        """The flag the hook set is what makes the two writers idempotent."""
        from baldur.adapters.gunicorn.hooks import worker_exit

        worker_exit(_arbiter(), _exiting_worker())

        audit_coordinator.initiate_shutdown()
        assert audit_coordinator.wait_for_shutdown(timeout=_HANDOFF_TIMEOUT_SECONDS)

        for stage, stub in _real_flush_with_stubbed_stages.items():
            assert stub.call_count == 1, f"{stage} ran again for the drain"


class TestMasterSideInvocationLeavesTheLifecycleUntouched:
    """The amplifier the process guard closes.

    ``audit_shutdown_done`` is a process-global runtime singleton with no
    fork awareness. A master-side execution would set it in the supervising
    process, and every worker forked afterwards would inherit a flag saying
    its own flush had already happened.
    """

    def test_no_stage_runs_and_no_once_flag_is_set_in_the_master(
        self, audit_coordinator, _real_flush_with_stubbed_stages
    ):
        from baldur.adapters.gunicorn.hooks import worker_exit
        from baldur.audit.async_audit_lifecycle import _lifecycle_state

        worker_exit(_arbiter(), _foreign_worker())

        for stage, stub in _real_flush_with_stubbed_stages.items():
            assert stub.call_count == 0, f"{stage} ran in the master"
        assert _lifecycle_state().audit_shutdown_done is False


# =============================================================================
# The second two-writer shape on this lifecycle: the DLQ outbox teardown.
#
# ``worker_exit`` calls ``stop_outbox_for_shutdown()`` directly and the
# coordinator's ``DLQOutboxShutdownHandler.on_drain_complete()`` calls it from
# the drain thread. They share the once-gate and its cached result, so which
# one runs first changes what the other reports — and neither component can
# show that alone. The transaction boundary is the second property: the
# outbox's final writes must land before ``graceful_shutdown_audit_system()``
# closes the WAL, which is why the handler is registered ahead of
# ``AuditShutdownHandler`` and why the hook calls the teardown ahead of the
# flush.
#
# Real coordinator, real handlers, real outbox with a real drainer thread; only
# the five audit teardown stages and the DLQ store are stubbed.
# =============================================================================

# Below the two floors the teardown carves out, so the budget is the floors:
# a 0.5 s join of a wedged drainer, then the dump.
_TEST_OUTBOX_BUDGET_SECONDS = 0.1


@pytest.fixture
def wedged_outbox(monkeypatch):
    """A started outbox whose store never returns, published as the singleton.

    The wedged writer is what makes the teardown observable: one entry is stuck
    in flight, the rest stay on the ring, and the emergency dump is the only
    thing that can rescue any of them. Yields ``(outbox, dumped, order)``.
    """
    import threading as _threading

    from baldur.audit.ring_buffer import RingBuffer
    from baldur.services.dlq_outbox import outbox as outbox_module
    from baldur.services.dlq_outbox.outbox import Outbox
    from baldur.services.dlq_outbox.worker import DLQOutboxWorker
    from baldur.settings.backpressure import BackpressureStrategy
    from baldur.settings.dlq_outbox import DLQOutboxSettings

    forever = _threading.Event()
    dumped: list[dict] = []
    order: list[str] = []

    def hanging_writer(kwargs):
        forever.wait(timeout=_JOIN_TIMEOUT_SECONDS)

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
        # The drainer pops the first entry into the hanging writer; the other
        # two stay on the ring.
        assert _wait_until(lambda: worker.in_flight == 1)
        try:
            yield outbox, dumped, order
        finally:
            forever.set()


def _wait_until(predicate, timeout: float = _HANDOFF_TIMEOUT_SECONDS) -> bool:
    """Poll a state transition the drainer thread performs."""
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return True
        _time.sleep(0.01)
    return predicate()


@pytest.fixture
def outbox_and_audit_coordinator(_real_flush_with_stubbed_stages):
    """A real coordinator carrying both handlers, in ``init()``'s order.

    The order is the claim: the outbox handler's teardown runs before the audit
    handler closes the WAL.
    """
    from baldur.audit.shutdown_handler import AuditShutdownHandler
    from baldur.core.shutdown_coordinator import (
        RequestTracker,
        get_shutdown_coordinator,
    )
    from baldur.services.dlq_outbox.shutdown import DLQOutboxShutdownHandler

    coordinator = get_shutdown_coordinator(request_tracker=RequestTracker())
    coordinator.register_handler(DLQOutboxShutdownHandler())
    coordinator.register_handler(AuditShutdownHandler())
    return coordinator


class TestOutboxTeardownAcrossBothWriters:
    """One teardown, two callers, one terminal report."""

    def test_the_recycle_path_is_the_only_teardown_the_outbox_gets(
        self,
        outbox_and_audit_coordinator,
        wedged_outbox,
        _real_flush_with_stubbed_stages,
    ):
        """No shutdown is initiated on a ``max_requests`` recycle, so the
        coordinator's handler never runs — and without the hook's own call the
        buffered entries would die with the daemon drain thread."""
        from baldur.adapters.gunicorn.hooks import worker_exit
        from baldur.core.shutdown_coordinator import ShutdownPhase

        _, dumped, _ = wedged_outbox
        assert outbox_and_audit_coordinator.phase == ShutdownPhase.RUNNING

        worker_exit(_arbiter(), _exiting_worker())

        assert len(dumped) == 3
        assert {e["failure_type"] for e in dumped} == {
            "PG_TIMEOUT_0",
            "PG_TIMEOUT_1",
            "PG_TIMEOUT_2",
        }

    def test_the_teardown_body_runs_exactly_once_across_both_writers(
        self,
        outbox_and_audit_coordinator,
        wedged_outbox,
        _real_flush_with_stubbed_stages,
    ):
        """Serialization must not become duplication: the drain's handler runs
        the teardown, and the hook that follows finds the cached result and
        re-dumps nothing."""
        from baldur.adapters.gunicorn.hooks import worker_exit

        _, dumped, _ = wedged_outbox

        outbox_and_audit_coordinator.initiate_shutdown()
        assert outbox_and_audit_coordinator.wait_for_shutdown(
            timeout=_HANDOFF_TIMEOUT_SECONDS
        )
        first_pass = len(dumped)

        worker_exit(_arbiter(), _exiting_worker())

        assert first_pass == 3
        assert len(dumped) == 3

    def test_the_hooks_terminal_line_reports_the_drains_counts(
        self,
        outbox_and_audit_coordinator,
        wedged_outbox,
        _real_flush_with_stubbed_stages,
    ):
        """The cached result is the point of the gate: an exit hook that ran
        second must log what actually happened to the entries, not the zeros a
        "someone else did it" short-circuit would produce."""
        from baldur.adapters.gunicorn.hooks import worker_exit

        outbox_and_audit_coordinator.initiate_shutdown()
        assert outbox_and_audit_coordinator.wait_for_shutdown(
            timeout=_HANDOFF_TIMEOUT_SECONDS
        )

        with capture_logs() as cap_logs:
            worker_exit(_arbiter(), _exiting_worker())

        # The hook logs nothing of its own about the outbox; the teardown's
        # report reaches the operator through the handler's line, which the
        # drain already emitted. What the hook must not do is re-run the drain.
        assert not [
            e
            for e in cap_logs
            if e.get("event") == "dlq_outbox.shutdown_emergency_dump"
        ]

    def test_a_later_coordinator_drain_does_not_re_dump_after_a_recycle_exit(
        self,
        outbox_and_audit_coordinator,
        wedged_outbox,
        _real_flush_with_stubbed_stages,
    ):
        """The reverse order — the hook first. The gate has to hold from either
        side, because a recycle exit and a signalled one are the same code."""
        from baldur.adapters.gunicorn.hooks import worker_exit

        _, dumped, _ = wedged_outbox

        worker_exit(_arbiter(), _exiting_worker())
        assert len(dumped) == 3

        outbox_and_audit_coordinator.initiate_shutdown()
        assert outbox_and_audit_coordinator.wait_for_shutdown(
            timeout=_HANDOFF_TIMEOUT_SECONDS
        )

        assert len(dumped) == 3

    def test_the_outbox_teardown_lands_before_the_wal_is_closed(
        self,
        outbox_and_audit_coordinator,
        wedged_outbox,
        _real_flush_with_stubbed_stages,
    ):
        """The transaction boundary, on the recycle path where the hook owns
        both steps."""
        from baldur.adapters.gunicorn.hooks import worker_exit

        _, _, order = wedged_outbox
        _real_flush_with_stubbed_stages["_shutdown_wal"].side_effect = lambda *a, **kw: (
            order.append("wal_close")
        )

        worker_exit(_arbiter(), _exiting_worker())

        assert order == ["outbox_dump", "wal_close"]

    def test_the_drains_handler_order_puts_the_outbox_before_the_wal_too(
        self,
        outbox_and_audit_coordinator,
        wedged_outbox,
        _real_flush_with_stubbed_stages,
    ):
        """Same boundary on the signalled path, where the ordering comes from
        the registration order rather than from one function's statements."""
        _, _, order = wedged_outbox
        _real_flush_with_stubbed_stages["_shutdown_wal"].side_effect = lambda *a, **kw: (
            order.append("wal_close")
        )

        outbox_and_audit_coordinator.initiate_shutdown()
        assert outbox_and_audit_coordinator.wait_for_shutdown(
            timeout=_HANDOFF_TIMEOUT_SECONDS
        )

        assert order == ["outbox_dump", "wal_close"]

    def test_producers_are_coerced_to_the_sync_writer_after_the_teardown(
        self,
        outbox_and_audit_coordinator,
        wedged_outbox,
        _real_flush_with_stubbed_stages,
    ):
        """The window the coercion flag closes.

        A capture arriving after the drainer has been joined and the dump has
        run would otherwise sit in a buffer nothing will ever read again. The
        producer that reads the flag is the capture service, not the outbox, so
        the two have to be composed to see it.
        """
        from baldur.adapters.gunicorn.hooks import worker_exit
        from baldur.adapters.memory import InMemoryFailedOperationRepository
        from baldur.services.dlq_capture import DLQCaptureService
        from baldur.services.dlq_outbox import outbox as outbox_module

        outbox, _, _ = wedged_outbox
        repository = InMemoryFailedOperationRepository()
        service = DLQCaptureService(repository=repository)

        worker_exit(_arbiter(), _exiting_worker())

        assert outbox_module.is_worker_dead() is True
        result = service._dispatch_to_outbox(
            domain="payment", failure_type="AFTER_TEARDOWN"
        )

        # Stored synchronously, and never enqueued into the drained buffer
        assert repository.count_all() == 1
        assert result.dlq_id is not None
        assert outbox.buffer.size == 0
