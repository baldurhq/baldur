"""Every automatic lane is scoped to one stored domain (807 D1, S3).

Target: ``baldur.services.replay_service.service.recovery_lanes`` — the one
place the sweep, its idle check and the recovery trial get their lane set —
and the sweep that selects through it.

An operator's ``service_failure_type_map`` used to put a mapped type in every
domain's sweep. It is now selected only under the stored domain of the service
name it is mapped to, and every lane needs that domain's replay handler.

Decision table (``recovery_lanes``):
- no handler / the unclassifiable bucket -> no lane at all;
- the mapped types of names projecting onto the domain, then the types the
  handler declares and the map does not, then the open-circuit lane
  (policy-chain captures only) unless the open-circuit type is mapped.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.adapters.memory import InMemoryFailedOperationRepository
from baldur.interfaces.governance import GovernanceChecker
from baldur.models.dlq import OPEN_CIRCUIT_FAILURE_TYPE, POLICY_CHAIN_CAPTURE_SOURCE
from baldur.models.governance import GovernanceCheckResult
from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur.services.replay_service import ReplayService
from baldur.services.replay_service.handlers import (
    _replay_handlers,
    register_replay_handler,
)
from baldur.services.replay_service.service import recovery_lanes
from baldur.utils.domain_validation import FALLBACK_DOMAIN
from tests.factories.replay_doubles import ScriptedReplayHandler

DOMAIN = "payment_api"
OTHER_DOMAIN = "orders_api"
MAPPED = "TIMEOUT"
DECLARED = "MAX_RETRIES_TIMEOUTERROR"
DECLARED_B = "MAX_RETRIES_CONNECTIONERROR"
_OPEN_CIRCUIT_LANE = (OPEN_CIRCUIT_FAILURE_TYPE, DOMAIN, POLICY_CHAIN_CAPTURE_SOURCE)


@pytest.fixture
def registry() -> Iterator[dict[str, ScriptedReplayHandler]]:
    """An empty handler registry for the test; the previous one restored after."""
    before = dict(_replay_handlers)
    _replay_handlers.clear()
    made: dict[str, ScriptedReplayHandler] = {}
    yield made
    _replay_handlers.clear()
    _replay_handlers.update(before)


class _UnreadableDeclaration(ScriptedReplayHandler):
    """A handler whose declared failure types cannot be read."""

    @property
    def auto_replay_failure_types(self) -> tuple[str, ...]:
        raise RuntimeError("declaration unreadable")


def _register(registry, domain: str = DOMAIN, declared=()) -> ScriptedReplayHandler:
    handler = ScriptedReplayHandler(domain, declared=declared)
    register_replay_handler(handler)
    registry[domain] = handler
    return handler


class TestRecoveryLanesBehavior:
    """The lane set one stored domain recovers through."""

    @pytest.mark.parametrize(
        ("failure_type_map", "declared", "expected"),
        [
            ({}, (), [_OPEN_CIRCUIT_LANE]),
            (
                {},
                (DECLARED, DECLARED_B),
                [
                    (DECLARED, DOMAIN, None),
                    (DECLARED_B, DOMAIN, None),
                    _OPEN_CIRCUIT_LANE,
                ],
            ),
            (
                {DOMAIN: [MAPPED]},
                (DECLARED,),
                [(MAPPED, DOMAIN, None), (DECLARED, DOMAIN, None), _OPEN_CIRCUIT_LANE],
            ),
            (
                {"Payment-API": [MAPPED]},
                (),
                [(MAPPED, DOMAIN, None), _OPEN_CIRCUIT_LANE],
            ),
            ({OTHER_DOMAIN: [MAPPED]}, (), [_OPEN_CIRCUIT_LANE]),
            (
                {DOMAIN: [OPEN_CIRCUIT_FAILURE_TYPE]},
                (),
                [(OPEN_CIRCUIT_FAILURE_TYPE, DOMAIN, None)],
            ),
            (
                {DOMAIN: [MAPPED, MAPPED], "payment-api": [MAPPED, DECLARED]},
                (DECLARED, DECLARED),
                [(MAPPED, DOMAIN, None), (DECLARED, DOMAIN, None), _OPEN_CIRCUIT_LANE],
            ),
            ({}, (OPEN_CIRCUIT_FAILURE_TYPE,), [_OPEN_CIRCUIT_LANE]),
            (
                {DOMAIN: "TIMEOUT", "Payment-API": [MAPPED, 7]},
                (),
                [(MAPPED, DOMAIN, None), _OPEN_CIRCUIT_LANE],
            ),
        ],
        ids=[
            "no_map",
            "declared_types",
            "own_name_mapped_first",
            "another_name_projecting_onto_the_domain",
            "another_domain_mapped",
            "open_circuit_mapped_replaces_the_capture_lane",
            "duplicates_collapse_in_order",
            "declared_open_circuit_is_not_a_second_lane",
            "malformed_map_values_ignored",
        ],
    )
    def test_recovery_lanes_with_a_handler(
        self, registry, failure_type_map, declared, expected
    ):
        _register(registry, declared=declared)

        assert recovery_lanes(DOMAIN, failure_type_map) == expected

    @pytest.mark.parametrize(
        "failure_type_map",
        [{}, {DOMAIN: [MAPPED]}, {DOMAIN: [OPEN_CIRCUIT_FAILURE_TYPE]}],
        ids=["no_map", "mapped", "open_circuit_mapped"],
    )
    def test_recovery_lanes_without_a_handler_is_empty_even_when_mapped(
        self, registry, failure_type_map
    ):
        """A default handler always fails: no lane may select for it."""
        _register(registry, domain=OTHER_DOMAIN)

        assert recovery_lanes(DOMAIN, failure_type_map) == []

    def test_recovery_lanes_for_the_unclassifiable_bucket_is_empty(self, registry):
        """The bucket pools unrelated names; nothing there is one job's."""
        _replay_handlers[FALLBACK_DOMAIN] = ScriptedReplayHandler(FALLBACK_DOMAIN)

        assert recovery_lanes(FALLBACK_DOMAIN, {FALLBACK_DOMAIN: [MAPPED]}) == []

    def test_recovery_lanes_unreadable_declaration_warns_and_keeps_the_other_lanes(
        self, registry
    ):
        register_replay_handler(_UnreadableDeclaration(DOMAIN))

        with capture_logs() as logs:
            lanes = recovery_lanes(DOMAIN, {DOMAIN: [MAPPED]})

        assert lanes == [(MAPPED, DOMAIN, None), _OPEN_CIRCUIT_LANE]
        warned = [
            e
            for e in logs
            if e["event"] == "replay_service.declared_failure_types_unreadable"
        ]
        assert len(warned) == 1
        assert warned[0]["log_level"] == "warning"


class TestRecoveryLanesSweepBehavior:
    """A mapped type is replayed only under the mapped service's own domain."""

    @pytest.fixture
    def service(self, registry) -> Iterator[ReplayService]:
        repo = InMemoryFailedOperationRepository()
        replay_service = ReplayService(
            repository=repo,
            cache=InMemoryCacheAdapter(key_prefix=f"t807l:{uuid.uuid4().hex}:"),
        )
        replay_service._event_bus = MagicMock(spec=BaldurEventBus)
        governance = MagicMock(spec=GovernanceChecker)
        governance.check_all_governance.return_value = GovernanceCheckResult(
            allowed=True
        )
        replay_service._governance = governance
        replay_service._governance_resolved = True
        with patch.object(
            ReplayService,
            "_get_replay_automation_config",
            autospec=True,
            return_value=None,
        ):
            yield replay_service

    def test_mapped_type_under_another_domain_stays_pending_untouched(
        self, service, registry
    ):
        # Given: the type is mapped to payment_api; both domains park it.
        payment = _register(registry, DOMAIN)
        orders = _register(registry, OTHER_DOMAIN)
        repo = service.repository
        own = repo.create(domain=DOMAIN, failure_type=MAPPED).id
        foreign = repo.create(domain=OTHER_DOMAIN, failure_type=MAPPED).id

        # When: payment_api recovers.
        result = service.replay_on_circuit_close(
            DOMAIN, max_items=10, service_failure_type_map={DOMAIN: [MAPPED]}
        )

        # Then
        assert result.success_count == 1
        assert payment.replayed == [own]
        assert orders.asked == []
        assert orders.replayed == []
        untouched = repo.get_by_id(foreign)
        assert (untouched.status, untouched.retry_count) == ("pending", 0)

    def test_mapped_type_is_not_selected_when_another_domain_recovers(
        self, service, registry
    ):
        _register(registry, DOMAIN)
        orders = _register(registry, OTHER_DOMAIN)
        repo = service.repository
        foreign = repo.create(domain=OTHER_DOMAIN, failure_type=MAPPED).id

        result = service.replay_on_circuit_close(
            OTHER_DOMAIN, max_items=10, service_failure_type_map={DOMAIN: [MAPPED]}
        )

        assert result.total == 0
        assert orders.replayed == []
        assert repo.get_by_id(foreign).status == "pending"

    def test_mapped_type_under_a_name_projecting_onto_the_domain_is_replayed(
        self, service, registry
    ):
        payment = _register(registry, DOMAIN)
        repo = service.repository
        own = repo.create(domain=DOMAIN, failure_type=MAPPED).id

        result = service.replay_on_circuit_close(
            "Payment-API",
            max_items=10,
            service_failure_type_map={"payment-api": [MAPPED]},
        )

        assert result.success_count == 1
        assert payment.replayed == [own]
