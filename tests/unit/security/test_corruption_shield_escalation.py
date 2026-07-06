"""
Tests for UU4: CorruptionShield escalation + ListCapableBackend.

Covers:
- ListCapableBackend protocol on MemoryStateBackend
- CorruptionShield emergency escalation settings defaults
- CorruptionShield._record_critical_violation() behavior
- CorruptionShield._escalate_if_needed() behavior
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from baldur.core.state_backend import (
    ListCapableBackend,
    MemoryStateBackend,
)

# =============================================================================
# ListCapableBackend on MemoryStateBackend
# =============================================================================


class TestMemoryStateBackendListCapableContract:
    """MemoryStateBackend satisfies the ListCapableBackend protocol."""

    def test_memory_backend_is_instance_of_list_capable(self):
        """MemoryStateBackend is isinstance of ListCapableBackend."""
        backend = MemoryStateBackend()
        assert isinstance(backend, ListCapableBackend)


class TestMemoryStateBackendListBehavior:
    """MemoryStateBackend list operations (push_limit, list_range)."""

    def test_push_limit_appends_value_and_returns_length(self):
        """push_limit appends value and returns new list length."""
        backend = MemoryStateBackend()

        length = backend.push_limit("test_key", "a", max_len=10)

        assert length == 1
        assert backend.list_range("test_key", 0, -1) == ["a"]

    def test_push_limit_trims_to_max_len(self):
        """push_limit with max_len=3 keeps only last 3 elements after 5 pushes."""
        backend = MemoryStateBackend()

        for val in ["a", "b", "c", "d", "e"]:
            backend.push_limit("test_key", val, max_len=3)

        result = backend.list_range("test_key", 0, -1)
        assert result == ["c", "d", "e"]

    def test_list_range_returns_correct_slice(self):
        """list_range returns elements in the requested range (inclusive)."""
        backend = MemoryStateBackend()
        for val in [1, 2, 3, 4, 5]:
            backend.push_limit("nums", val, max_len=10)

        # Range [1, 3] should return elements at index 1, 2, 3
        result = backend.list_range("nums", 1, 3)
        assert result == [2, 3, 4]

    def test_list_range_on_nonexistent_key_returns_empty_list(self):
        """list_range on a non-existent key returns empty list."""
        backend = MemoryStateBackend()

        result = backend.list_range("nonexistent", 0, -1)
        assert result == []


# =============================================================================
# CorruptionShield escalation settings defaults
# =============================================================================


class TestCorruptionShieldEscalationSettingsContract:
    """Emergency escalation settings have correct default values."""

    def test_emergency_escalation_enabled_default_is_false(self):
        """emergency_escalation_enabled defaults to False (v1.1 deferred per impl 527)."""
        from baldur.settings.corruption_shield import CorruptionShieldSettings

        settings = CorruptionShieldSettings()
        assert settings.emergency_escalation_enabled is False

    def test_emergency_level2_threshold_default_is_3(self):
        """emergency_level2_threshold defaults to 3."""
        from baldur.settings.corruption_shield import CorruptionShieldSettings

        settings = CorruptionShieldSettings()
        assert settings.emergency_level2_threshold == 3

    def test_emergency_window_seconds_default_is_300(self):
        """emergency_window_seconds defaults to 300."""
        from baldur.settings.corruption_shield import CorruptionShieldSettings

        settings = CorruptionShieldSettings()
        assert settings.emergency_window_seconds == 300


# =============================================================================
# CorruptionShield._record_critical_violation()
# =============================================================================


class TestRecordCriticalViolationBehavior:
    """_record_critical_violation records timestamps and returns window count."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def _make_shield(self):
        """Create a CorruptionShield with MemoryStateBackend."""
        from baldur_pro.services.corruption_shield.shield import CorruptionShield

        shield = CorruptionShield()
        backend = MemoryStateBackend()
        shield._state_backend = backend
        shield._critical_timestamps = []
        return shield, backend

    @patch(
        "baldur.settings.corruption_shield.get_corruption_shield_settings",
        autospec=True,
    )
    def test_records_violation_and_returns_count(self, mock_settings):
        """Records violation and returns count within time window."""
        mock_settings.return_value = MagicMock(
            emergency_window_seconds=300,
        )

        shield, _ = self._make_shield()

        count = shield._record_critical_violation()

        assert count >= 1

    @patch(
        "baldur.settings.corruption_shield.get_corruption_shield_settings",
        autospec=True,
    )
    def test_old_violations_outside_window_excluded_from_count(self, mock_settings):
        """Old violations outside the time window are excluded from count."""
        mock_settings.return_value = MagicMock(
            emergency_window_seconds=60,
        )

        shield, backend = self._make_shield()

        # Given — push old timestamps well outside the 60s window
        old_time = time.time() - 120  # 120 seconds ago
        backend.push_limit(
            "corruption_shield_violations", old_time, max_len=100, ttl_seconds=120
        )
        backend.push_limit(
            "corruption_shield_violations", old_time - 10, max_len=100, ttl_seconds=120
        )

        # When — record a new violation (only the new one should count)
        count = shield._record_critical_violation()

        # Then — old violations excluded, only the new one counts
        assert count == 1


# =============================================================================
# CorruptionShield._escalate_if_needed()
# =============================================================================


class TestEscalateIfNeededBehavior:
    """_escalate_if_needed escalates based on critical violation count."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def _make_shield_with_mocks(self, *, escalation_enabled=True, threshold=3):
        """Create shield with mocked settings and emergency manager."""
        from baldur_pro.services.corruption_shield.shield import CorruptionShield

        shield = CorruptionShield()
        backend = MemoryStateBackend()
        shield._state_backend = backend
        shield._critical_timestamps = []
        return shield, backend

    @patch(
        "baldur_pro.services.emergency_mode.get_emergency_manager",
        autospec=True,
    )
    @patch(
        "baldur.settings.corruption_shield.get_corruption_shield_settings",
        autospec=True,
    )
    def test_escalates_level2_when_count_exceeds_threshold(
        self, mock_settings, mock_get_manager
    ):
        """With count >= emergency_level2_threshold, calls activate_auto with LEVEL_2."""
        from baldur.models.emergency import EmergencyLevel

        mock_settings.return_value = MagicMock(
            emergency_escalation_enabled=True,
            emergency_level2_threshold=3,
            emergency_window_seconds=300,
        )
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        shield, backend = self._make_shield_with_mocks()

        # Given — pre-populate enough violations to exceed threshold
        now = time.time()
        for i in range(3):
            backend.push_limit(
                "corruption_shield_violations",
                now - i,
                max_len=100,
                ttl_seconds=600,
            )

        # When
        shield._escalate_if_needed([])

        # Then
        mock_manager.activate_auto.assert_called_once()
        call_args = mock_manager.activate_auto.call_args
        assert call_args[0][0] == EmergencyLevel.LEVEL_2

    @patch(
        "baldur_pro.services.emergency_mode.get_emergency_manager",
        autospec=True,
    )
    @patch(
        "baldur.settings.corruption_shield.get_corruption_shield_settings",
        autospec=True,
    )
    def test_escalates_level1_when_count_below_threshold(
        self, mock_settings, mock_get_manager
    ):
        """With count < emergency_level2_threshold, calls activate_auto with LEVEL_1."""
        from baldur.models.emergency import EmergencyLevel

        mock_settings.return_value = MagicMock(
            emergency_escalation_enabled=True,
            emergency_level2_threshold=10,  # High threshold
            emergency_window_seconds=300,
        )
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        shield, _ = self._make_shield_with_mocks()

        # When — no pre-existing violations, so count will be 1 (< 10)
        shield._escalate_if_needed([])

        # Then
        mock_manager.activate_auto.assert_called_once()
        call_args = mock_manager.activate_auto.call_args
        assert call_args[0][0] == EmergencyLevel.LEVEL_1

    @patch(
        "baldur_pro.services.emergency_mode.get_emergency_manager",
        autospec=True,
    )
    @patch(
        "baldur.settings.corruption_shield.get_corruption_shield_settings",
        autospec=True,
    )
    def test_no_escalation_when_disabled(self, mock_settings, mock_get_manager):
        """When emergency_escalation_enabled is False, does not call activate_auto."""
        mock_settings.return_value = MagicMock(
            emergency_escalation_enabled=False,
            emergency_level2_threshold=3,
            emergency_window_seconds=300,
        )
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        shield, _ = self._make_shield_with_mocks()

        # When
        shield._escalate_if_needed([])

        # Then
        mock_manager.activate_auto.assert_not_called()


# =============================================================================
# Edge-triggered EventBus emission (#413 UU4)
# =============================================================================


class TestEscalateEdgeTriggeredEmissionBehavior:
    """EventBus emission only fires on escalation level transitions."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def _make_shield(self):
        from baldur_pro.services.corruption_shield.shield import CorruptionShield

        shield = CorruptionShield()
        shield._state_backend = MemoryStateBackend()
        shield._critical_timestamps = []
        return shield

    @patch(
        "baldur_pro.services.emergency_mode.get_emergency_manager",
        autospec=True,
    )
    @patch(
        "baldur.settings.corruption_shield.get_corruption_shield_settings",
        autospec=True,
    )
    def test_first_escalation_emits_event(self, mock_settings, mock_get_manager):
        """First escalation emits CORRUPTION_VIOLATION_CRITICAL."""
        mock_settings.return_value = MagicMock(
            emergency_escalation_enabled=True,
            emergency_level2_threshold=100,
            emergency_window_seconds=300,
        )
        mock_get_manager.return_value = MagicMock()

        shield = self._make_shield()

        with patch.object(shield, "_emit_event") as mock_emit:
            shield._escalate_if_needed([])

            mock_emit.assert_called_once()
            call_data = mock_emit.call_args[1]["data"]
            assert call_data["severity"] == "critical"
            assert "escalation_level" in call_data
            assert "violation_types" in call_data

    @patch(
        "baldur_pro.services.emergency_mode.get_emergency_manager",
        autospec=True,
    )
    @patch(
        "baldur.settings.corruption_shield.get_corruption_shield_settings",
        autospec=True,
    )
    def test_same_level_escalation_does_not_re_emit(
        self, mock_settings, mock_get_manager
    ):
        """Repeated escalation at the same level does NOT emit again (dedup)."""
        from baldur.models.emergency import EmergencyLevel

        mock_settings.return_value = MagicMock(
            emergency_escalation_enabled=True,
            emergency_level2_threshold=100,
            emergency_window_seconds=300,
        )
        mock_get_manager.return_value = MagicMock()

        shield = self._make_shield()
        # Simulate already emitted at LEVEL_1
        shield._last_emitted_escalation_level = EmergencyLevel.LEVEL_1.value

        with patch.object(shield, "_emit_event") as mock_emit:
            shield._escalate_if_needed([])

            mock_emit.assert_not_called()

    @patch(
        "baldur_pro.services.emergency_mode.get_emergency_manager",
        autospec=True,
    )
    @patch(
        "baldur.settings.corruption_shield.get_corruption_shield_settings",
        autospec=True,
    )
    def test_level_transition_emits_again(self, mock_settings, mock_get_manager):
        """Transition from LEVEL_1 to LEVEL_2 emits a new event."""
        from baldur.models.emergency import EmergencyLevel

        mock_settings.return_value = MagicMock(
            emergency_escalation_enabled=True,
            emergency_level2_threshold=3,
            emergency_window_seconds=300,
        )
        mock_get_manager.return_value = MagicMock()

        shield = self._make_shield()
        # Pre-populate violations to exceed threshold
        now = time.time()
        backend = shield._state_backend
        for i in range(3):
            backend.push_limit(
                "corruption_shield_violations",
                now - i,
                max_len=100,
                ttl_seconds=600,
            )
        # Already emitted at LEVEL_1
        shield._last_emitted_escalation_level = EmergencyLevel.LEVEL_1.value

        with patch.object(shield, "_emit_event") as mock_emit:
            shield._escalate_if_needed([])

            # Should emit because level transitioned to LEVEL_2
            mock_emit.assert_called_once()

    def test_reset_clears_last_emitted_level(self):
        """reset() clears _last_emitted_escalation_level."""
        shield = self._make_shield()
        shield._last_emitted_escalation_level = "LEVEL_1"

        shield.reset()

        assert shield._last_emitted_escalation_level is None

    @patch(
        "baldur_pro.services.emergency_mode.get_emergency_manager",
        autospec=True,
    )
    @patch(
        "baldur.settings.corruption_shield.get_corruption_shield_settings",
        autospec=True,
    )
    def test_violation_types_uses_code_fallback(self, mock_settings, mock_get_manager):
        """Violation objects without violation_type fall back to code."""
        mock_settings.return_value = MagicMock(
            emergency_escalation_enabled=True,
            emergency_level2_threshold=100,
            emergency_window_seconds=300,
        )
        mock_get_manager.return_value = MagicMock()

        shield = self._make_shield()

        from baldur_pro.services.corruption_shield.validators import Violation

        violations = [
            Violation(layer="L3", code="anomaly_statistical", message="anomaly"),
        ]

        with patch.object(shield, "_emit_event") as mock_emit:
            shield._escalate_if_needed(violations)

            call_data = mock_emit.call_args[1]["data"]
            assert call_data["violation_types"] == ["anomaly_statistical"]

    @patch(
        "baldur_pro.services.emergency_mode.get_emergency_manager",
        autospec=True,
    )
    @patch(
        "baldur.settings.corruption_shield.get_corruption_shield_settings",
        autospec=True,
    )
    def test_emit_failure_does_not_block_escalation(
        self, mock_settings, mock_get_manager
    ):
        """EventBus emit failure does not prevent activate_auto from being called."""
        mock_settings.return_value = MagicMock(
            emergency_escalation_enabled=True,
            emergency_level2_threshold=100,
            emergency_window_seconds=300,
        )
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        shield = self._make_shield()

        with patch.object(shield, "_emit_event", side_effect=RuntimeError("bus down")):
            # Should not raise — emit_event is inside the try block
            # but activate_auto was already called before emit
            shield._escalate_if_needed([])

        mock_manager.activate_auto.assert_called_once()
