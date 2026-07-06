"""
Tests for D3: EM->Backoff hybrid wiring.

Source: src/baldur/services/backoff_calculator/calculator.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import Mock, patch


@dataclass
class FakeEvent:
    """Lightweight event stand-in for EventBus events."""

    data: dict[str, Any] = field(default_factory=dict)
    source: str = "test"


class TestEmergencyBackoffBehavior:
    """Behavior tests for _on_emergency_changed handler in ThrottleAwareBackoffCalculator."""

    def _make_calculator(self):
        """Create a ThrottleAwareBackoffCalculator with push cache disabled."""
        from baldur.services.backoff_calculator.calculator import (
            ThrottleAwareBackoffCalculator,
        )

        return ThrottleAwareBackoffCalculator(
            throttle_getter=lambda: None,
            enable_push_cache=False,
        )

    def test_on_emergency_changed_level3_sets_high_multiplier_and_full_stop(self):
        """level_3 event sets multiplier=4.0, full_stop_active=True, emergency_level=3."""
        calc = self._make_calculator()

        # Given
        event = FakeEvent(data={"level": "level_3"})

        # When
        calc._on_emergency_changed(event)

        # Then
        assert calc._state_cache.multiplier == 4.0
        assert calc._state_cache.full_stop_active is True
        assert calc._state_cache.emergency_level == 3

    def test_on_emergency_changed_level1_sets_moderate_multiplier_no_full_stop(self):
        """level_1 event sets multiplier=2.5, full_stop_active=False, emergency_level=1."""
        calc = self._make_calculator()

        # Given
        event = FakeEvent(data={"level": "level_1"})

        # When
        calc._on_emergency_changed(event)

        # Then
        assert calc._state_cache.multiplier == 2.5
        assert calc._state_cache.full_stop_active is False
        assert calc._state_cache.emergency_level == 1

    def test_on_emergency_changed_normal_resets_to_baseline(self):
        """normal event resets multiplier=1.0, reason='normal'."""
        calc = self._make_calculator()

        # Given - first set to emergency, then back to normal
        calc._on_emergency_changed(FakeEvent(data={"level": "level_3"}))
        event = FakeEvent(data={"level": "normal"})

        # When
        calc._on_emergency_changed(event)

        # Then
        assert calc._state_cache.multiplier == 1.0
        assert calc._state_cache.reason == "normal"
        assert calc._state_cache.emergency_level == 0
        assert calc._state_cache.full_stop_active is False


class TestGetThrottleStateFallbackBehavior:
    """Behavior tests for _get_throttle_state EM fallback when throttle is None."""

    @patch(
        "baldur_pro.services.emergency_mode.get_emergency_manager",
    )
    def test_get_throttle_state_queries_em_when_throttle_none(self, mock_get_em):
        """When throttle is None, queries EM and returns ThrottleState with emergency data."""
        from baldur.services.backoff_calculator.calculator import (
            ThrottleAwareBackoffCalculator,
        )
        from baldur.services.backoff_calculator.models import ThrottleState

        # Given
        mock_level = Mock()
        mock_level.severity = 2
        mock_manager = Mock()
        mock_manager.get_current_level.return_value = mock_level
        mock_get_em.return_value = mock_manager

        calc = ThrottleAwareBackoffCalculator(
            throttle_getter=lambda: None,
            enable_push_cache=False,
        )

        # When
        result = calc._get_throttle_state()

        # Then
        assert isinstance(result, ThrottleState)
        assert result.emergency_level == 2
        assert result.full_stop_active is False  # level 2 < 3

    @patch(
        "baldur_pro.services.emergency_mode.get_emergency_manager",
    )
    def test_get_throttle_state_returns_none_when_em_level_zero(self, mock_get_em):
        """When throttle is None and EM returns level 0, returns None."""
        from baldur.services.backoff_calculator.calculator import (
            ThrottleAwareBackoffCalculator,
        )

        # Given
        mock_level = Mock()
        mock_level.severity = 0
        mock_manager = Mock()
        mock_manager.get_current_level.return_value = mock_level
        mock_get_em.return_value = mock_manager

        calc = ThrottleAwareBackoffCalculator(
            throttle_getter=lambda: None,
            enable_push_cache=False,
        )

        # When
        result = calc._get_throttle_state()

        # Then
        assert result is None
