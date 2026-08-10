"""Concurrency contract of ``graceful_shutdown_audit_system``.

Two writers reach this teardown against one process lifecycle: the gunicorn
worker-exit hook and the shutdown coordinator's drain thread. The once-guard
alone used to be fire-and-skip — a second trigger returned immediately while
the first was still mid-flush, so on the overlap that actually happens (the
drain thread starting the flush at drain-timeout just as the hook's join
expires) the hook returned, the process exited, and the daemon drain thread
was killed with the WAL unclosed and no checkpoint saved.

The body is now serialized under the module lock, acquired with a deadline
rather than blocking: three of the five stages have no ceiling of their own,
so an unbounded acquire would hang the second caller on a wedged destination,
and on a non-gunicorn host there is no arbiter to kill it.

Both timing tests carry an explicit deadline, because their failure mode is a
hang rather than a wrong value.
"""

from __future__ import annotations

import threading
from contextlib import ExitStack
from unittest.mock import patch

import pytest

# Every wait below is on a threading.Event, so a healthy run blocks for as
# long as the test needs it to and no longer. These bound the failure modes.
_HANDOFF_TIMEOUT_SECONDS = 5.0
_JOIN_TIMEOUT_SECONDS = 10.0
# Long enough that a *correct* second caller is still blocked when we sample
# it, short enough not to pad the suite.
_STILL_BLOCKED_PROBE_SECONDS = 0.3

_STAGES = (
    "_shutdown_async_logger",
    "_shutdown_sync_worker",
    "_shutdown_wal",
    "_save_final_checkpoint",
    "_shutdown_disk_buffer",
)


@pytest.fixture(autouse=True)
def _flush_runs_for_real(monkeypatch):
    """Take the suite out of test mode and start from a clean once-flag.

    ``graceful_shutdown_audit_system`` returns before anything else when
    ``BALDUR_TEST_MODE`` is true, which is the session default — so without
    this the whole concurrency contract is unreachable.
    """
    from baldur.audit.async_audit_lifecycle import _reset_audit_shutdown_state

    monkeypatch.setenv("BALDUR_TEST_MODE", "false")
    _reset_audit_shutdown_state()
    yield
    _reset_audit_shutdown_state()


class TestGracefulShutdownConcurrencyBehavior:
    """One caller runs the body; the other waits it out or gives up."""

    @pytest.fixture(autouse=True)
    def _stubbed_stages(self):
        """Replace the five teardown stages so no real WAL/buffer work happens.

        Individual tests re-patch one of them to control when the first caller
        leaves the critical section.
        """
        with ExitStack() as stack:
            for stage in _STAGES:
                stack.enter_context(
                    patch(f"baldur.audit.async_audit_lifecycle.{stage}")
                )
            yield

    def test_second_caller_waits_for_the_in_flight_flush_and_runs_nothing(self):
        """The defect this replaces: the second caller returned *into* the
        first's truncation. It must now outlast the flush, and it must not
        re-run any stage when it finally gets the lock."""
        from baldur.audit import async_audit_lifecycle as lifecycle

        first_inside = threading.Event()
        release_first = threading.Event()
        second_returned = threading.Event()
        stage_calls: list[str] = []

        def _slow_first_stage():
            stage_calls.append("async_logger")
            first_inside.set()
            release_first.wait(timeout=_HANDOFF_TIMEOUT_SECONDS)

        def _second_caller():
            lifecycle.graceful_shutdown_audit_system()
            second_returned.set()

        with patch.object(
            lifecycle, "_shutdown_async_logger", side_effect=_slow_first_stage
        ):
            first = threading.Thread(target=lifecycle.graceful_shutdown_audit_system)
            first.start()
            assert first_inside.wait(timeout=_HANDOFF_TIMEOUT_SECONDS), (
                "first caller never entered the flush"
            )

            second = threading.Thread(target=_second_caller)
            second.start()

            # Then: the second caller is still inside the acquire while the
            # first holds the lock — it did not return into a truncation.
            assert not second_returned.wait(timeout=_STILL_BLOCKED_PROBE_SECONDS), (
                "second caller returned while the first flush was in flight"
            )

            release_first.set()
            first.join(timeout=_JOIN_TIMEOUT_SECONDS)
            second.join(timeout=_JOIN_TIMEOUT_SECONDS)

        assert not first.is_alive()
        assert not second.is_alive()
        assert stage_calls == ["async_logger"], "the teardown body ran more than once"

    def test_second_caller_gives_up_with_a_named_warning_on_a_wedged_flush(self):
        """A wedged destination must not hang the caller: three of the five
        stages have no ceiling, and off gunicorn there is no arbiter to kill
        a blocked process. Giving up is reported, never silent."""
        from structlog.testing import capture_logs

        from baldur.audit import async_audit_lifecycle as lifecycle

        first_inside = threading.Event()
        release_first = threading.Event()
        second_done = threading.Event()
        captured: list[dict] = []

        def _wedged_first_stage():
            first_inside.set()
            release_first.wait(timeout=_HANDOFF_TIMEOUT_SECONDS)

        def _second_caller():
            with capture_logs() as cap_logs:
                lifecycle.graceful_shutdown_audit_system()
                captured.extend(cap_logs)
            second_done.set()

        with (
            patch.object(
                lifecycle, "_shutdown_async_logger", side_effect=_wedged_first_stage
            ),
            patch.object(lifecycle, "_FLUSH_WAIT_TIMEOUT_SECONDS", 0.1),
        ):
            first = threading.Thread(target=lifecycle.graceful_shutdown_audit_system)
            first.start()
            assert first_inside.wait(timeout=_HANDOFF_TIMEOUT_SECONDS), (
                "first caller never entered the flush"
            )

            second = threading.Thread(target=_second_caller)
            second.start()

            # The whole point: this returns on the ceiling rather than
            # waiting out a flush that is never going to finish.
            assert second_done.wait(timeout=_JOIN_TIMEOUT_SECONDS), (
                "second caller blocked past the flush-wait ceiling"
            )

            release_first.set()
            first.join(timeout=_JOIN_TIMEOUT_SECONDS)
            second.join(timeout=_JOIN_TIMEOUT_SECONDS)

        assert not first.is_alive()
        matching = [
            e
            for e in captured
            if e.get("event") == "graceful_shutdown.concurrent_flush_wait_timeout"
        ]
        assert len(matching) == 1
        assert matching[0]["log_level"] == "warning"
        assert matching[0]["waited_seconds"] == 0.1

    def test_a_later_caller_takes_the_once_guard_rather_than_reflushing(self):
        """Serialization did not replace the once-guard — a caller arriving
        after a completed teardown still finds the flag and runs nothing."""
        from baldur.audit import async_audit_lifecycle as lifecycle

        stage_calls: list[str] = []

        with patch.object(
            lifecycle,
            "_shutdown_async_logger",
            side_effect=lambda: stage_calls.append("async_logger"),
        ):
            lifecycle.graceful_shutdown_audit_system()
            lifecycle.graceful_shutdown_audit_system()

        assert stage_calls == ["async_logger"]

    def test_the_lock_is_released_when_a_stage_raises(self):
        """Every stage is catch-log-continue, but the release lives in a
        ``finally``: a leaked lock would convert one bad teardown into a
        permanent flush-wait timeout for every later caller."""
        from baldur.audit import async_audit_lifecycle as lifecycle

        with patch.object(
            lifecycle,
            "_shutdown_disk_buffer",
            side_effect=RuntimeError("buffer teardown blew up"),
        ):
            with pytest.raises(RuntimeError):
                lifecycle.graceful_shutdown_audit_system()

        assert lifecycle._audit_shutdown_lock.acquire(timeout=0)
        lifecycle._audit_shutdown_lock.release()


class TestAuditFlushBudgetContract:
    """The flush-wait ceiling is derived from the stage budgets it names."""

    def test_flush_wait_ceiling_is_the_stage_budgets_plus_the_margin(self):
        from baldur.audit.async_audit_lifecycle import (
            _ASYNC_LOGGER_STOP_TIMEOUT_SECONDS,
            _FLUSH_WAIT_MARGIN_SECONDS,
            _FLUSH_WAIT_TIMEOUT_SECONDS,
            _SYNC_WORKER_STOP_TIMEOUT_SECONDS,
        )

        assert _ASYNC_LOGGER_STOP_TIMEOUT_SECONDS == 5.0
        assert _SYNC_WORKER_STOP_TIMEOUT_SECONDS == 30.0
        assert _FLUSH_WAIT_MARGIN_SECONDS == 10.0
        assert _FLUSH_WAIT_TIMEOUT_SECONDS == 45.0
        assert _FLUSH_WAIT_TIMEOUT_SECONDS == (
            _ASYNC_LOGGER_STOP_TIMEOUT_SECONDS
            + _SYNC_WORKER_STOP_TIMEOUT_SECONDS
            + _FLUSH_WAIT_MARGIN_SECONDS
        )

    def test_async_logger_stage_passes_its_named_budget_to_stop(self):
        """The constant documents where the ceiling comes from only if the
        stage actually hands it to the call it claims to bound."""
        from baldur.audit import async_audit_lifecycle as lifecycle

        with patch("baldur.utils.async_logger.AsyncHealingLogger") as m_logger:
            lifecycle._shutdown_async_logger()

        m_logger.stop.assert_called_once_with(
            timeout=lifecycle._ASYNC_LOGGER_STOP_TIMEOUT_SECONDS
        )

    def test_sync_worker_stage_passes_its_named_budget_to_stop(self):
        from baldur.audit import async_audit_lifecycle as lifecycle

        with patch("baldur.audit.sync_worker.AuditSyncWorker") as m_worker:
            lifecycle._shutdown_sync_worker()

        m_worker.get_instance.return_value.stop.assert_called_once_with(
            timeout=lifecycle._SYNC_WORKER_STOP_TIMEOUT_SECONDS
        )
