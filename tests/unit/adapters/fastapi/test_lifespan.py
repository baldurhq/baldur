"""Unit tests for ``baldur.adapters.fastapi.lifespan``.

The lifespan teardown is the ASGI drain sync point: it initiates the
coordinator drain (idempotent — covers non-signal teardown such as
uvicorn ``--reload``) and waits for it off-loop, the counterpart of the
gunicorn ``worker_exit`` wait. Cancellation mid-wait must propagate
(asyncio contract); the daemon drain thread continues independently.

No fastapi import is required: the module only type-checks FastAPI.
"""
# Teardown drain-sync coverage: 597 D9.

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
import structlog

from baldur.adapters.fastapi.lifespan import fastapi_lifespan
from baldur.core.shutdown_coordinator import GracefulShutdownCoordinator


def _mock_coordinator(
    drain_timeout: float = 7.5, wait_result: bool = True
) -> MagicMock:
    """Coordinator double exposing the surface the lifespan touches."""
    coordinator = MagicMock(spec=GracefulShutdownCoordinator)
    coordinator.get_stats.return_value.drain_timeout_seconds = drain_timeout
    coordinator.wait_for_shutdown.return_value = wait_result
    return coordinator


class TestFastapiLifespanDrainSyncBehavior:
    """fastapi_lifespan startup init + teardown drain synchronization (597 D9)."""

    @pytest.mark.asyncio
    async def test_teardown_initiates_shutdown_and_waits_for_drain(self):
        """Teardown initiates the drain and waits drain_timeout + 5s slack."""
        # Given
        drain_timeout = 7.5
        coordinator = _mock_coordinator(drain_timeout=drain_timeout)

        # When
        with (
            patch("baldur.init") as mock_init,
            patch(
                "baldur.core.shutdown_coordinator.get_shutdown_coordinator",
                return_value=coordinator,
            ),
        ):
            async with fastapi_lifespan(MagicMock()) as state:
                # Startup ran init; the drain only starts at teardown
                assert state == {}
                coordinator.initiate_shutdown.assert_not_called()

        # Then
        mock_init.assert_called_once_with()
        coordinator.initiate_shutdown.assert_called_once_with()
        coordinator.wait_for_shutdown.assert_called_once_with(
            timeout=drain_timeout + 5.0
        )

    @pytest.mark.asyncio
    async def test_teardown_logs_warning_when_drain_times_out(self):
        """An incomplete drain (wait returns False) emits the timeout warning."""
        coordinator = _mock_coordinator(wait_result=False)

        with (
            patch("baldur.init"),
            patch(
                "baldur.core.shutdown_coordinator.get_shutdown_coordinator",
                return_value=coordinator,
            ),
            structlog.testing.capture_logs() as logs,
        ):
            async with fastapi_lifespan(MagicMock()):
                pass

        events = [entry["event"] for entry in logs]
        assert "baldur.fastapi_lifespan_drain_timeout" in events

    @pytest.mark.asyncio
    async def test_teardown_completes_without_warning_when_drain_finishes(self):
        """A completed drain (wait returns True) emits no timeout warning."""
        coordinator = _mock_coordinator(wait_result=True)

        with (
            patch("baldur.init"),
            patch(
                "baldur.core.shutdown_coordinator.get_shutdown_coordinator",
                return_value=coordinator,
            ),
            structlog.testing.capture_logs() as logs,
        ):
            async with fastapi_lifespan(MagicMock()):
                pass

        events = [entry["event"] for entry in logs]
        assert "baldur.fastapi_lifespan_drain_timeout" not in events

    @pytest.mark.asyncio
    async def test_teardown_reraises_cancellation_without_swallowing(self):
        """Loop teardown cancelling the wait propagates (never swallowed)."""
        coordinator = _mock_coordinator()
        coordinator.wait_for_shutdown.side_effect = asyncio.CancelledError()

        with (
            patch("baldur.init"),
            patch(
                "baldur.core.shutdown_coordinator.get_shutdown_coordinator",
                return_value=coordinator,
            ),
        ):
            with pytest.raises(asyncio.CancelledError):
                async with fastapi_lifespan(MagicMock()):
                    pass

        # The drain was still initiated before the cancelled wait
        coordinator.initiate_shutdown.assert_called_once_with()
