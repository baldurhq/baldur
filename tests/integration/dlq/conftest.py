"""Shared fixtures for the DLQ round-trip integration tests.

The pure-OSS capture chain, wired to one in-memory repository: a call parked
through ``protect(dlq=True)`` lands in the same instance the replay side reads,
with both PRO registry slots empty and the outbox draining for real.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from baldur.adapters.memory.failed_operation import InMemoryFailedOperationRepository
from baldur.audit.ring_buffer import RingBuffer
from baldur.services.dlq_outbox import outbox as outbox_module
from baldur.services.dlq_outbox.outbox import Outbox
from baldur.services.dlq_outbox.worker import DLQOutboxWorker
from baldur.settings.backpressure import BackpressureStrategy


@pytest.fixture
def repository() -> InMemoryFailedOperationRepository:
    """A fresh in-memory DLQ repository — the instance both halves share."""
    return InMemoryFailedOperationRepository()


@pytest.fixture
def oss_backing(monkeypatch, repository) -> Iterator[InMemoryFailedOperationRepository]:
    """Wire the repository behind the canonical chain with both slots empty.

    Simulates a pure-OSS install: ``resolve_dlq_backing()`` misses the PRO
    ``dlq_service`` slot and falls through to the OSS capture singleton, which
    is replaced here by one holding the test repository.
    """
    from baldur.factory.registry import ProviderRegistry
    from baldur.services.dlq_capture import service as capture_module
    from baldur.services.dlq_capture.service import (
        DLQCaptureService,
        reset_dlq_capture_service,
    )

    monkeypatch.setattr(ProviderRegistry.dlq_service, "safe_get", lambda: None)
    monkeypatch.setattr(ProviderRegistry.dlq_repository, "safe_get", lambda: None)
    monkeypatch.setattr(
        capture_module,
        "_capture_service",
        DLQCaptureService(repository=repository),
    )
    yield repository
    reset_dlq_capture_service()


@pytest.fixture
def started_outbox(oss_backing) -> Iterator[Outbox]:
    """A real outbox draining into the OSS capture backing.

    The sink stores without a ``mode``, which resolves to the async outbox by
    default — the production path — so the drain has to be real for the entry
    to reach the repository at all.
    """
    from baldur.services.dlq_capture.service import resolve_dlq_backing

    def sync_writer(kwargs: dict) -> object:
        return resolve_dlq_backing().store_failure(mode="sync", **kwargs)

    buffer: RingBuffer = RingBuffer(
        capacity=100, strategy=BackpressureStrategy.DROP_OLDEST
    )
    # batch_size=1 makes the drain deterministic: every popped batch flushes.
    worker = DLQOutboxWorker(
        buffer=buffer,
        sync_writer=sync_writer,
        batch_size=1,
        flush_interval_seconds=0.01,
    )
    outbox = Outbox(buffer=buffer, worker=worker)
    outbox.start()
    outbox_module._outbox = outbox
    outbox_module._worker_dead = False

    yield outbox

    try:
        outbox.stop(timeout=1.0)
    except Exception:
        pass
    outbox_module._outbox = None
    outbox_module._worker_dead = False
    outbox_module._worker_dead_coercions = 0


@pytest.fixture
def open_circuit() -> Iterator[object]:
    """Force named circuits OPEN for the test and close them afterwards."""
    from baldur.services.circuit_breaker.convenience import (
        force_close_circuit,
        force_open_circuit,
    )

    opened: list[str] = []

    def _open(service_name: str) -> str:
        force_open_circuit(service_name, reason="integration test")
        opened.append(service_name)
        return service_name

    yield _open

    for service_name in opened:
        try:
            force_close_circuit(service_name, reason="integration test teardown")
        except Exception:
            pass
