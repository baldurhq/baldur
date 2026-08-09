"""What the two layers carry between them, and in which direction.

Before this, the layers only ever moved four state fields — ``state``,
``failure_count``, ``success_count``, ``opened_at`` — in either direction. An
operator's pin therefore could not cross: a process started after a Block
hydrated the OPEN state without the manual-control fields, so its first trial
success closed the circuit the operator had pinned shut.

Hydration (L2→L1) now goes through one locked primitive that carries the pin.
The generic mirror (L1→L2) stays narrow on purpose, so one process's unpinned
snapshot cannot clear an operator's pin in the shared store. The assembled
class declares both sets plus the fields excluded from bulk transfer, and the
ratchet here fails when a new DTO field is added without a classification.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.adapters.memory.layered_repository import (
    LayeredCircuitBreakerStateRepository,
)
from baldur.interfaces.repositories import CircuitBreakerStateData
from baldur.utils.time import utc_now

SERVICE = "payment-api"


def _pinned_snapshot(**overrides) -> CircuitBreakerStateData:
    """A durable row as an operator's Block leaves it."""
    now = utc_now()
    fields = {
        "service_name": SERVICE,
        "state": "open",
        "failure_count": 4,
        "success_count": 1,
        "last_failure_at": now - timedelta(minutes=2),
        "opened_at": now - timedelta(minutes=10),
        "manually_controlled": True,
        "controlled_by_id": 7,
        "control_reason": "incident-4821",
        "manual_override_expires_at": now + timedelta(minutes=80),
        "metadata": {"runbook": "PAY-17"},
    }
    fields.update(overrides)
    return CircuitBreakerStateData(**fields)


# =============================================================================
# D2 — the hydration primitive carries the pin
# =============================================================================


class TestHydrateSnapshotBehavior:
    """``InMemoryCircuitBreakerStateRepository.hydrate_snapshot``."""

    @pytest.mark.parametrize(
        "field",
        [
            "state",
            "failure_count",
            "success_count",
            "opened_at",
            "last_failure_at",
            "manually_controlled",
            "controlled_by_id",
            "control_reason",
            "manual_override_expires_at",
            "metadata",
        ],
    )
    def test_declared_hydration_field_survives_into_l1(self, field):
        """Every field the class declares as hydrated actually crosses.

        The parametrization is the point: the four state fields crossed before
        and the six manual-control/context fields did not, which is precisely
        why a Block did not survive into a process that had not taken it.
        """
        repo = InMemoryCircuitBreakerStateRepository()
        snapshot = _pinned_snapshot()

        repo.hydrate_snapshot(snapshot)

        assert getattr(repo.get_by_service_name(SERVICE), field) == getattr(
            snapshot, field
        )

    def test_hydrating_an_absent_row_creates_it(self):
        """Create half of create-or-replace: the row need not pre-exist."""
        repo = InMemoryCircuitBreakerStateRepository()

        repo.hydrate_snapshot(_pinned_snapshot())

        row = repo.get_by_service_name(SERVICE)
        assert row is not None
        assert row.id is not None
        assert row.created_at is not None

    def test_hydrating_an_existing_row_replaces_its_payload(self):
        """Replace half: a local unpinned row is overwritten by the snapshot."""
        repo = InMemoryCircuitBreakerStateRepository()
        repo.get_or_create(SERVICE)
        repo.record_failure(SERVICE)

        repo.hydrate_snapshot(_pinned_snapshot())

        row = repo.get_by_service_name(SERVICE)
        assert row.state == "open"
        assert row.manually_controlled is True
        assert row.control_reason == "incident-4821"

    def test_layer_local_identity_and_half_open_window_are_preserved(self):
        """Identity and the half-open window belong to L1, not to a snapshot.

        The window fields are owned by the atomic slot-acquire primitives; a
        bulk copy would race them. ``id``/``created_at`` are layer-local, and a
        replaced ``id`` would break rows already handed out by reference.
        """
        repo = InMemoryCircuitBreakerStateRepository()
        existing = repo.get_or_create(SERVICE)
        repo.update_state(SERVICE, state="half_open", half_open_request_count=2)
        before = repo.get_by_service_name(SERVICE)

        repo.hydrate_snapshot(_pinned_snapshot())

        after = repo.get_by_service_name(SERVICE)
        assert after.id == existing.id
        assert after.created_at == before.created_at
        assert after.half_open_request_count == before.half_open_request_count
        assert after.half_open_window_started_at == before.half_open_window_started_at

    def test_hydration_does_not_restamp_opened_at(self):
        """The two-step ``set_manual_control`` + ``update_state`` alternative
        restamps ``opened_at`` on an OPEN row, which corrupts the discriminator
        that decides whether a pin's lift is due. One locked replace does not.
        """
        repo = InMemoryCircuitBreakerStateRepository()
        snapshot = _pinned_snapshot()

        repo.hydrate_snapshot(snapshot)

        assert repo.get_by_service_name(SERVICE).opened_at == snapshot.opened_at

    def test_repeated_hydration_of_the_same_snapshot_is_idempotent(self):
        """Restores run at construction and on the admin force-resync; a second
        pass must not drift the row it already wrote."""
        repo = InMemoryCircuitBreakerStateRepository()
        snapshot = _pinned_snapshot()

        repo.hydrate_snapshot(snapshot)
        first = repo.get_by_service_name(SERVICE)
        repo.hydrate_snapshot(snapshot)
        second = repo.get_by_service_name(SERVICE)

        assert dataclasses.replace(first, updated_at=None) == dataclasses.replace(
            second, updated_at=None
        )

    def test_snapshot_metadata_is_copied_not_aliased(self):
        """A shared dict would let an L2 row mutate through into L1."""
        repo = InMemoryCircuitBreakerStateRepository()
        snapshot = _pinned_snapshot()

        repo.hydrate_snapshot(snapshot)
        snapshot.metadata["runbook"] = "MUTATED"

        assert repo.get_by_service_name(SERVICE).metadata == {"runbook": "PAY-17"}


# =============================================================================
# D2 — the two hydration lanes route through that primitive
# =============================================================================


class TestHydrationLanesCarryThePinBehavior:
    """Construction-time load and miss-hydration, end to end."""

    def test_construction_time_load_hydrates_a_pinned_row(self):
        """A process started after a Block enforces it from its first request.

        Pre-fix red run: the initial load ran ``get_or_create`` +
        ``update_state`` with the four state fields, so the new L1 row was OPEN
        with ``manually_controlled=False``.
        """
        l2 = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
        l2.get_all_states.return_value = [_pinned_snapshot()]

        repo = LayeredCircuitBreakerStateRepository(l2_repo=l2)

        row = repo.get_by_service_name(SERVICE)
        assert row.state == "open"
        assert row.manually_controlled is True
        assert row.manual_override_expires_at is not None

    def test_l1_miss_hydration_carries_the_pin(self):
        """A row that appears in L2 after construction arrives pinned too."""
        l2 = InMemoryCircuitBreakerStateRepository()
        repo = LayeredCircuitBreakerStateRepository(l2_repo=l2)
        l2.hydrate_snapshot(_pinned_snapshot())

        row = repo.get_by_service_name(SERVICE)

        assert row.manually_controlled is True
        assert row.control_reason == "incident-4821"


# =============================================================================
# D5 — the declared field sets, and the ratchet over them
# =============================================================================


class TestLayeredSyncedFieldsContract:
    """The declaration on the assembled class is complete and consistent."""

    def test_synced_field_declaration_classifies_every_state_field(self):
        """Ratchet: a new DTO field must be declared before it can ship.

        Undeclared fields are how the pin got lost in the first place — the
        manual-control columns existed on the DTO for far longer than any lane
        that carried them.
        """
        cls = LayeredCircuitBreakerStateRepository
        declared = (
            set(cls._L1_HYDRATION_FIELDS)
            | set(cls._TRANSFER_EXCLUDED_FIELDS)
            | {"service_name"}  # the row key, not payload
        )
        dto_fields = {f.name for f in dataclasses.fields(CircuitBreakerStateData)}

        assert dto_fields == declared

    def test_synced_field_sets_do_not_overlap(self):
        """A field cannot be both carried and documented-excluded."""
        cls = LayeredCircuitBreakerStateRepository

        assert not set(cls._L1_HYDRATION_FIELDS) & set(cls._TRANSFER_EXCLUDED_FIELDS)

    def test_synced_field_mirror_is_a_strict_subset_of_hydration(self):
        """The mirror is deliberately narrower — never wider.

        A mirror carrying the manual-control fields would let one process's
        unpinned snapshot clear an operator's pin in the shared store.
        """
        cls = LayeredCircuitBreakerStateRepository
        mirror = set(cls._L2_STATE_MIRROR_FIELDS)
        hydration = set(cls._L1_HYDRATION_FIELDS)

        assert mirror < hydration

    def test_synced_field_mirror_carries_no_manual_control_field(self):
        """Named negative assertion for the property the subset test implies."""
        cls = LayeredCircuitBreakerStateRepository
        manual_fields = {
            "manually_controlled",
            "controlled_by_id",
            "control_reason",
            "manual_override_expires_at",
        }

        assert not manual_fields & set(cls._L2_STATE_MIRROR_FIELDS)

    def test_synced_field_mirror_lane_transfers_its_declared_set(self):
        """Lane coverage: what ``_sync_to_l2_with_timeout`` actually writes.

        The declaration is only worth something if the lane matches it, so the
        assertion is on the L2 row the lane produced, not on the tuple.
        """
        l2 = InMemoryCircuitBreakerStateRepository()
        repo = LayeredCircuitBreakerStateRepository(l2_repo=l2)
        source = _pinned_snapshot()
        repo._l1.hydrate_snapshot(source)

        repo._sync_to_l2_with_timeout(SERVICE, source)

        mirrored = l2.get_by_service_name(SERVICE)
        for field in LayeredCircuitBreakerStateRepository._L2_STATE_MIRROR_FIELDS:
            assert getattr(mirrored, field) == getattr(source, field)
        assert mirrored.manually_controlled is False

    def test_synced_field_hydration_lane_transfers_its_declared_set(self):
        """Lane coverage for the other direction."""
        l2 = InMemoryCircuitBreakerStateRepository()
        source = _pinned_snapshot()
        l2.hydrate_snapshot(source)
        repo = LayeredCircuitBreakerStateRepository(l2_repo=l2)

        hydrated = repo._l1.get_by_service_name(SERVICE)
        for field in LayeredCircuitBreakerStateRepository._L1_HYDRATION_FIELDS:
            assert getattr(hydrated, field) == getattr(source, field)


# =============================================================================
# #227 §7.4 — the hot-path guarantee the layered view exists for
# =============================================================================


class TestClosedAdmissionNoL2Contract:
    """A CLOSED admission that hits L1 performs no synchronous L2 I/O.

    This is what made the layered view the right default in the first place;
    routing every default consumer through it (D1) must not have moved Redis
    onto the admission path.
    """

    def test_closed_admission_no_l2_io_on_an_l1_hit(self):
        """Negative assertion over the whole L2 surface, not one method."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService
        from baldur.settings.circuit_breaker import (
            get_circuit_breaker_settings,
            reset_circuit_breaker_settings,
        )

        # Guard the premise: the cold-start L2 read on an L1 miss is flag-gated
        # and default-off. If that default ever flips, this test's subject
        # changes and it must be revisited rather than silently still passing.
        reset_circuit_breaker_settings()
        assert get_circuit_breaker_settings().cluster_state_propagation_enabled is False

        l2 = MagicMock(spec=InMemoryCircuitBreakerStateRepository)
        l2.get_all_states.return_value = []
        repo = LayeredCircuitBreakerStateRepository(l2_repo=l2)
        service = CircuitBreakerService(
            config=CircuitBreakerConfig(enabled=True), repository=repo
        )
        # Warm the L1 row, then forget everything L2 saw during the warm-up.
        service.should_allow(SERVICE)
        l2.reset_mock()

        allowed = service.should_allow(SERVICE)

        assert allowed is True
        assert l2.method_calls == []
