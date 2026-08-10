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
