"""Shared fixtures for dlq_outbox unit tests (impl doc 486)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from baldur.audit.ring_buffer import RingBuffer
from baldur.services.dlq_outbox.outbox import Outbox
from baldur.services.dlq_outbox.worker import DLQOutboxWorker
from baldur.settings.backpressure import BackpressureStrategy


@pytest.fixture(autouse=True)
def _reset_outbox_module_state():
    """Ensure module-level singleton + worker_dead state is clean per test.

    The origin-PID stamp is part of that state: a test that leaves a mismatched
    PID behind makes the *next* test's first ``get_outbox()`` run the fork
    repair, renewing module locks and respawning a writer out of nowhere.
    """
    from baldur.services.dlq_outbox import outbox as outbox_module

    def _clear() -> None:
        if outbox_module._outbox is not None:
            try:
                outbox_module._outbox.stop(timeout=1.0)
            except Exception:
                pass
            outbox_module._outbox = None
        outbox_module._outbox_origin_pid = None
        outbox_module._worker_dead = False
        outbox_module._worker_dead_coercions = 0
        # The teardown caches its terminal result so repeat callers are
        # no-ops; left behind, the next test's teardown returns this test's
        # counts without draining anything.
        outbox_module._shutdown_result = None

    _clear()
    yield
    _clear()


@pytest.fixture
def collected_writes() -> list[dict[str, Any]]:
    """List that accumulates kwargs dispatched through the test sync_writer."""
    return []


@pytest.fixture
def make_sync_writer() -> Callable[..., Callable[[dict[str, Any]], None]]:
    """Factory for a sync_writer that records dispatched kwargs.

    The default writer records to ``collected_writes``. Pass ``raise_n``
    to make the writer raise ``RuntimeError`` for the first N invocations
    before falling through to the recording path.
    """

    def _make(
        sink: list[dict[str, Any]],
        raise_n: int = 0,
        always_raise: bool = False,
    ):
        state = {"calls": 0}

        def _writer(kwargs: dict[str, Any]) -> None:
            state["calls"] += 1
            if always_raise:
                raise RuntimeError("test-induced failure")
            if state["calls"] <= raise_n:
                raise RuntimeError("test-induced failure")
            sink.append(kwargs)

        return _writer

    return _make


@pytest.fixture
def build_outbox():
    """Build an Outbox from injected primitives (no env-dependent settings).

    Returns a factory that takes ``sync_writer`` + RingBuffer / worker
    knobs and returns ``(Outbox, RingBuffer, DLQOutboxWorker)``.
    """

    def _build(
        sync_writer: Callable[[dict[str, Any]], None],
        capacity: int = 100,
        batch_size: int = 10,
        flush_interval_seconds: float = 0.01,
        drop_rate_threshold: float = 0.01,
        on_drop_threshold=None,
        on_emergency_dump=None,
        on_processing_delay=None,
        on_drops_observed=None,
        on_drop_alert=None,
    ) -> tuple[Outbox, RingBuffer, DLQOutboxWorker]:
        buffer: RingBuffer = RingBuffer(
            capacity=capacity,
            strategy=BackpressureStrategy.DROP_OLDEST,
            drop_rate_threshold=drop_rate_threshold,
            on_drop_threshold=on_drop_threshold,
        )
        worker = DLQOutboxWorker(
            buffer=buffer,
            sync_writer=sync_writer,
            batch_size=batch_size,
            flush_interval_seconds=flush_interval_seconds,
            on_emergency_dump=on_emergency_dump,
            on_processing_delay=on_processing_delay,
            on_drops_observed=on_drops_observed,
            on_drop_alert=on_drop_alert,
            drop_rate_threshold=drop_rate_threshold,
        )
        outbox = Outbox(buffer=buffer, worker=worker)
        return outbox, buffer, worker

    return _build
