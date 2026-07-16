"""Unit tests for the OSS DLQ read backing — ``DLQReadService`` (708).

Covers the OSS read/visibility + single-entry-action facade that un-gates the
DLQ surface for a pure ``pip install baldur`` install:

- Composition: the facade's read + single-entry methods resolve to the moved
  mixins, and the capture core stays inherited from ``DLQCaptureService`` — the
  707 extraction pattern, extended to read/replay.
- Slot invariant (707 D3): the OSS backing is NOT the ``ProviderRegistry``
  ``dlq_service`` slot instance — it resolves through a handler-layer chain,
  never the slot, so it can never shadow a registered PRO service.
- Single-entry actions (retry / resolve / force-redrive) drive real state
  transitions over an in-memory repository, including the negative
  ``DLQStateConflictError`` transitions that map to HTTP 409.
- Queries (list / detail / facets / cleanup stats) pass through to the
  repository read primitives with the pagination clamp applied.

State transitions run against a real ``InMemoryFailedOperationRepository`` (a
full-lifecycle in-process double), not a Mock, so ``try_acquire_for_replay`` /
``complete_replay`` / ``mark_as_resolved`` move real state a mock cannot
fabricate. ``_log_dlq_audit`` is stubbed to keep construction I/O-free.
"""

from __future__ import annotations

import pytest

from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.core.exceptions import (
    DLQEntryNotFoundError,
    DLQStateConflictError,
)
from baldur.factory.registry import ProviderRegistry
from baldur.interfaces.repositories import FailedOperationData, FailedOperationStatus
from baldur.models.dlq import CleanupStats, DLQConfig
from baldur.services.dlq_capture import DLQCaptureService
from baldur.services.dlq_read import (
    DLQReadService,
    EntryOperationsMixin,
    ListOperationsMixin,
    QueryOperationsMixin,
    ReplayExecutionMixin,
    get_dlq_read_service,
    reset_dlq_read_service,
)
from baldur.services.replay_service import ReplayHandler, register_replay_handler
from baldur.services.replay_service.models import ReplayResult

PENDING = FailedOperationStatus.PENDING.value
REPLAYING = FailedOperationStatus.REPLAYING.value
REQUIRES_REVIEW = FailedOperationStatus.REQUIRES_REVIEW.value
RESOLVED = FailedOperationStatus.RESOLVED.value
ARCHIVED = FailedOperationStatus.ARCHIVED.value
REVIEWING = FailedOperationStatus.REVIEWING.value


# =============================================================================
# Test doubles + fixtures (mirrors the PRO force-redrive suite)
# =============================================================================


class _StubReplayHandler(ReplayHandler):
    """Deterministic replay handler so ``_execute_replay`` has a known outcome."""

    def __init__(
        self,
        domain: str,
        *,
        succeed: bool = True,
        raises: Exception | None = None,
    ):
        self._domain = domain
        self._succeed = succeed
        self._raises = raises
        self.replay_calls: list[str] = []

    @property
    def domain(self) -> str:
        return self._domain

    def can_replay(self, failed_op: FailedOperationData) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op: FailedOperationData) -> ReplayResult:
        self.replay_calls.append(failed_op.id)
        if self._raises is not None:
            raise self._raises
        if self._succeed:
            return ReplayResult.succeeded(failed_op.id, "stub success")
        return ReplayResult.failed(failed_op.id, "stub failure")


@pytest.fixture
def register_handler():
    """Register stub replay handlers, restoring the registry afterwards."""
    from baldur.services.replay_service import handlers as _handlers

    snapshot = dict(_handlers._replay_handlers)

    def _register(
        domain: str,
        *,
        succeed: bool = True,
        raises: Exception | None = None,
    ) -> _StubReplayHandler:
        handler = _StubReplayHandler(domain, succeed=succeed, raises=raises)
        register_replay_handler(handler)
        return handler

    yield _register

    _handlers._replay_handlers.clear()
    _handlers._replay_handlers.update(snapshot)


@pytest.fixture
def make_read_service():
    """Build a real ``DLQReadService`` over an in-memory repository.

    Audit is stubbed (no WAL disk I/O); every repository primitive runs for
    real against the in-memory adapter so state transitions are genuine.
    """

    def _make(cap: int = 2):
        repo = InMemoryFailedOperationRepository()
        config = DLQConfig(enabled=True, max_replay_attempts=cap)
        service = DLQReadService(config=config, repository=repo)
        service._log_dlq_audit = lambda **kwargs: None  # type: ignore[method-assign]
        return service, repo

    return _make


def _at_cap_entry(repo, domain="poison", cap=2):
    """Create an at-cap entry parked in REQUIRES_REVIEW (the 606 terminal)."""
    entry = repo.create(
        domain=domain, failure_type="poison", retry_count=cap, max_retries=cap
    )
    repo.update_status(entry.id, status=REQUIRES_REVIEW)
    return entry


# =============================================================================
# TestDLQReadServiceComposition — MRO resolution owners + singleton lifecycle
# =============================================================================


class TestDLQReadServiceComposition:
    """The facade wires the moved mixins; each method resolves to its owner."""

    def test_read_service_is_a_capture_service(self):
        """IS-A ``DLQCaptureService`` — inherits repository/config/store_failure."""
        assert issubclass(DLQReadService, DLQCaptureService)

    def test_store_failure_inherited_from_capture_base(self):
        """The capture core is inherited, not re-implemented in the read facade."""
        assert DLQReadService.store_failure is DLQCaptureService.store_failure

    @pytest.mark.parametrize(
        ("method_name", "owner"),
        [
            ("retry_entry", EntryOperationsMixin),
            ("resolve_entry", EntryOperationsMixin),
            ("force_redrive_entry", EntryOperationsMixin),
            ("get_entry", EntryOperationsMixin),
            ("list_entries", ListOperationsMixin),
            ("get_facet_counts", QueryOperationsMixin),
            ("get_cleanup_stats", QueryOperationsMixin),
            ("get_stats", QueryOperationsMixin),
            ("_execute_replay", ReplayExecutionMixin),
            ("_emit_replay_exhausted", ReplayExecutionMixin),
        ],
    )
    def test_method_resolves_to_expected_mixin(self, method_name, owner):
        """Every read + single-entry + replay-exec method resolves to its mixin."""
        assert getattr(DLQReadService, method_name) is getattr(owner, method_name)

    def test_replay_exec_precedes_capture_base_in_mro(self):
        """``_execute_replay`` resolves to the mixin, not any capture-base copy."""
        mro = DLQReadService.__mro__
        assert mro.index(ReplayExecutionMixin) < mro.index(DLQCaptureService)

    def test_singleton_caches_until_reset(self):
        """``get_dlq_read_service`` caches; reset forces a fresh instance."""
        reset_dlq_read_service()
        first = get_dlq_read_service()
        second = get_dlq_read_service()
        assert first is second

        reset_dlq_read_service()
        assert get_dlq_read_service() is not first

    def test_singleton_is_a_read_service(self):
        reset_dlq_read_service()
        assert isinstance(get_dlq_read_service(), DLQReadService)


# =============================================================================
# TestDLQReadSlotInvariant — the OSS backing is not the registry slot (707 D3)
# =============================================================================


class TestDLQReadSlotInvariant:
    """707 D3: the OSS read backing resolves via the handler chain, not the slot."""

    def test_read_backing_is_not_the_registry_slot_instance(self):
        """The OSS singleton is a distinct object from whatever backs the slot.

        Holds in both tiers: PRO-present the slot resolves the PRO service (a
        different object); PRO-absent it resolves ``None``. Either way the OSS
        read backing is never the slot instance, so it cannot shadow PRO.
        """
        read = get_dlq_read_service()
        slot = ProviderRegistry.dlq_service.safe_get()
        assert read is not slot

    def test_getting_read_backing_does_not_populate_the_slot(self, monkeypatch):
        """Resolving the OSS backing never registers it into the slot."""
        monkeypatch.setattr(ProviderRegistry.dlq_service, "safe_get", lambda: None)
        reset_dlq_read_service()

        get_dlq_read_service()

        # The read backing self-registering would make the slot non-empty here.
        assert ProviderRegistry.dlq_service.safe_get() is None


# =============================================================================
# TestDLQReadSingleEntryActions — retry / resolve / force-redrive state moves
# =============================================================================


class TestDLQReadSingleEntryActions:
    """Single-entry actions drive real state transitions on the OSS facade."""

    def test_retry_success_resolves_entry(self, make_read_service, register_handler):
        domain = "retry_ok"
        service, repo = make_read_service(cap=2)
        handler = register_handler(domain, succeed=True)
        entry = repo.create(domain=domain, failure_type="x", retry_count=0)

        result = service.retry_entry(entry.id)

        assert result["success"] is True
        assert result["status"] == RESOLVED
        assert handler.replay_calls == [entry.id]
        assert repo.get_by_id(entry.id).status == RESOLVED

    def test_retry_handler_failure_under_cap_reverts_to_pending(
        self, make_read_service, register_handler
    ):
        domain = "retry_fail"
        service, repo = make_read_service(cap=2)
        register_handler(domain, succeed=False)
        entry = repo.create(
            domain=domain, failure_type="x", retry_count=0, max_retries=2
        )

        result = service.retry_entry(entry.id)

        assert result["success"] is False
        assert result["status"] == PENDING
        assert repo.get_by_id(entry.id).status == PENDING

    def test_resolve_marks_entry_resolved(self, make_read_service):
        service, repo = make_read_service()
        entry = repo.create(domain="d", failure_type="x")

        result = service.resolve_entry(entry.id, notes="handled")

        assert result["success"] is True
        assert result["current_status"] == RESOLVED
        assert result["previous_status"] == PENDING
        assert repo.get_by_id(entry.id).status == RESOLVED

    def test_force_redrive_at_cap_resolves_on_success(
        self, make_read_service, register_handler
    ):
        domain = "fr_ok"
        service, repo = make_read_service(cap=2)
        register_handler(domain, succeed=True)
        entry = _at_cap_entry(repo, domain)

        result = service.force_redrive_entry(entry.id, actor_id="ops", reason="fixed")

        assert result["success"] is True
        assert result["status"] == RESOLVED
        assert result["retry_count"] == 1
        assert result["previous_retry_count"] == 2
        assert repo.get_by_id(entry.id).status == RESOLVED

    def test_force_redrive_handler_failure_reverts_to_pending_fresh_budget(
        self, make_read_service, register_handler
    ):
        domain = "fr_fail"
        service, repo = make_read_service(cap=2)
        register_handler(domain, succeed=False)
        entry = _at_cap_entry(repo, domain)

        result = service.force_redrive_entry(entry.id, reason="tried")

        assert result["success"] is False
        assert result["status"] == PENDING
        recovered = repo.get_by_id(entry.id)
        assert recovered.retry_count == 1
        assert recovered.metadata["force_redrive_count"] == 1

    def test_retry_missing_entry_raises_not_found(self, make_read_service):
        service, _ = make_read_service()

        with pytest.raises(DLQEntryNotFoundError):
            service.retry_entry("999")

    @pytest.mark.parametrize("status", [RESOLVED, ARCHIVED])
    def test_retry_resolved_or_archived_raises_conflict(
        self, make_read_service, status
    ):
        service, repo = make_read_service()
        entry = repo.create(domain="d", failure_type="x")
        repo.update_status(entry.id, status=status)

        with pytest.raises(DLQStateConflictError):
            service.retry_entry(entry.id)

    def test_retry_at_cap_hard_blocks_with_conflict(self, make_read_service):
        service, repo = make_read_service(cap=2)
        entry = _at_cap_entry(repo, "blocked")

        with pytest.raises(DLQStateConflictError, match="exhausted replay attempts"):
            service.retry_entry(entry.id)

    @pytest.mark.parametrize("status", [RESOLVED, ARCHIVED])
    def test_force_redrive_resolved_or_archived_raises_conflict(
        self, make_read_service, status
    ):
        service, repo = make_read_service()
        entry = repo.create(domain="d", failure_type="x", retry_count=1)
        repo.update_status(entry.id, status=status)

        with pytest.raises(DLQStateConflictError):
            service.force_redrive_entry(entry.id, reason="r")

    def test_force_redrive_non_acquirable_state_raises_conflict(
        self, make_read_service
    ):
        """A REVIEWING entry passes the friendly pre-checks but is not
        force-acquirable → acquire returns None → conflict."""
        service, repo = make_read_service()
        entry = repo.create(domain="d", failure_type="x", retry_count=1)
        repo.update_status(entry.id, status=REVIEWING)

        with pytest.raises(DLQStateConflictError):
            service.force_redrive_entry(entry.id, reason="r")

    def test_resolve_already_resolved_raises_conflict(self, make_read_service):
        service, repo = make_read_service()
        entry = repo.create(domain="d", failure_type="x")
        repo.update_status(entry.id, status=RESOLVED)

        with pytest.raises(DLQStateConflictError):
            service.resolve_entry(entry.id)

    def test_double_force_redrive_second_raises_conflict(
        self, make_read_service, register_handler
    ):
        """After a successful force-redrive the entry is RESOLVED; a second
        attempt hits the resolved pre-check → conflict (double-click idempotency)."""
        domain = "fr_double"
        service, repo = make_read_service(cap=2)
        register_handler(domain, succeed=True)
        entry = _at_cap_entry(repo, domain)

        first = service.force_redrive_entry(entry.id, reason="r")
        assert first["success"] is True

        with pytest.raises(DLQStateConflictError):
            service.force_redrive_entry(entry.id, reason="r")


# =============================================================================
# TestDLQReadQueries — list / detail / facets / cleanup stats pass-through
# =============================================================================


class TestDLQReadQueries:
    """Read queries delegate to the repository read primitives."""

    def test_list_entries_returns_page_of_results(self, make_read_service):
        service, repo = make_read_service()
        for _ in range(3):
            repo.create(domain="payment", failure_type="X")

        result = service.list_entries(page=1, page_size=20)

        assert result["total_count"] == 3
        assert len(result["results"]) == 3
        assert result["page"] == 1
        assert result["has_next"] is False

    def test_list_entries_clamps_page_size_above_max(self, make_read_service):
        """page_size > 100 is clamped to the 100 upper bound (boundary)."""
        service, _ = make_read_service()

        result = service.list_entries(page=1, page_size=101)

        assert result["page_size"] == 100

    def test_list_entries_clamps_zero_page_to_one(self, make_read_service):
        """page <= 0 clamps to 1 so the offset never goes negative (541 D2)."""
        service, _ = make_read_service()

        result = service.list_entries(page=0, page_size=20)

        assert result["page"] == 1

    def test_list_entries_paginates_with_offset(self, make_read_service):
        service, repo = make_read_service()
        for _ in range(5):
            repo.create(domain="payment", failure_type="X")

        page1 = service.list_entries(page=1, page_size=2)
        page2 = service.list_entries(page=2, page_size=2)

        assert page1["total_count"] == 5
        assert len(page1["results"]) == 2
        assert page1["has_next"] is True
        assert len(page2["results"]) == 2
        assert page2["has_previous"] is True

    def test_get_entry_returns_detail_dict(self, make_read_service):
        service, repo = make_read_service()
        entry = repo.create(domain="payment", failure_type="PG_TIMEOUT")

        detail = service.get_entry(entry.id)

        assert detail is not None
        assert detail["id"] == entry.id
        assert detail["domain"] == "payment"
        assert detail["failure_type"] == "PG_TIMEOUT"
        assert detail["status"] == PENDING

    def test_get_entry_missing_returns_none(self, make_read_service):
        service, _ = make_read_service()

        assert service.get_entry("999") is None

    def test_get_facet_counts_reflects_status_and_domain(self, make_read_service):
        service, repo = make_read_service()
        repo.create(domain="payment", failure_type="X")
        repo.create(domain="payment", failure_type="Y")
        repo.create(domain="order", failure_type="Z")

        counts = service.get_facet_counts()

        assert counts["by_status"][PENDING] == 3
        assert counts["by_domain"]["payment"] == 2
        assert counts["by_domain"]["order"] == 1

    def test_get_cleanup_stats_returns_cleanup_stats_value(self, make_read_service):
        service, repo = make_read_service()
        repo.create(domain="payment", failure_type="X")
        repo.create(domain="order", failure_type="Y")

        stats = service.get_cleanup_stats()

        assert isinstance(stats, CleanupStats)
        assert stats.total == 2
        assert stats.by_status[PENDING] == 2

    def test_get_stats_passes_through_repository_statistics(self, make_read_service):
        service, repo = make_read_service()
        repo.create(domain="payment", failure_type="X")

        stats = service.get_stats()

        assert stats["total"] == 1
        assert stats["by_status"][PENDING] == 1
