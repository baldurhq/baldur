"""An operator's close-with-replay on a breaker that is already CLOSED (807 D7).

A close on a breaker already CLOSED changes no state, so no CLOSED event
carries the operator's replay request. With ``trigger_replay`` the recovery
sweep is dispatched by the close itself — as the operator's own chain, which
their pin does not stop; without it nothing is dispatched. A dispatch that
cannot be made never fails the close.

The breaker service runs for real over the in-memory state repository; the one
dispatch path (``dispatch_recovery_sweep``) is the seam, since it publishes a
Celery task.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.interfaces.repositories import ResolutionTrigger
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur.services.event_bus.bus.event_types import EventType
from tests.factories import (
    InMemoryCircuitBreakerRepository,
    MockCircuitBreakerStateData,
)

NAME = "payment-api"
_DISPATCH = "baldur.services.replay_service.recovery.dispatch_recovery_sweep"
_SYSTEM_ENABLED = "baldur.services.circuit_breaker.manual_control._is_system_enabled"


@pytest.fixture
def breaker() -> CircuitBreakerService:
    service = CircuitBreakerService(
        config=CircuitBreakerConfig(enabled=True),
        repository=InMemoryCircuitBreakerRepository(),
    )
    service._event_bus = MagicMock(spec=BaldurEventBus)
    return service


def _closed_events(breaker) -> list[dict]:
    return [
        call.kwargs["data"]
        for call in breaker._event_bus.emit.call_args_list
        if call.args[0] == EventType.CIRCUIT_BREAKER_CLOSED
    ]


class TestForceCloseAlreadyClosedReplayBehavior:
    """The close itself carries the replay request a state change cannot."""

    def test_force_close_already_closed_with_replay_dispatches_one_operator_sweep(
        self, breaker
    ):
        with (
            patch(_SYSTEM_ENABLED, return_value=True),
            patch(_DISPATCH, autospec=True, return_value="dispatched") as dispatch,
        ):
            result = breaker.force_close(NAME, reason="drain", trigger_replay=True)

        assert result.success is True
        dispatch.assert_called_once_with(
            NAME,
            trigger=ResolutionTrigger.AUTO_REPLAY_CIRCUIT_CLOSE,
            escalate_failures=True,
            operator_requested=True,
        )
        assert _closed_events(breaker) == []

    def test_force_close_already_closed_without_replay_dispatches_nothing(
        self, breaker
    ):
        with (
            patch(_SYSTEM_ENABLED, return_value=True),
            patch(_DISPATCH, autospec=True) as dispatch,
        ):
            result = breaker.force_close(NAME, reason="pin it", trigger_replay=False)

        assert result.success is True
        dispatch.assert_not_called()

    def test_force_close_already_closed_replay_dispatch_failure_keeps_the_close(
        self, breaker
    ):
        with (
            patch(_SYSTEM_ENABLED, return_value=True),
            patch(_DISPATCH, autospec=True, side_effect=RuntimeError("broker down")),
            capture_logs() as logs,
        ):
            result = breaker.force_close(NAME, reason="drain", trigger_replay=True)

        assert result.success is True
        failed = [
            e
            for e in logs
            if e["event"] == "circuit_breaker.operator_replay_dispatch_failed"
        ]
        assert len(failed) == 1
        assert failed[0]["log_level"] == "warning"
        assert failed[0]["service_name"] == NAME

    def test_force_close_already_closed_replay_blocked_by_the_kill_switch(
        self, breaker
    ):
        """A close the kill switch refused dispatches nothing either."""
        with (
            patch(_SYSTEM_ENABLED, return_value=False),
            patch(_DISPATCH, autospec=True) as dispatch,
        ):
            result = breaker.force_close(NAME, reason="drain", trigger_replay=True)

        assert result.success is False
        dispatch.assert_not_called()

    def test_force_close_of_an_open_breaker_leaves_the_dispatch_to_its_closed_event(
        self, breaker
    ):
        breaker._repository._states[NAME] = MockCircuitBreakerStateData(
            service_name=NAME, state="open"
        )

        with (
            patch(_SYSTEM_ENABLED, return_value=True),
            patch(_DISPATCH, autospec=True) as dispatch,
        ):
            result = breaker.force_close(NAME, reason="recovered", trigger_replay=True)

        assert result.success is True
        dispatch.assert_not_called()
        closed = _closed_events(breaker)
        assert len(closed) == 1
        assert closed[0]["trigger_replay"] is True
