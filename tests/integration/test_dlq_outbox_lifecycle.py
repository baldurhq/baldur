"""DLQ outbox end-to-end mock-based integration test (impl doc 486).

Wires the full producer→outbox→worker→DLQService→InMemory repository chain
without infrastructure. Asserts on actual repository state to verify that
the kwargs serialization across the RingBuffer + thread boundary is intact.

Mock-based — no Docker. Uses ``InMemoryFailedOperationRepository`` injected
into a ``DLQService`` instance, then plugs the test sync_writer into the
outbox so it dispatches to that service.
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from collections.abc import Iterator

import pytest

from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.audit.ring_buffer import RingBuffer
from baldur.services.dlq_outbox import outbox as outbox_module
from baldur.services.dlq_outbox.outbox import Outbox
from baldur.services.dlq_outbox.worker import DLQOutboxWorker
from baldur.settings.backpressure import BackpressureStrategy
from baldur_pro.services.dlq import DLQService, reset_dlq_service

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def in_memory_dlq_repo() -> Iterator[InMemoryFailedOperationRepository]:
    """Provide an in-memory DLQ repository, reset the DLQ singleton on entry/exit."""
    reset_dlq_service()
    repo = InMemoryFailedOperationRepository()

    import baldur_pro.services.dlq as dlq_pkg

    dlq_pkg._dlq_service = DLQService(repository=repo)
    yield repo
    reset_dlq_service()


@pytest.fixture
def started_outbox():
    """Build, install, and start a real Outbox wired to the test DLQService.

    The sync_writer dispatches to ``DLQService.store_failure(mode='sync', ...)``
    so that the InMemory repo records the entry.
    """
    from baldur_pro.services.dlq import get_dlq_service

    def sync_writer(kwargs):
        return get_dlq_service().store_failure(mode="sync", **kwargs)

    buffer: RingBuffer = RingBuffer(
        capacity=100,
        strategy=BackpressureStrategy.DROP_OLDEST,
    )
    worker = DLQOutboxWorker(
        buffer=buffer,
        sync_writer=sync_writer,
        batch_size=5,
        flush_interval_seconds=0.01,
    )
    outbox = Outbox(buffer=buffer, worker=worker)
    outbox.start()
    outbox_module._outbox = outbox

    yield outbox

    try:
        outbox.stop(timeout=1.0)
    except Exception:
        pass
    outbox_module._outbox = None
    outbox_module._worker_dead = False
    outbox_module._worker_dead_coercions = 0


# =============================================================================
# E2E — async dispatch lands in repository
# =============================================================================


class TestDlqOutboxRepositoryE2E:
    """Producer → outbox → worker → DLQService → repository."""

    def test_async_dispatch_writes_through_outbox_to_repository(
        self, in_memory_dlq_repo, started_outbox
    ):
        # Given
        from baldur_pro.services.dlq import store_to_dlq

        # When — async dispatch (default per ``BALDUR_DLQ_OUTBOX_ENABLED=true``)
        result = store_to_dlq(
            domain="payment",
            failure_type="PG_TIMEOUT",
            error_message="external pg timed out",
            mode="async",
        )

        # Then — async path returned dlq_id=None
        assert result.success is True
        assert result.dlq_id is None

        # Block until the worker has fully drained. D6 made flush_and_wait
        # in_flight-aware, so it returns only once size==0 AND in_flight==0 —
        # i.e. the through-write to the repository has settled. This replaces
        # the former manual storage poll (which raced the pop->increment
        # window and made this test flake ~12% of the time, impl doc 559 G3).
        started_outbox.flush_and_wait(timeout=3.0)

        # Then — repository observed the write
        assert len(in_memory_dlq_repo._storage) == 1
        stored = list(in_memory_dlq_repo._storage.values())[0]
        assert stored.domain == "payment"
        assert stored.failure_type == "PG_TIMEOUT"

        # And — the conservation invariant (impl doc 559 D2/D6) holds: every
        # enqueued entry is accounted for as written / failed / dropped /
        # still-buffered / in-flight / emergency-dumped, with zero silent
        # worker-loop loss. After flush_and_wait both size and in_flight are 0
        # and no shutdown dump fired, so the entry must show up in
        # entries_written. A regression (the discarded-partial-batch bug) would
        # leave total_enqueued > the sum, failing deterministically.
        stats = started_outbox.get_stats()
        assert stats.size == 0
        assert stats.in_flight == 0
        assert stats.entries_emergency_dumped == 0
        assert stats.entries_written == 1
        assert stats.total_enqueued == (
            stats.entries_written
            + stats.entries_failed
            + stats.total_dropped
            + stats.size
            + stats.in_flight
            + stats.entries_emergency_dumped
        )

    def test_sync_dispatch_returns_real_dlq_id(
        self, in_memory_dlq_repo, started_outbox
    ):
        # Given
        from baldur_pro.services.dlq import store_to_dlq

        # When — explicit sync mode bypasses outbox
        result = store_to_dlq(
            domain="payment",
            failure_type="PG_TIMEOUT",
            mode="sync",
        )

        # Then — sync path returned the real id
        assert result.success is True
        assert result.dlq_id is not None
        # Repository synchronously updated
        assert len(in_memory_dlq_repo._storage) == 1


# =============================================================================
# E2E — reset chain (D8) drains + clears outbox state
# =============================================================================


class TestDlqOutboxResetChainE2E:
    """``reset_protect_caches`` invokes ``reset_dlq_outbox`` (D8)."""

    def test_reset_chain_drains_and_clears_singleton(
        self, in_memory_dlq_repo, started_outbox
    ):
        # Given
        from baldur.protect_facade import reset_protect_caches
        from baldur_pro.services.dlq import store_to_dlq

        for i in range(3):
            store_to_dlq(
                domain="payment",
                failure_type=f"E{i}",
                mode="async",
            )

        # When — reset chain runs (clears caches + outbox)
        reset_protect_caches()

        # Then — outbox singleton is cleared
        assert outbox_module._outbox is None
        # And worker_dead state is reset
        assert outbox_module._worker_dead is False
        assert outbox_module._worker_dead_coercions == 0
