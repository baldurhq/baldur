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
    C. Failure kinds the composer now completes:
        - ``@dlq_protect`` with retry switched off persists one entry
        - a tenacity retry stage persists one entry with its attempt count
        - a retry sequence the bound cut off persists one entry under the
          protect name, and no second once the abandoned worker finishes
        - an enclosing DLQ site around an inner site whose breaker is open
          persists the rejection once, through the outbox and through a
          local fallback record

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
import json
import threading
import time
from collections.abc import Iterator
from unittest.mock import patch

import pytest
import tenacity

from baldur import protect_facade
from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.audit.persistence.disk_buffer_adapter import DiskBufferAdapter
from baldur.audit.ring_buffer import RingBuffer
from baldur.bridges.tenacity.policy import TenacityBridgePolicy
from baldur.core.exceptions import TimeoutPolicyError
from baldur.decorators.dlq_protect import dlq_protect
from baldur.models.dlq import OPEN_CIRCUIT_FAILURE_TYPE
from baldur.protect_facade import protect, protected
from baldur.resilience.policies.timeout import TimeoutPolicy
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError
from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.services.dlq_capture import DLQCaptureService
from baldur.services.dlq_capture import service as dlq_capture_service
from baldur.services.dlq_outbox import outbox as outbox_module
from baldur.services.dlq_outbox.outbox import Outbox
from baldur.services.dlq_outbox.worker import DLQOutboxWorker
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.settings.backpressure import BackpressureStrategy
from baldur.settings.dlq_outbox import reset_dlq_outbox_settings
from baldur.settings.protect import reset_protect_settings
from baldur.settings.retry import reset_retry_settings
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


# =============================================================================
# E2E — failure kinds that used to reach the caller unparked
# =============================================================================


class _RaisingRepository(InMemoryFailedOperationRepository):
    """A repository whose writes fail, counting each attempt."""

    def __init__(self) -> None:
        super().__init__()
        self.create_calls = 0

    def create(self, *args, **kwargs):  # type: ignore[override]
        self.create_calls += 1
        raise RuntimeError("database unavailable")


def _install_breaker_opening_on_first_failure(name: str) -> None:
    """An in-memory breaker for ``name`` that opens on its first failure,
    placed in the facade's per-name cache (dropped by the protect reset)."""
    cb_service = CircuitBreakerService(
        config=CircuitBreakerConfig(
            enabled=True,
            failure_threshold=1,
            minimum_calls=1,
            failure_rate_threshold=0,
            recovery_timeout=60,
        ),
        repository=InMemoryCircuitBreakerStateRepository(),
    )
    protect_facade._cb_policy_cache[name] = CircuitBreakerPolicy(
        service_name=name, cb_service=cb_service, hooks=[]
    )


def _fail() -> str:
    raise ConnectionError("upstream down")


def _call_through_open_inner_site(inner: str, outer: str) -> None:
    """Open ``inner``'s breaker without parking anything, then make the
    nested call: an enclosing DLQ site around a ``dlq=True`` inner site."""
    _install_breaker_opening_on_first_failure(inner)
    with pytest.raises(ConnectionError):
        protect(inner, _fail, dlq=False, timeout=None)

    with pytest.raises(CircuitBreakerOpenError):
        protect(
            outer,
            lambda: protect(inner, _fail, dlq=True, timeout=None),
            retry=True,
            dlq=True,
            timeout=None,
        )


class TestProtectDlqCaptureWidenedE2E:
    """The failure kinds the composer now completes reach the repository.

    Validates:
    - the verdict the composer completes — for a retry stage switched off, a
      tenacity stage and a timeout that cut a retry sequence off — is the one
      the sink reads, so the entry is persisted under the protect name
    - the custody mark the inner site writes from the capture service's real
      result keeps the enclosing site from writing a second copy, through the
      outbox and through a local fallback record
    """

    def setup_method(self):
        reset_protect_settings()

    def teardown_method(self):
        reset_protect_settings()
        reset_retry_settings()
        reset_dlq_outbox_settings()

    def test_repository_receives_entry_from_dlq_protect_with_retry_switched_off(
        self,
        in_memory_dlq_repo: InMemoryFailedOperationRepository,
        started_outbox: Outbox,
        monkeypatch,
    ):
        """
        Purpose:
            Verify ``@dlq_protect`` under ``BALDUR_RETRY_ENABLED=false`` persists
            its failure — the retry stage's single attempt states its verdict.
        Expected:
            - exactly one entry, under the decorator name
            - failure_type MAX_RETRIES_CONNECTIONERROR; metadata max_attempts == 1
        """
        # The retry stage snapshots the switch when the composer is built, on
        # the first call below. A protect reset here would also stop the
        # outbox this test drains.
        monkeypatch.setenv("BALDUR_RETRY_ENABLED", "false")
        reset_retry_settings()

        @dlq_protect("e2e_widened_retry_off", timeout=None)
        def charge(order_id: str) -> str:
            raise ConnectionError("upstream down")

        with pytest.raises(ConnectionError):
            charge("o-1")

        _wait_for_repo_count(in_memory_dlq_repo, 1)

        pending = in_memory_dlq_repo.get_pending_by_domain(
            "e2e_widened_retry_off", limit=10
        )
        assert in_memory_dlq_repo.count_all() == 1
        assert len(pending) == 1
        assert pending[0].failure_type == "MAX_RETRIES_CONNECTIONERROR"
        assert (pending[0].metadata or {}).get("max_attempts") == 1

    def test_repository_receives_entry_from_a_tenacity_retry_stage(
        self,
        in_memory_dlq_repo: InMemoryFailedOperationRepository,
        started_outbox: Outbox,
    ):
        """
        Purpose:
            Verify a ``TenacityBridgePolicy`` as ``retry=`` persists its
            exhausted call — the bridge writes no verdict, the composer does.
        Expected:
            - exactly one entry, under the protect name
            - metadata max_attempts == the bridge's attempt count
        """
        bridge = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(3), wait=tenacity.wait_none()
        )

        @protected(
            "e2e_widened_tenacity",
            dlq=True,
            retry=bridge,
            circuit_breaker=False,
            timeout=None,
        )
        def charge(order_id: str) -> str:
            raise ConnectionError("upstream down")

        with pytest.raises(ConnectionError):
            charge("o-1")

        _wait_for_repo_count(in_memory_dlq_repo, 1)

        pending = in_memory_dlq_repo.get_pending_by_domain(
            "e2e_widened_tenacity", limit=10
        )
        assert in_memory_dlq_repo.count_all() == 1
        assert len(pending) == 1
        assert pending[0].failure_type == "MAX_RETRIES_CONNECTIONERROR"
        assert (pending[0].metadata or {}).get("max_attempts") == 3

    def test_repository_receives_entry_for_a_retry_sequence_cut_off_by_the_bound(
        self,
        in_memory_dlq_repo: InMemoryFailedOperationRepository,
        started_outbox: Outbox,
    ):
        """
        Purpose:
            Verify a call ``timeout=`` cuts off while its retry stage runs is
            persisted once, under the protect name — the retry config names no
            domain, so its placeholder is not where the entry lands.
        Expected:
            - exactly one entry, under the protect name
            - failure_type MAX_RETRIES_TIMEOUTPOLICYERROR
            - no second entry once the abandoned worker has finished
        """
        release = threading.Event()

        @protected(
            "e2e_widened_cut_off",
            dlq=True,
            retry=RetryPolicyConfig(
                max_attempts=2, backoff_base=0, backoff_max=0, jitter_percent=0
            ),
            circuit_breaker=False,
            timeout=0.05,
        )
        def slow(order_id: str) -> str:
            release.wait(timeout=5.0)
            return "late"

        try:
            with pytest.raises(TimeoutPolicyError):
                slow("o-1")
        finally:
            release.set()
            TimeoutPolicy.shutdown_executor()

        _wait_for_repo_count(in_memory_dlq_repo, 1)
        _wait_for_repo_count(in_memory_dlq_repo, 2, timeout=0.3)

        pending = in_memory_dlq_repo.get_pending_by_domain(
            "e2e_widened_cut_off", limit=10
        )
        assert in_memory_dlq_repo.count_all() == 1
        assert len(pending) == 1
        assert pending[0].failure_type == "MAX_RETRIES_TIMEOUTPOLICYERROR"

    def test_nested_rejection_is_persisted_once_through_the_outbox(
        self,
        in_memory_dlq_repo: InMemoryFailedOperationRepository,
        started_outbox: Outbox,
    ):
        """
        Purpose:
            Verify an enclosing DLQ site around an inner ``dlq=True`` site whose
            breaker is open persists the rejection once: the outbox acks with
            no entry id, and the inner site still marks the rejection.
        Expected:
            - exactly one entry, under the inner site's name
            - failure_type CIRCUIT_BREAKER_OPEN
        """
        _call_through_open_inner_site("e2e_nested_inner", "e2e_nested_outer")

        _wait_for_repo_count(in_memory_dlq_repo, 1)
        _wait_for_repo_count(in_memory_dlq_repo, 2, timeout=0.3)

        pending = in_memory_dlq_repo.get_pending_by_domain("e2e_nested_inner", limit=10)
        assert in_memory_dlq_repo.count_all() == 1
        assert len(pending) == 1
        assert pending[0].failure_type == OPEN_CIRCUIT_FAILURE_TYPE

    def test_nested_rejection_held_in_a_local_fallback_record_is_not_stored_again(
        self, monkeypatch, tmp_path
    ):
        """
        Purpose:
            Verify a rejection whose store failed into a local fallback record
            is in custody: with the outbox off the capture service writes on
            the calling thread, its repository raises, the JSONL fallback holds
            the entry, and the enclosing site does not try again.
        Expected:
            - the repository write is attempted once
            - one fallback record, the inner site's open-circuit entry
        """
        monkeypatch.setenv("BALDUR_DLQ_OUTBOX_ENABLED", "false")
        reset_dlq_outbox_settings()
        repository = _RaisingRepository()
        fallback_path = tmp_path / "dlq_fallback.jsonl"

        # The backing seam pins the capture service whichever tier the
        # registry would resolve; the LMDB tier is reported missing so the
        # fallback lands in a JSONL file this test owns.
        with (
            patch(
                "baldur.services.dlq_capture.resolve_dlq_backing",
                autospec=True,
                return_value=DLQCaptureService(repository=repository),
            ),
            patch.object(dlq_capture_service, "DLQ_FALLBACK_PATH", fallback_path),
            patch.object(
                DiskBufferAdapter,
                "get_instance",
                autospec=True,
                side_effect=ImportError("disk buffer tier not installed"),
            ),
        ):
            _call_through_open_inner_site("e2e_fallback_inner", "e2e_fallback_outer")

        assert repository.create_calls == 1
        records = fallback_path.read_text(encoding="utf-8").splitlines()
        assert len(records) == 1
        entry = json.loads(records[0])["entry_data"]
        assert entry["domain"] == "e2e_fallback_inner"
        assert entry["failure_type"] == OPEN_CIRCUIT_FAILURE_TYPE
