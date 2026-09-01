"""On-recovery auto-replay of open-circuit captures.

A call an OPEN circuit rejected is parked under that circuit's own name. When
the circuit closes, the sweep replays those entries with no operator mapping
at all — the circuit that recovered IS the one that rejected them, which is the
whole eligibility test.

Three things make that safe, and each has a way to go wrong that this file
pins:

- the join runs through the same projection the store used, so a name the
  store quietly reprojected (``Payment-API`` is filed as ``payment_api``) is
  still found;
- the selection is scoped to that domain AND filtered to policy-chain
  captures, because a request-boundary layer files the same failure type under
  a path-inferred domain that may name a different, still-dead circuit;
- it runs only when a real handler is registered, because the automatic lane
  calls ``replay()`` without consulting ``can_replay`` — an unregistered domain
  would spend every selected entry's budget on a handler that always fails.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from baldur.interfaces.governance import GovernanceChecker
from baldur.interfaces.repositories import (
    FailedOperationData,
    FailedOperationRepository,
)
from baldur.models.governance import GovernanceCheckResult
from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.handlers import (
    ReplayHandler,
    _replay_handlers,
    has_replay_handler,
    register_replay_handler,
)
from baldur.services.replay_service.models import ReplayResult

OPEN_CIRCUIT = "CIRCUIT_BREAKER_OPEN"
POLICY_CHAIN = "policy_chain"

# =============================================================================
# Helpers
# =============================================================================


class _RecordingHandler(ReplayHandler):
    """Registered handler that succeeds and records what it was handed."""

    def __init__(self, domain: str) -> None:
        self._domain = domain
        self.replayed: list[str] = []

    @property
    def domain(self) -> str:
        return self._domain

    def can_replay(self, failed_op: FailedOperationData) -> tuple[bool, str]:
        return True, ""

    def replay(self, failed_op: FailedOperationData) -> ReplayResult:
        self.replayed.append(failed_op.id)
        return ReplayResult.succeeded(failed_op.id, "done")


@pytest.fixture
def registered_handlers() -> Iterator[list[_RecordingHandler]]:
    """Register handlers for the test and remove exactly those afterwards."""
    created: list[_RecordingHandler] = []

    def _register(domain: str) -> _RecordingHandler:
        handler = _RecordingHandler(domain)
        register_replay_handler(handler)
        created.append(handler)
        return handler

    _register.created = created  # type: ignore[attr-defined]
    yield _register  # type: ignore[misc]
    for handler in created:
        _replay_handlers.pop(handler.domain, None)


def _entry(
    entry_id: str,
    domain: str,
    *,
    failure_type: str = OPEN_CIRCUIT,
    source: str | None = POLICY_CHAIN,
    retry_count: int = 0,
) -> FailedOperationData:
    metadata: dict[str, Any] = {}
    if source is not None:
        metadata["source"] = source
    return FailedOperationData(
        id=entry_id,
        domain=domain,
        failure_type=failure_type,
        status="pending",
        retry_count=retry_count,
        max_retries=2,
        metadata=metadata,
    )


def _service(entries: list[FailedOperationData] | None = None) -> ReplayService:
    """ReplayService over a recording repository with governance allowed.

    Governance is injected rather than patched so the test states the same
    thing with the private tier present or absent.
    """
    pool = {entry.id: entry for entry in (entries or [])}
    repo = MagicMock(spec=FailedOperationRepository)

    def _find(
        max_retries: int,
        domain: str | None = None,
        failure_type: str | None = None,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        return [
            entry
            for entry in pool.values()
            if entry.status == "pending"
            and entry.retry_count < max_retries
            and (domain is None or entry.domain == domain)
            and (failure_type is None or entry.failure_type == failure_type)
        ][:limit]

    def _acquire(dlq_id: str, max_retries: int) -> FailedOperationData | None:
        entry = pool.get(dlq_id)
        if entry is None or entry.status != "pending":
            return None
        if entry.retry_count >= max_retries:
            return None
        entry.retry_count += 1
        return entry

    repo.find_replayable.side_effect = _find
    repo.try_acquire_for_replay.side_effect = _acquire
    repo.get_by_id.side_effect = pool.get

    svc = ReplayService(repository=repo)
    svc._event_bus = MagicMock(spec=BaldurEventBus)
    svc._governance = MagicMock(spec=GovernanceChecker)
    svc._governance.check_all_governance.return_value = GovernanceCheckResult(
        allowed=True
    )
    svc._governance_resolved = True
    svc._pool = pool  # type: ignore[attr-defined]
    return svc


# =============================================================================
# Behavior — has_replay_handler
# =============================================================================


class TestReplayHandlerRegistryBehavior:
    """``has_replay_handler`` separates a real handler from the default one."""

    def test_unregistered_domain_has_no_handler(self, registered_handlers):
        assert has_replay_handler("never_registered_domain") is False

    def test_registered_domain_has_a_handler(self, registered_handlers):
        registered_handlers("payment_api")

        assert has_replay_handler("payment_api") is True

    def test_registration_does_not_leak_onto_a_neighbouring_domain(
        self, registered_handlers
    ):
        registered_handlers("payment_api")

        assert has_replay_handler("point_api") is False


# =============================================================================
# Behavior — auto-replay domain resolution
# =============================================================================


class TestOpenCircuitReplayDomainBehavior:
    """``_resolve_open_circuit_replay_domain`` decides whether the lane runs."""

    def test_registered_domain_resolves_to_itself(self, registered_handlers):
        registered_handlers("payment_api")
        svc = _service()

        assert svc._resolve_open_circuit_replay_domain("payment_api", []) == (
            "payment_api"
        )

    def test_reprojected_name_resolves_to_the_stored_form(self, registered_handlers):
        """The store filed the entry under the projection — so must the join."""
        registered_handlers("payment_api")
        svc = _service()

        assert svc._resolve_open_circuit_replay_domain("Payment-API", []) == (
            "payment_api"
        )

    def test_unregistered_domain_resolves_to_none(self, registered_handlers):
        """No handler means every selected entry burns its budget on a
        guaranteed failure, so the lane must not run at all."""
        svc = _service()

        assert svc._resolve_open_circuit_replay_domain("payment_api", []) is None

    def test_fallback_bucket_name_resolves_to_none(self, registered_handlers):
        """A name with no domain identity of its own shares one bucket with
        every other unclassifiable name — matching it is not an identity
        match, so those entries stay operator-driven."""
        registered_handlers("OTHER_DOMAIN")
        svc = _service()

        assert svc._resolve_open_circuit_replay_domain("3ds-gateway", []) is None

    def test_operator_mapped_type_resolves_to_none(self, registered_handlers):
        """The operator's own lane already covers the type — running both
        would fetch the same entries twice in one sweep."""
        registered_handlers("payment_api")
        svc = _service()

        assert (
            svc._resolve_open_circuit_replay_domain("payment_api", [OPEN_CIRCUIT])
            is None
        )

    def test_other_mapped_types_do_not_block_the_lane(self, registered_handlers):
        registered_handlers("payment_api")
        svc = _service()

        assert (
            svc._resolve_open_circuit_replay_domain("payment_api", ["TIMEOUT"])
            == "payment_api"
        )


# =============================================================================
# Behavior — circuit-close sweep auto-inclusion
# =============================================================================


class TestCircuitCloseSweepBehavior:
    """The sweep's open-circuit lane: placement, scope, filter, handler gate."""

    def test_auto_inclusion_fires_with_no_failure_type_map_at_all(
        self, registered_handlers
    ):
        """Placement: a plain `dlq=True` deployment configures no map, which
        is the case the empty-map early return used to swallow."""
        handler = registered_handlers("payment_api")
        svc = _service([_entry("e1", "payment_api")])

        result = svc.replay_on_circuit_close(
            service_name="payment_api", service_failure_type_map={}
        )

        assert handler.replayed == ["e1"]
        assert result.total == 1
        assert result.success_count == 1

    def test_no_handler_leaves_entries_pending_with_budget_unconsumed(
        self, registered_handlers
    ):
        """Negative half: nothing is fetched, nothing is acquired, no budget
        is spent — the entries stay for an operator."""
        svc = _service([_entry("e1", "payment_api")])

        result = svc.replay_on_circuit_close(
            service_name="payment_api", service_failure_type_map={}
        )

        assert result.total == 0
        svc.repository.try_acquire_for_replay.assert_not_called()
        assert svc._pool["e1"].status == "pending"
        assert svc._pool["e1"].retry_count == 0

    def test_lane_is_scoped_to_the_closing_service_domain(self, registered_handlers):
        """The fetch itself carries the domain — a still-open service's
        entries are never even read into the batch."""
        registered_handlers("payment_api")
        svc = _service([_entry("e1", "payment_api")])

        svc.replay_on_circuit_close(
            service_name="payment_api", service_failure_type_map={}
        )

        open_circuit_calls = [
            call.kwargs
            for call in svc.repository.find_replayable.call_args_list
            if call.kwargs.get("failure_type") == OPEN_CIRCUIT
        ]
        assert len(open_circuit_calls) == 1
        assert open_circuit_calls[0]["domain"] == "payment_api"

    def test_a_still_open_service_entries_keep_their_budget(self, registered_handlers):
        """The costly failure: replaying Y's entries into a dead dependency
        while X recovers burns them toward review."""
        handler = registered_handlers("payment_api")
        registered_handlers("async_gw")
        svc = _service([_entry("e1", "payment_api"), _entry("e_y", "async_gw")])

        svc.replay_on_circuit_close(
            service_name="payment_api", service_failure_type_map={}
        )

        assert handler.replayed == ["e1"]
        assert svc._pool["e_y"].retry_count == 0
        assert svc._pool["e_y"].status == "pending"

    def test_entries_from_another_capture_layer_are_filtered_out(
        self, registered_handlers
    ):
        """A request-boundary capture is stored under a path-inferred domain
        naming a different circuit, so a domain+type match is not proof."""
        handler = registered_handlers("payment_api")
        svc = _service(
            [
                _entry("e_chain", "payment_api"),
                _entry("e_boundary", "payment_api", source="BaldurMiddleware"),
            ]
        )

        svc.replay_on_circuit_close(
            service_name="payment_api", service_failure_type_map={}
        )

        assert handler.replayed == ["e_chain"]
        assert svc._pool["e_boundary"].retry_count == 0

    def test_entry_without_a_source_is_filtered_out(self, registered_handlers):
        """Absent metadata fails the filter — the default is to leave it."""
        handler = registered_handlers("payment_api")
        svc = _service([_entry("e_untagged", "payment_api", source=None)])

        svc.replay_on_circuit_close(
            service_name="payment_api", service_failure_type_map={}
        )

        assert handler.replayed == []

    def test_reprojected_service_name_round_trips_capture_to_replay(
        self, registered_handlers
    ):
        """End-to-end derivation: the protect name is `Payment-API`, the entry
        is filed as `payment_api`, and the closing circuit still finds it."""
        handler = registered_handlers("payment_api")
        svc = _service([_entry("e1", "payment_api")])

        svc.replay_on_circuit_close(
            service_name="Payment-API", service_failure_type_map={}
        )

        assert handler.replayed == ["e1"]

    def test_operator_mapped_types_still_run_alongside_the_lane(
        self, registered_handlers
    ):
        """The operator map keeps working; the lane is additive."""
        handler = registered_handlers("payment_api")
        svc = _service(
            [
                _entry("e_oc", "payment_api"),
                _entry("e_to", "payment_api", failure_type="TIMEOUT"),
            ]
        )

        svc.replay_on_circuit_close(
            service_name="payment_api",
            service_failure_type_map={"payment_api": ["TIMEOUT"]},
        )

        assert sorted(handler.replayed) == ["e_oc", "e_to"]

    def test_mapped_open_circuit_type_is_not_fetched_twice(self, registered_handlers):
        """An operator who maps the type explicitly gets one lane, not two."""
        registered_handlers("payment_api")
        svc = _service([_entry("e1", "payment_api")])

        svc.replay_on_circuit_close(
            service_name="payment_api",
            service_failure_type_map={"payment_api": [OPEN_CIRCUIT]},
        )

        open_circuit_calls = [
            call.kwargs
            for call in svc.repository.find_replayable.call_args_list
            if call.kwargs.get("failure_type") == OPEN_CIRCUIT
        ]
        assert len(open_circuit_calls) == 1
        # The operator's lane keeps today's unscoped, type-only selection.
        assert open_circuit_calls[0]["domain"] is None

    def test_misconfig_warning_is_suppressed_when_the_lane_applies(
        self, registered_handlers
    ):
        """The empty map is no longer a misconfiguration when the auto lane
        has something to do."""
        registered_handlers("payment_api")
        svc = _service([_entry("e1", "payment_api")])

        svc.replay_on_circuit_close(
            service_name="payment_api", service_failure_type_map={}
        )

        blocked = [
            call
            for call in svc._event_bus.emit.call_args_list
            if "block_reason" in (call.kwargs.get("data") or {})
        ]
        assert blocked == []

    def test_misconfig_warning_still_fires_when_no_lane_applies(
        self, registered_handlers
    ):
        """Negative control: with no map AND no handler it is still a
        misconfiguration an operator has to see."""
        svc = _service([_entry("e1", "payment_api")])

        svc.replay_on_circuit_close(
            service_name="payment_api", service_failure_type_map={}
        )

        blocked = [
            call
            for call in svc._event_bus.emit.call_args_list
            if "block_reason" in (call.kwargs.get("data") or {})
        ]
        assert len(blocked) == 1
