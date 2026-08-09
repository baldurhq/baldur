"""The operator's manual pin reaches the view the traffic path reads.

The control surface used to resolve the registry's *default* repository while
``protect()``, the decorator and the presets decided from the *layered* one. A
Block therefore landed in a store admission never read: every request was
admitted for the whole lifetime of the block, and the admitted traffic's trial
successes closed the circuit outright.

These tests drive the real registry rather than a repository stub, because the
defect was entirely in which instance each side resolved — a stub hands both
sides the same object by construction and can never fail on it.

Pre-fix red run: with the resolution split restored (``repository`` resolving
``get_circuit_breaker_repo()`` with no name, ``_create_default_service``
resolving ``name="layered"``), the enforcement test admits 10/10 requests and
the resolution test resolves the durable-default instance.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.adapters.memory.layered_repository import (
    LayeredCircuitBreakerStateRepository,
    reset_layered_repository_executor,
)
from baldur.factory import ProviderRegistry
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.convenience import (
    get_circuit_breaker_service,
    reset_circuit_breaker_service,
)
from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.utils.time import utc_now

SERVICE = "payment-api"


@pytest.fixture
def wired_registry(monkeypatch):
    """Real default wiring: a layered view plus a distinct durable default.

    The two registered views are deliberately different objects, so a
    resolution that picks the wrong one is observable. ``"redis"`` is the
    registry's production default name and stands in here for the durable view
    the control surface used to write into; its backing store is in-memory so
    no infrastructure is involved.

    Yields ``(layered_view, durable_default_view)``.
    """
    # The layered constructor schedules background drift reconciliation and an
    # L2 prewarm on the shared executor; both would race the assertions here.
    monkeypatch.setattr(
        "baldur.adapters.memory.layered_repository.drift_operations."
        "DriftOperationsMixin._schedule_drift_reconciliation",
        lambda self: None,
    )
    monkeypatch.setattr(
        "baldur.adapters.memory.layered_repository.base."
        "LayeredRepositoryBase._ensure_l2_warmup_once",
        lambda self: None,
    )

    registry = ProviderRegistry.circuit_breaker_repo
    with registry.snapshot():
        registry.clear_instances()

        durable = InMemoryCircuitBreakerStateRepository()
        layered = LayeredCircuitBreakerStateRepository(l2_repo=durable)

        registry.register("layered", lambda: layered)
        registry.register("redis", lambda: durable)
        registry.set_default("redis")

        reset_circuit_breaker_service()
        try:
            yield layered, durable
        finally:
            reset_circuit_breaker_service()
            reset_layered_repository_executor()


def _config(**overrides) -> CircuitBreakerConfig:
    return CircuitBreakerConfig(enabled=True, **overrides)


# =============================================================================
# D1 — one resolution point, and it yields the layered view
# =============================================================================


class TestDefaultRepositoryResolutionBehavior:
    """Which instance the uninjected default resolves to."""

    def test_default_repository_resolution_yields_the_layered_view(
        self, wired_registry
    ):
        """The service's own property yields the layered view, not the default.

        Negative half: the resolved object is NOT the registry's default
        instance. Without it the assertion would hold for either view, since
        both satisfy the repository interface.
        """
        layered, durable = wired_registry

        service = CircuitBreakerService(config=_config())

        assert service.repository is layered
        assert service.repository is not durable

    def test_default_repository_resolution_is_shared_by_policy_and_service(
        self, wired_registry
    ):
        """The traffic path and the operator surface share one view.

        This is the invariant the split broke: two components resolving the
        same registry slot by different names got two different stores, and an
        operator's write to one was invisible to the other.
        """
        layered, _durable = wired_registry

        operator_side = get_circuit_breaker_service()
        traffic_side = CircuitBreakerPolicy(
            service_name=SERVICE, config=_config()
        ).cb_service

        assert operator_side.repository is traffic_side.repository is layered

    def test_default_repository_resolution_falls_back_when_layered_fails(
        self, wired_registry, monkeypatch
    ):
        """ "layered" unregistered or unconstructible → the old fallback chain.

        The fallback is what keeps a deployment that never registered the
        layered view working, so it must survive the resolution change.
        """
        _layered, durable = wired_registry
        registry = ProviderRegistry.circuit_breaker_repo
        registry.clear_instances()
        registry.register("layered", lambda: (_ for _ in ()).throw(RuntimeError("no")))

        service = CircuitBreakerService(config=_config())

        assert service.repository is durable

    def test_default_repository_resolution_is_cached_after_first_access(
        self, wired_registry
    ):
        """Repeated access does not re-resolve — the view cannot change mid-life.

        A service that re-resolved per access could straddle a registry
        mutation and read a different store than the one it wrote to.
        """
        layered, _durable = wired_registry
        service = CircuitBreakerService(config=_config())

        first = service.repository
        registry = ProviderRegistry.circuit_breaker_repo
        registry.clear_instances()
        registry.register("layered", InMemoryCircuitBreakerStateRepository)
        second = service.repository

        assert first is second is layered


# =============================================================================
# D1 — the end-to-end consequence: a Block is enforced on the traffic path
# =============================================================================


class TestManualPinEnforcementBehavior:
    """An operator Block, taken through the control surface, blocks traffic.

    Pre-fix red run: with the split resolution restored, ``executed`` reaches
    10 and ``rejected`` stays 0 — the scenario 1.12 measurement (15/15
    admitted during a live block) reproduced in-process.
    """

    def test_block_taken_through_the_control_surface_rejects_traffic(
        self, wired_registry
    ):
        """Zero of ten requests run while the block is live."""
        # Given: the operator blocks the service — the exact call the control
        # REST surface makes (``ControlAPIService._execute_block``).
        result = get_circuit_breaker_service().force_open(
            SERVICE, reason="incident", ttl_minutes=90
        )
        assert result.success is True

        # When: ten requests arrive on the protected path.
        policy: CircuitBreakerPolicy = CircuitBreakerPolicy(
            service_name=SERVICE, config=_config()
        )
        executed = 0

        def _call() -> str:
            nonlocal executed
            executed += 1
            return "ok"

        outcomes = [policy.execute(_call) for _ in range(10)]

        # Then: none of them reached the dependency.
        assert executed == 0
        assert all(r.outcome.value == "rejected" for r in outcomes)

    def test_admitted_traffic_cannot_close_a_pinned_circuit(self, wired_registry):
        """The pass-through was total, not bounded, because successes closed it.

        An Allow pinned CLOSED is the mirror case: traffic flows, but a burst
        of failures must not trip the circuit the operator pinned open — the
        record path skips on the pin.
        """
        # Given: the operator allows the service for the next 90 minutes.
        get_circuit_breaker_service().force_close(
            SERVICE, reason="known-noisy", ttl_minutes=90
        )

        # When: enough failures to trip an unpinned breaker several times over.
        policy: CircuitBreakerPolicy = CircuitBreakerPolicy(
            service_name=SERVICE, config=_config(failure_threshold=2)
        )
        for _ in range(6):
            with pytest.raises(RuntimeError):
                policy.execute(_raise)

        # Then: the operator's pin still stands and traffic still flows.
        layered, _durable = wired_registry
        row = layered.get_by_service_name(SERVICE)
        assert row.state == "closed"
        assert row.manually_controlled is True
        assert policy.execute(lambda: "ok").outcome.value == "success"

    def test_the_block_is_visible_in_the_durable_layer(self, wired_registry):
        """The pin reaches L2, so a process started later enforces it too.

        Without the write-through the durable row would carry the OPEN state
        with no pin, and the next process to hydrate would treat the block as
        an ordinary automatic open — recoverable by one trial success.
        """
        layered, durable = wired_registry

        get_circuit_breaker_service().force_open(
            SERVICE, reason="incident", ttl_minutes=90
        )

        durable_row = durable.get_by_service_name(SERVICE)
        assert durable_row.state == "open"
        assert durable_row.manually_controlled is True
        assert durable_row.manual_override_expires_at is not None
        assert (
            durable_row.manual_override_expires_at
            == layered.get_by_service_name(SERVICE).manual_override_expires_at
        )


def _raise() -> None:
    raise RuntimeError("dependency down")


# =============================================================================
# F2 — the flag's lifecycle: sweep + one post-expiry admission clears it
# =============================================================================


class TestOverrideExpiryLifecycleBehavior:
    """A lapsed pin stops being honoured, then stops being displayed.

    Two distinct events, in this order, on the Celery-less default shape:

    1. the admission path consumes the lift (``is_pin_lift_due``) and moves the
       row out of OPEN — the sweep deliberately does not do this, because it is
       an unsynchronized read-modify-write with no state-change audit;
    2. a later sweep clears ``manually_controlled`` on the now-non-OPEN row.

    Pre-fix, neither could happen through the control surface's own view: the
    sweep read one store and admission the other, so the flag stayed set until
    an operator reset it (scenario 1.12 measured 195 s and counting).
    """

    def test_override_expiry_lifecycle_stops_honouring_a_lapsed_block(
        self, wired_registry
    ):
        """The pin stops rejecting at its promised instant, sweep or no sweep."""
        layered, _durable = wired_registry
        service = CircuitBreakerService(config=_config(recovery_timeout=3600))
        _pin_expired_block(layered)

        assert service.should_allow(SERVICE) is True

    def test_override_expiry_lifecycle_leaves_a_due_lift_to_admission(
        self, wired_registry
    ):
        """The sweep skips a due lift: the flag survives one pass untouched.

        Clearing here would erase the only evidence that this OPEN was an
        operator block, and the recovery gate would then hold the circuit shut
        for the rest of ``recovery_timeout``.
        """
        layered, _durable = wired_registry
        service = CircuitBreakerService(config=_config(recovery_timeout=3600))
        _pin_expired_block(layered)

        cleared = service.check_and_expire_manual_overrides()

        assert cleared == []
        assert layered.get_by_service_name(SERVICE).manually_controlled is True

    def test_override_expiry_lifecycle_clears_the_flag_after_one_admission(
        self, wired_registry
    ):
        """The full lifecycle: admission consumes the lift, the sweep clears."""
        layered, _durable = wired_registry
        service = CircuitBreakerService(config=_config(recovery_timeout=3600))
        _pin_expired_block(layered)

        # When: one post-expiry request moves the row out of OPEN, then the
        # sweep runs against a row that is no longer a due lift.
        service.should_allow(SERVICE)
        cleared = service.check_and_expire_manual_overrides()

        # Then: the operator-facing "manual" label is gone.
        assert cleared == [SERVICE]
        assert layered.get_by_service_name(SERVICE).manually_controlled is False


def _pin_expired_block(repo) -> None:
    """Seed a manual block whose lifetime has already passed.

    Written straight to L1 because the row under test is already aged — the
    service layer refuses to create a pin with a past expiry.
    """
    now = utc_now()
    repo.set_manual_control(
        SERVICE,
        state="open",
        reason="incident",
        expires_at=now - timedelta(minutes=1),
    )
    # ``set_manual_control`` stamps ``opened_at`` at now, which reads as an
    # automatic OPEN written *after* the expiry. Move it before the expiry so
    # the row is the operator's own lapsed block — the case under test.
    row = repo._l1.get_by_service_name(SERVICE)
    repo._l1._storage[SERVICE] = _with_opened_at(row, now - timedelta(minutes=30))


def _with_opened_at(row, opened_at):
    from dataclasses import replace

    return replace(row, opened_at=opened_at)
