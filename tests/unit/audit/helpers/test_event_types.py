"""
AuditEventType Chaos/Emergency Extension Tests.

Tests for chaos and emergency AuditEventType additions.
Uses lazy imports to avoid Prometheus registry conflicts.
"""


class TestAuditEventTypeChaosEmergency:
    """Tests for chaos and emergency AuditEventType additions."""

    def test_chaos_event_types_exist(self):
        """Should have Chaos-related event types."""
        from baldur.audit.event_buffer import AuditEventType

        assert hasattr(AuditEventType, "CHAOS_EXPERIMENT_STARTED")
        assert hasattr(AuditEventType, "CHAOS_EXPERIMENT_COMPLETED")
        assert hasattr(AuditEventType, "CHAOS_INJECTION_APPLIED")
        assert hasattr(AuditEventType, "CHAOS_ROLLBACK_TRIGGERED")

        assert (
            AuditEventType.CHAOS_EXPERIMENT_STARTED.value == "chaos_experiment_started"
        )
        assert (
            AuditEventType.CHAOS_EXPERIMENT_COMPLETED.value
            == "chaos_experiment_completed"
        )
        assert AuditEventType.CHAOS_INJECTION_APPLIED.value == "chaos_injection_applied"
        assert (
            AuditEventType.CHAOS_ROLLBACK_TRIGGERED.value == "chaos_rollback_triggered"
        )

    def test_emergency_mode_event_types_exist(self):
        """Should have Emergency Mode-related event types."""
        from baldur.audit.event_buffer import AuditEventType

        assert hasattr(AuditEventType, "EMERGENCY_MODE_ACTIVATED")
        assert hasattr(AuditEventType, "EMERGENCY_MODE_DEACTIVATED")

        assert (
            AuditEventType.EMERGENCY_MODE_ACTIVATED.value == "emergency_mode_activated"
        )
        assert (
            AuditEventType.EMERGENCY_MODE_DEACTIVATED.value
            == "emergency_mode_deactivated"
        )

    def test_error_budget_event_types_exist(self):
        """Should have Error Budget-related event types."""
        from baldur.audit.event_buffer import AuditEventType

        assert hasattr(AuditEventType, "ERROR_BUDGET_DEPLETED")
        assert hasattr(AuditEventType, "ERROR_BUDGET_BLOCKED")

        assert AuditEventType.ERROR_BUDGET_DEPLETED.value == "error_budget_depleted"
        assert AuditEventType.ERROR_BUDGET_BLOCKED.value == "error_budget_blocked"
