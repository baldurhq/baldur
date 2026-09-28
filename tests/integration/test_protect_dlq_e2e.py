"""End-to-end mock-based integration test for ``protect(dlq=True)`` persistence (#466).

Wires the full failure-path: ``protect()`` → ``PolicyComposer`` → (``RetryPolicy``)
→ ``DLQSink`` → ``store_to_dlq`` → outbox → worker → ``DLQService`` →
``InMemoryFailedOperationRepository``.

Test Categories:
    A. Retry stage present (``retry=``):
        - retry exhaustion persists one entry under the retry domain
        - the retry history travels into the entry's metadata
        - a successful call persists nothing
    B. No retry stage (``dlq=True`` without ``retry=``):
        - a failed call persists one entry under the protect name
        - a call the wall-clock bound cut off persists one entry
        - an async failed call persists one entry through the same outbox

Async dispatch (impl doc 486)
-----------------------------
``DLQSink.handle_failure`` calls ``store_to_dlq`` WITHOUT a ``mode`` kwarg, so
``DLQService.store_failure`` resolves ``mode=None`` against
``BALDUR_DLQ_OUTBOX_ENABLED`` — which defaults to ``True`` (the async-default
flip per plan 2026-05-08). The failure therefore lands in the RingBuffer outbox
and is persisted to the repository **asynchronously** by the worker thread, not
synchronously on the calling thread. The test reflects that production reality:
it waits for the worker drain before asserting on repository state.

The ``started_outbox`` fixture installs an ``Outbox`` whose ``sync_writer``
dispatches to ``get_dlq_service().store_failure(mode="sync", ...)`` so the worker
drains into the in-memory ``DLQService`` swapped in by ``in_memory_dlq_repo``.
(The production default sync_writer resolves the service via
``ProviderRegistry.dlq_service``, which carries no OSS-default instance — the
injected writer is the established test seam, mirroring
``test_dlq_outbox_lifecycle.py``.)

Pre-fix #466 regression class: ``RetryPolicy.metadata['should_dlq']`` was lost in
the composer's outer catch branch, so ``DLQSink.handle_failure`` short-circuited
and nothing was ever enqueued — neither sync nor async. Asserting on the resulting
repository entry (count + contents) keeps this the strongest regression guard
against the metadata-propagation bug class.

Mock-based — no Docker.
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


import asyncio
import threading
import time
from collections.abc import Iterator

import pytest

from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.audit.ring_buffer import RingBuffer
from baldur.core.exceptions import TimeoutPolicyError
from baldur.protect_facade import protect, protected
from baldur.services.dlq_outbox import outbox as outbox_module
from baldur.services.dlq_outbox.outbox import Outbox
from baldur.services.dlq_outbox.worker import DLQOutboxWorker
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.settings.backpressure import BackpressureStrategy
from baldur_pro.services.dlq import DLQService, reset_dlq_service


@pytest.fixture
def in_memory_dlq_repo() -> Iterator[InMemoryFailedOperationRepository]:
    """Reset the DLQ singleton, swap in an in-memory repository."""
    reset_dlq_service()
    repo = InMemoryFailedOperationRepository()
    # Re-bind the singleton to a service that uses our in-memory repo.
    import baldur_pro.services.dlq as dlq_pkg

    dlq_pkg._dlq_service = DLQService(repository=repo)
    yield repo
    reset_dlq_service()


@pytest.fixture
def started_outbox() -> Iterator[Outbox]:
    """Install + start a real Outbox wired to the test ``DLQService``.

    The ``sync_writer`` dispatches to ``get_dlq_service().store_failure(
    mode='sync', ...)`` (resolved lazily at drain time) so worker drains land in
    the in-memory repo swapped in by ``in_memory_dlq_repo``. A short
    ``flush_interval`` keeps the drain prompt for the poll loop.
    """
    from baldur_pro.services.dlq import get_dlq_service

    def sync_writer(kwargs: dict) -> object:
        return get_dlq_service().store_failure(mode="sync", **kwargs)

    buffer: RingBuffer = RingBuffer(
        capacity=100,
        strategy=BackpressureStrategy.DROP_OLDEST,
    )
    # batch_size=1 makes the drain deterministic: any popped non-empty batch
    # satisfies len(batch) >= batch_size, so the worker's should_flush is always
    # True and never discards a sub-threshold batch. This isolates the path
    # under test (protect -> DLQSink -> outbox -> repo) from the worker's
    # batching cadence. (The worker drops a partial batch popped within
    # flush_interval of the last flush — tracked separately as a #486 worker bug.)
    worker = DLQOutboxWorker(
        buffer=buffer,
        sync_writer=sync_writer,
        batch_size=1,
        flush_interval_seconds=0.01,
    )
    outbox = Outbox(buffer=buffer, worker=worker)
    outbox.start()
    outbox_module._outbox = outbox
    # Producer-side fail-open flag must start clear so the async fast path is
    # taken (a prior test's dead-worker flag would coerce dispatch to sync).
    outbox_module._worker_dead = False

    yield outbox

    try:
        outbox.stop(timeout=1.0)
    except Exception:
        pass
    outbox_module._outbox = None
    outbox_module._worker_dead = False
    outbox_module._worker_dead_coercions = 0


def _retry_cfg(*, max_attempts: int = 2, domain: str = "e2e") -> RetryPolicyConfig:
    return RetryPolicyConfig(
        max_attempts=max_attempts,
        backoff_base=0,
        backoff_max=0,
        jitter_percent=0,
        enable_dlq=True,
        domain=domain,
    )


def _wait_for_repo_count(
    repo: InMemoryFailedOperationRepository,
    expected: int,
    timeout: float = 3.0,
) -> None:
    """Block until the worker has drained ``expected`` entries into ``repo``.

    Polls ``count_all()`` rather than the worker's ``entries_written`` /
    ``flush_and_wait`` return value: the worker pops a batch off the buffer
    BEFORE the repo write completes, so a buffer-size signal can fire while the
    write is still in flight. ``count_all()`` observes the persisted end state.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and repo.count_all() < expected:
        time.sleep(0.02)


# =============================================================================
# E2E — DLQ repository receives a write on Retry exhaustion (async outbox path)
# =============================================================================


class TestProtectDlqRepositoryE2E:
    """Repository-backed verification: a write actually lands (via the outbox)."""

    def test_repository_receives_entry_after_retry_exhaustion(
        self,
        in_memory_dlq_repo: InMemoryFailedOperationRepository,
        started_outbox: Outbox,
    ):
        def always_fails() -> None:
            raise ValueError("always-fails")

        with pytest.raises(ValueError):
            protect(
                "e2e.charge",
                always_fails,
                dlq=True,
                retry=_retry_cfg(domain="e2e_charge"),
                circuit_breaker=False,
                timeout=None,
            )

        # The DLQSink dispatched through the outbox — wait for the worker drain.
        _wait_for_repo_count(in_memory_dlq_repo, 1)

        # Repository now has exactly one entry — pre-fix this would be 0.
        assert in_memory_dlq_repo.count_all() == 1
        assert in_memory_dlq_repo.count_by_domain("e2e_charge") == 1

        # Pull the entry directly from the index by domain to verify contents.
        pending = in_memory_dlq_repo.get_pending_by_domain("e2e_charge", limit=10)
        assert len(pending) == 1
        entry = pending[0]
        assert entry.domain == "e2e_charge"
        # DLQSink._store_to_dlq composes failure_type as "MAX_RETRIES_<TYPENAME>".
        assert entry.failure_type == "MAX_RETRIES_VALUEERROR"
        assert entry.error_message == "always-fails"

    def test_repository_metadata_includes_retry_history(
        self,
        in_memory_dlq_repo: InMemoryFailedOperationRepository,
        started_outbox: Outbox,
    ):
        attempts: list[int] = []

        def fails_with_history() -> None:
            attempts.append(1)
            raise RuntimeError(f"attempt-{len(attempts)}")

        with pytest.raises(RuntimeError):
            protect(
                "e2e.history",
                fails_with_history,
                dlq=True,
                retry=_retry_cfg(max_attempts=3, domain="e2e_history"),
                circuit_breaker=False,
                timeout=None,
            )

        _wait_for_repo_count(in_memory_dlq_repo, 1)

        pending = in_memory_dlq_repo.get_pending_by_domain("e2e_history", limit=10)
        assert len(pending) == 1
        entry = pending[0]

        # RetryPolicy.metadata['retry_history'] flows through DLQSink._build_dlq_metadata
        # into the repo entry. Final attempt count == max_attempts.
        meta = entry.metadata or {}
        assert meta.get("max_attempts") == 3
        assert meta.get("domain") == "e2e_history"
        history = meta.get("retry_history") or []
        assert len(history) == 3  # one history record per attempt

    def test_repository_unchanged_when_fn_succeeds(
        self,
        in_memory_dlq_repo: InMemoryFailedOperationRepository,
        started_outbox: Outbox,
    ):
        result = protect(
            "e2e.success",
            lambda: "ok",
            dlq=True,
            retry=_retry_cfg(domain="e2e_success"),
            circuit_breaker=False,
            timeout=None,
        )

        assert result == "ok"
        # Success path never reaches the DLQSink, so nothing is enqueued. Give
        # the worker a drain window to prove no stray entry appears.
        _wait_for_repo_count(in_memory_dlq_repo, 1, timeout=0.3)
        assert in_memory_dlq_repo.count_all() == 0


# =============================================================================
# E2E — no retry stage: the single attempt's failure or timeout is persisted
# =============================================================================


class TestProtectUnretriedDlqRepositoryE2E:
    """``dlq=True`` without ``retry=`` — the README profile.

    Validates:
    - the composer's verdict and the sink's read of it share one terminal, so
      the entry reaches the repository through the outbox
    - the entry is filed under the protect name, not the ``"default"`` bucket
    - the call-site arguments travel with it, and ``max_attempts`` says the
      call ran once
    """

    def test_repository_receives_entry_for_a_failed_call(
        self,
        in_memory_dlq_repo: InMemoryFailedOperationRepository,
        started_outbox: Outbox,
    ):
        """
        Purpose:
            Verify a raising call on the no-retry profile is persisted.
        Expected:
            - exactly one entry, under the protect name
            - failure_type MAX_RETRIES_RUNTIMEERROR with the error message
            - request_data from the call site; metadata max_attempts == 1
        """

        @protected(
            "e2e_unretried_summarize", dlq=True, circuit_breaker=False, timeout=None
        )
        def summarize(doc_id: str) -> str:
            raise RuntimeError("upstream 500")

        with pytest.raises(RuntimeError):
            summarize("doc-7")

        _wait_for_repo_count(in_memory_dlq_repo, 1)

        assert in_memory_dlq_repo.count_all() == 1
        pending = in_memory_dlq_repo.get_pending_by_domain(
            "e2e_unretried_summarize", limit=10
        )
        assert len(pending) == 1
        entry = pending[0]
        assert entry.failure_type == "MAX_RETRIES_RUNTIMEERROR"
        assert entry.error_message == "upstream 500"
        assert entry.request_data == {"doc_id": "doc-7"}
        assert (entry.metadata or {}).get("max_attempts") == 1

    def test_repository_receives_entry_for_a_call_cut_off_by_the_bound(
        self,
        in_memory_dlq_repo: InMemoryFailedOperationRepository,
        started_outbox: Outbox,
    ):
        """
        Purpose:
            Verify a call the wall-clock bound cut off is persisted — the
            TIMEOUT terminal reaches the sink on the no-retry profile.
        Expected:
            - exactly one entry, under the protect name
            - failure_type MAX_RETRIES_TIMEOUTPOLICYERROR
            - request_data from the call site
        """
        release = threading.Event()

        @protected("e2e_unretried_slow", dlq=True, circuit_breaker=False, timeout=0.05)
        def slow(doc_id: str) -> str:
            release.wait(timeout=5.0)
            return "late"

        try:
            with pytest.raises(TimeoutPolicyError):
                slow("doc-9")
        finally:
            release.set()

        _wait_for_repo_count(in_memory_dlq_repo, 1)

        assert in_memory_dlq_repo.count_all() == 1
        pending = in_memory_dlq_repo.get_pending_by_domain(
            "e2e_unretried_slow", limit=10
        )
        assert len(pending) == 1
        assert pending[0].failure_type == "MAX_RETRIES_TIMEOUTPOLICYERROR"
        assert pending[0].request_data == {"doc_id": "doc-9"}

    def test_repository_receives_entry_for_a_failed_async_call(
        self,
        in_memory_dlq_repo: InMemoryFailedOperationRepository,
        started_outbox: Outbox,
    ):
        """
        Purpose:
            Verify the async twin persists too: its sink runs off the event
            loop and hands the entry to the same outbox.
        Expected:
            - exactly one entry, under the protect name
            - failure_type MAX_RETRIES_RUNTIMEERROR, request_data from the call site
        """

        @protected(
            "e2e_unretried_asummarize",
            dlq=True,
            circuit_breaker=False,
            timeout=None,
        )
        async def asummarize(doc_id: str) -> str:
            raise RuntimeError("upstream 500")

        with pytest.raises(RuntimeError):
            asyncio.run(asummarize("doc-7"))

        _wait_for_repo_count(in_memory_dlq_repo, 1)

        assert in_memory_dlq_repo.count_all() == 1
        pending = in_memory_dlq_repo.get_pending_by_domain(
            "e2e_unretried_asummarize", limit=10
        )
        assert len(pending) == 1
        assert pending[0].failure_type == "MAX_RETRIES_RUNTIMEERROR"
        assert pending[0].request_data == {"doc_id": "doc-7"}
