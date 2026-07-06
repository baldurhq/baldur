"""AuditSyncWorker delivery-type tests (669 D1/D2).

``_sync_entry_to_adapter`` is the WAL-drain (Pipeline A) delivery point. The
669 fix makes it convert the raw WAL dict to an ``AuditEntry`` via
``AuditEntry.from_wal_dict()`` and hand that to ``adapter.log()`` — matching
what Pipeline B (``continuous_audit``) already delivers. This module pins:

- The adapter's ``log()`` receives an ``AuditEntry`` instance, not a raw dict.
- The dead ``write`` dispatch branch is gone (D1): a double exposing BOTH
  ``write`` and ``log`` (a ``MagicMock``) is driven through ``log`` only —
  pre-fix it would have taken the ``write`` branch.
- A non-adapter object (no ``log``) falls to the structlog ``else`` emit.

The end-to-end WAL-replay lifecycle (real ``WriteAheadLog``, cursor-hold,
file cleanup) lives in ``test_sync_worker_idempotency.py`` and
``wal/test_audit_wal_zero_loss.py``; here the worker method is driven directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from baldur.audit.sync_worker import AuditSyncWorker, SyncWorkerConfig
from baldur.interfaces.audit_adapter import AuditEntry
from baldur.services.idempotency.models import IdempotencyResult

# Module path the worker imports IdempotencyService from.
IDEMPOTENCY_MODULE = "baldur.services.idempotency"

# A fixed, clearly-past epoch so timestamp preservation is unmistakable.
_FIXED_DT = datetime(2020, 6, 15, 12, 0, 0, tzinfo=UTC)
_FIXED_EPOCH = _FIXED_DT.timestamp()


@dataclass
class MockWALEntry:
    """Test WAL entry (mirrors the WALEntry attributes the worker reads)."""

    sequence: int
    checksum: str
    data: dict


def _native_wal_entry(**overrides) -> dict:
    """Minimal native-shape WAL payload (``event_type`` + float epoch)."""
    entry = {
        "event_type": "CB_STATE_CHANGE",
        "record_id": "audit-abc123",
        "timestamp": _FIXED_EPOCH,
        "details": {"old_state": "closed"},
    }
    entry.update(overrides)
    return entry


def _make_worker() -> AuditSyncWorker:
    """A directly-constructed worker (no singleton pollution)."""
    return AuditSyncWorker(config=SyncWorkerConfig(max_retries=0))


def _non_duplicate_idempotency():
    """Patch IdempotencyService so the dedup gate always admits the write.

    Keeps each test self-contained (no shared process-wide dedup cache to
    leak across tests); the write path is what these tests exercise.
    """
    ctx = patch(f"{IDEMPOTENCY_MODULE}.IdempotencyService")
    return ctx


class TestSyncEntryDelivery:
    """The adapter receives a converted ``AuditEntry`` via ``log()`` (669)."""

    def test_adapter_log_receives_audit_entry_not_dict(self):
        """The delivered argument is an ``AuditEntry`` instance — the native
        WAL dict is converted before ``log()`` (the whole point of 669)."""
        adapter = MagicMock()
        worker = _make_worker()
        entry = MockWALEntry(sequence=1, checksum="cs000001", data=_native_wal_entry())

        with _non_duplicate_idempotency() as mock_service:
            mock_service.return_value.check.return_value = IdempotencyResult(
                is_duplicate=False
            )
            worker._sync_entry_to_adapter(adapter, entry)

        adapter.log.assert_called_once()
        delivered = adapter.log.call_args.args[0]
        assert isinstance(delivered, AuditEntry)
        # The native shape was routed through from_wal_dict: event_type->action,
        # float epoch preserved.
        assert delivered.action == "CB_STATE_CHANGE"
        assert delivered.timestamp == _FIXED_DT
        assert delivered.details["old_state"] == "closed"

    def test_dead_write_branch_is_not_invoked(self):
        """D1: the removed ``write`` dispatch means a ``MagicMock`` (which
        auto-creates BOTH ``.write`` and ``.log``) is driven through ``log``
        only. Pre-fix, ``hasattr(adapter, "write")`` sent it to ``write``."""
        adapter = MagicMock()
        worker = _make_worker()
        entry = MockWALEntry(sequence=2, checksum="cs000002", data=_native_wal_entry())

        with _non_duplicate_idempotency() as mock_service:
            mock_service.return_value.check.return_value = IdempotencyResult(
                is_duplicate=False
            )
            worker._sync_entry_to_adapter(adapter, entry)

        adapter.log.assert_called_once()
        adapter.write.assert_not_called()

    def test_non_adapter_object_falls_to_structlog_else(self):
        """An object without ``log`` (not an ``AuditLogAdapter``) takes the
        structlog ``else`` branch — emitted, not crashed."""
        worker = _make_worker()
        entry = MockWALEntry(sequence=3, checksum="cs000003", data=_native_wal_entry())

        non_adapter = object()  # no .log attribute

        with (
            _non_duplicate_idempotency() as mock_service,
            patch("baldur.audit.sync_worker.logger") as mock_logger,
        ):
            mock_service.return_value.check.return_value = IdempotencyResult(
                is_duplicate=False
            )
            # Must not raise — the else-branch is a safe fallback.
            worker._sync_entry_to_adapter(non_adapter, entry)

        events = [c.args[0] for c in mock_logger.info.call_args_list if c.args]
        assert "audit_sync.event" in events
