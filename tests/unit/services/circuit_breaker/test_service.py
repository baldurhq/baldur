"""
Tests for Circuit Breaker Service

Covers:
- CircuitBreakerService class
- State management
- Repository integration
- should_allow method
- get_state method

Refactored to use Factory Pattern (Phase 2):
- MockCircuitBreakerStateData → factories.MockCircuitBreakerStateData
- MockRepository → factories.InMemoryCircuitBreakerRepository
"""

from unittest.mock import patch

# Factory Pattern imports
from tests.factories import (
    InMemoryCircuitBreakerRepository,
    MockCircuitBreakerStateData,
)


class TestCircuitBreakerServiceInit:
    """Tests for CircuitBreakerService initialization."""

    def test_init_with_defaults(self):
        """Test initialization with default config."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig()
        mock_repo = InMemoryCircuitBreakerRepository()

        service = CircuitBreakerService(config=config, repository=mock_repo)

        assert service.config is config
        assert service._repository is mock_repo

    def test_init_without_config(self):
        """Test initialization without config loads from settings."""
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(repository=mock_repo)

        assert service.config is not None

    def test_is_enabled_property(self):
        """Test is_enabled property."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        assert service.is_enabled is True

    def test_is_enabled_false_by_default(self):
        """Test is_enabled is False by default."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig()
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        assert service.is_enabled is False


class TestCircuitBreakerStateQuery:
    """Tests for state query operations."""

    def test_get_or_create_state(self):
        """Test get_or_create_state creates new state."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig()
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        state = service.get_or_create_state("new_service")

        assert state.service_name == "new_service"
        assert state.state == "closed"

    def test_get_state(self):
        """Test get_state returns current state."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig()
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        state = service.get_state("test_service")
        assert state == "closed"

    def test_get_state_existing_service(self):
        """Test get_state for existing service."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig()
        mock_repo = InMemoryCircuitBreakerRepository()

        # Pre-populate with open state
        mock_repo._states["test_service"] = MockCircuitBreakerStateData(
            service_name="test_service", state="open"
        )

        service = CircuitBreakerService(config=config, repository=mock_repo)

        state = service.get_state("test_service")
        assert state == "open"


class TestShouldAllow:
    """Tests for should_allow method."""

    def test_should_allow_when_closed(self):
        """Test should_allow returns True when closed."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        assert service.should_allow("test_service") is True

    def test_should_allow_when_open(self):
        """Test should_allow returns False when open."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        mock_repo._states["test_service"] = MockCircuitBreakerStateData(
            service_name="test_service", state="open"
        )

        service = CircuitBreakerService(config=config, repository=mock_repo)

        assert service.should_allow("test_service") is False

    def test_should_allow_when_disabled(self):
        """Test should_allow returns True when CB disabled."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=False)
        mock_repo = InMemoryCircuitBreakerRepository()
        mock_repo._states["test_service"] = MockCircuitBreakerStateData(
            service_name="test_service", state="open"
        )

        service = CircuitBreakerService(config=config, repository=mock_repo)

        # When disabled, should always allow
        assert service.should_allow("test_service") is True

    def test_should_allow_half_open(self):
        """Test should_allow behavior when half-open."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        mock_repo._states["test_service"] = MockCircuitBreakerStateData(
            service_name="test_service", state="half_open"
        )

        service = CircuitBreakerService(config=config, repository=mock_repo)

        # Half-open should allow limited requests
        result = service.should_allow("test_service")
        assert isinstance(result, bool)


class TestRepositoryProperty:
    """Tests for repository property."""

    def test_repository_returns_injected(self):
        """Test repository property returns injected repository."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig()
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        assert service.repository is mock_repo

    def test_repository_lazy_creates_default(self):
        """Test repository creates default when not injected."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig()

        # Mock the factory/registry to avoid DB connection
        with patch("baldur.factory.ProviderRegistry") as mock_registry:
            mock_repo = InMemoryCircuitBreakerRepository()
            mock_registry.get_circuit_breaker_repo.return_value = mock_repo

            service = CircuitBreakerService(config=config)
            repo = service.repository

            assert repo is not None


class TestCircuitBreakerServiceIntegration:
    """Integration-style tests for CircuitBreakerService."""

    def test_full_lifecycle(self):
        """Test full circuit breaker lifecycle."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        # Initially closed
        assert service.get_state("test_service") == "closed"
        assert service.should_allow("test_service") is True

        # Force open (mocking system control)
        # Actor info is now read from ActorContext (SYSTEM_ACTOR fallback)
        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ):
            result = service.force_open(
                service_name="test_service",
                reason="Test",
            )
            assert result.success is True

        # Now should be open
        assert service.get_state("test_service") == "open"
        assert service.should_allow("test_service") is False

    def test_multiple_services_independent(self):
        """Test multiple services are independent."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(enabled=True)
        mock_repo = InMemoryCircuitBreakerRepository()
        service = CircuitBreakerService(config=config, repository=mock_repo)

        # Open service A
        mock_repo._states["service_a"] = MockCircuitBreakerStateData(
            service_name="service_a", state="open"
        )

        # Service B should still be closed
        assert service.get_state("service_a") == "open"
        assert service.get_state("service_b") == "closed"

        assert service.should_allow("service_a") is False
        assert service.should_allow("service_b") is True


# =============================================================================
# 490 D4 — hint_state pass-through on record_success / record_failure
# =============================================================================

from datetime import UTC
from unittest.mock import Mock

import pytest

from baldur.interfaces.repositories import (
    CircuitBreakerCloseAttempt,
    CircuitBreakerOpenAttempt,
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.service import CircuitBreakerService


def _make_hint(
    service_name: str,
    state: str = CircuitBreakerStateEnum.CLOSED.value,
    failure_count: int = 0,
    success_count: int = 0,
    manually_controlled: bool = False,
) -> CircuitBreakerStateData:
    """Construct a CircuitBreakerStateData hint for the parametrize cases."""
    return CircuitBreakerStateData(
        id=1,
        service_name=service_name,
        state=state,
        failure_count=failure_count,
        success_count=success_count,
        manually_controlled=manually_controlled,
    )


def _build_service_with_mock_repo() -> tuple[CircuitBreakerService, Mock]:
    """Service wired to a Mock repo with all hot-path methods stubbed.

    record_failure default returns a low-count state so the post-record
    threshold check in record_failure() doesn't open the circuit and call
    update_state(open) — keeps the hint-state assertions focused.
    """
    config = CircuitBreakerConfig(
        enabled=True,
        failure_threshold=100,  # high enough that record_failure won't open
        success_threshold=2,
        minimum_calls=10,
    )
    repo = Mock()
    repo.get_or_create = Mock(return_value=_make_hint("svc"))
    repo.update_state = Mock(return_value=True)
    repo.record_failure = Mock(return_value=_make_hint("svc", failure_count=1))
    repo.record_success = Mock(return_value=_make_hint("svc", success_count=1))
    # 497 D1/D2: HALF_OPEN branch now uses record_success_with_close_check.
    # Default to did_close=False (success_count=1 < threshold=2) to keep
    # the hint-state assertions focused on call-routing, not emit paths.
    repo.record_success_with_close_check = Mock(
        return_value=CircuitBreakerCloseAttempt(
            state=_make_hint(
                "svc",
                state=CircuitBreakerStateEnum.HALF_OPEN.value,
                success_count=1,
            ),
            did_close=False,
        )
    )
    # 656 D7: HALF_OPEN failure branch now uses record_failure_with_open_check.
    # Default to did_open=True (this caller performed the re-open) so the
    # call-routing assertions cover the side-effect path, symmetric with the
    # close-check stub above.
    repo.record_failure_with_open_check = Mock(
        return_value=CircuitBreakerOpenAttempt(
            state=_make_hint(
                "svc",
                state=CircuitBreakerStateEnum.OPEN.value,
            ),
            did_open=True,
        )
    )
    service = CircuitBreakerService(config=config, repository=repo)
    return service, repo


# Parametrize covers all 7 cases from the Test Assessment.
HINT_CASES = [
    pytest.param(None, id="None"),
    pytest.param(
        _make_hint("svc", state=CircuitBreakerStateEnum.CLOSED.value, failure_count=0),
        id="fresh-CLOSED-fc=0",
    ),
    pytest.param(
        _make_hint("svc", state=CircuitBreakerStateEnum.CLOSED.value, failure_count=3),
        id="stale-CLOSED-fc>0",
    ),
    pytest.param(
        _make_hint("svc", state=CircuitBreakerStateEnum.HALF_OPEN.value),
        id="HALF_OPEN",
    ),
    pytest.param(
        _make_hint("svc", state=CircuitBreakerStateEnum.OPEN.value),
        id="OPEN",
    ),
    pytest.param(
        _make_hint(
            "svc", state=CircuitBreakerStateEnum.CLOSED.value, manually_controlled=True
        ),
        id="manually_controlled=True",
    ),
    pytest.param(
        _make_hint("other-svc", state=CircuitBreakerStateEnum.CLOSED.value),
        id="mismatched-service_name",
    ),
]


class TestRecordSuccessHintStateBehavior:
    """490 D4 — record_success(hint_state=...) parametrize across all 7 hints.

    Fast path (fresh-CLOSED-fc=0) MUST NOT touch the repository at all.
    Slow paths reuse the hint when service_name matches; mismatched name
    falls through to a fresh get_or_create_state().
    """

    @pytest.mark.parametrize("hint", HINT_CASES)
    def test_record_success_with_hint(self, hint):
        service, repo = _build_service_with_mock_repo()

        # When: record_success() is invoked with the parametrized hint.
        service.record_success("svc", hint_state=hint)

        # Then: side-effect signature varies by case (see id).
        case_id = (
            hint.state
            if hint is not None and hint.service_name == "svc"
            else "fallback"
        )
        if hint is not None and hint.service_name == "svc":
            if (
                hint.state == CircuitBreakerStateEnum.CLOSED.value
                and not hint.manually_controlled
                and hint.failure_count == 0
            ):
                # Fast path: zero repository touches.
                assert repo.get_or_create.call_count == 0
                assert repo.update_state.call_count == 0
                assert repo.record_success.call_count == 0
            elif hint.manually_controlled:
                # Manual-control short-circuit: hint reused, no writes.
                assert repo.get_or_create.call_count == 0
                assert repo.update_state.call_count == 0
                assert repo.record_success.call_count == 0
            elif hint.state == CircuitBreakerStateEnum.HALF_OPEN.value:
                # HALF_OPEN success: hint reused, atomic record-success +
                # close-check called. 497 D1/D2 replaced the prior
                # record_success + threshold-check + update_state(close)
                # sequence with a single repository method.
                assert repo.get_or_create.call_count == 0
                assert repo.record_success_with_close_check.call_count == 1
                assert repo.record_success.call_count == 0
            elif hint.state == CircuitBreakerStateEnum.CLOSED.value:
                # Stale CLOSED with fc>0: hint reused (no get_or_create),
                # update_state(closed, fc=0) is called.
                assert repo.get_or_create.call_count == 0
                assert repo.update_state.call_count == 1
            elif hint.state == CircuitBreakerStateEnum.OPEN.value:
                # OPEN: no branch fires; just hint-reuse skip of get_or_create.
                assert repo.get_or_create.call_count == 0
                assert repo.update_state.call_count == 0
                assert repo.record_success.call_count == 0
            else:
                pytest.fail(f"unexpected hint case: {case_id}")
        else:
            # No hint or mismatched name: falls back to get_or_create_state.
            assert repo.get_or_create.call_count == 1


class TestRecordFailureHintStateBehavior:
    """490 D4 — record_failure(hint_state=...) parametrize across all 7 hints.

    Failures must always be recorded, so the fast path is narrower: the hint
    only skips the redundant get_or_create_state lookup. The eventual write
    (record_failure or update_state-revert) still runs.
    """

    @pytest.mark.parametrize("hint", HINT_CASES)
    def test_record_failure_with_hint(self, hint):
        service, repo = _build_service_with_mock_repo()

        # When: record_failure() is invoked with the parametrized hint.
        service.record_failure("svc", hint_state=hint)

        # Then: matched hint always skips get_or_create; failures are still
        # recorded (or, for HALF_OPEN, an update_state(OPEN) revert fires).
        if hint is not None and hint.service_name == "svc":
            assert repo.get_or_create.call_count == 0
            if hint.manually_controlled:
                assert repo.record_failure.call_count == 0
                assert repo.update_state.call_count == 0
            elif hint.state == CircuitBreakerStateEnum.HALF_OPEN.value:
                # 656 D7: HALF_OPEN failure reverts to OPEN via the atomic
                # record_failure_with_open_check primitive (the OPEN write
                # happens inside it), not a direct update_state call.
                assert repo.record_failure.call_count == 0
                assert repo.update_state.call_count == 0
                assert repo.record_failure_with_open_check.call_count == 1
            else:
                # CLOSED (fresh or stale) and OPEN both fall through to
                # repository.record_failure() (no OPEN-specific branch).
                assert repo.record_failure.call_count == 1
        else:
            # No hint or mismatched name: falls back to get_or_create_state.
            assert repo.get_or_create.call_count == 1
            assert repo.record_failure.call_count == 1


class TestRecordSuccessFastPathConversionRatio:
    """490 D4 testability gate — N=1000 steady-state CLOSED record_success
    calls with a fresh-CLOSED-fc=0 hint produce zero repository calls.

    Mock-equivalent of the "99.9% fast-path hit rate" production target,
    without introducing a runtime telemetry counter (which would re-add
    the dict-write contention this fix removes — see § Out of Scope).
    """

    def test_fast_path_makes_zero_repository_calls_over_1000_iterations(self):
        # Given: a service and a steady-state CLOSED+fc=0 hint.
        service, repo = _build_service_with_mock_repo()
        hint = _make_hint(
            "svc",
            state=CircuitBreakerStateEnum.CLOSED.value,
            failure_count=0,
        )

        # When: 1000 record_success calls with the steady-state hint.
        for _ in range(1000):
            service.record_success("svc", hint_state=hint)

        # Then: every method on the repo went untouched — full no-op fast path.
        assert repo.get_or_create.call_count == 0
        assert repo.update_state.call_count == 0
        assert repo.record_success.call_count == 0
        assert repo.record_failure.call_count == 0


class TestGetAllStatesSerializationBehavior:
    """get_all_states() emits JSON-safe temporal fields (impl 634 verify fix).

    Regression: after 634 D1 the console Circuit Breakers panel reads
    /control/status, which serializes get_all_states() to JSON in the admin
    server. Raw datetime opened_at/last_failure_at crashed json.dumps, dropping
    the connection unanswered, so the panel failed to render whenever any
    breaker was OPEN. get_all_states() now emits ISO-8601 strings.
    """

    def test_open_state_temporal_fields_are_iso_strings(self):
        from datetime import datetime

        from baldur.adapters.memory import InMemoryCircuitBreakerStateRepository
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        # Given: an OPEN breaker carrying datetime opened_at/last_failure_at.
        opened = datetime(2026, 6, 16, 3, 55, 0, tzinfo=UTC)
        repo = InMemoryCircuitBreakerStateRepository()
        repo.get_or_create("svc-open")
        repo.update_state(
            "svc-open",
            "open",
            failure_count=8,
            opened_at=opened,
            last_failure_at=opened,
        )
        service = CircuitBreakerService(repository=repo)

        # When: the dict snapshot is built.
        row = next(
            s for s in service.get_all_states() if s["service_name"] == "svc-open"
        )

        # Then: temporal fields are ISO strings, not datetime objects.
        assert isinstance(row["opened_at"], str)
        assert row["opened_at"] == opened.isoformat()
        assert row["last_failure_at"] == opened.isoformat()

    def test_full_snapshot_is_json_serializable_with_open_breaker(self):
        import json
        from datetime import datetime

        from baldur.adapters.memory import InMemoryCircuitBreakerStateRepository
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        repo = InMemoryCircuitBreakerStateRepository()
        repo.get_or_create("svc-open")
        repo.update_state(
            "svc-open",
            "open",
            opened_at=datetime(2026, 6, 16, 3, 55, 0, tzinfo=UTC),
        )
        service = CircuitBreakerService(repository=repo)

        # The bug was a json.dumps TypeError on the raw datetime — this must not raise.
        json.dumps(service.get_all_states())

    def test_closed_state_temporal_fields_stay_none(self):
        from baldur.adapters.memory import InMemoryCircuitBreakerStateRepository
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        repo = InMemoryCircuitBreakerStateRepository()
        repo.get_or_create("svc-closed")
        service = CircuitBreakerService(repository=repo)

        row = next(
            s for s in service.get_all_states() if s["service_name"] == "svc-closed"
        )
        assert row["opened_at"] is None
        assert row["last_failure_at"] is None
