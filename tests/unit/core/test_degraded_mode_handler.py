"""
DegradedModeHandler unit tests.

Tests the new reason parameter and get_status() method
added for DegradedModeProtocol conformance.
"""

from __future__ import annotations

import pytest

from baldur.core.degraded_mode_handler import DegradedModeHandler


@pytest.fixture(autouse=True)
def _reset_handler():
    """Reset DegradedModeHandler before and after each test."""
    DegradedModeHandler.reset()
    yield
    DegradedModeHandler.reset()


class TestDegradedModeHandlerReasonBehavior:
    """enter_degraded_mode reason parameter behavior."""

    def test_enter_with_reason_stores_reason(self):
        """Reason string is stored and accessible via get_status."""
        DegradedModeHandler.enter_degraded_mode(reason="redis_unreachable")
        status = DegradedModeHandler.get_status()
        assert status["reason"] == "redis_unreachable"

    def test_enter_without_reason_stores_empty_string(self):
        """Default reason is empty string."""
        DegradedModeHandler.enter_degraded_mode()
        status = DegradedModeHandler.get_status()
        assert status["reason"] == ""

    def test_exit_clears_reason(self):
        """exit_degraded_mode clears the stored reason."""
        DegradedModeHandler.enter_degraded_mode(reason="test")
        DegradedModeHandler.exit_degraded_mode()
        status = DegradedModeHandler.get_status()
        assert status["reason"] == ""


class TestDegradedModeHandlerGetStatusBehavior:
    """get_status() method behavior."""

    def test_get_status_healthy_state(self):
        """get_status returns healthy state when not degraded."""
        status = DegradedModeHandler.get_status()
        assert status["is_degraded"] is False
        assert status["status"] == "healthy"
        assert status["source"] == "command_center"

    def test_get_status_degraded_state(self):
        """get_status returns degraded state when in degraded mode."""
        DegradedModeHandler.enter_degraded_mode(reason="connection_lost")
        status = DegradedModeHandler.get_status()
        assert status["is_degraded"] is True
        assert status["status"] == "degraded"
        assert status["source"] == "local_defaults"
        assert status["reason"] == "connection_lost"


class TestDegradedModeHandlerStateTransitionBehavior:
    """Degraded mode state transition behavior."""

    def test_enter_sets_is_degraded_true(self):
        """enter_degraded_mode sets is_degraded to True."""
        assert DegradedModeHandler.is_degraded() is False
        DegradedModeHandler.enter_degraded_mode(reason="test")
        assert DegradedModeHandler.is_degraded() is True

    def test_exit_sets_is_degraded_false(self):
        """exit_degraded_mode sets is_degraded to False."""
        DegradedModeHandler.enter_degraded_mode(reason="test")
        DegradedModeHandler.exit_degraded_mode()
        assert DegradedModeHandler.is_degraded() is False

    def test_double_enter_keeps_degraded(self):
        """Entering degraded mode twice stays degraded."""
        DegradedModeHandler.enter_degraded_mode(reason="first")
        DegradedModeHandler.enter_degraded_mode(reason="second")
        assert DegradedModeHandler.is_degraded() is True
        # Second reason overwrites
        assert DegradedModeHandler.get_status()["reason"] == "second"

    def test_reset_clears_all_state(self):
        """reset clears degraded state, overrides, and reason."""
        DegradedModeHandler.enter_degraded_mode(reason="test")
        DegradedModeHandler.set("CB_FAILURE_THRESHOLD", 10)
        DegradedModeHandler.reset()
        assert DegradedModeHandler.is_degraded() is False
        assert DegradedModeHandler.get_status()["reason"] == ""
        default = DegradedModeHandler._defaults["CB_FAILURE_THRESHOLD"]
        assert DegradedModeHandler.get("CB_FAILURE_THRESHOLD") == default
