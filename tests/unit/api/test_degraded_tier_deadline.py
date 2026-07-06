"""
Unit tests — Degraded Tier Forced Deadline.

backpressure HIGH or above + non_essential tier -> force a 1s deadline. Safety
guard preventing a heavy query from holding critical/standard tier resources.

The logic lives in the framework-free admission helper
(``api/middleware/admission.py``) as ``_maybe_force_degraded_deadline`` so
Django / Flask / FastAPI share it; these tests exercise it directly.

Test items:
- HIGH + non_essential -> 1000ms deadline set
- CRITICAL + non_essential -> 1000ms deadline set
- MEDIUM + non_essential -> deadline not set
- existing shorter deadline -> preserved (set_deadline not called)
- non non_essential tier -> deadline not set
"""

from unittest.mock import MagicMock, patch

from baldur.api.middleware.admission import (
    _DEGRADED_TIER_DEADLINE_MS,
    _maybe_force_degraded_deadline,
)
from baldur.settings.backpressure import BackpressureLevel


class TestDegradedTierDeadlineBehavior:
    """Forced deadline injection for non_essential under HIGH/CRITICAL load."""

    def _gate(self, bp_level: BackpressureLevel):
        gate = MagicMock()
        gate.get_level.return_value = bp_level
        return gate

    def test_forced_deadline_on_high_level(self):
        """HIGH + non_essential -> 1000ms deadline is set."""
        gate = self._gate(BackpressureLevel.HIGH)
        with (
            patch("baldur.scaling.deadline_context.set_deadline") as mock_set,
            patch(
                "baldur.scaling.deadline_context.get_remaining_ms",
                return_value=None,
            ),
        ):
            _maybe_force_degraded_deadline(gate, "non_essential")
            mock_set.assert_called_once_with(_DEGRADED_TIER_DEADLINE_MS)

    def test_forced_deadline_on_critical_level(self):
        """CRITICAL + non_essential -> 1000ms deadline is set."""
        gate = self._gate(BackpressureLevel.CRITICAL)
        with (
            patch("baldur.scaling.deadline_context.set_deadline") as mock_set,
            patch(
                "baldur.scaling.deadline_context.get_remaining_ms",
                return_value=None,
            ),
        ):
            _maybe_force_degraded_deadline(gate, "non_essential")
            mock_set.assert_called_once_with(_DEGRADED_TIER_DEADLINE_MS)

    def test_no_deadline_below_high(self):
        """MEDIUM + non_essential -> deadline not set."""
        gate = self._gate(BackpressureLevel.MEDIUM)
        with (
            patch("baldur.scaling.deadline_context.set_deadline") as mock_set,
            patch(
                "baldur.scaling.deadline_context.get_remaining_ms",
                return_value=None,
            ),
        ):
            _maybe_force_degraded_deadline(gate, "non_essential")
            mock_set.assert_not_called()

    def test_existing_shorter_deadline_kept(self):
        """Existing 500ms deadline < 1000ms -> set_deadline not called."""
        gate = self._gate(BackpressureLevel.HIGH)
        with (
            patch("baldur.scaling.deadline_context.set_deadline") as mock_set,
            patch(
                "baldur.scaling.deadline_context.get_remaining_ms",
                return_value=500.0,
            ),
        ):
            _maybe_force_degraded_deadline(gate, "non_essential")
            mock_set.assert_not_called()

    def test_non_non_essential_tier_skipped(self):
        """A non non_essential tier never forces a deadline."""
        gate = self._gate(BackpressureLevel.CRITICAL)
        with patch("baldur.scaling.deadline_context.set_deadline") as mock_set:
            _maybe_force_degraded_deadline(gate, "critical")
            mock_set.assert_not_called()
            gate.get_level.assert_not_called()
