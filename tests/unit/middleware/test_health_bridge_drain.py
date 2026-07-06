"""HealthBridgeMiddleware drain integration tests (impl 471 D1).

Coverage:
- Bridge endpoint returns 503 + Connection: close + Retry-After +
  ``status: draining`` payload during DRAINING phase
- Bridge endpoint returns 200 + ``status: bridge_active`` during RUNNING
- ``_get_drain_state()`` failure-mode: coordinator probe raises → returns
  None → bridge stays 200 (best-effort, must never wedge readiness)
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from baldur.api.django.middleware.health_bridge import HealthBridgeMiddleware
from baldur.core.shutdown_coordinator import ShutdownPhase, ShutdownStats

# =============================================================================
# Test helpers
# =============================================================================


class _FakeRequest:
    def __init__(self, path: str):
        self.path = path
        self.method = "GET"
        self.META: dict = {}


def _make_middleware():
    """Build a HealthBridgeMiddleware with a no-op get_response."""
    return HealthBridgeMiddleware(get_response=lambda req: MagicMock())


def _coordinator_in_phase(
    phase: ShutdownPhase,
    *,
    remaining: float | None = 12.0,
    in_flight: int = 3,
):
    coord = MagicMock()
    coord.phase = phase
    coord.get_stats.return_value = ShutdownStats(
        phase=phase,
        shutdown_started_at=None,
        in_flight_count=in_flight,
        completed_during_drain=0,
        aborted_count=0,
        drain_timeout_seconds=30.0,
        remaining_drain_time=remaining,
    )
    return coord


# =============================================================================
# Bridge endpoint behavior across phases
# =============================================================================


class TestHealthBridgeDrainBehavior:
    """``_serve_bridge_response`` matrix: RUNNING vs DRAINING."""

    @pytest.mark.parametrize("path", HealthBridgeMiddleware.BRIDGE_PATHS)
    def test_running_phase_returns_200_bridge_active(self, path):
        """RUNNING phase keeps the existing ``bridge_active`` 200 response."""
        mw = _make_middleware()
        coord = _coordinator_in_phase(ShutdownPhase.RUNNING)

        with patch(
            "baldur.core.shutdown_coordinator.get_shutdown_coordinator",
            return_value=coord,
        ):
            response = mw(_FakeRequest(path))

        assert response.status_code == 200
        body = json.loads(response.content.decode("utf-8"))
        assert body["status"] == "bridge_active"
        # Connection: close must NOT be set in RUNNING — that header is
        # the drain-503 LB-eviction signal.
        assert "Connection" not in response.headers

    @pytest.mark.parametrize("path", HealthBridgeMiddleware.BRIDGE_PATHS)
    def test_draining_phase_returns_503_with_drain_payload(self, path):
        """DRAINING phase returns 503 + Retry-After + Connection: close + draining payload."""
        mw = _make_middleware()
        coord = _coordinator_in_phase(
            ShutdownPhase.DRAINING, remaining=15.0, in_flight=2
        )

        with patch(
            "baldur.core.shutdown_coordinator.get_shutdown_coordinator",
            return_value=coord,
        ):
            response = mw(_FakeRequest(path))

        assert response.status_code == 503
        assert response["Connection"] == "close"
        assert int(response["Retry-After"]) >= 1

        body = json.loads(response.content.decode("utf-8"))
        assert body["status"] == "draining"
        assert body["shutdown"]["phase"] == ShutdownPhase.DRAINING.value
        assert body["shutdown"]["in_flight_count"] == 2

    def test_terminating_phase_falls_through_to_200(self):
        """Phases other than DRAINING fall back to the normal bridge response.

        ``_get_drain_state()`` returns None for non-DRAINING phases so the
        readiness probe stays 200 — once a worker has moved past DRAINING
        the LB has already evicted it via the prior 503s.
        """
        mw = _make_middleware()
        coord = _coordinator_in_phase(ShutdownPhase.TERMINATING, remaining=None)

        with patch(
            "baldur.core.shutdown_coordinator.get_shutdown_coordinator",
            return_value=coord,
        ):
            response = mw(_FakeRequest(HealthBridgeMiddleware.BRIDGE_PATHS[0]))

        assert response.status_code == 200


# =============================================================================
# Failure-mode of _get_drain_state probe
# =============================================================================


class TestHealthBridgeDrainProbeFailFallback:
    """When the coordinator probe fails, bridge must stay 200 (fail-open)."""

    def test_get_shutdown_coordinator_raises_returns_none(self):
        """Coordinator import/lookup error → ``_get_drain_state()`` returns None."""
        with patch(
            "baldur.core.shutdown_coordinator.get_shutdown_coordinator",
            side_effect=RuntimeError("coordinator init failed"),
        ):
            assert HealthBridgeMiddleware._get_drain_state() is None

    def test_get_stats_raises_returns_none(self):
        """Coordinator probe failure mid-call → returns None, bridge stays 200."""
        broken = MagicMock()
        broken.phase = ShutdownPhase.DRAINING
        broken.get_stats.side_effect = RuntimeError("stats unavailable")

        with patch(
            "baldur.core.shutdown_coordinator.get_shutdown_coordinator",
            return_value=broken,
        ):
            assert HealthBridgeMiddleware._get_drain_state() is None

    def test_drain_state_fallback_clamps_retry_after_to_minimum_one(self):
        """When ``remaining_drain_time`` is None during DRAINING, fallback applies + clamp."""
        coord = _coordinator_in_phase(ShutdownPhase.DRAINING, remaining=None)

        with (
            patch(
                "baldur.core.shutdown_coordinator.get_shutdown_coordinator",
                return_value=coord,
            ),
            patch(
                "baldur.settings.recovery_shutdown.get_recovery_shutdown_settings"
            ) as get_settings,
        ):
            get_settings.return_value.drain_default_retry_after_seconds = 5.0
            state = HealthBridgeMiddleware._get_drain_state()

        assert state is not None
        assert state["retry_after_seconds"] == 5
        assert state["phase"] == ShutdownPhase.DRAINING.value
