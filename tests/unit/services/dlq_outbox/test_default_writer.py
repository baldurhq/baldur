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
