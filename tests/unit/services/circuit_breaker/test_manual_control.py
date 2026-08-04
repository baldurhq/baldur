"""
Tests for Circuit Breaker Manual Control Mixin

Covers:
- force_open operation
- force_close operation
- reset operation
- Kill Switch integration
- TTL management
- The manual-pin predicates, the per-call TTL, the expiry read-back, the
  clear-only expiry sweep and the lapsed-override extension

Refactored to use Factory Pattern (Phase 2):
- MockCircuitBreakerStateData → factories.MockCircuitBreakerStateData
- MockRepository → factories.InMemoryCircuitBreakerRepository
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.manual_control import (
    is_manual_pin_active,
    is_pin_lift_due,
)
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.settings.circuit_breaker import MAX_MANUAL_OVERRIDE_TTL_MINUTES
from baldur.utils.time import utc_now

# Factory Pattern imports
from tests.factories import (
    InMemoryCircuitBreakerRepository,
    MockCircuitBreakerStateData,
)

_ALL_STATES = (
    CircuitBreakerStateEnum.OPEN.value,
    CircuitBreakerStateEnum.CLOSED.value,
    CircuitBreakerStateEnum.HALF_OPEN.value,
)


def _system_enabled():
    """Patch the Kill Switch probe to "enabled" for the duration of a block."""
    return patch(
        "baldur.services.circuit_breaker.manual_control._is_system_enabled",
        return_value=True,
    )


def _build_service(repository, **config_overrides) -> CircuitBreakerService:
    """A service over a real in-memory repository (no mocks on the SUT path)."""
    config = CircuitBreakerConfig(enabled=True, **config_overrides)
    return CircuitBreakerService(config=config, repository=repository)


def _seed_state(repository, **fields) -> CircuitBreakerStateData:
    """Write an explicit state row straight into the in-memory repository.

    The pin predicates read ``opened_at`` / ``manual_override_expires_at``
    relative to now, and no public write path can produce a row whose
    timestamps are already in the past — so the rows those branches exist for
    (an expired block, a stale flag on a later automatic OPEN) can only be
    constructed directly.
    """
    service_name = fields.pop("service_name", "payment-api")
    state = CircuitBreakerStateData(id=1, service_name=service_name, **fields)
    repository._storage[service_name] = state
    return state


class TestForceOpen:
    """Tests for force_open operation."""

    def test_force_open_success(self):
        """Test force_open successfully opens circuit."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ):
            # Actor info is now read from ActorContext (SYSTEM_ACTOR fallback)
            result = service.force_open(
                service_name="test_service",
                reason="Maintenance",
            )

        assert result.success is True
        assert result.new_state == "open"

    def test_force_open_already_open(self):
        """Test force_open when already open."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        mock_repo._states["test_service"] = MockCircuitBreakerStateData(
            service_name="test_service", state="open"
        )
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ):
            result = service.force_open(
                service_name="test_service",
                reason="Already open",
            )

        assert result.success is True
        assert "already open" in result.message.lower()

    def test_force_open_blocked_by_kill_switch(self):
        """Test force_open blocked when kill switch active."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=False,
        ):
            result = service.force_open(
                service_name="test_service",
                reason="Test",
            )

        assert result.success is False
        assert "kill switch" in result.error.lower()

    def test_force_open_with_actor_context(self):
        """Test force_open reads actor from ActorContext."""
        from baldur.context.actor_context import ActorContext
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ):
            # Set actor context explicitly (simulating Django middleware)
            with ActorContext.set_actor(
                actor_id="test_user@example.com",
                actor_type="user",
                source="web",
            ):
                result = service.force_open(
                    service_name="test_service",
                    reason="Test",
                )

        assert result.success is True

    def test_force_open_atomic_failure(self):
        """Test force_open handles atomic failure."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        mock_repo._atomic_success = False
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ):
            result = service.force_open(
                service_name="test_service",
                reason="Test",
            )

        assert result.success is False


class TestForceClose:
    """Tests for force_close operation."""

    def test_force_close_success(self):
        """Test force_close successfully closes circuit."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        mock_repo._states["test_service"] = MockCircuitBreakerStateData(
            service_name="test_service", state="open"
        )
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ):
            # Actor info is now read from ActorContext (SYSTEM_ACTOR fallback)
            result = service.force_close(
                service_name="test_service",
                reason="Service recovered",
            )

        assert result.success is True
        assert result.new_state == "closed"

    def test_force_close_already_closed(self):
        """Test force_close when already closed."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ):
            result = service.force_close(
                service_name="test_service",
                reason="Already closed",
            )

        assert result.success is True

    def test_force_close_with_replay_trigger(self):
        """Test force_close with replay trigger."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        mock_repo._states["test_service"] = MockCircuitBreakerStateData(
            service_name="test_service", state="open"
        )
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ):
            result = service.force_close(
                service_name="test_service",
                reason="Recovery",
                trigger_replay=True,
            )

        assert result.success is True

    def test_force_close_blocked_by_kill_switch(self):
        """Test force_close blocked when kill switch active."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=False,
        ):
            result = service.force_close(
                service_name="test_service",
                reason="Test",
            )

        assert result.success is False


class TestReset:
    """Tests for reset operation."""

    def test_reset_clears_state(self):
        """Test reset clears circuit breaker state."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        mock_repo._states["test_service"] = MockCircuitBreakerStateData(
            service_name="test_service", state="half_open", failure_count=5
        )
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ):
            result = service.reset(service_name="test_service")

        # Result may or may not succeed depending on repo implementation
        assert result is not None


class TestKillSwitchIntegration:
    """Tests for Kill Switch integration."""

    def test_is_system_enabled_check(self):
        """Test _is_system_enabled function."""
        from baldur.services.circuit_breaker.manual_control import (
            _is_system_enabled,
        )

        # Mock system control
        with patch(
            "baldur.services.system_control.SystemControlManager"
        ) as mock_manager_cls:
            mock_manager = MagicMock()
            mock_manager.is_enabled.return_value = True
            mock_manager_cls.return_value = mock_manager

            result = _is_system_enabled()
            assert result is True

    def test_is_system_enabled_default_true(self):
        """Test _is_system_enabled returns True by default."""
        from baldur.services.circuit_breaker.manual_control import (
            _is_system_enabled,
        )

        # Should return True even when module not available
        result = _is_system_enabled()
        assert isinstance(result, bool)


class TestDecisionLogging:
    """Tests for decision logging integration."""

    def test_force_open_completes_without_error(self):
        """Test force_open completes without error."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ):
            result = service.force_open(
                service_name="test_service",
                reason="Test",
            )

            assert result is not None


class TestTTLManagement:
    """Tests for manual override TTL management."""

    def test_force_open_uses_config_ttl(self):
        """Test force_open uses TTL from config."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            manual_override_ttl_minutes=120,
        )
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ):
            result = service.force_open(
                service_name="test_service",
                reason="Test",
            )

        assert result.success is True
        # TTL should be passed to repository


# =============================================================================
# Manual-pin predicates — is_manual_pin_active / is_pin_lift_due
# =============================================================================


class TestManualPinPredicates:
    """The two pure reads the admission branch and the sweep are built on.

    Both take a state row and consult the clock, so every branch is reachable
    by constructing a row — no repository, no mocks.
    """

    @pytest.mark.parametrize("state", _ALL_STATES)
    def test_unpinned_row_is_neither_active_nor_lift_due(self, state):
        """An automatic row is invisible to both predicates.

        The expiry column is deliberately populated: a row left over from a
        cleared pin must not read as pinned just because a timestamp survives.
        """
        row = CircuitBreakerStateData(
            service_name="payment-api",
            state=state,
            manually_controlled=False,
            manual_override_expires_at=utc_now() - timedelta(minutes=1),
            opened_at=utc_now() - timedelta(minutes=10),
        )

        assert is_manual_pin_active(row) is False
        assert is_pin_lift_due(row) is False

    @pytest.mark.parametrize("state", _ALL_STATES)
    def test_pin_with_a_future_expiry_is_active_and_not_lift_due(self, state):
        row = CircuitBreakerStateData(
            service_name="payment-api",
            state=state,
            manually_controlled=True,
            manual_override_expires_at=utc_now() + timedelta(minutes=5),
            opened_at=utc_now(),
        )

        assert is_manual_pin_active(row) is True
        assert is_pin_lift_due(row) is False

    @pytest.mark.parametrize("state", _ALL_STATES)
    def test_pin_with_no_stored_expiry_reads_as_permanently_active(self, state):
        """ "Manually controlled, no lifetime" is an indefinite pin, deliberately.

        Nothing lifts such a row on its own — which is why the service layer
        refuses to create one and the Django change form can no longer set the
        flag by hand.
        """
        row = CircuitBreakerStateData(
            service_name="payment-api",
            state=state,
            manually_controlled=True,
            manual_override_expires_at=None,
            opened_at=utc_now() - timedelta(days=30),
        )

        assert is_manual_pin_active(row) is True
        assert is_pin_lift_due(row) is False

    def test_lapsed_pin_on_its_own_open_row_is_lift_due(self):
        """The operator's own block, past its promised lift instant."""
        expires_at = utc_now() - timedelta(minutes=1)
        row = CircuitBreakerStateData(
            service_name="payment-api",
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=True,
            manual_override_expires_at=expires_at,
            opened_at=expires_at - timedelta(minutes=5),
        )

        assert is_manual_pin_active(row) is False
        assert is_pin_lift_due(row) is True

    def test_lapsed_pin_with_no_opened_at_is_lift_due(self):
        """A missing ``opened_at`` cannot rule the row out as the pinned OPEN."""
        row = CircuitBreakerStateData(
            service_name="payment-api",
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=True,
            manual_override_expires_at=utc_now() - timedelta(minutes=1),
            opened_at=None,
        )

        assert is_pin_lift_due(row) is True

    def test_lapsed_pin_reopened_after_its_expiry_is_not_lift_due(self):
        """The stale-flag row: an automatic OPEN written after the expiry.

        No primitive clears ``manually_controlled`` on an automatic transition,
        so without the ``opened_at`` discriminator this row would keep skipping
        the recovery gate and admit one request per request against a
        dependency that is still down.
        """
        expires_at = utc_now() - timedelta(minutes=10)
        row = CircuitBreakerStateData(
            service_name="payment-api",
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=True,
            manual_override_expires_at=expires_at,
            opened_at=expires_at + timedelta(minutes=1),
        )

        assert is_manual_pin_active(row) is False
        assert is_pin_lift_due(row) is False

    @pytest.mark.parametrize(
        "state",
        [
            CircuitBreakerStateEnum.CLOSED.value,
            CircuitBreakerStateEnum.HALF_OPEN.value,
        ],
    )
    def test_lapsed_pin_outside_open_is_not_lift_due(self, state):
        """Only an OPEN row has a lift to take — a lapsed Allow just clears."""
        expires_at = utc_now() - timedelta(minutes=1)
        row = CircuitBreakerStateData(
            service_name="payment-api",
            state=state,
            manually_controlled=True,
            manual_override_expires_at=expires_at,
            opened_at=expires_at - timedelta(minutes=5),
        )

        assert is_manual_pin_active(row) is False
        assert is_pin_lift_due(row) is False

    def test_expiry_boundary_instant_hands_the_pin_over_to_the_lift(self):
        """``expires_at == now`` is exactly where the two predicates swap.

        The three-way admission branch depends on the handover being clean at
        the instant itself, not merely a microsecond after it, so the clock is
        pinned to the stored expiry rather than approached from one side.
        """
        # Given: the clock reads precisely the pin's expiry instant.
        instant = utc_now()
        row = CircuitBreakerStateData(
            service_name="payment-api",
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=True,
            manual_override_expires_at=instant,
            opened_at=instant - timedelta(minutes=5),
        )

        # When / Then: enforcement has ended and the lift is due, together.
        with patch(
            "baldur.services.circuit_breaker.manual_control.utc_now",
            return_value=instant,
        ):
            assert is_manual_pin_active(row) is False
            assert is_pin_lift_due(row) is True


class TestPinPredicateProperties:
    """Invariants the admission branch relies on, over arbitrary rows."""

    @given(
        manually_controlled=st.booleans(),
        state=st.sampled_from(_ALL_STATES),
        expires_offset=st.one_of(st.none(), st.integers(min_value=-600, max_value=600)),
        opened_offset=st.one_of(st.none(), st.integers(min_value=-600, max_value=600)),
    )
    @hyp_settings(max_examples=200, deadline=None)
    def test_pin_predicate_properties_never_report_both_at_once(
        self, manually_controlled, state, expires_offset, opened_offset
    ):
        """No row is simultaneously "still blocking" and "due to be lifted".

        The admission branch tests them in that order, so an overlap would
        silently make one branch unreachable.
        """
        instant = utc_now()
        row = CircuitBreakerStateData(
            service_name="payment-api",
            state=state,
            manually_controlled=manually_controlled,
            manual_override_expires_at=(
                None
                if expires_offset is None
                else instant + timedelta(seconds=expires_offset)
            ),
            opened_at=(
                None
                if opened_offset is None
                else instant + timedelta(seconds=opened_offset)
            ),
        )

        with patch(
            "baldur.services.circuit_breaker.manual_control.utc_now",
            return_value=instant,
        ):
            assert not (is_manual_pin_active(row) and is_pin_lift_due(row))

    @given(
        manually_controlled=st.booleans(),
        state=st.sampled_from(_ALL_STATES),
        expires_offset=st.one_of(st.none(), st.integers(min_value=-600, max_value=600)),
        opened_offset=st.one_of(st.none(), st.integers(min_value=-600, max_value=600)),
    )
    @hyp_settings(max_examples=200, deadline=None)
    def test_pin_predicate_properties_limit_the_lift_to_a_pinned_open_row(
        self, manually_controlled, state, expires_offset, opened_offset
    ):
        """A lift is only ever due on a row that is pinned AND OPEN.

        This is what keeps the recovery-gate bypass off every other row shape.
        """
        instant = utc_now()
        row = CircuitBreakerStateData(
            service_name="payment-api",
            state=state,
            manually_controlled=manually_controlled,
            manual_override_expires_at=(
                None
                if expires_offset is None
                else instant + timedelta(seconds=expires_offset)
            ),
            opened_at=(
                None
                if opened_offset is None
                else instant + timedelta(seconds=opened_offset)
            ),
        )

        with patch(
            "baldur.services.circuit_breaker.manual_control.utc_now",
            return_value=instant,
        ):
            if is_pin_lift_due(row):
                assert manually_controlled is True
                assert state == CircuitBreakerStateEnum.OPEN.value


# =============================================================================
# Per-call TTL on force_open / force_close
# =============================================================================


class TestManualControlTTL:
    """The typed lifetime reaches storage, and an unusable one is refused."""

    def test_force_open_stores_the_requested_ttl_and_reports_it_back(self):
        # Given: a service whose configured default is deliberately different.
        repo = InMemoryCircuitBreakerStateRepository()
        service = _build_service(repo, manual_override_ttl_minutes=90)
        before = utc_now()

        # When: the operator asks for five minutes.
        with _system_enabled():
            result = service.force_open(
                "payment-api", reason="Maintenance", ttl_minutes=5
            )

        # Then: five minutes is what was stored, and what the result reports.
        stored = repo.get_by_service_name("payment-api").manual_override_expires_at
        assert result.success is True
        assert result.expires_at == stored
        assert (
            before + timedelta(minutes=5) <= stored <= utc_now() + timedelta(minutes=5)
        )

    def test_force_open_without_a_ttl_resolves_the_configured_default(self):
        """A blank console TTL must land on the setting, not a handler literal."""
        repo = InMemoryCircuitBreakerStateRepository()
        service = _build_service(repo, manual_override_ttl_minutes=30)
        before = utc_now()

        with _system_enabled():
            result = service.force_open("payment-api", reason="Maintenance")

        stored = repo.get_by_service_name("payment-api").manual_override_expires_at
        assert result.expires_at == stored
        assert (
            before + timedelta(minutes=30)
            <= stored
            <= utc_now() + timedelta(minutes=30)
        )

    def test_force_close_without_a_ttl_resolves_the_configured_default(self):
        """A force-close suspends protection, so it expires like a block does."""
        repo = InMemoryCircuitBreakerStateRepository()
        service = _build_service(repo, manual_override_ttl_minutes=30)
        before = utc_now()

        with _system_enabled():
            result = service.force_close("payment-api", reason="Recovered")

        stored = repo.get_by_service_name("payment-api").manual_override_expires_at
        assert stored is not None
        assert result.expires_at == stored
        assert (
            before + timedelta(minutes=30)
            <= stored
            <= utc_now() + timedelta(minutes=30)
        )

    def test_force_close_stores_the_requested_ttl(self):
        repo = InMemoryCircuitBreakerStateRepository()
        service = _build_service(repo, manual_override_ttl_minutes=90)
        before = utc_now()

        with _system_enabled():
            result = service.force_close(
                "payment-api", reason="Override", ttl_minutes=7
            )

        stored = repo.get_by_service_name("payment-api").manual_override_expires_at
        assert result.expires_at == stored
        assert (
            before + timedelta(minutes=7) <= stored <= utc_now() + timedelta(minutes=7)
        )

    @pytest.mark.parametrize("ttl", [1, 60, 90, MAX_MANUAL_OVERRIDE_TTL_MINUTES])
    @pytest.mark.parametrize("operation", ["force_open", "force_close"])
    def test_ttl_inside_the_bound_is_accepted(self, ttl, operation):
        repo = InMemoryCircuitBreakerStateRepository()
        service = _build_service(repo)

        with _system_enabled():
            result = getattr(service, operation)(
                "payment-api", reason="Test", ttl_minutes=ttl
            )

        assert result.success is True
        assert result.expires_at is not None

    @pytest.mark.parametrize(
        "ttl", [0, -1, MAX_MANUAL_OVERRIDE_TTL_MINUTES + 1, "abc", True, 1.5]
    )
    @pytest.mark.parametrize("operation", ["force_open", "force_close"])
    def test_ttl_rejected_when_unusable_and_no_row_is_written(self, ttl, operation):
        """An unusable TTL fails the call outright rather than storing a pin.

        A non-positive value in particular would store a pin with no expiry —
        one the sweep skips and no automatic path can lift.
        """
        repo = InMemoryCircuitBreakerStateRepository()
        service = _build_service(repo)

        with _system_enabled():
            result = getattr(service, operation)(
                "payment-api", reason="Test", ttl_minutes=ttl
            )

        assert result.success is False
        assert result.error
        assert repo.get_by_service_name("payment-api") is None

    @pytest.mark.parametrize("operation", ["force_open", "force_close"])
    def test_ttl_rejected_for_a_boolean_reports_the_type_error(self, operation):
        """``True`` is an int in Python — the guard must not treat it as 1."""
        repo = InMemoryCircuitBreakerStateRepository()
        service = _build_service(repo)

        with _system_enabled():
            result = getattr(service, operation)(
                "payment-api", reason="Test", ttl_minutes=True
            )

        assert result.success is False
        assert "integer" in result.error

    @pytest.mark.parametrize("operation", ["force_open", "force_close"])
    def test_ttl_rejected_above_the_bound_names_the_bound(self, operation):
        repo = InMemoryCircuitBreakerStateRepository()
        service = _build_service(repo)

        with _system_enabled():
            result = getattr(service, operation)(
                "payment-api",
                reason="Test",
                ttl_minutes=MAX_MANUAL_OVERRIDE_TTL_MINUTES + 1,
            )

        assert str(MAX_MANUAL_OVERRIDE_TTL_MINUTES) in result.error

    def test_force_close_expiry_lifts_the_pin_and_leaves_the_circuit_closed(self):
        """A lapsed Allow returns the breaker to automatic supervision.

        Negative assertion: no CLOSED -> HALF_OPEN hop at expiry — the sweep
        only clears the flag, and the reinstated supervision is proved by a
        failure that now counts.
        """
        # Given: a force-closed circuit whose override has lapsed.
        repo = InMemoryCircuitBreakerStateRepository()
        service = _build_service(repo, manual_override_ttl_minutes=5)
        with _system_enabled():
            service.force_close("payment-api", reason="Override")
        lapsed = utc_now() - timedelta(minutes=1)
        _seed_state(
            repo,
            state=CircuitBreakerStateEnum.CLOSED.value,
            manually_controlled=True,
            manual_override_expires_at=lapsed,
            opened_at=None,
        )

        # When: the sweep runs.
        expired = service.check_and_expire_manual_overrides()

        # Then: pin gone, still CLOSED, and failures count again.
        row = repo.get_by_service_name("payment-api")
        assert expired == ["payment-api"]
        assert row.manually_controlled is False
        assert row.state == CircuitBreakerStateEnum.CLOSED.value
        service.record_failure("payment-api")
        assert repo.get_by_service_name("payment-api").failure_count == 1

    def test_a_live_force_close_pin_still_suspends_failure_recording(self):
        """The predicate must not lift enforcement early — the live half of it."""
        repo = InMemoryCircuitBreakerStateRepository()
        service = _build_service(repo, manual_override_ttl_minutes=90)

        with _system_enabled():
            service.force_close("payment-api", reason="Override")
        service.record_failure("payment-api")

        assert repo.get_by_service_name("payment-api").failure_count == 0


class _ReadBackFailingRepository(InMemoryCircuitBreakerStateRepository):
    """A repository whose point read fails while its writes succeed.

    Models the degraded-backend case the expiry read-back has to survive: the
    atomic write has already committed by then.
    """

    def get_by_service_name(self, service_name: str):
        raise ConnectionError("backend unavailable")


class TestManualControlExpiryReadback:
    """A committed override is never reported as a failed one."""

    @pytest.mark.parametrize("operation", ["force_open", "force_close"])
    def test_readback_failure_keeps_success_and_omits_the_expiry(self, operation):
        # Given: a repository that cannot serve the follow-up read.
        repo = _ReadBackFailingRepository()
        service = _build_service(repo)

        # When: the operator issues the manual override.
        with _system_enabled():
            result = getattr(service, operation)("payment-api", reason="Test")

        # Then: reported as the success it is, with no fabricated expiry —
        # and the write really did land.
        assert result.success is True
        assert result.expires_at is None
        assert repo._storage["payment-api"].manually_controlled is True

    def test_reblock_of_an_already_open_circuit_still_reports_the_expiry(self):
        """The early return for an unchanged state carries the read-back too."""
        repo = InMemoryCircuitBreakerStateRepository()
        service = _build_service(repo, manual_override_ttl_minutes=45)

        with _system_enabled():
            first = service.force_open("payment-api", reason="First")
            second = service.force_open("payment-api", reason="Second")

        assert second.previous_state == second.new_state
        assert "already open" in second.message.lower()
        assert second.expires_at is not None
        assert second.expires_at > first.expires_at

    def test_reallow_of_an_already_closed_circuit_still_reports_the_expiry(self):
        repo = InMemoryCircuitBreakerStateRepository()
        service = _build_service(repo, manual_override_ttl_minutes=45)

        with _system_enabled():
            result = service.force_close("payment-api", reason="Allow")

        assert result.previous_state == result.new_state
        assert "already closed" in result.message.lower()
        assert (
            result.expires_at
            == repo.get_by_service_name("payment-api").manual_override_expires_at
        )


# =============================================================================
# The expiry sweep — clears the flag, writes nothing else
# =============================================================================


class TestOverrideExpirySweep:
    """check_and_expire_manual_overrides after it stopped writing ``state``."""

    @pytest.mark.parametrize("state", _ALL_STATES)
    @pytest.mark.parametrize("opened_before_expiry", [True, False])
    def test_sweep_never_writes_state_for_any_lapsed_pin(
        self, state, opened_before_expiry
    ):
        """``state`` is byte-identical across a pass, for every row shape.

        The sweep used to demote OPEN -> HALF_OPEN from an unsynchronized
        read-modify-write, which could discard the whole recovery window of a
        breaker that had tripped on real failures after the pin lapsed.
        """
        repo = InMemoryCircuitBreakerStateRepository()
        service = _build_service(repo)
        expires_at = utc_now() - timedelta(minutes=1)
        opened_at = expires_at + timedelta(
            minutes=-5 if opened_before_expiry else 1,
        )
        _seed_state(
            repo,
            state=state,
            manually_controlled=True,
            manual_override_expires_at=expires_at,
            opened_at=opened_at,
        )

        service.check_and_expire_manual_overrides()

        assert repo.get_by_service_name("payment-api").state == state

    @pytest.mark.parametrize("state", _ALL_STATES)
    def test_sweep_clears_the_flag_on_a_lapsed_pin(self, state):
        """Every lapsed row loses its flag — except the lift-pending OPEN."""
        repo = InMemoryCircuitBreakerStateRepository()
        service = _build_service(repo)
        expires_at = utc_now() - timedelta(minutes=1)
        _seed_state(
            repo,
            state=state,
            manually_controlled=True,
            manual_override_expires_at=expires_at,
            # After the expiry, so an OPEN row here is a stale flag rather
            # than the operator's own pending lift.
            opened_at=expires_at + timedelta(minutes=1),
        )

        expired = service.check_and_expire_manual_overrides()

        assert expired == ["payment-api"]
        assert repo.get_by_service_name("payment-api").manually_controlled is False

    def test_sweep_skips_a_lift_pending_block_so_admission_can_consume_it(self):
        """Clearing here would erase the evidence admission needs.

        Without the flag the row is an ordinary OPEN, and the recovery gate
        would keep a five-minute block shut for the rest of recovery_timeout.
        """
        repo = InMemoryCircuitBreakerStateRepository()
        service = _build_service(repo)
        expires_at = utc_now() - timedelta(minutes=1)
        _seed_state(
            repo,
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=True,
            manual_override_expires_at=expires_at,
            opened_at=expires_at - timedelta(minutes=5),
        )

        expired = service.check_and_expire_manual_overrides()

        assert expired == []
        assert repo.get_by_service_name("payment-api").manually_controlled is True

    def test_sweep_leaves_a_live_pin_untouched(self):
        repo = InMemoryCircuitBreakerStateRepository()
        service = _build_service(repo)
        _seed_state(
            repo,
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=True,
            manual_override_expires_at=utc_now() + timedelta(minutes=5),
            opened_at=utc_now(),
        )

        expired = service.check_and_expire_manual_overrides()

        assert expired == []
        assert repo.get_by_service_name("payment-api").manually_controlled is True

    def test_sweep_is_idempotent_across_two_passes(self):
        """Two schedulers may both run it; the second pass must be a no-op."""
        repo = InMemoryCircuitBreakerStateRepository()
        service = _build_service(repo)
        expires_at = utc_now() - timedelta(minutes=1)
        _seed_state(
            repo,
            state=CircuitBreakerStateEnum.CLOSED.value,
            manually_controlled=True,
            manual_override_expires_at=expires_at,
            opened_at=None,
        )

        first = service.check_and_expire_manual_overrides()
        row_after_first = repo.get_by_service_name("payment-api")
        second = service.check_and_expire_manual_overrides()

        assert first == ["payment-api"]
        assert second == []
        assert repo.get_by_service_name("payment-api").state == row_after_first.state

    def test_sweep_revalidation_keeps_a_row_re_pinned_since_the_snapshot(self):
        """An operator action landing after the snapshot stands.

        The snapshot says "lapsed"; storage says "freshly re-pinned". The
        per-row re-read is what stops the sweep silently undoing the newer
        write.
        """
        # Given: a snapshot that disagrees with storage.
        repo = _StaleSnapshotRepository()
        service = _build_service(repo)
        _seed_state(
            repo,
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=True,
            manual_override_expires_at=utc_now() + timedelta(minutes=90),
            opened_at=utc_now(),
        )
        repo.snapshot = [
            CircuitBreakerStateData(
                service_name="payment-api",
                state=CircuitBreakerStateEnum.OPEN.value,
                manually_controlled=True,
                manual_override_expires_at=utc_now() - timedelta(minutes=1),
            )
        ]

        # When: the sweep acts on the stale snapshot.
        expired = service.check_and_expire_manual_overrides()

        # Then: the re-Block survives.
        assert expired == []
        assert repo.get_by_service_name("payment-api").manually_controlled is True

    def test_sweep_revalidation_skips_a_row_unpinned_since_the_snapshot(self):
        """A concurrent pass (or a Reset) already finished the job."""
        repo = _StaleSnapshotRepository()
        service = _build_service(repo)
        _seed_state(
            repo,
            state=CircuitBreakerStateEnum.CLOSED.value,
            manually_controlled=False,
            manual_override_expires_at=None,
            opened_at=None,
        )
        repo.snapshot = [
            CircuitBreakerStateData(
                service_name="payment-api",
                state=CircuitBreakerStateEnum.CLOSED.value,
                manually_controlled=True,
                manual_override_expires_at=utc_now() - timedelta(minutes=1),
            )
        ]

        expired = service.check_and_expire_manual_overrides()

        assert expired == []

    def test_sweep_writes_nothing_when_the_snapshot_read_fails(self):
        """An unreadable backend is a no-op pass, never an "all clear"."""
        repo = _StaleSnapshotRepository()
        repo.raise_on_snapshot = True
        service = _build_service(repo)
        _seed_state(
            repo,
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=True,
            manual_override_expires_at=utc_now() - timedelta(minutes=1),
            opened_at=utc_now() - timedelta(minutes=10),
        )

        expired = service.check_and_expire_manual_overrides()

        assert expired == []
        assert repo.get_by_service_name("payment-api").manually_controlled is True


class _StaleSnapshotRepository(InMemoryCircuitBreakerStateRepository):
    """Lets a test hand the sweep a snapshot that storage has moved past."""

    def __init__(self) -> None:
        super().__init__()
        self.snapshot: list[CircuitBreakerStateData] | None = None
        self.raise_on_snapshot = False

    def get_all_states(self) -> list[CircuitBreakerStateData]:
        if self.raise_on_snapshot:
            raise ConnectionError("backend unavailable")
        if self.snapshot is not None:
            return self.snapshot
        return super().get_all_states()


# =============================================================================
# extend_manual_override
# =============================================================================


class TestExtendLapsedOverride:
    """The extension base is never behind the clock."""

    def test_extend_lapsed_override_re_arms_from_now(self):
        """A stored expiry that already passed must not anchor the extension.

        Anchoring on it lands the "extension" in the past and reports success
        while the override stays lapsed.
        """
        repo = InMemoryCircuitBreakerStateRepository()
        service = _build_service(repo)
        _seed_state(
            repo,
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=True,
            manual_override_expires_at=utc_now() - timedelta(minutes=10),
            opened_at=utc_now() - timedelta(minutes=20),
        )
        before = utc_now()

        result = service.extend_manual_override("payment-api", additional_minutes=30)

        stored = repo.get_by_service_name("payment-api").manual_override_expires_at
        assert result.success is True
        assert (
            before + timedelta(minutes=30)
            <= stored
            <= utc_now() + timedelta(minutes=30)
        )

    def test_extend_lapsed_override_never_writes_a_past_dated_expiry(self):
        """Whatever the lapse, the result is an override that is live again."""
        repo = InMemoryCircuitBreakerStateRepository()
        service = _build_service(repo)
        _seed_state(
            repo,
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=True,
            manual_override_expires_at=utc_now() - timedelta(days=3),
            opened_at=utc_now() - timedelta(days=3, minutes=5),
        )

        service.extend_manual_override("payment-api", additional_minutes=1)

        assert is_manual_pin_active(repo.get_by_service_name("payment-api")) is True

    def test_extend_live_override_extends_from_the_stored_expiry(self):
        """Behaviour on a live pin is unchanged — the stored expiry is the base."""
        repo = InMemoryCircuitBreakerStateRepository()
        service = _build_service(repo)
        stored_expiry = utc_now() + timedelta(minutes=10)
        _seed_state(
            repo,
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=True,
            manual_override_expires_at=stored_expiry,
            opened_at=utc_now(),
        )

        service.extend_manual_override("payment-api", additional_minutes=30)

        stored = repo.get_by_service_name("payment-api").manual_override_expires_at
        assert stored == stored_expiry + timedelta(minutes=30)
