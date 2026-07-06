"""#485 D2/G1 — CB reject hot-path single-fetch regression tests.

Locks in the companion-API single-fetch contract:

- ``CircuitBreakerService.should_allow_with_state(name)`` returns a
  ``CircuitBreakerDecision`` pairing the bool admit decision with the
  resolved state, replacing the prior ``should_allow`` + ``get_state``
  call pair on ``CircuitBreakerPolicy.execute()``'s reject branch.
- ``CircuitBreakerPolicy.execute()`` invokes
  ``should_allow_with_state`` exactly once per call, never falling
  back to ``get_state`` for the rejection metadata.
- ``CircuitBreakerDecision`` is frozen + slots — value-typed companion
  result (mirrors ``CircuitBreakerFallbackResult`` precedent).
"""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

import pytest

from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)
from baldur.services.circuit_breaker.config import (
    CircuitBreakerConfig,
    CircuitBreakerDecision,
)
from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError
from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy
from baldur.services.circuit_breaker.service import CircuitBreakerService

# =============================================================================
# Contract — CircuitBreakerDecision dataclass shape
# =============================================================================


class TestCircuitBreakerDecisionContract:
    """``CircuitBreakerDecision`` is the public companion-API return type."""

    def test_decision_is_frozen_dataclass(self):
        """Decision is a dataclass and cannot be mutated post-construction."""
        decision = CircuitBreakerDecision(
            allowed=True,
            state=CircuitBreakerStateData(service_name="svc"),
        )

        assert dataclasses.is_dataclass(decision)
        with pytest.raises(dataclasses.FrozenInstanceError):
            decision.allowed = False  # type: ignore[misc]

    def test_decision_uses_slots(self):
        """``slots=True`` keeps allocation cost tuple-equivalent (no __dict__)."""
        decision = CircuitBreakerDecision(
            allowed=True,
            state=CircuitBreakerStateData(service_name="svc"),
        )
        assert not hasattr(decision, "__dict__")

    def test_decision_named_attribute_access(self):
        """Named-attribute access blocks the truthy-tuple footgun."""
        state = CircuitBreakerStateData(service_name="svc")
        decision = CircuitBreakerDecision(allowed=False, state=state)

        assert decision.allowed is False
        assert decision.state is state

    def test_decision_is_exported_from_config_module(self):
        """``CircuitBreakerDecision`` is in the config module's __all__."""
        from baldur.services.circuit_breaker import config

        assert "CircuitBreakerDecision" in config.__all__

    def test_decision_supports_dataclasses_replace(self):
        """``dataclasses.replace`` rebinds frozen+slots fields cleanly."""
        original = CircuitBreakerDecision(
            allowed=True,
            state=CircuitBreakerStateData(service_name="svc"),
        )
        updated = dataclasses.replace(original, allowed=False)

        assert updated.allowed is False
        assert updated.state is original.state


# =============================================================================
# Behavior — should_allow_with_state companion API
# =============================================================================


class TestShouldAllowWithStateBehavior:
    """``should_allow_with_state`` returns the (allowed, state) pair via a
    single repository round-trip across all three CB states."""

    def _build_service(
        self,
        *,
        state: str,
        opened_at: object = None,
        enabled: bool = True,
    ) -> tuple[CircuitBreakerService, MagicMock]:
        config = CircuitBreakerConfig(enabled=enabled)
        repo = MagicMock()
        state_data = CircuitBreakerStateData(
            service_name="svc",
            state=state,
            opened_at=opened_at,
        )
        repo.get_or_create.return_value = state_data
        repo.try_acquire_half_open_slot.return_value = (
            True,
            state,
            state,
        )
        service = CircuitBreakerService(config=config, repository=repo)
        return service, repo

    def test_disabled_returns_allowed_with_state(self):
        """is_enabled=False short-circuits with allowed=True + non-None state."""
        service, _ = self._build_service(state="closed", enabled=False)

        decision = service.should_allow_with_state("svc")

        assert decision.allowed is True
        assert decision.state.service_name == "svc"

    def test_closed_state_returns_allowed_decision(self):
        """CLOSED → admit and surface the resolved state."""
        service, repo = self._build_service(state=CircuitBreakerStateEnum.CLOSED.value)

        decision = service.should_allow_with_state("svc")

        assert decision.allowed is True
        assert decision.state.state == CircuitBreakerStateEnum.CLOSED.value
        repo.get_or_create.assert_called_once_with("svc")
        repo.try_acquire_half_open_slot.assert_not_called()

    def test_open_pre_recovery_returns_blocked_decision(self):
        """OPEN with opened_at=None / recovery_timeout NOT elapsed → reject."""
        service, repo = self._build_service(
            state=CircuitBreakerStateEnum.OPEN.value, opened_at=None
        )

        decision = service.should_allow_with_state("svc")

        assert decision.allowed is False
        assert decision.state.state == CircuitBreakerStateEnum.OPEN.value
        repo.try_acquire_half_open_slot.assert_not_called()

    def test_open_elapsed_routes_through_atomic_acquire(self):
        """OPEN with elapsed timeout → atomic acquire returns updated state."""
        from datetime import timedelta

        from baldur.utils.time import utc_now

        service, repo = self._build_service(
            state=CircuitBreakerStateEnum.OPEN.value,
            opened_at=utc_now() - timedelta(seconds=3600),
        )
        repo.try_acquire_half_open_slot.return_value = (
            True,
            CircuitBreakerStateEnum.OPEN.value,
            CircuitBreakerStateEnum.HALF_OPEN.value,
        )

        decision = service.should_allow_with_state("svc")

        repo.try_acquire_half_open_slot.assert_called_once()
        assert decision.allowed is True
        assert decision.state.state == CircuitBreakerStateEnum.HALF_OPEN.value

    def test_half_open_full_returns_blocked_decision(self):
        """HALF_OPEN with limit reached → atomic acquire denies."""
        service, repo = self._build_service(
            state=CircuitBreakerStateEnum.HALF_OPEN.value
        )
        repo.try_acquire_half_open_slot.return_value = (
            False,
            CircuitBreakerStateEnum.HALF_OPEN.value,
            CircuitBreakerStateEnum.HALF_OPEN.value,
        )

        decision = service.should_allow_with_state("svc")

        assert decision.allowed is False
        assert decision.state.state == CircuitBreakerStateEnum.HALF_OPEN.value


# =============================================================================
# Behavior — CircuitBreakerPolicy reject branch single-fetch
# =============================================================================


class TestCBPolicyStateCachingBehavior:
    """``CircuitBreakerPolicy.execute()`` reject branch never invokes the old
    ``get_state`` lookup — it must propagate ``decision.state.state`` directly
    through ``should_allow_with_state``."""

    def _build_policy(self, *, allowed: bool, state_str: str = "open"):
        cb_service = MagicMock()
        cb_service.is_enabled = True
        cb_service.should_allow_with_state.return_value = CircuitBreakerDecision(
            allowed=allowed,
            state=MagicMock(state=state_str),
        )
        policy = CircuitBreakerPolicy(
            service_name="payment_api",
            cb_service=cb_service,
            hooks=[],
        )
        return policy, cb_service

    def test_reject_calls_should_allow_with_state_exactly_once(self):
        """Reject branch invokes the companion API exactly once per execute()."""
        policy, cb_service = self._build_policy(allowed=False)

        policy.execute(lambda: "should_not_run")

        cb_service.should_allow_with_state.assert_called_once_with("payment_api")

    def test_reject_does_not_fall_back_to_get_state(self):
        """Old ``get_state`` lookup must NOT run on the reject path (G1)."""
        policy, cb_service = self._build_policy(allowed=False)

        policy.execute(lambda: "should_not_run")

        cb_service.get_state.assert_not_called()
        cb_service.get_or_create_state.assert_not_called()

    def test_reject_metadata_state_taken_from_decision(self):
        """``metadata['state']`` reflects ``decision.state.state``, not a fresh fetch."""
        policy, _ = self._build_policy(allowed=False, state_str="half_open")

        result = policy.execute(lambda: "nope")

        assert result.metadata["state"] == "half_open"

    def test_reject_error_is_circuit_breaker_open_error(self):
        """Rejection still surfaces ``CircuitBreakerOpenError`` for upstream callers."""
        policy, _ = self._build_policy(allowed=False)

        result = policy.execute(lambda: "nope")

        assert isinstance(result.error, CircuitBreakerOpenError)

    def test_admit_does_not_record_failure_on_success(self):
        """Admit branch executes fn and records success only once.

        490 D4: ``record_success`` receives ``hint_state=decision.state`` so the
        service can short-circuit the redundant repository fetch.
        """
        policy, cb_service = self._build_policy(allowed=True, state_str="closed")
        decision = cb_service.should_allow_with_state.return_value

        result = policy.execute(lambda: "ok")

        assert result.value == "ok"
        cb_service.should_allow_with_state.assert_called_once_with("payment_api")
        cb_service.record_success.assert_called_once_with(
            "payment_api", hint_state=decision.state
        )
        cb_service.record_failure.assert_not_called()
