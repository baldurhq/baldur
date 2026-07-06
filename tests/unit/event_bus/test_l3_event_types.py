"""
L3 EventType Definitions — Contract Tests.

테스트 대상: services/event_bus/bus/event_types.py (doc 382 additions)
검증 기법: 계약 검증 (하드코딩 값), 고유성, 직렬화

Test Categories:
    A. Contract — 25 new EventType members have correct string values
    B. Contract — Value uniqueness across all EventType members
    C. Contract — Naming convention (member name = uppercase of value)
"""

from __future__ import annotations

from baldur.services.event_bus.bus.event_types import EventType

# =============================================================================
# A. Contract — HIGH priority EventType string values
# =============================================================================


class TestL3HighPriorityEventTypeContract:
    """HIGH priority 서비스 EventType 문자열 값 계약 검증."""

    # -- FinOps --

    def test_finops_budget_exceeded_value(self):
        """FINOPS_BUDGET_EXCEEDED = 'finops_budget_exceeded'."""
        assert EventType.FINOPS_BUDGET_EXCEEDED == "finops_budget_exceeded"

    # -- Rollback --

    def test_rollback_requested_value(self):
        """ROLLBACK_REQUESTED = 'rollback_requested'."""
        assert EventType.ROLLBACK_REQUESTED == "rollback_requested"

    def test_rollback_started_value(self):
        """ROLLBACK_STARTED = 'rollback_started'."""
        assert EventType.ROLLBACK_STARTED == "rollback_started"

    def test_rollback_completed_value(self):
        """ROLLBACK_COMPLETED = 'rollback_completed'."""
        assert EventType.ROLLBACK_COMPLETED == "rollback_completed"

    def test_rollback_failed_value(self):
        """ROLLBACK_FAILED = 'rollback_failed'."""
        assert EventType.ROLLBACK_FAILED == "rollback_failed"

    def test_rollback_cancelled_value(self):
        """ROLLBACK_CANCELLED = 'rollback_cancelled'."""
        assert EventType.ROLLBACK_CANCELLED == "rollback_cancelled"

    # -- Learning --

    def test_learning_parameter_blacklisted_value(self):
        """LEARNING_PARAMETER_BLACKLISTED = 'learning_parameter_blacklisted'."""
        assert (
            EventType.LEARNING_PARAMETER_BLACKLISTED == "learning_parameter_blacklisted"
        )

    def test_learning_pattern_detected_value(self):
        """LEARNING_PATTERN_DETECTED = 'learning_pattern_detected'."""
        assert EventType.LEARNING_PATTERN_DETECTED == "learning_pattern_detected"

    def test_learning_manual_only_activated_value(self):
        """LEARNING_MANUAL_ONLY_ACTIVATED = 'learning_manual_only_activated'."""
        assert (
            EventType.LEARNING_MANUAL_ONLY_ACTIVATED == "learning_manual_only_activated"
        )

    def test_learning_manual_only_deactivated_value(self):
        """LEARNING_MANUAL_ONLY_DEACTIVATED = 'learning_manual_only_deactivated'."""
        assert (
            EventType.LEARNING_MANUAL_ONLY_DEACTIVATED
            == "learning_manual_only_deactivated"
        )

    # -- Cell Topology --

    def test_cell_state_changed_value(self):
        """CELL_STATE_CHANGED = 'cell_state_changed'."""
        assert EventType.CELL_STATE_CHANGED == "cell_state_changed"

    def test_cell_evacuation_started_value(self):
        """CELL_EVACUATION_STARTED = 'cell_evacuation_started'."""
        assert EventType.CELL_EVACUATION_STARTED == "cell_evacuation_started"

    def test_cell_evacuation_completed_value(self):
        """CELL_EVACUATION_COMPLETED = 'cell_evacuation_completed'."""
        assert EventType.CELL_EVACUATION_COMPLETED == "cell_evacuation_completed"

    def test_cell_restored_value(self):
        """CELL_RESTORED = 'cell_restored'."""
        assert EventType.CELL_RESTORED == "cell_restored"

    # -- Circuit Mesh --

    def test_circuit_mesh_override_applied_value(self):
        """CIRCUIT_MESH_OVERRIDE_APPLIED = 'circuit_mesh_override_applied'."""
        assert (
            EventType.CIRCUIT_MESH_OVERRIDE_APPLIED == "circuit_mesh_override_applied"
        )

    def test_circuit_mesh_override_expired_value(self):
        """CIRCUIT_MESH_OVERRIDE_EXPIRED = 'circuit_mesh_override_expired'."""
        assert (
            EventType.CIRCUIT_MESH_OVERRIDE_EXPIRED == "circuit_mesh_override_expired"
        )

    def test_circuit_mesh_override_released_value(self):
        """CIRCUIT_MESH_OVERRIDE_RELEASED = 'circuit_mesh_override_released'."""
        assert (
            EventType.CIRCUIT_MESH_OVERRIDE_RELEASED == "circuit_mesh_override_released"
        )

    def test_circuit_mesh_max_overrides_reached_value(self):
        """CIRCUIT_MESH_MAX_OVERRIDES_REACHED = 'circuit_mesh_max_overrides_reached'."""
        assert (
            EventType.CIRCUIT_MESH_MAX_OVERRIDES_REACHED
            == "circuit_mesh_max_overrides_reached"
        )

    def test_circuit_mesh_escalation_triggered_value(self):
        """CIRCUIT_MESH_ESCALATION_TRIGGERED = 'circuit_mesh_escalation_triggered'."""
        assert (
            EventType.CIRCUIT_MESH_ESCALATION_TRIGGERED
            == "circuit_mesh_escalation_triggered"
        )


# =============================================================================
# B. Contract — MEDIUM priority EventType string values
# =============================================================================


class TestL3MediumPriorityEventTypeContract:
    """MEDIUM priority 서비스 EventType 문자열 값 계약 검증."""

    def test_config_rolled_back_value(self):
        """CONFIG_ROLLED_BACK = 'config_rolled_back'."""
        assert EventType.CONFIG_ROLLED_BACK == "config_rolled_back"

    def test_blast_radius_policy_changed_value(self):
        """BLAST_RADIUS_POLICY_CHANGED = 'blast_radius_policy_changed'."""
        assert EventType.BLAST_RADIUS_POLICY_CHANGED == "blast_radius_policy_changed"

    def test_blast_radius_service_isolated_value(self):
        """BLAST_RADIUS_SERVICE_ISOLATED = 'blast_radius_service_isolated'."""
        assert (
            EventType.BLAST_RADIUS_SERVICE_ISOLATED == "blast_radius_service_isolated"
        )

    def test_cache_l1_l2_drift_detected_value(self):
        """CACHE_L1_L2_DRIFT_DETECTED = 'cache_l1_l2_drift_detected'."""
        assert EventType.CACHE_L1_L2_DRIFT_DETECTED == "cache_l1_l2_drift_detected"

    def test_daily_report_send_failed_value(self):
        """DAILY_REPORT_SEND_FAILED = 'daily_report_send_failed'."""
        assert EventType.DAILY_REPORT_SEND_FAILED == "daily_report_send_failed"

    def test_notification_delivery_failed_value(self):
        """NOTIFICATION_DELIVERY_FAILED = 'notification_delivery_failed'."""
        assert EventType.NOTIFICATION_DELIVERY_FAILED == "notification_delivery_failed"


# =============================================================================
# C. Contract — Value uniqueness and naming convention
# =============================================================================


class TestEventTypeUniquenessContract:
    """EventType 값 고유성 및 직렬화 계약 검증."""

    def test_all_event_type_values_are_unique(self):
        """모든 EventType 멤버의 문자열 값이 고유하다."""
        values = [member.value for member in EventType]
        assert len(values) == len(set(values))

    def test_event_type_member_name_matches_uppercase_value(self):
        """모든 EventType 멤버 이름은 값의 대문자 변환과 일치한다."""
        for member in EventType:
            assert member.name == member.value.upper()

    def test_event_type_is_str_serializable(self):
        """EventType은 str(Enum) 상속으로 JSON 직렬화 가능하다."""
        assert (
            str(EventType.FINOPS_BUDGET_EXCEEDED) == "EventType.FINOPS_BUDGET_EXCEEDED"
        )
        assert EventType.FINOPS_BUDGET_EXCEEDED.value == "finops_budget_exceeded"

    def test_l3_high_priority_event_type_count_is_20(self):
        """doc 382에서 추가된 HIGH priority L3 EventType은 20개이다 (5 services)."""
        high_prefixes = (
            "FINOPS_",
            "ROLLBACK_",
            "LEARNING_",
            "CELL_STATE_",
            "CELL_EVACUATION_",
            "CELL_RESTORED",
            "CIRCUIT_MESH_",
        )
        high_members = [m for m in EventType if m.name.startswith(high_prefixes)]
        assert len(high_members) == 20

    def test_l3_medium_priority_event_type_count_is_6(self):
        """doc 382에서 추가된 MEDIUM priority L3 EventType은 6개이다."""
        medium_prefixes = (
            "CONFIG_ROLLED_",
            "BLAST_RADIUS_",
            "CACHE_L1_L2_",
            "DAILY_REPORT_",
            "NOTIFICATION_DELIVERY_",
        )
        medium_members = [m for m in EventType if m.name.startswith(medium_prefixes)]
        assert len(medium_members) == 6
