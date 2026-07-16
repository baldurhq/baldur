"""Mock-based integration test — the OSS DLQ "aha" round-trip (708).

Exercises the full slot-empty (PRO-absent) lifecycle a ``pip install baldur``
operator gets from 708: a failure is captured, appears in the read list/detail,
and is re-driven to RESOLVED via a single-entry retry — all through the admin
handler layer with the ``dlq_service`` slot empty, composing:

    store_failure (707 capture core, inherited)
        → DLQReadService list/detail (708 read facade)
        → retry_entry → try_acquire_for_replay → _execute_replay → resolve_entry
        → InMemoryFailedOperationRepository state transitions
        → replay-handler registry

This is a composition/state-transition lifecycle (not simple delegation), so it
runs against a real ``InMemoryFailedOperationRepository`` (a full-lifecycle
in-process double) rather than a Mock, with no external infrastructure. A
``requires_redis`` variant of the acquire/complete atomicity is deferred to the
Redis adapter suite (Docker).
"""

from __future__ import annotations

import pytest

from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.api.handlers.dlq import dlq_detail, dlq_list, dlq_retry
from baldur.factory.registry import ProviderRegistry
from baldur.interfaces.repositories import FailedOperationData, FailedOperationStatus
from baldur.interfaces.web_framework import HttpMethod, RequestContext
from baldur.models.dlq import DLQConfig
from baldur.services.dlq_read import DLQReadService
from baldur.services.replay_service import ReplayHandler, register_replay_handler
from baldur.services.replay_service.models import ReplayResult

PENDING = FailedOperationStatus.PENDING.value
RESOLVED = FailedOperationStatus.RESOLVED.value


class _RecoveringReplayHandler(ReplayHandler):
    """A handler that succeeds — the root cause is 'fixed' before the retry."""

    def __init__(self, domain: str):
        self._domain = domain
        self.replayed: list[str] = []

    @property
    def domain(self) -> str:
        return self._domain

    def can_replay(self, failed_op: FailedOperationData) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op: FailedOperationData) -> ReplayResult:
        self.replayed.append(failed_op.id)
        return ReplayResult.succeeded(failed_op.id, "recovered")


def _ctx(method: HttpMethod, path: str, *, pk=None, body=None) -> RequestContext:
    return RequestContext(
        method=method,
        path=path,
        query_params={},
        path_params={"pk": pk} if pk is not None else {},
        json_body=body,
        user=None,
    )


@pytest.fixture
def oss_dlq(monkeypatch):
    """A slot-empty OSS DLQ stack: read backing injected, replay handler live."""
    from baldur.services.replay_service import handlers as _handlers

    repo = InMemoryFailedOperationRepository()
    service = DLQReadService(
        config=DLQConfig(enabled=True, max_replay_attempts=2), repository=repo
    )
    service._log_dlq_audit = lambda **kwargs: None  # type: ignore[method-assign]

    # PRO absent: the slot resolves to None so handlers fall back to the OSS
    # backing; inject our in-memory-backed backing as that fallback.
    monkeypatch.setattr(ProviderRegistry.dlq_service, "safe_get", lambda: None)
    monkeypatch.setattr(
        "baldur.services.dlq_read.get_dlq_read_service", lambda: service
    )

    snapshot = dict(_handlers._replay_handlers)
    handler = _RecoveringReplayHandler("payment")
    register_replay_handler(handler)
    try:
        yield service, repo, handler
    finally:
        _handlers._replay_handlers.clear()
        _handlers._replay_handlers.update(snapshot)


class TestOssDlqReadRoundTrip:
    """Capture → see it in the list → re-drive it to RESOLVED, all OSS."""

    def test_captured_failure_is_listed_inspected_and_retried_to_resolved(
        self, oss_dlq
    ):
        service, repo, handler = oss_dlq

        # 1. Capture a failed operation (707 capture core, sync mode).
        stored = service.store_failure(
            domain="payment", failure_type="PG_TIMEOUT", mode="sync"
        )
        assert stored.success is True
        dlq_id = stored.dlq_id
        assert dlq_id is not None

        # 2. It shows up in the OSS read list through the handler chain.
        list_resp = dlq_list(_ctx(HttpMethod.GET, "/dlq/list"))
        assert list_resp.status_code == 200
        listed_ids = [row["id"] for row in list_resp.body["results"]]
        assert dlq_id in listed_ids

        # 3. Its detail is inspectable (still PENDING, awaiting recovery).
        detail_resp = dlq_detail(_ctx(HttpMethod.GET, f"/dlq/{dlq_id}", pk=dlq_id))
        assert detail_resp.status_code == 200
        assert detail_resp.body["status"] == PENDING
        assert detail_resp.body["domain"] == "payment"

        # 4. A single-entry retry re-executes it and resolves it.
        retry_resp = dlq_retry(
            _ctx(HttpMethod.POST, f"/dlq/{dlq_id}/retry", pk=dlq_id, body={})
        )
        assert retry_resp.status_code == 200
        assert retry_resp.body["status"] == "success"
        assert retry_resp.body["entry_status"] == RESOLVED

        # 5. The repository reflects the terminal RESOLVED state, and the
        #    recovery handler actually ran on this entry.
        assert repo.get_by_id(dlq_id).status == RESOLVED
        assert handler.replayed == [dlq_id]

    def test_empty_queue_lists_nothing_but_does_not_fault(self, oss_dlq):
        """Slot-empty read on an empty queue returns an empty page, never a 500."""
        list_resp = dlq_list(_ctx(HttpMethod.GET, "/dlq/list"))

        assert list_resp.status_code == 200
        assert list_resp.body["results"] == []
        assert list_resp.body["pagination"]["total_count"] == 0
