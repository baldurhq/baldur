"""TieringCircuitBreaker state casing and trip/recovery transitions."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from baldur.scaling.tiering.circuit_breaker import (
    TieringCircuitBreaker,
    get_tiering_circuit_breaker,
)


@pytest.fixture
def tiering_cb():
    """Reset the singleton's state around each test (instance persists)."""
    cb = get_tiering_circuit_breaker()
    cb.reset()
    yield cb
    cb.reset()


def _trip(cb: TieringCircuitBreaker) -> None:
    """Record enough consecutive failures to open the breaker."""
    for _ in range(TieringCircuitBreaker.FAILURE_THRESHOLD):
        cb.record_failure(RuntimeError("regex evaluation failed"))


class TestTieringCircuitBreakerStateCasing:
    """Live state values are the canonical lowercase CircuitBreakerStateEnum values."""

    def test_initial_state_is_canonical_closed(self, tiering_cb):
        assert tiering_cb.state == "closed"
        assert tiering_cb.is_open is False

    def test_tripped_state_is_canonical_open(self, tiering_cb):
        _trip(tiering_cb)

        assert tiering_cb.state == "open"
        assert tiering_cb.is_open is True

    def test_recovery_cycle_states_are_canonical(self, tiering_cb):
        """open → half_open (delay elapsed) → closed (success) all lowercase."""
        _trip(tiering_cb)
        tiering_cb._last_failure_time = (
            time.time() - TieringCircuitBreaker.HALF_OPEN_DELAY_SEC - 1
        )

        assert tiering_cb.is_open is False
        assert tiering_cb.state == "half_open"

        tiering_cb.record_success(latency_ms=1.0)
        assert tiering_cb.state == "closed"

    def test_trip_audit_payload_uses_canonical_states(self, tiering_cb):
        """The log_config_change audit payload escapes canonical lowercase values."""
        with patch("baldur.audit.log_config_change") as mock_log:
            _trip(tiering_cb)

        mock_log.assert_called_once()
        kwargs = mock_log.call_args.kwargs
        assert kwargs["old_value"] == "closed"
        assert kwargs["new_value"]["state"] == "open"
