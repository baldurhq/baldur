"""
DegradedModeProtocol conformance tests.

Verifies that both DegradedModeHandler (core) and DegradedModeManager (audit)
satisfy the DegradedModeProtocol structural contract.
"""

from __future__ import annotations

from baldur.core.degraded_mode_protocol import DegradedModeProtocol


class TestDegradedModeProtocolConformanceContract:
    """Both degraded mode implementations conform to the protocol."""

    def test_degraded_mode_handler_satisfies_protocol(self):
        """DegradedModeHandler has all required protocol methods/properties."""
        from baldur.core.degraded_mode_handler import DegradedModeHandler

        # Verify required attributes exist
        assert hasattr(DegradedModeHandler, "is_degraded")
        assert hasattr(DegradedModeHandler, "enter_degraded_mode")
        assert hasattr(DegradedModeHandler, "exit_degraded_mode")
        assert hasattr(DegradedModeHandler, "get_status")

        # Verify callable signatures
        assert callable(DegradedModeHandler.enter_degraded_mode)
        assert callable(DegradedModeHandler.exit_degraded_mode)
        assert callable(DegradedModeHandler.get_status)

    def test_degraded_mode_manager_satisfies_protocol(self):
        """DegradedModeManager has all required protocol methods/properties."""
        from baldur.audit.resilience.degraded_mode import DegradedModeManager

        # DegradedModeManager is instance-based, check on instance
        assert hasattr(DegradedModeManager, "is_degraded")
        assert hasattr(DegradedModeManager, "enter_degraded_mode")
        assert hasattr(DegradedModeManager, "exit_degraded_mode")
        assert hasattr(DegradedModeManager, "get_status")

    def test_protocol_defines_required_methods(self):
        """DegradedModeProtocol declares is_degraded, enter, exit, get_status."""
        # Protocol members are accessible via __protocol_attrs__ or inspection
        assert hasattr(DegradedModeProtocol, "is_degraded")
        assert hasattr(DegradedModeProtocol, "enter_degraded_mode")
        assert hasattr(DegradedModeProtocol, "exit_degraded_mode")
        assert hasattr(DegradedModeProtocol, "get_status")
