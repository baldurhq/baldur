"""
Event filters utility unit tests.

Tests for namespace-aware event filtering in multi-pod deployments.

Code reference:
    utils/event_filters.py
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from baldur.utils.event_filters import should_handle_emergency_event


class TestShouldHandleEmergencyEventContract:
    """Contract tests for should_handle_emergency_event (fail-open policy)."""

    def test_global_namespace_always_handled(self):
        """Global namespace events must be handled by all pods."""
        event = SimpleNamespace(data={"namespace": "global", "level": "level_3"})
        assert should_handle_emergency_event(event) is True

    def test_global_namespace_explicit_string(self):
        """Explicit 'global' string triggers all-pod handling."""
        event = SimpleNamespace(data={"namespace": "global", "scope": "GLOBAL"})
        assert should_handle_emergency_event(event) is True

    def test_missing_namespace_defaults_to_global(self):
        """Missing namespace field defaults to 'global' (fail-open)."""
        event = SimpleNamespace(data={"level": "level_2"})
        assert should_handle_emergency_event(event) is True

    def test_unknown_event_structure_fail_open(self):
        """Non-dict event data returns True (fail-open)."""
        event = SimpleNamespace(data="invalid_string")
        assert should_handle_emergency_event(event) is True

    def test_no_data_attribute_fail_open(self):
        """Event without data attribute is used as data directly (fail-open)."""
        event = {"namespace": "global"}  # Plain dict, no .data attribute
        assert should_handle_emergency_event(event) is True


class TestShouldHandleEmergencyEventBehavior:
    """Behavior tests for regional namespace filtering."""

    def test_regional_event_matching_region_handled(self):
        """Regional event matching pod's region is handled."""
        event = SimpleNamespace(data={"namespace": "seoul", "scope": "REGIONAL"})

        mock_identity = MagicMock()
        mock_identity.region = "seoul"

        with patch(
            "baldur.core.cluster_identity.get_cluster_identity",
            return_value=mock_identity,
        ):
            assert should_handle_emergency_event(event) is True

    def test_regional_event_different_region_not_handled(self):
        """Regional event for different region is NOT handled."""
        event = SimpleNamespace(data={"namespace": "tokyo", "scope": "REGIONAL"})

        mock_identity = MagicMock()
        mock_identity.region = "seoul"

        with patch(
            "baldur.core.cluster_identity.get_cluster_identity",
            return_value=mock_identity,
        ):
            assert should_handle_emergency_event(event) is False

    def test_regional_event_pod_with_no_region_handled(self):
        """Pod without region (None) handles all regional events (fail-open)."""
        event = SimpleNamespace(data={"namespace": "tokyo", "scope": "REGIONAL"})

        mock_identity = MagicMock()
        mock_identity.region = None

        with patch(
            "baldur.core.cluster_identity.get_cluster_identity",
            return_value=mock_identity,
        ):
            assert should_handle_emergency_event(event) is True

    def test_cluster_identity_failure_fail_open(self):
        """ClusterIdentity exception returns True (fail-open)."""
        event = SimpleNamespace(data={"namespace": "tokyo", "scope": "REGIONAL"})

        with patch(
            "baldur.core.cluster_identity.get_cluster_identity",
            side_effect=RuntimeError("Redis unavailable"),
        ):
            assert should_handle_emergency_event(event) is True

    def test_dict_event_without_data_attribute(self):
        """Plain dict event (no .data) is treated as data itself."""
        # When event is a dict without .data attribute
        event = {"namespace": "seoul", "scope": "REGIONAL"}

        mock_identity = MagicMock()
        mock_identity.region = "seoul"

        with patch(
            "baldur.core.cluster_identity.get_cluster_identity",
            return_value=mock_identity,
        ):
            # Should use the dict itself as data
            assert should_handle_emergency_event(event) is True


class TestShouldHandleEmergencyEventEdgeCases:
    """Edge case tests for robust handling."""

    def test_empty_dict_event_fail_open(self):
        """Empty dict event returns True (fail-open)."""
        event = SimpleNamespace(data={})
        # namespace defaults to "global" when missing
        assert should_handle_emergency_event(event) is True

    def test_none_event_fail_open(self):
        """None event returns True (fail-open)."""
        # None has no .data attribute, treated as non-dict
        assert should_handle_emergency_event(None) is True

    def test_list_event_fail_open(self):
        """List event returns True (fail-open)."""
        event = SimpleNamespace(data=["item1", "item2"])
        assert should_handle_emergency_event(event) is True
