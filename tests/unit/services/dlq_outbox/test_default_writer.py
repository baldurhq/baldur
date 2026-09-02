"""Unit tests for the OSS outbox default writer + emergency dump chain.

``_default_sync_writer`` and ``_default_emergency_dump`` resolve the single DLQ
backing chain (PRO ``DLQService`` under ACTIVE entitlement, else the OSS
``DLQCaptureService``) and dispatch through ``store_failure`` /
``_write_to_local_fallback`` — so a pure OSS install drains the outbox without
the old ``RuntimeError("...requires baldur_pro...")``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.factory.registry import ProviderRegistry
from baldur.models.dlq import DLQEntryResult
from baldur.services.dlq_capture import DLQCaptureService
from baldur.services.dlq_capture import service as capture_module
from baldur.services.dlq_outbox import outbox

_RESOLVE = "baldur.services.dlq_capture.resolve_dlq_backing"

# Captured at import (collection) time, before any test runs. The PRO durable
# install (and other tests) RAW-reassign ``outbox._default_sync_writer`` /
# ``_default_emergency_dump``; a leaked swap would otherwise make these tests
# call the wrong writer under xdist ordering. Restore the pristine functions
# around each test so this file is isolated from (and does not leak) that swap.
_PRISTINE_SYNC_WRITER = outbox._default_sync_writer
_PRISTINE_EMERGENCY_DUMP = outbox._default_emergency_dump


class _FakeClock:
    """A monotonic clock the test advances explicitly.

    The dump's bound is arithmetic over ``time.monotonic()`` readings taken
    between entries. Waiting for a real clock would either take the whole
    budget or assert nothing; advancing a fake one asserts the cut exactly.
    """

    def __init__(self, start: float = 10_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _restore_pristine_writers():
    outbox._default_sync_writer = _PRISTINE_SYNC_WRITER
    outbox._default_emergency_dump = _PRISTINE_EMERGENCY_DUMP
    yield
    outbox._default_sync_writer = _PRISTINE_SYNC_WRITER
    outbox._default_emergency_dump = _PRISTINE_EMERGENCY_DUMP


class TestOutboxWriterChainBehavior:
    """The worker-thread writers resolve the backing and dispatch correctly."""

    def test_sync_writer_dispatches_kwargs_through_backing_as_sync(self):
        """Forwards the kwargs to ``store_failure(mode='sync', ...)`` verbatim."""
        service = MagicMock(spec=DLQCaptureService)
        service.store_failure.return_value = "dispatched"

        with patch(_RESOLVE, return_value=service):
            out = outbox._default_sync_writer(
                {"domain": "payment", "failure_type": "X"}
            )

        service.store_failure.assert_called_once_with(
            mode="sync", domain="payment", failure_type="X"
        )
        assert out == "dispatched"

    def test_sync_writer_resolves_oss_backing_without_runtime_error(self, monkeypatch):
        """Slot empty (PRO absent) → real OSS backing captures; no RuntimeError."""
        monkeypatch.setattr(ProviderRegistry.dlq_service, "safe_get", lambda: None)

        # Real in-process repo (§6.4) — the point is the chain resolves the OSS
        # backing and captures, not that a mock was called.
        repo = InMemoryFailedOperationRepository()
        monkeypatch.setattr(
            capture_module,
            "_capture_service",
            capture_module.DLQCaptureService(repository=repo),
        )

        result = outbox._default_sync_writer({"domain": "payment", "failure_type": "X"})

        assert isinstance(result, DLQEntryResult)
        assert result.success is True
        assert repo.count_all() == 1

    def test_emergency_dump_reaches_through_to_local_fallback_per_entry(self):
        """Each remaining batch entry is dumped via the zero-loss local fallback."""
        service = MagicMock(spec=DLQCaptureService)

        with patch(_RESOLVE, return_value=service):
            outbox._default_emergency_dump([{"domain": "a"}, {"domain": "b"}])

        assert service._write_to_local_fallback.call_count == 2
        service._write_to_local_fallback.assert_any_call(
            {"domain": "a"}, "shutdown_emergency_dump"
        )

    def test_emergency_dump_backing_without_fallback_does_not_raise(self):
        """A backing lacking ``_write_to_local_fallback`` is handled gracefully."""

        class _NoFallbackBacking:
            """Deterministic backing double with no local-fallback method."""

        with patch(_RESOLVE, return_value=_NoFallbackBacking()):
            # Contract is "does not raise" (§9.3) — the getattr None branch logs.
            outbox._default_emergency_dump([{"domain": "a"}])


class TestDefaultEmergencyDumpBehavior:
    """``_default_emergency_dump`` — the bounded, counting safety net.

    Two properties the shutdown report rests on: the loop stops at the deadline
    it was handed, and it returns what the fallback actually stored. Counting
    the call rather than its answer would report a failed write as a rescue.
    """

    def test_emergency_dump_returns_the_count_the_fallback_stored(self):
        # Given — every entry lands somewhere on disk
        service = MagicMock(spec=DLQCaptureService)
        service._write_to_local_fallback.return_value = "/var/lib/baldur/dlq.jsonl"

        # When
        with patch(_RESOLVE, return_value=service):
            written = outbox._default_emergency_dump(
                [{"domain": "a"}, {"domain": "b"}, {"domain": "c"}]
            )

        # Then
        assert written == 3

    def test_emergency_dump_does_not_count_an_entry_the_fallback_dropped(self):
        """``_write_to_local_fallback`` returns the destination it stored to, or
        ``None`` when every tier failed. A ``None`` is not a write, and an entry
        that reached no tier is exactly what the residual bucket is for."""
        # Given — the middle entry fails every tier
        service = MagicMock(spec=DLQCaptureService)
        service._write_to_local_fallback.side_effect = [
            "/var/lib/baldur/dlq.jsonl",
            None,
            "/var/lib/baldur/dlq.jsonl",
        ]

        # When
        with patch(_RESOLVE, return_value=service):
            written = outbox._default_emergency_dump(
                [{"domain": "a"}, {"domain": "b"}, {"domain": "c"}]
            )

        # Then
        assert written == 2

    def test_emergency_dump_does_not_count_an_entry_whose_write_raised(self):
        """A raising tier is the same outcome as a dropped one, and the loop
        must keep going: one bad entry may not cost the rest their rescue."""
        # Given
        service = MagicMock(spec=DLQCaptureService)
        service._write_to_local_fallback.side_effect = [
            RuntimeError("disk full"),
            "/var/lib/baldur/dlq.jsonl",
        ]

        # When
        with patch(_RESOLVE, return_value=service):
            written = outbox._default_emergency_dump([{"domain": "a"}, {"domain": "b"}])

        # Then
        assert written == 1
        assert service._write_to_local_fallback.call_count == 2

    def test_emergency_dump_checks_the_deadline_before_each_entry(self):
        """Per entry, not per batch: the fallback's file tier does an
        open/write/flush/fsync per entry under a class-level lock, so at
        network-storage fsync latencies a single entry is the granularity that
        matters."""
        # Given — each write costs 1.0 s against a 2.0 s deadline
        clock = _FakeClock()
        service = MagicMock(spec=DLQCaptureService)

        def _slow_write(entry, reason):
            clock.advance(1.0)
            return "/var/lib/baldur/dlq.jsonl"

        service._write_to_local_fallback.side_effect = _slow_write

        # When
        with (
            patch(_RESOLVE, return_value=service),
            patch.object(outbox.time, "monotonic", clock),
        ):
            written = outbox._default_emergency_dump(
                [{"domain": f"d{i}"} for i in range(10)],
                deadline=clock() + 2.0,
            )

        # Then — two full writes, and the third is refused at the instant the
        # deadline is reached (the check is ``>=``), rather than the remaining
        # eight running past the budget
        assert written == 2
        assert service._write_to_local_fallback.call_count == 2

    def test_emergency_dump_with_a_deadline_already_past_writes_nothing(self):
        """Boundary: a teardown that blew its whole budget before reaching the
        dump must not start a write it cannot bound."""
        # Given
        clock = _FakeClock()
        service = MagicMock(spec=DLQCaptureService)
        service._write_to_local_fallback.return_value = "/var/lib/baldur/dlq.jsonl"

        # When
        with (
            patch(_RESOLVE, return_value=service),
            patch.object(outbox.time, "monotonic", clock),
        ):
            written = outbox._default_emergency_dump(
                [{"domain": "a"}], deadline=clock() - 1.0
            )

        # Then
        assert written == 0
        service._write_to_local_fallback.assert_not_called()

    def test_emergency_dump_without_a_deadline_writes_the_whole_batch(self):
        """``None`` is unbounded — the reset path's shape, and the default."""
        # Given
        service = MagicMock(spec=DLQCaptureService)
        service._write_to_local_fallback.return_value = "/var/lib/baldur/dlq.jsonl"

        # When
        with patch(_RESOLVE, return_value=service):
            written = outbox._default_emergency_dump(
                [{"domain": f"d{i}"} for i in range(5)], deadline=None
            )

        # Then
        assert written == 5

    def test_emergency_dump_returns_zero_when_the_backing_cannot_be_resolved(self):
        """An unresolvable backing wrote nothing, and the caller has to be able
        to tell that apart from a completed dump."""
        with patch(_RESOLVE, side_effect=RuntimeError("no backing")):
            assert outbox._default_emergency_dump([{"domain": "a"}]) == 0

    def test_emergency_dump_returns_zero_when_the_backing_has_no_fallback(self):
        """Same reasoning for a backing that exposes no local-fallback path:
        "did not raise" is not "rescued"."""

        class _NoFallbackBacking:
            """Deterministic backing double with no local-fallback method."""

        with patch(_RESOLVE, return_value=_NoFallbackBacking()):
            assert outbox._default_emergency_dump([{"domain": "a"}]) == 0
