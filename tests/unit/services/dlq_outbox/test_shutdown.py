"""The outbox's seat at the graceful-shutdown table.

The handler is what makes a *signalled* exit drain the outbox. Its two
interesting properties are both negatives:

- ``on_shutdown_start`` deliberately does nothing. Unlike the precomputed-cache
  handler it must NOT stop its worker there: entries captured *during* the
  drain — by the very in-flight requests the coordinator is waiting on — still
  have to reach the DLQ, and stopping the drainer at shutdown start strands
  exactly those.
- ``is_drain_complete`` reports True whenever waiting can no longer change the
  outcome, including when the drainer is dead or wedged in backoff. Without
  that arm a dead drainer would hold the coordinator's whole drain window open
  on every shutdown, and the teardown spills the remainder either way.

Registration *position* is the third property, and it is not the handler's own:
teardown hooks run in registration order, so the outbox's final writes land
while the audit WAL is still open only because ``init()`` registers this
handler ahead of the audit one.
"""

from __future__ import annotations

from unittest.mock import PropertyMock, patch

import pytest

from baldur.services.dlq_outbox import outbox as outbox_module
from baldur.services.dlq_outbox.outbox import OutboxShutdownResult
from baldur.services.dlq_outbox.shutdown import (
    DLQOutboxShutdownHandler,
    integrate_with_shutdown_coordinator,
)

_TEARDOWN = "baldur.services.dlq_outbox.outbox.stop_outbox_for_shutdown"


def _install_outbox(outbox_obj) -> None:
    outbox_module._outbox = outbox_obj


def _entry(failure_type: str = "PG_TIMEOUT") -> tuple[float, dict]:
    return (0.0, {"domain": "payment", "failure_type": failure_type})


class TestDLQOutboxShutdownHandlerBehavior:
    """The drain predicate's state matrix and the two teardown hooks."""

    def test_on_shutdown_start_does_not_stop_the_worker(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """The negative the design turns on: entries captured by the requests
        the coordinator is still draining must keep reaching the DLQ."""
        # Given
        outbox, _, worker = build_outbox(
            make_sync_writer(collected_writes), flush_interval_seconds=0.01
        )
        outbox.start()
        _install_outbox(outbox)
        try:
            # When
            DLQOutboxShutdownHandler().on_shutdown_start()

            # Then — the drainer is untouched
            assert worker.is_running is True
            assert worker.is_alive is True
        finally:
            outbox.stop(timeout=1.0)

    def test_is_drain_complete_true_when_no_outbox_exists_in_this_process(self):
        """A process that built none has nothing to wait for."""
        assert outbox_module._outbox is None

        assert DLQOutboxShutdownHandler().is_drain_complete() is True

    def test_is_drain_complete_true_when_the_drainer_is_not_alive(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """Waiting cannot help. Without this arm a dead drainer holds the
        coordinator's entire drain window open on every shutdown."""
        # Given — built but never started, and holding an entry
        outbox, buffer, _ = build_outbox(make_sync_writer(collected_writes))
        buffer.put(_entry())
        _install_outbox(outbox)

        # Then
        assert DLQOutboxShutdownHandler().is_drain_complete() is True

    def test_is_drain_complete_true_when_the_drainer_is_in_sustained_backoff(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """A drainer already sleeping between failing writes will not empty the
        buffer, so the remainder goes to the teardown's dump instead."""
        # Given
        outbox, buffer, worker = build_outbox(make_sync_writer(collected_writes))
        buffer.put(_entry())
        worker._consecutive_failures = 99
        _install_outbox(outbox)

        # When — alive, but backing off
        with patch.object(
            type(worker), "is_alive", new_callable=PropertyMock, return_value=True
        ):
            # Then
            assert DLQOutboxShutdownHandler().is_drain_complete() is True

    def test_is_drain_complete_false_while_the_buffer_still_holds_entries(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """The positive control: a healthy drainer with work left is exactly
        what the coordinator should keep waiting for."""
        # Given
        outbox, buffer, worker = build_outbox(make_sync_writer(collected_writes))
        buffer.put(_entry())
        _install_outbox(outbox)

        # When
        with patch.object(
            type(worker), "is_alive", new_callable=PropertyMock, return_value=True
        ):
            # Then
            assert DLQOutboxShutdownHandler().is_drain_complete() is False

    def test_is_drain_complete_false_while_a_write_is_still_in_flight(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """An entry popped off the ring is not drained yet — an empty buffer
        alone would report completion over a write still running."""
        # Given
        outbox, _, worker = build_outbox(make_sync_writer(collected_writes))
        worker._in_flight = 1
        _install_outbox(outbox)

        # When
        with patch.object(
            type(worker), "is_alive", new_callable=PropertyMock, return_value=True
        ):
            # Then
            assert DLQOutboxShutdownHandler().is_drain_complete() is False

    def test_is_drain_complete_true_when_the_buffer_is_empty_and_nothing_in_flight(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        # Given
        outbox, _, worker = build_outbox(make_sync_writer(collected_writes))
        _install_outbox(outbox)

        # When
        with patch.object(
            type(worker), "is_alive", new_callable=PropertyMock, return_value=True
        ):
            # Then
            assert DLQOutboxShutdownHandler().is_drain_complete() is True

    def test_is_drain_complete_true_when_reading_the_state_raises(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """Undecidable: report drained rather than hold the whole shutdown open
        on a handler that cannot read its own state."""
        # Given
        outbox, _, worker = build_outbox(make_sync_writer(collected_writes))
        _install_outbox(outbox)

        # When
        with patch.object(
            type(worker),
            "is_alive",
            new_callable=PropertyMock,
            side_effect=RuntimeError("state unreadable"),
        ):
            # Then
            assert DLQOutboxShutdownHandler().is_drain_complete() is True

    def test_on_drain_complete_runs_the_teardown(self):
        """The signalled-exit path: the coordinator's drain converged, and the
        handler is what turns the remainder into persisted entries."""
        with patch(
            _TEARDOWN, return_value=OutboxShutdownResult(0, 0, 0, 0, 0, 0, 0)
        ) as m:
            DLQOutboxShutdownHandler().on_drain_complete()

        m.assert_called_once_with()

    def test_on_force_shutdown_runs_the_same_teardown(self):
        """A forced shutdown is where buffered entries are most likely to still
        exist, so it may not be the path that skips the teardown."""
        with patch(
            _TEARDOWN, return_value=OutboxShutdownResult(0, 0, 0, 0, 0, 0, 0)
        ) as m:
            DLQOutboxShutdownHandler().on_force_shutdown([])

        m.assert_called_once_with()

    def test_the_terminal_counts_are_logged_where_an_operator_reads_them(self):
        """The handler is one of the two places the terminal report surfaces;
        dropping the counts here would make a signalled exit silent about a
        residual the recycle path reports."""
        from structlog.testing import capture_logs

        result = OutboxShutdownResult(
            pending_at_entry=9,
            dispatched=4,
            soft_failed=1,
            failed=0,
            emergency_dumped=3,
            residual=1,
            duplicated=0,
        )

        with patch(_TEARDOWN, return_value=result), capture_logs() as cap_logs:
            DLQOutboxShutdownHandler().on_drain_complete()

        matching = [
            e
            for e in cap_logs
            if e.get("event") == "dlq_outbox.shutdown_teardown_completed"
        ]
        assert len(matching) == 1
        assert matching[0]["pending_at_entry"] == 9
        assert matching[0]["residual"] == 1

    def test_a_raising_teardown_does_not_escape_into_the_coordinator(self):
        """Handler hooks run inside the coordinator's drain thread; a raise
        here would be attributed to the shutdown chain, not to the outbox."""
        with patch(_TEARDOWN, side_effect=RuntimeError("teardown blew up")):
            # Contract is "does not raise" — the outbox is a side-effect
            # subsystem and may not take the shutdown down with it.
            DLQOutboxShutdownHandler().on_force_shutdown([])

    def test_integrate_returns_a_handler_for_the_bootstrap_to_register(self):
        handler = integrate_with_shutdown_coordinator()

        assert isinstance(handler, DLQOutboxShutdownHandler)


class TestDLQOutboxShutdownRegistrationContract:
    """Where ``init()`` puts the handler in the coordinator's list.

    Teardown hooks run in registration order. This one must run after every
    other subsystem's (so nothing is still producing DLQ entries) and before
    the audit flush (so the outbox's final writes land while the WAL is still
    open) — a position, not a behavior, and therefore its own assertion.
    """

    @pytest.fixture
    def registered_handlers(self):
        """Run ``init()``'s real handler registration against a fresh coordinator.

        OS signal wiring is patched out: it is the step after the registrations
        and installs process-wide handlers a unit test must not leave behind.
        """
        import baldur.bootstrap as bootstrap_module
        from baldur.core.shutdown_coordinator import (
            get_shutdown_coordinator,
            reset_shutdown_coordinator,
        )

        reset_shutdown_coordinator()
        try:
            with patch(
                "baldur.core.shutdown_coordinator.GracefulShutdownCoordinator.register_signals"
            ):
                bootstrap_module._register_shutdown_handlers()
            yield list(get_shutdown_coordinator()._handlers)
        finally:
            reset_shutdown_coordinator()

    def test_registration_places_the_outbox_handler_in_the_coordinator_list(
        self, registered_handlers
    ):
        names = [type(h).__name__ for h in registered_handlers]

        assert "DLQOutboxShutdownHandler" in names

    def test_registration_places_the_outbox_handler_before_the_audit_handler(
        self, registered_handlers
    ):
        """The transaction boundary: the outbox's final writes have to land
        before ``graceful_shutdown_audit_system()`` closes the WAL."""
        names = [type(h).__name__ for h in registered_handlers]

        assert "AuditShutdownHandler" in names
        assert names.index("DLQOutboxShutdownHandler") < names.index(
            "AuditShutdownHandler"
        )

    def test_registration_places_the_outbox_handler_after_every_other_subsystem(
        self, registered_handlers
    ):
        """Nothing but the audit flush may run after it — a subsystem torn down
        later could still be producing DLQ entries into a drained outbox."""
        names = [type(h).__name__ for h in registered_handlers]

        assert names[-2:] == ["DLQOutboxShutdownHandler", "AuditShutdownHandler"]
