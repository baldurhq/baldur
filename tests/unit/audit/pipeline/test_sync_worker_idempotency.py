"""
AuditSyncWorker Idempotent Consumer tests.

Test scope:
1. ``_sync_entry_to_adapter()`` integration with ``IdempotencyService`` against
   the **real** ``IdempotencyResult`` contract — the value-based
   ``result.is_duplicate`` dedup gate (the inverted ``result is not None`` gate
   skipped every first write) + the canonical ``mark_as_processed`` post-write
   marking (replacing the ``check()``-as-setter hack).
2. Duplicate-entry skip behavior.
3. Central-outage -> recovery WAL-replay zero-loss: an entry buffered in WAL
   while central is down is re-written after recovery, and the WAL file is not
   unlinked before the successful central write.
4. No-adapter-wired guard: a drain with no central destination advances neither
   the cursor nor deletes the WAL, and warns once per unwired episode.
"""

from __future__ import annotations

import glob
import os
import sys
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.audit.sync_worker import AuditSyncWorker, SyncWorkerConfig
from baldur.audit.wal import WriteAheadLog
from baldur.audit.wal._models import WALConfig
from baldur.interfaces.audit_adapter import AuditEntry
from baldur.services.idempotency import IdempotencyService
from baldur.services.idempotency.models import (
    IdempotencyDomain,
    IdempotencyResult,
)

# Module path the worker imports IdempotencyService from.
IDEMPOTENCY_MODULE = "baldur.services.idempotency"


@dataclass
class MockWALEntry:
    """Test WAL entry (mirrors the WALEntry attributes the worker reads)."""

    sequence: int
    checksum: str
    data: dict


def _make_worker() -> AuditSyncWorker:
    """A directly-constructed worker (no singleton pollution)."""
    return AuditSyncWorker(config=SyncWorkerConfig(max_retries=0))


@pytest.fixture
def fresh_idempotency_cache():
    """Route every IdempotencyService instance built inside the worker to a
    single per-test in-memory cache.

    IdempotencyService resolves a process-wide cache (the registry-level
    in-memory fallback when no Redis), so the check -> mark_as_processed ->
    check dedup sequence would otherwise leak across tests on a shared key.
    Patching ``_get_cache`` to a fresh ``InMemoryCacheAdapter`` gives the real
    ``check()`` / ``mark_as_processed()`` logic full per-test isolation while
    exercising the genuine ``IdempotencyResult``-returning contract (not a
    ``check()``->``None`` mock).
    """
    cache = InMemoryCacheAdapter(key_prefix="test_sync_worker_idem:")
    with patch.object(IdempotencyService, "_get_cache", return_value=cache):
        yield cache


@pytest.fixture
def real_wal(tmp_path):
    """A real file-based WAL over ``tmp_path`` (own-PID files, drained by
    ``mode="runtime"``)."""
    wal = WriteAheadLog(
        config=WALConfig(
            wal_dir=str(tmp_path),
            file_prefix="sw_idem",
            sync_on_write=True,
        )
    )
    yield wal
    wal.close()


class TestAuditSyncWorkerIdempotency:
    """Value-based dedup gate + canonical ``mark_as_processed`` (D1/D2)."""

    def test_first_write_writes_each_entry_once(self, fresh_idempotency_cache):
        """SC1: a functional IdempotencyService (real IdempotencyResult
        contract, no ``check()``->``None`` mock) -> ``adapter.log`` is called
        exactly once per never-before-seen entry, each time with a converted
        ``AuditEntry`` (not the raw WAL dict).

        The pre-fix ``result is not None`` gate skipped every first write
        (``IdempotencyResult(...) is not None`` is always True); the value-based
        ``result.is_duplicate`` gate writes them.
        """
        adapter = MagicMock()
        worker = _make_worker()
        entries = [
            MockWALEntry(sequence=s, checksum=f"cs{s:06d}", data={"event": f"e{s}"})
            for s in (1, 2, 3)
        ]

        for entry in entries:
            worker._sync_entry_to_adapter(adapter, entry)

        assert adapter.log.call_count == 3
        logged = [c.args[0] for c in adapter.log.call_args_list]
        assert all(isinstance(e, AuditEntry) for e in logged)
        assert [e.details["event"] for e in logged] == ["e1", "e2", "e3"]

    def test_duplicate_skipped_second_sync(self, fresh_idempotency_cache):
        """SC2: a second sync of the same entry (same sequence+checksum) is
        skipped — no duplicate ``adapter.write``.

        The first sync marks the entry via the canonical ``mark_as_processed``;
        the second ``check`` reports ``is_duplicate=True`` against the real
        contract.
        """
        adapter = MagicMock()
        worker = _make_worker()
        entry = MockWALEntry(sequence=1, checksum="abcd1234", data={"event": "test"})

        worker._sync_entry_to_adapter(adapter, entry)
        worker._sync_entry_to_adapter(adapter, entry)

        adapter.log.assert_called_once()
        logged = adapter.log.call_args.args[0]
        assert isinstance(logged, AuditEntry)
        assert logged.details["event"] == "test"

    def test_marking_uses_mark_as_processed_setter(self):
        """D2: the post-write marking calls the canonical
        ``mark_as_processed(key)`` — NOT ``check()``-as-setter — reusing the
        exact ``key`` object built by the dedup check.
        """
        adapter = MagicMock()
        worker = _make_worker()
        entry = MockWALEntry(sequence=1, checksum="abcd1234", data={"event": "test"})

        with patch(f"{IDEMPOTENCY_MODULE}.IdempotencyService") as mock_service:
            instance = mock_service.return_value
            instance.check.return_value = IdempotencyResult(is_duplicate=False)

            worker._sync_entry_to_adapter(adapter, entry)

        adapter.log.assert_called_once()
        instance.mark_as_processed.assert_called_once()
        # check() is used purely as a read (single positional key, no setter
        # lambda), and the same key object is reused for the marking write.
        assert instance.check.call_count == 1
        assert (
            instance.check.call_args.args[0]
            is instance.mark_as_processed.call_args.args[0]
        )

    def test_sync_entry_skips_when_check_reports_duplicate(self):
        """A pre-marked entry (``check`` reports ``is_duplicate=True`` up front)
        is skipped before any write — the gate the bug got wrong (it tested
        ``result is not None``, which is always True).
        """
        adapter = MagicMock()
        worker = _make_worker()
        entry = MockWALEntry(sequence=1, checksum="abcd1234", data={"event": "test"})

        with patch(f"{IDEMPOTENCY_MODULE}.IdempotencyService") as mock_service:
            mock_service.return_value.check.return_value = IdempotencyResult(
                is_duplicate=True
            )

            worker._sync_entry_to_adapter(adapter, entry)

        adapter.log.assert_not_called()

    def test_sync_entry_works_without_idempotency_service(self):
        """Fail-open: an IdempotencyService import failure -> the entry is still
        written (dedup is best-effort; durability is not). The post-write
        marking is skipped (no service to mark with), not a crash.
        """
        adapter = MagicMock()
        worker = _make_worker()
        entry = MockWALEntry(sequence=1, checksum="abcd1234", data={"event": "test"})

        # Simulate IdempotencyService import failure by removing the modules.
        original_modules = {}
        modules_to_remove = [k for k in sys.modules if "idempotency" in k.lower()]
        for mod in modules_to_remove:
            original_modules[mod] = sys.modules.pop(mod, None)

        try:
            with patch.dict(sys.modules, {"baldur.services.idempotency": None}):
                worker._sync_entry_to_adapter(adapter, entry)
        finally:
            for mod, val in original_modules.items():
                if val is not None:
                    sys.modules[mod] = val

        adapter.log.assert_called_once()

    def test_sync_entry_uses_wal_recovery_domain(self):
        """The dedup key is built for the ``WAL_RECOVERY`` domain over the
        entry's sequence + checksum prefix. The check returns a real
        (non-duplicate) ``IdempotencyResult`` so the write path runs.
        """
        adapter = MagicMock()
        worker = _make_worker()
        entry = MockWALEntry(sequence=42, checksum="efgh5678", data={"event": "test"})

        with (
            patch(f"{IDEMPOTENCY_MODULE}.IdempotencyService") as mock_service,
            patch(f"{IDEMPOTENCY_MODULE}.IdempotencyKey") as mock_key_class,
        ):
            mock_service.return_value.check.return_value = IdempotencyResult(
                is_duplicate=False
            )

            worker._sync_entry_to_adapter(adapter, entry)

            mock_key_class.for_operation.assert_called_once()
            kwargs = mock_key_class.for_operation.call_args.kwargs
            assert kwargs["entity_type"] == "wal_entry"
            assert kwargs["entity_id"] == 42
            assert kwargs["operation"] == "sync:efgh5678"
            assert kwargs["domain"] == IdempotencyDomain.WAL_RECOVERY


class TestSyncWorkerOutageRecoveryZeroLoss:
    """D3: the WAL -> central drain is the durability replay path. An
    entry buffered in WAL while central is down MUST reach central after
    recovery, and the WAL file MUST NOT be unlinked before the successful
    central write.
    """

    class _FlakyAdapter:
        """Raises on the first ``fail_times`` log calls, then records entries.

        Defines ``log`` (the ``AuditLogAdapter`` contract), not ``write`` — a
        ``write``-only double would fall to the worker's structlog else-branch
        and never simulate a central failure.
        """

        def __init__(self, fail_times: int):
            self._fail_times = fail_times
            self.calls = 0
            self.received: list = []

        def log(self, entry: AuditEntry) -> None:
            self.calls += 1
            if self.calls <= self._fail_times:
                raise RuntimeError("central down")
            self.received.append(entry)

    def test_outage_recovery_zero_loss(
        self, real_wal, tmp_path, fresh_idempotency_cache
    ):
        adapter = self._FlakyAdapter(fail_times=1)
        worker = AuditSyncWorker(
            wal=real_wal,
            central_adapter=adapter,
            config=SyncWorkerConfig(max_retries=0),
        )
        real_wal.write({"event": "audit_e1"})

        # Central down: the write fails, nothing is delivered, the WAL is kept.
        synced1, failed1 = worker.sync_now()
        assert (synced1, failed1) == (0, 1)
        assert worker._last_processed_seq == 0
        assert adapter.received == []
        # The WAL file is NOT unlinked before a successful central write.
        assert len(glob.glob(os.path.join(str(tmp_path), "*.wal"))) == 1

        # Central recovers: the same buffered entry is re-read and delivered
        # as a converted AuditEntry (not the raw WAL dict).
        synced2, failed2 = worker.sync_now()
        assert (synced2, failed2) == (1, 0)
        assert worker._last_processed_seq == 1
        assert len(adapter.received) == 1
        assert isinstance(adapter.received[0], AuditEntry)
        assert adapter.received[0].details["event"] == "audit_e1"


class TestSyncWorkerNoAdapter:
    """D3: no central destination wired -> the drain is a no-op that preserves
    the WAL (no zero-write drain) and warns once per unwired episode.
    """

    def test_no_adapter_does_not_drain_or_delete_wal(self, real_wal, tmp_path):
        real_wal.write({"event": "audit_e1"})
        worker = AuditSyncWorker(
            wal=real_wal,
            central_adapter=None,
            config=SyncWorkerConfig(max_retries=0),
        )

        # _get_adapter falls back to the registry's 'null' audit adapter, so the
        # unwired state must be forced explicitly.
        with (
            patch.object(worker, "_get_adapter", return_value=None),
            patch("baldur.audit.sync_worker.logger") as mock_logger,
        ):
            synced1, failed1 = worker.sync_now()
            synced2, failed2 = worker.sync_now()

        assert (synced1, failed1) == (0, 0)
        assert (synced2, failed2) == (0, 0)
        assert worker._last_processed_seq == 0
        # WAL retained — no zero-write drain.
        assert len(glob.glob(os.path.join(str(tmp_path), "*.wal"))) == 1
        # Edge-triggered WARNING: exactly once across two consecutive drains.
        unwired = [
            c
            for c in mock_logger.warning.call_args_list
            if c.args and c.args[0] == "audit_sync_worker.central_adapter_unwired"
        ]
        assert len(unwired) == 1
