"""Integration: a failed ``dlq=True`` call is parked whatever primitive it took.

The call-site auto-extract keeps every whitelisted primitive argument in the
entry's ``request_data`` as is, and the store encodes that payload as JSON.
The two halves pass their own unit tests while still disagreeing: a ``Decimal``
amount, a ``bytes`` body or (on SQL) a ``UUID`` / ``date`` argument made the
store refuse the entry, which then left the queue for the local fallback. Only
the composition — decorator, sink, capture service and a real repository — can
show that the entry reaches the queue.

Mock-based (no infra): the Redis adapter runs on its in-memory degraded backend
and the SQL adapter on a ``sqlite:///`` file under ``tmp_path``. Each is
injected through the capture service's DI seam with both PRO registry slots
empty, and the outbox is switched off so the store runs on the calling thread.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID

import pytest

from baldur.adapters.redis.dlq import RedisDLQRepository
from baldur.adapters.resilient.backend import (
    ResilientStorageBackend,
    ResilientStorageMode,
    reset_storage_backend,
)
from baldur.adapters.sql.base import SchemaVersionManager
from baldur.adapters.sql.connection import build_connection_factory
from baldur.adapters.sql.failed_operation import SQLFailedOperationRepository
from baldur.interfaces.repositories import FailedOperationRepository
from baldur.protect_facade import protected
from baldur.settings.dlq_outbox import reset_dlq_outbox_settings
from baldur.settings.protect import reset_protect_settings
from baldur.settings.resilient_storage import ResilientStorageSettings
from baldur.settings.sql import reset_sql_settings

_REF = UUID("12345678-1234-5678-1234-567812345678")


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def redis_adapter_on_memory() -> Iterator[RedisDLQRepository]:
    """The Redis DLQ adapter over its in-memory degraded backend."""
    reset_storage_backend()
    with tempfile.TemporaryDirectory() as wal_dir:
        config = ResilientStorageSettings(
            redis_url="redis://nonexistent:6379/0",
            wal_dir=wal_dir,
            allow_memory_only=True,
        )
        with patch("baldur.adapters.cache.RedisCacheAdapter") as mock_adapter:
            mock_adapter.side_effect = Exception("Redis unavailable")
            backend = ResilientStorageBackend(config)
        assert backend.mode == ResilientStorageMode.DEGRADED
        try:
            yield RedisDLQRepository(backend)
        finally:
            backend.close()
            reset_storage_backend()


@pytest.fixture
def sql_on_sqlite_file(tmp_path, monkeypatch) -> Iterator[SQLFailedOperationRepository]:
    """The SQL DLQ adapter over a file-backed sqlite database."""
    monkeypatch.setenv("BALDUR_SQL_DSN", f"sqlite:///{tmp_path / 'dlq.db'}")
    reset_sql_settings()
    SchemaVersionManager._reset_applied_cache()
    yield SQLFailedOperationRepository(build_connection_factory())
    reset_sql_settings()
    SchemaVersionManager._reset_applied_cache()


@pytest.fixture(params=["redis_adapter_on_memory", "sql_on_sqlite_file"])
def parked_into(request, monkeypatch) -> Iterator[FailedOperationRepository]:
    """Route ``dlq=True`` capture, synchronously, into the parametrized store."""
    from baldur.factory.registry import ProviderRegistry
    from baldur.services.dlq_capture import service as capture_module
    from baldur.services.dlq_capture.service import (
        DLQCaptureService,
        reset_dlq_capture_service,
    )

    repository = request.getfixturevalue(request.param)
    monkeypatch.setenv("BALDUR_DLQ_OUTBOX_ENABLED", "false")
    reset_dlq_outbox_settings()
    reset_protect_settings()
    monkeypatch.setattr(ProviderRegistry.dlq_service, "safe_get", lambda: None)
    monkeypatch.setattr(ProviderRegistry.dlq_repository, "safe_get", lambda: None)
    monkeypatch.setattr(
        capture_module,
        "_capture_service",
        DLQCaptureService(repository=repository),
    )
    yield repository
    reset_dlq_capture_service()
    reset_dlq_outbox_settings()
    reset_protect_settings()


# =============================================================================
# Tests
# =============================================================================


class TestSnapshotPrimitiveCaptureRoundtrip:
    """Every whitelisted argument type reaches the queue, on both JSON stores."""

    def test_a_failed_call_with_non_json_native_arguments_is_parked(self, parked_into):
        # Given: a protected call whose arguments JSON cannot encode natively.
        @protected("charge", dlq=True, retry=False)
        def charge(order_id: str, amount: Decimal, raw: bytes, ref: UUID, due: date):
            raise RuntimeError("gateway down")

        # When: the call fails.
        with pytest.raises(RuntimeError, match="gateway down"):
            charge("o-1", Decimal("9.99"), b"xy", _REF, date(2026, 10, 1))

        # Then: its entry is in the queue, the snapshot in string form.
        entries = parked_into.find(domain="charge", limit=10)
        assert len(entries) == 1
        assert entries[0].request_data == {
            "order_id": "o-1",
            "amount": "9.99",
            "raw": "b'xy'",
            "ref": "12345678-1234-5678-1234-567812345678",
            "due": "2026-10-01",
        }
