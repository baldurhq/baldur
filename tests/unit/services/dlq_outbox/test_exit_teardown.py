"""The third exit path: the outbox teardown on a polite interpreter exit.

A signalled stop drains through the coordinator's handler and a worker recycle
through the adapter's exit hook, but a script that returns — or calls
``sys.exit()`` — runs neither, and the drainer is a daemon thread the
interpreter kills with the buffer still in it. Observed on a dogfood cron job:
an entry captured seconds before the process ended was gone from the SQL store,
with no report. The outbox now registers the same idempotent teardown with
``atexit`` the first time it starts in a process.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from unittest.mock import patch

import pytest

from baldur.services.dlq_outbox import outbox as outbox_module
from baldur.services.dlq_outbox.outbox import (
    get_outbox,
    reset_dlq_outbox,
    setup_dlq_outbox,
    stop_outbox_for_shutdown,
)

_NO_OP_WRITER = "baldur.services.dlq_outbox.outbox._default_sync_writer"


def _install_outbox(outbox_obj) -> None:
    outbox_module._outbox = outbox_obj
    outbox_module._outbox_origin_pid = os.getpid()


def _entry(domain: str = "payment") -> dict:
    return {"domain": domain, "failure_type": "PG_TIMEOUT"}


@pytest.fixture
def unregistered_hook(monkeypatch):
    """Pretend no outbox has started in this process yet.

    The registration flag is process-lifetime on purpose (one hook per process),
    so an earlier test that started an outbox has already set it; the tests
    that assert registration reset it and intercept ``atexit.register``.
    """
    monkeypatch.setattr(outbox_module, "_exit_teardown_registered", False)
    with patch("baldur.services.dlq_outbox.outbox.atexit.register") as register:
        yield register


class TestExitTeardownRegistrationContract:
    """One hook per process, installed by whichever start path runs first."""

    def test_eager_start_registers_the_teardown_once(self, unregistered_hook):
        # When — init()'s eager start, then a re-entry
        with patch(_NO_OP_WRITER, new=lambda kwargs: None):
            assert setup_dlq_outbox() is True
            assert setup_dlq_outbox() is False

        # Then
        unregistered_hook.assert_called_once_with(outbox_module._teardown_at_exit)

    def test_lazy_start_registers_the_teardown_once(self, unregistered_hook):
        # When — a script that touches the DLQ store before init()
        with patch(_NO_OP_WRITER, new=lambda kwargs: None):
            get_outbox()
            get_outbox()

        # Then
        unregistered_hook.assert_called_once_with(outbox_module._teardown_at_exit)

    def test_restart_after_reset_does_not_register_a_second_hook(
        self, unregistered_hook
    ):
        """The hook outlives the singleton it was registered for: the teardown
        it calls reads the module state at exit time, whatever was rebuilt."""
        with patch(_NO_OP_WRITER, new=lambda kwargs: None):
            setup_dlq_outbox()
            reset_dlq_outbox()
            setup_dlq_outbox()

        assert unregistered_hook.call_count == 1


class TestExitTeardownBehavior:
    """What the hook does when the interpreter finally runs it."""

    def test_drains_an_entry_the_worker_had_not_reached(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """The observed loss: an entry captured just before the exit, sitting in
        the ring while the drainer waits out its flush interval."""
        # Given — a drainer that will not wake up on its own before the exit
        outbox, _, _ = build_outbox(
            make_sync_writer(collected_writes), flush_interval_seconds=30.0
        )
        outbox.start()
        _install_outbox(outbox)
        assert outbox.put(_entry()) is True
        assert collected_writes == []

        # When
        outbox_module._teardown_at_exit()

        # Then — written through the real writer, and reported as such
        assert collected_writes == [_entry()]
        assert outbox_module._shutdown_result is not None
        assert outbox_module._shutdown_result.pending_at_entry == 1
        assert outbox_module._shutdown_result.dispatched == 1

    def test_after_the_coordinator_teardown_it_reports_nothing_new(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """A signalled exit already tore the outbox down; the hook must neither
        drain again nor log a second terminal report."""
        outbox, _, _ = build_outbox(make_sync_writer(collected_writes))
        outbox.start()
        _install_outbox(outbox)
        first = stop_outbox_for_shutdown()

        # When
        with patch.object(outbox_module, "logger") as logger:
            outbox_module._teardown_at_exit()

        # Then
        assert outbox_module._shutdown_result is first
        logger.info.assert_not_called()

    def test_skips_a_singleton_inherited_across_fork(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """A child that never re-owned the outbox holds a copy of the parent's
        buffer; dumping it would write the parent's pending entries twice."""
        outbox, _, _ = build_outbox(
            make_sync_writer(collected_writes), flush_interval_seconds=30.0
        )
        outbox.start()
        try:
            _install_outbox(outbox)
            outbox_module._outbox_origin_pid = os.getpid() + 1  # the parent's pid
            assert outbox.put(_entry()) is True

            # When
            with patch(
                "baldur.services.dlq_outbox.outbox.stop_outbox_for_shutdown"
            ) as teardown:
                outbox_module._teardown_at_exit()

            # Then
            teardown.assert_not_called()
            assert collected_writes == []
        finally:
            outbox.stop(timeout=1.0)

    def test_a_teardown_error_is_reported_not_raised(self):
        """atexit swallows exceptions into a traceback on stderr; the hook
        reports through the log instead so the exit stays clean."""
        with (
            patch(
                "baldur.services.dlq_outbox.outbox.stop_outbox_for_shutdown",
                side_effect=RuntimeError("store gone"),
            ),
            patch.object(outbox_module, "logger") as logger,
        ):
            outbox_module._teardown_at_exit()

        logger.warning.assert_called_once()
        assert logger.warning.call_args.args[0] == "dlq_outbox.exit_teardown_failed"


class TestPoliteExitEndToEnd:
    """The reproduction itself: a process that captures one failure and returns."""

    def test_entry_captured_before_a_plain_exit_reaches_the_sql_store(self, tmp_path):
        db = tmp_path / "dlq.db"
        # A clean environment: the test session's own BALDUR_* overrides (test
        # mode, memory backends) would decide the child's posture otherwise.
        env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("BALDUR_")},
            "BALDUR_SQL_DSN": f"sqlite:///{db.as_posix()}",
            "BALDUR_DLQ_BACKEND": "sql",
            "BALDUR_ADMIN_ENABLED": "false",
            "BALDUR_SCHEDULER_AUTOSTART": "0",
            "BALDUR_RETRY_MAX_ATTEMPTS": "2",
            "BALDUR_RETRY_BASE_DELAY": "0.1",
            "BALDUR_LOG_LEVEL": "WARNING",
            # Whether the drainer's own 0.1 s tick lands before the interpreter
            # is gone depends on what else the exit has to do; a long interval
            # pins the entry in the ring so only the exit teardown can write it.
            "BALDUR_DLQ_OUTBOX_FLUSH_INTERVAL_SECONDS": "30",
            # The teardown's optimistic flush phase waits out its share of this
            # budget before the join wakes the drainer; a small budget keeps
            # the test short without changing what gets written.
            "BALDUR_DLQ_OUTBOX_JOIN_TIMEOUT_SECONDS": "2",
        }
        # The observed shape: an async call whose capture lands in the ring on
        # its way out of asyncio.run(), with the script ending right behind it.
        script = textwrap.dedent(
            """
            import asyncio

            import baldur

            baldur.init()

            @baldur.protected("exit_probe", retry=True, dlq=True)
            async def fetch(name: str) -> None:
                raise RuntimeError(f"upstream down for {name}")

            async def main() -> None:
                try:
                    await fetch("Python")
                except RuntimeError:
                    pass

            asyncio.run(main())
            # The script simply ends here: no signal, no adapter hook, no sleep.
            """
        )

        # When
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            cwd=tmp_path,
        )

        # Then — the store holds the entry; a missing table means the writer
        # never got to its first write before the interpreter was gone.
        assert result.returncode == 0, result.stderr[-2000:]
        conn = sqlite3.connect(db)
        tables = {name for (name,) in conn.execute("SELECT name FROM sqlite_master")}
        assert "baldur_dlq" in tables, "no entry reached the store before the exit"
        rows = conn.execute("SELECT domain, status FROM baldur_dlq").fetchall()
        assert rows == [("exit_probe", "pending")]
