"""
Durable dead-letter storage integration tests (778 D1/D5).

Two compositions, both about what happens to a captured failure once the
dead-letter backend is SQL:

    A. *Durability* — an entry stored through ``SQLFailedOperationRepository``
       survives losing every connection and every repository instance. The
       claim is only observable across that whole lifecycle: the repository,
       the connection factory and the lazy schema bootstrap share a
       transaction boundary and a file-backed store, and each of them passes
       its own unit tests while the entry still evaporates if any one of them
       keeps the data in memory.

    B. *Async capture composition* — the default capture path is
       ``DLQCaptureService.store_failure`` -> outbox ``RingBuffer`` -> outbox
       worker thread -> repository store, and the store is the part that can
       fail. Each component passes in isolation; the composition is what can
       drop an entry. With the store raising, the entry must land in the
       local fallback with a WARNING, never vanish.

No infra: a ``sqlite:///`` file DSN under ``tmp_path``, so no ``requires_*``
marker. The outbox is drained through its ``flush_and_wait`` seam rather than
by sleeping.
"""

from __future__ import annotations

import json

import pytest

from baldur.adapters.sql.base import SchemaVersionManager
from baldur.adapters.sql.connection import build_connection_factory
from baldur.adapters.sql.failed_operation import SQLFailedOperationRepository
from baldur.interfaces.repositories import FailedOperationStatus
from baldur.settings.sql import reset_sql_settings


@pytest.fixture
def sqlite_file_dsn(tmp_path, monkeypatch) -> str:
    """A file-backed sqlite DSN — the point is that it outlives a connection."""
    db_path = tmp_path / "dlq.db"
    dsn = f"sqlite:///{db_path}"
    monkeypatch.setenv("BALDUR_SQL_DSN", dsn)
    reset_sql_settings()
    SchemaVersionManager._reset_applied_cache()
    yield dsn
    reset_sql_settings()
    SchemaVersionManager._reset_applied_cache()


class TestSQLDlqDurabilityBehavior:
    """A parked call outlives the process that parked it."""

    def test_a_stored_entry_survives_losing_every_connection_and_instance(
        self, sqlite_file_dsn, tmp_path
    ):
        """The durability claim, end to end.

        The writer, its connection factory and its connections are all
        dropped between the two halves — the only thing carried across is
        the entry id, the way a restarted process carries nothing but the
        database on disk.
        """
        # Given: a failure captured through the SQL dead-letter repository.
        writer = SQLFailedOperationRepository(build_connection_factory())
        entry = writer.create(
            domain="payment",
            failure_type="gateway_timeout",
            entity_type="order",
            entity_id="4242",
            error_message="gateway did not answer",
        )
        entry_id = entry.id

        # When: everything holding the data in this process goes away.
        del writer
        del entry
        SchemaVersionManager._reset_applied_cache()

        # Then: a repository built from scratch still finds it.
        reader = SQLFailedOperationRepository(build_connection_factory())
        restored = reader.get_by_id(entry_id)

        assert restored is not None
        assert restored.domain == "payment"
        assert restored.entity_id == "4242"
        assert restored.error_message == "gateway did not answer"
        assert restored.status == FailedOperationStatus.PENDING.value
        # And the store really is the file, not a per-process cache.
        assert (tmp_path / "dlq.db").stat().st_size > 0

    def test_a_replay_claim_survives_the_same_way(self, sqlite_file_dsn):
        """State transitions are durable too, not just the initial insert.

        An entry that a worker claimed for replay and then died mid-flight is
        exactly the state an operator needs to still be there after the
        restart — otherwise the entry silently becomes replayable again.
        """
        writer = SQLFailedOperationRepository(build_connection_factory())
        entry = writer.create(domain="payment", failure_type="gateway_timeout")
        claimed = writer.try_acquire_for_replay(entry.id, max_retries=3)
        assert claimed is not None
        entry_id = entry.id
        del writer

        reader = SQLFailedOperationRepository(build_connection_factory())

        assert reader.get_by_id(entry_id).status == (
            FailedOperationStatus.REPLAYING.value
        )


class _StoreDownRepository(SQLFailedOperationRepository):
    """A SQL repository whose store is down — reads fine, writes raise.

    Narrower than patching the whole repository out: the overflow checks the
    capture path runs before storing still go to the real table, so the
    failure lands where a database outage actually lands.
    """

    def create(self, **kwargs):  # noqa: D102 — behavior stated on the class
        raise RuntimeError("could not connect to the database")


@pytest.fixture
def store_down_dlq_backend(sqlite_file_dsn, monkeypatch, tmp_path):
    """Wire the dead-letter registry to a SQL backend that cannot store.

    Yields the fallback file the capture path should end up writing to.
    """
    from baldur.factory.registry import ProviderRegistry
    from baldur.services.dlq_capture import service as capture_module
    from baldur.services.dlq_capture.service import reset_dlq_capture_service
    from baldur.services.dlq_outbox import outbox as outbox_module

    fallback_path = tmp_path / "baldur_dlq_fallback.jsonl"
    monkeypatch.setattr(capture_module, "DLQ_FALLBACK_PATH", fallback_path)

    # Tier 1 of the fallback chain is the LMDB disk buffer, which is present
    # in this environment and would absorb the entry before the JSONL tier is
    # reached. Closing it makes the tier under test the one that runs.
    monkeypatch.setattr(
        "baldur.audit.persistence.disk_buffer_adapter.DiskBufferAdapter.get_instance",
        classmethod(lambda cls: (_ for _ in ()).throw(ImportError("disabled"))),
    )

    outbox_module.reset_dlq_outbox()
    reset_dlq_capture_service()
    with ProviderRegistry.failed_op_repo.snapshot():
        ProviderRegistry.failed_op_repo.register(
            "sql", lambda: _StoreDownRepository(build_connection_factory())
        )
        ProviderRegistry.failed_op_repo.clear_instances()
        ProviderRegistry.failed_op_repo.set_default("sql")
        try:
            yield fallback_path
        finally:
            outbox_module.reset_dlq_outbox()
            reset_dlq_capture_service()


class TestAsyncCaptureFallbackComposition:
    """778 — the default async path composes with a failing store.

    The store being durable does not make the capture synchronous: with the
    outbox enabled (the default) ``store_failure`` returns before the write
    is attempted. What must hold is that the write's failure, when it comes,
    still reaches the local fallback from the worker thread.
    """

    def test_entries_enqueued_asynchronously_reach_the_local_fallback(
        self, store_down_dlq_backend
    ):
        from structlog.testing import capture_logs

        from baldur.services.dlq_capture.service import get_dlq_capture_service
        from baldur.services.dlq_outbox import outbox as outbox_module

        fallback_path = store_down_dlq_backend
        service = get_dlq_capture_service()

        # When: three failures are captured on the default (async) path and
        # the outbox is drained deterministically.
        with capture_logs() as logs:
            results = [
                service.store_failure(
                    domain="payment",
                    failure_type="gateway_timeout",
                    entity_type="order",
                    entity_id=str(4000 + i),
                    error_message="gateway did not answer",
                )
                for i in range(3)
            ]
            drained = outbox_module.get_outbox().flush_and_wait(timeout=5.0)

        # The producer returned before any write — that is the async contract,
        # and the reason the fallback has to work from the worker thread.
        assert all(r.success for r in results)
        assert all(r.dlq_id is None for r in results)

        # The drain is a barrier, not a completion count. ``flush_and_wait``
        # returns the ``entries_written`` delta, and a store write that the
        # local fallback rescued is a soft failure, not a write — so with the
        # store down the barrier settles at zero and the soft-failed bucket is
        # where the three entries are accounted. Reading the return value as
        # "nothing was lost" is precisely the claim it cannot carry.
        assert drained == 0
        assert outbox_module.get_outbox().get_stats().entries_soft_failed == 3

        # Then: every entry is on disk in the fallback, none silently dropped.
        lines = fallback_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3
        entity_ids = {json.loads(line)["entry_data"]["entity_id"] for line in lines}
        assert entity_ids == {"4000", "4001", "4002"}

        # And the operator is told, at WARNING, that this happened.
        saved = [e for e in logs if e.get("event") == "dlq.fallback_local_file_saved"]
        assert len(saved) == 3
        assert all(e["log_level"] == "warning" for e in saved)

    def test_a_synchronous_capture_reports_the_fallback_to_its_caller(
        self, store_down_dlq_backend
    ):
        """``mode="sync"`` is the opt-in for callers that need the verdict.

        The async path can only report "enqueued"; a caller that needs to
        know the entry did not reach the store has to ask synchronously.
        """
        from baldur.services.dlq_capture.service import get_dlq_capture_service

        fallback_path = store_down_dlq_backend

        result = get_dlq_capture_service().store_failure(
            domain="payment",
            failure_type="gateway_timeout",
            entity_id="5000",
            error_message="gateway did not answer",
            mode="sync",
        )

        assert result.success is False
        assert result.is_fallback is True
        assert str(fallback_path) in (result.fallback_path or "")
        assert fallback_path.exists()
