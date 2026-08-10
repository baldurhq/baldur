"""Real-``fork()`` integration tests for the audit pipeline's revival.

The defect this covers only exists across a process boundary, so no unit test
can observe it: CPython marks a foreign process's threads stopped, and a
buffered file object's finalizer flushes into the *parent's* file through the
inherited file description. Both are properties of ``fork()`` itself.

Three compositions, each spanning the bootstrap starter, the lifecycle
singleton, the async logger and the WAL:

1. **Regression pair** — a child that ran the post-fork start path delivers a
   non-CRITICAL event; a child that did not demonstrates the loss the starter
   exists to end.
2. **Parent-file integrity** — the parent buffers a record with
   ``sync_on_write=false``, forks, and the child repairs and exits via
   ``os._exit``; the parent's file contains that record exactly once.
3. **Inherited-queue negative** — an event enqueued before the fork is
   delivered exactly once across the whole process group, not once per worker.

Infrastructure: none. A mock adapter (an append-only sink file, the only
channel a forked child shares with the parent) plus a ``tmp_path`` WAL. No
Redis, database or Celery.

Children always leave through ``os._exit`` so they never run pytest's exit
handlers, flush the parent's buffers, or emit a second test report.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from baldur.audit.wal import WriteAheadLog
from baldur.audit.wal._models import WALConfig
from baldur.utils.async_logger import AsyncHealingLogger, EventSeverity

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="fork() is POSIX-only")


@pytest.fixture
def sink(tmp_path) -> Path:
    """Append-only delivery log shared by every process in the group."""
    return tmp_path / "delivered.jsonl"


@pytest.fixture
def logger_with_sink(sink):
    """A started ``AsyncHealingLogger`` whose flush callback appends to ``sink``.

    The callback is a plain closure over a path, so it survives ``fork()`` and
    each process writes its own deliveries into the same file.
    """
    AsyncHealingLogger.reset()

    def deliver(events: list[dict]) -> None:
        with open(sink, "a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps({"pid": os.getpid(), **event}) + "\n")
            handle.flush()

    AsyncHealingLogger.configure(deliver)
    AsyncHealingLogger.start()
    yield AsyncHealingLogger
    AsyncHealingLogger.reset()


def _delivered(sink: Path) -> list[dict]:
    if not sink.exists():
        return []
    return [
        json.loads(line)
        for line in sink.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _run_in_child(body) -> int:
    """Run ``body`` in a forked child; return the child's exit status.

    The child never returns into pytest: it leaves through ``os._exit`` so no
    finalizer of the inherited interpreter state runs there.
    """
    pid = os.fork()
    if pid == 0:  # pragma: no cover - executes only in the forked child
        try:
            body()
        except BaseException:
            os._exit(1)
        os._exit(0)

    _, status = os.waitpid(pid, 0)
    return status


# =============================================================================
# 1. Regression pair
# =============================================================================


class TestRealForkAuditRevival:
    """A preload worker's non-CRITICAL events reach a consumer, or they do not
    exist at all — there is no third outcome.
    """

    def test_a_child_that_runs_the_start_path_delivers_its_event(
        self, logger_with_sink, sink
    ):
        """The revived path: the starter re-runs ``start()`` in the worker, the
        repair hands it a fresh queue and a real consumer, and the event lands.
        """

        def child():
            AsyncHealingLogger.start()
            AsyncHealingLogger.log({"type": "revived_worker_event"}, EventSeverity.INFO)
            AsyncHealingLogger.flush()

        status = _run_in_child(child)

        assert os.waitstatus_to_exitcode(status) == 0
        types = [event.get("type") for event in _delivered(sink)]
        assert "revived_worker_event" in types

    def test_a_child_with_no_revival_loses_its_event(self, logger_with_sink, sink):
        """The defect, reproduced: the child enqueues to a consumer that does
        not exist here and exits. Nothing downstream ever sees the event —
        which is what made the starter's absence a data-loss path rather than a
        latency one.
        """

        def child():
            AsyncHealingLogger.log(
                {"type": "unrevived_worker_event"}, EventSeverity.INFO
            )
            # No start(), no flush(): exactly the pre-fix worker.

        status = _run_in_child(child)

        assert os.waitstatus_to_exitcode(status) == 0
        types = [event.get("type") for event in _delivered(sink)]
        assert "unrevived_worker_event" not in types

    def test_the_child_does_not_disturb_the_parents_pipeline(
        self, logger_with_sink, sink
    ):
        """The parent keeps its own consumer: the child's repair replaces class
        state in the child's address space only.
        """

        def child():
            AsyncHealingLogger.start()
            AsyncHealingLogger.flush()

        _run_in_child(child)

        AsyncHealingLogger.log({"type": "parent_after_fork"}, EventSeverity.INFO)
        AsyncHealingLogger.flush()

        types = [event.get("type") for event in _delivered(sink)]
        assert "parent_after_fork" in types
        assert AsyncHealingLogger._worker_thread is not None
        assert AsyncHealingLogger._worker_thread.is_alive()


# =============================================================================
# 2. Parent-file integrity
# =============================================================================


class TestRealForkWalParentFileIntegrity:
    """A child must not write the parent's buffered bytes into the parent's
    file — the one failure mode a socket-backed handle does not have.
    """

    def test_a_buffered_parent_record_is_written_exactly_once(self, tmp_path):
        """The parent buffers a record (``sync_on_write=false``, so it is in
        Python's buffer, not on disk) and forks. Releasing the inherited handle
        by dropping the reference would run a finalizer that flushes it into
        the parent's file; the parent then writes it again itself, and the
        record appears twice.
        """
        marker = "parent_buffered_record_7f3a"
        wal = WriteAheadLog(
            config=WALConfig(
                wal_dir=str(tmp_path),
                sync_on_write=False,
                file_prefix="fork_integrity_wal",
            )
        )
        try:
            wal.write({"marker": marker})
            parent_file = wal._current_file
            assert parent_file is not None

            def child():
                # Any entry point repairs; this one also proves the child can
                # keep writing after the inherited handle is released.
                wal.write({"marker": "child_record"})

            status = _run_in_child(child)
            assert os.waitstatus_to_exitcode(status) == 0

            wal.flush()
            contents = parent_file.read_bytes()
        finally:
            wal.close()

        assert contents.count(marker.encode()) == 1
        assert b"child_record" not in contents

    def test_the_child_writes_into_its_own_pid_stamped_file(self, tmp_path):
        """PID isolation is preserved by the repair rather than broken by it:
        the child abandons the inherited handle and lazily opens its own file,
        which is what keeps the per-worker drain partitioning valid.
        """
        wal = WriteAheadLog(
            config=WALConfig(
                wal_dir=str(tmp_path),
                sync_on_write=True,
                file_prefix="fork_isolation_wal",
            )
        )
        try:
            wal.write({"marker": "parent"})
            parent_file = wal._current_file

            def child():
                wal.write({"marker": "child"})

            status = _run_in_child(child)
            assert os.waitstatus_to_exitcode(status) == 0

            files = sorted(tmp_path.glob("fork_isolation_wal_*.wal"))
        finally:
            wal.close()

        assert len(files) == 2
        assert parent_file in files
        child_files = [f for f in files if f != parent_file]
        assert b"child" in child_files[0].read_bytes()


# =============================================================================
# 3. Inherited-queue negative
# =============================================================================


class TestRealForkInheritedQueue:
    """The parent's live pipeline owns the events queued before the fork."""

    def test_a_pre_fork_event_is_delivered_once_across_the_process_group(
        self, logger_with_sink, sink
    ):
        """Each child inherits a *copy* of the queue. Flushing those copies
        would write one duplicate per worker into the audit trail, with nothing
        downstream to dedup them — the reason the repair abandons the inherited
        contents rather than draining them.
        """
        AsyncHealingLogger.log({"type": "queued_before_fork"}, EventSeverity.INFO)

        def child():
            AsyncHealingLogger.start()
            AsyncHealingLogger.log({"type": "child_own_event"}, EventSeverity.INFO)
            AsyncHealingLogger.flush()

        for _ in range(2):
            assert os.waitstatus_to_exitcode(_run_in_child(child)) == 0

        AsyncHealingLogger.flush()

        delivered = _delivered(sink)
        pre_fork = [e for e in delivered if e.get("type") == "queued_before_fork"]
        child_events = [e for e in delivered if e.get("type") == "child_own_event"]

        assert len(pre_fork) == 1
        assert pre_fork[0]["pid"] == os.getpid()
        # Each child still delivers its own event — the abandonment is scoped
        # to what it inherited, not to everything it enqueues.
        assert len(child_events) == 2
        assert len({e["pid"] for e in child_events}) == 2
