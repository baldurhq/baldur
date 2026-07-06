"""
Safety Views RunningExperimentInfo Serialization Tests (385 D-13).

Tests for:
A. kill_switch_status handler — RunningExperimentInfo → API dict with elapsed_seconds
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


import time
from unittest.mock import MagicMock, patch

from baldur.interfaces.web_framework import HttpMethod, RequestContext
from baldur_pro.services.chaos.scheduler.service import RunningExperimentInfo

_HANDLER_MODULE = "baldur.api.handlers.chaos_safety"


def _make_ctx() -> RequestContext:
    """Create a RequestContext for kill_switch_status handler."""
    return RequestContext(
        method=HttpMethod.GET,
        path="/chaos/kill-switch/",
    )


# =============================================================================
# A. RunningExperimentInfo → API Serialization (D-13)
# =============================================================================


class TestKillSwitchViewSerializationBehavior:
    """Behavior verification for RunningExperimentInfo → API dict conversion (D-13)."""

    def test_running_experiments_serialized_with_elapsed_seconds(self):
        """API response contains experiment_id and elapsed_seconds per experiment."""
        from baldur.api.handlers.chaos_safety import kill_switch_status

        # Given
        mono_start = time.monotonic() - 120.0  # started 120s ago
        running = {
            "sched-1": RunningExperimentInfo(
                experiment_id="exp-abc",
                started_at_monotonic=mono_start,
            ),
        }
        mock_guard = MagicMock()
        mock_guard.is_globally_blocked.return_value = (False, "")
        mock_scheduler = MagicMock()
        mock_scheduler.get_running_experiments.return_value = running

        # When
        with (
            patch(f"{_HANDLER_MODULE}._safety_guard", return_value=mock_guard),
            patch(f"{_HANDLER_MODULE}._scheduler", return_value=mock_scheduler),
        ):
            ctx = _make_ctx()
            response = kill_switch_status(ctx)

        # Then
        data = response.body["data"]
        exp_data = data["running_experiments"]["sched-1"]
        assert "experiment_id" in exp_data
        assert exp_data["experiment_id"] == "exp-abc"
        assert "elapsed_seconds" in exp_data
        assert exp_data["elapsed_seconds"] >= 100.0  # ~120s, allow small variance

    def test_empty_running_experiments_returns_empty_dict(self):
        """No running experiments → empty dict in response."""
        from baldur.api.handlers.chaos_safety import kill_switch_status

        mock_guard = MagicMock()
        mock_guard.is_globally_blocked.return_value = (False, "")
        mock_scheduler = MagicMock()
        mock_scheduler.get_running_experiments.return_value = {}

        with (
            patch(f"{_HANDLER_MODULE}._safety_guard", return_value=mock_guard),
            patch(f"{_HANDLER_MODULE}._scheduler", return_value=mock_scheduler),
        ):
            ctx = _make_ctx()
            response = kill_switch_status(ctx)

        assert response.body["data"]["running_experiments"] == {}
        assert response.body["data"]["running_count"] == 0
