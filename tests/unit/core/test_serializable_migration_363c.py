"""363C SerializableMixin Category B/C Migration Tests

Verify that Category B/C migrated classes produce correct to_dict()/from_dict()
output after migration to SerializableMixin.

Migration phases tested:
    Phase 1-2: Override removal (CoordinationAction, ScopedEmergencyState,
               DangerousForceRecoveryAuditEntry, RecoveryAuditEntry)
    Phase 3:   exclude_none=True (ErrorInfo, ResponseMeta, GateCheckResult)
    Phase 4-5: _post_serialize() hooks, property injection
    Phase 6:   Mixin inheritance with explicit override retention
    Special:   EmergencyLevel (str, Enum) migration, CheckpointData kw_only

Test Categories:
    Contract:  Hardcoded expected values
    Behavior:  Source-referenced expected values
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

# =============================================================================
# 1. EmergencyLevel Contract Tests
# =============================================================================


class TestEmergencyLevelMigrationContract:
    """EmergencyLevel (str, Enum) migration contract — hardcoded expected values."""

    def test_values_are_string_type(self):
        """All EmergencyLevel values are str instances."""
        from baldur.models.emergency import EmergencyLevel

        for member in EmergencyLevel:
            assert isinstance(member.value, str)

    def test_normal_value_is_normal(self):
        """EmergencyLevel.NORMAL.value == 'normal'."""
        from baldur.models.emergency import EmergencyLevel

        assert EmergencyLevel.NORMAL.value == "normal"

    def test_level_1_value_is_level_1(self):
        """EmergencyLevel.LEVEL_1.value == 'level_1'."""
        from baldur.models.emergency import EmergencyLevel

        assert EmergencyLevel.LEVEL_1.value == "level_1"

    def test_level_2_value_is_level_2(self):
        """EmergencyLevel.LEVEL_2.value == 'level_2'."""
        from baldur.models.emergency import EmergencyLevel

        assert EmergencyLevel.LEVEL_2.value == "level_2"

    def test_level_3_value_is_level_3(self):
        """EmergencyLevel.LEVEL_3.value == 'level_3'."""
        from baldur.models.emergency import EmergencyLevel

        assert EmergencyLevel.LEVEL_3.value == "level_3"

    def test_severity_normal_is_zero(self):
        """EmergencyLevel.NORMAL.severity == 0."""
        from baldur.models.emergency import EmergencyLevel

        assert EmergencyLevel.NORMAL.severity == 0

    def test_severity_level_3_is_three(self):
        """EmergencyLevel.LEVEL_3.severity == 3."""
        from baldur.models.emergency import EmergencyLevel

        assert EmergencyLevel.LEVEL_3.severity == 3

    def test_from_severity_zero_returns_normal(self):
        """from_severity(0) returns EmergencyLevel.NORMAL."""
        from baldur.models.emergency import EmergencyLevel

        assert EmergencyLevel.from_severity(0) is EmergencyLevel.NORMAL

    def test_from_severity_three_returns_level_3(self):
        """from_severity(3) returns EmergencyLevel.LEVEL_3."""
        from baldur.models.emergency import EmergencyLevel

        assert EmergencyLevel.from_severity(3) is EmergencyLevel.LEVEL_3


# =============================================================================
# 2. EmergencyLevel Ordering Behavior
# =============================================================================


class TestEmergencyLevelOrderingBehavior:
    """EmergencyLevel severity-based ordering."""

    def test_normal_less_than_level_1(self):
        """NORMAL < LEVEL_1 (severity 0 < 1)."""
        from baldur.models.emergency import EmergencyLevel

        assert EmergencyLevel.NORMAL < EmergencyLevel.LEVEL_1

    def test_level_3_greater_than_level_2(self):
        """LEVEL_3 > LEVEL_2 (severity 3 > 2)."""
        from baldur.models.emergency import EmergencyLevel

        assert EmergencyLevel.LEVEL_3 > EmergencyLevel.LEVEL_2

    def test_ordering_is_severity_based_not_lexicographic(self):
        """NORMAL < LEVEL_1 even though 'normal' > 'level_1' lexicographically."""
        from baldur.models.emergency import EmergencyLevel

        # Given: lexicographic ordering would be reversed
        assert "normal" > "level_1"

        # When/Then: severity-based ordering is used
        assert EmergencyLevel.NORMAL < EmergencyLevel.LEVEL_1

    def test_equal_levels_are_not_less_than(self):
        """Same level is not less than itself."""
        from baldur.models.emergency import EmergencyLevel

        assert not (EmergencyLevel.LEVEL_2 < EmergencyLevel.LEVEL_2)
        assert EmergencyLevel.LEVEL_2 >= EmergencyLevel.LEVEL_2
        assert EmergencyLevel.LEVEL_2 <= EmergencyLevel.LEVEL_2

    def test_from_severity_invalid_raises_value_error(self):
        """from_severity with invalid value raises ValueError."""

        from baldur.models.emergency import EmergencyLevel

        with pytest.raises(ValueError, match="Unknown severity"):
            EmergencyLevel.from_severity(99)


# =============================================================================
# 3. Phase 1-2 Override Removal Roundtrip
# =============================================================================


class TestOverrideRemovalRoundtripBehavior:
    """Phase 1-2: classes that had redundant to_dict() overrides removed."""

    def test_coordination_action_roundtrip(self):
        """CoordinationAction to_dict -> from_dict preserves all fields."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.coordination.enums import ActionType
        from baldur_pro.services.coordination.models import CoordinationAction

        # Given
        original = CoordinationAction(
            type=ActionType.CANARY_ROLLBACK,
            immediate=True,
            delay_seconds=30,
            params={"multiplier": 0.5},
            ttl_minutes=90,
            is_dry_run=True,
        )

        # When
        data = original.to_dict()
        restored = CoordinationAction.from_dict(data)

        # Then
        assert restored.type == original.type
        assert restored.immediate == original.immediate
        assert restored.delay_seconds == original.delay_seconds
        assert restored.params == original.params
        assert restored.ttl_minutes == original.ttl_minutes
        assert restored.is_dry_run == original.is_dry_run

    def test_coordination_action_enum_serialized_as_value(self):
        """ActionType enum is serialized as its string value."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.coordination.enums import ActionType
        from baldur_pro.services.coordination.models import CoordinationAction

        obj = CoordinationAction(type=ActionType.GOVERNANCE_STRICT)
        data = obj.to_dict()

        assert data["type"] == "governance_strict"

    def test_scoped_emergency_state_roundtrip(self):
        """ScopedEmergencyState to_dict -> from_dict preserves all fields."""
        from baldur.models.emergency import (
            EmergencyLevel,
            EmergencyScope,
            ScopedEmergencyState,
        )

        # Given
        now = datetime.now(UTC)
        original = ScopedEmergencyState(
            namespace="seoul",
            emergency_level=EmergencyLevel.LEVEL_3,
            governance_mode="STRICT",
            scope=EmergencyScope.GLOBAL,
            activated_at=now,
            activated_by="admin-001",
            reason="DB overload",
            expires_at=now,
            metadata={"region": "ap-northeast-2"},
        )

        # When
        data = original.to_dict()
        restored = ScopedEmergencyState.from_dict(data)

        # Then
        assert restored.namespace == original.namespace
        assert restored.emergency_level == original.emergency_level
        assert restored.governance_mode == original.governance_mode
        assert restored.scope == original.scope
        assert restored.activated_at == original.activated_at
        assert restored.activated_by == original.activated_by
        assert restored.reason == original.reason
        assert restored.expires_at == original.expires_at
        assert restored.metadata == original.metadata

    def test_scoped_emergency_state_optional_datetime_none(self):
        """ScopedEmergencyState handles None datetimes correctly."""
        from baldur.models.emergency import ScopedEmergencyState

        obj = ScopedEmergencyState(
            namespace="tokyo",
            activated_at=None,
            expires_at=None,
        )
        data = obj.to_dict()
        restored = ScopedEmergencyState.from_dict(data)

        assert restored.activated_at is None
        assert restored.expires_at is None

    def test_recovery_audit_entry_roundtrip(self):
        """RecoveryAuditEntry to_dict -> from_dict preserves all fields."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.coordination.recovery_audit import (
            RecoveryAuditEntry,
            RecoveryAuditEventType,
        )

        # Given
        original = RecoveryAuditEntry(
            event_type=RecoveryAuditEventType.RECOVERY_STEP_EXECUTED,
            session_id="sess-123",
            namespace="seoul",
            step_type="health_check",
            step_order=2,
            executed_by="system",
            success=True,
            duration_ms=150.5,
            trace_id="trace-abc",
            metadata={"key": "value"},
        )

        # When
        data = original.to_dict()
        restored = RecoveryAuditEntry.from_dict(data)

        # Then
        assert restored.event_type == original.event_type
        assert restored.session_id == original.session_id
        assert restored.namespace == original.namespace
        assert restored.step_type == original.step_type
        assert restored.step_order == original.step_order
        assert restored.executed_by == original.executed_by
        assert restored.success == original.success
        assert restored.duration_ms == original.duration_ms
        assert restored.trace_id == original.trace_id
        assert restored.metadata == original.metadata


# =============================================================================
# 4. Phase 3 Exclude None Snapshot
# =============================================================================


class TestExcludeNoneSnapshotBehavior:
    """Phase 3: classes with exclude_none=True."""

    def test_error_info_excludes_none_fields(self):
        """ErrorInfo with detail=None and field=None excludes those keys."""
        from baldur.api.django.exceptions.response import ErrorInfo

        obj = ErrorInfo(
            code="VALIDATION_ERROR",
            message="Bad input",
            detail=None,
            field=None,
        )
        data = obj.to_dict()

        assert "detail" not in data
        assert "field" not in data
        assert data["code"] == "VALIDATION_ERROR"
        assert data["message"] == "Bad input"

    def test_error_info_includes_non_none_fields(self):
        """ErrorInfo with detail='x' includes that field."""
        from baldur.api.django.exceptions.response import ErrorInfo

        obj = ErrorInfo(
            code="VALIDATION_ERROR",
            message="Bad input",
            detail="Missing amount",
            field="amount",
        )
        data = obj.to_dict()

        assert data["detail"] == "Missing amount"
        assert data["field"] == "amount"

    def test_response_meta_excludes_none_fields(self):
        """ResponseMeta with None optional fields excludes those keys, timestamp always present."""
        from baldur.api.django.exceptions.response import ResponseMeta

        obj = ResponseMeta(
            request_id=None,
            path=None,
            method=None,
            causation_id=None,
            region=None,
        )
        data = obj.to_dict()

        assert "request_id" not in data
        assert "path" not in data
        assert "method" not in data
        assert "causation_id" not in data
        assert "region" not in data
        # timestamp is always present (has default_factory)
        assert "timestamp" in data

    def test_gate_check_result_excludes_optional_none_fields(self):
        """GateCheckResult with rate_limit_remaining=None excludes that key."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.error_budget_gate.config import (
            GateCheckResult,
            GateStatus,
        )

        obj = GateCheckResult(
            allowed=True,
            status=GateStatus.OPEN,
            error_budget_percent=None,
            rate_limit_remaining=None,
            rate_limit_reset_at=None,
            tier_id=None,
            region=None,
        )
        data = obj.to_dict()

        assert "rate_limit_remaining" not in data
        assert "rate_limit_reset_at" not in data
        assert "error_budget_percent" not in data
        assert "tier_id" not in data
        assert "region" not in data

    def test_gate_check_result_includes_present_optional_fields(self):
        """GateCheckResult with rate_limit_remaining=10 includes that field."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.error_budget_gate.config import (
            GateCheckResult,
            GateStatus,
        )

        now = datetime.now(UTC)
        obj = GateCheckResult(
            allowed=False,
            status=GateStatus.BLOCKED,
            error_budget_percent=5.0,
            rate_limit_remaining=10,
            rate_limit_reset_at=now,
            tier_id="critical",
            region="seoul",
        )
        data = obj.to_dict()

        assert data["rate_limit_remaining"] == 10
        assert data["tier_id"] == "critical"
        assert data["region"] == "seoul"
        assert data["error_budget_percent"] == 5.0


# =============================================================================
# 5. Phase 4-5 Post-Serialize Snapshot
# =============================================================================


class TestPostSerializeSnapshotBehavior:
    """Phase 4-5: _post_serialize() hooks and property injection."""

    def test_control_response_omits_falsy_optional_fields(self):
        """ControlResponse omits falsy string/dict fields via _post_serialize."""
        from baldur.services.control_api_service.models import ControlResponse

        obj = ControlResponse(
            status="accepted",
            action_applied="circuit_breaker_override",
            system_state="",  # falsy
            effective_until=None,  # falsy
            reason_classification="",  # falsy
            evidence={},  # falsy
            error_code="",  # falsy
            error_message="",  # falsy
            risk_level="",  # falsy
        )
        data = obj.to_dict()

        assert data["status"] == "accepted"
        assert data["action_applied"] == "circuit_breaker_override"
        assert "system_state" not in data
        assert "effective_until" not in data
        assert "reason_classification" not in data
        assert "evidence" not in data
        assert "error_code" not in data
        assert "error_message" not in data
        assert "risk_level" not in data

    def test_stress_test_result_merges_extra_dict(self):
        """StressTestResult merges extra dict into top-level output."""
        from baldur.services.stress_test_service.models import StressTestResult

        obj = StressTestResult(
            status="completed",
            elapsed_seconds=1.23456,
            message="OK",
            extra={"throughput_rps": 500, "p99_ms": 42.1},
        )
        data = obj.to_dict()

        # extra contents merged to top level
        assert data["throughput_rps"] == 500
        assert data["p99_ms"] == 42.1
        # extra key itself is removed
        assert "extra" not in data
        # elapsed_seconds is rounded
        assert data["elapsed_seconds"] == 1.23

    def test_audit_config_masks_sensitive_fields(self):
        """AuditConfig masks hash_seed and hash_chain_redis_url in to_dict()."""
        from baldur.audit.config import AuditConfig

        obj = AuditConfig(hash_seed="my-secret-seed")
        data = obj.to_dict()

        assert data["hash_seed"] == "***"

    def test_impact_assessment_injects_is_critical_property(self):
        """ImpactAssessment injects is_critical property via _post_serialize."""
        pytest.importorskip("baldur_pro")
        from baldur.services.blast_radius.models import ImpactAssessment
        from baldur_pro.services.chaos.blast_radius_analyzer import BlastRadiusLevel

        # Given: CRITICAL level
        obj = ImpactAssessment(
            assessment_id="assess-001",
            service_name="payment",
            trigger_event="cb_opened",
            level=BlastRadiusLevel.CRITICAL,
            affected_services=["order", "billing"],
        )

        # When
        data = obj.to_dict()

        # Then
        assert data["is_critical"] is True

        # Given: MINIMAL level
        obj2 = ImpactAssessment(
            assessment_id="assess-002",
            service_name="payment",
            trigger_event="cb_opened",
            level=BlastRadiusLevel.MINIMAL,
            affected_services=[],
        )
        data2 = obj2.to_dict()

        assert data2["is_critical"] is False

    def test_fail_safe_period_injects_duration_minutes_property(self):
        """FailSafePeriod injects duration_minutes and removes service_name."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.error_budget.reconciliation.models import (
            FailSafePeriod,
        )

        now = datetime.now(UTC)
        obj = FailSafePeriod(
            period_id="fsp-001",
            started_at=now,
            ended_at=now,
            service_name="payment",
            is_active=False,
        )
        data = obj.to_dict()

        # duration_minutes injected
        assert "duration_minutes" in data
        assert isinstance(data["duration_minutes"], float)
        # service_name removed by _post_serialize
        assert "service_name" not in data


# =============================================================================
# 6. Roundtrip for Key Migrated Classes
# =============================================================================


class TestMigrationRoundtripBehavior:
    """Roundtrip tests for key migrated classes."""

    def test_checkpoint_data_roundtrip_with_defaults(self):
        """CheckpointData.from_dict({}) uses field defaults."""
        from baldur.audit.checkpoint_manager import CheckpointData

        # Given: empty dict
        restored = CheckpointData.from_dict({})

        # Then: defaults applied
        assert restored.last_sequence == 0
        assert restored.timestamp == 0.0
        assert restored.version == 1

    def test_checkpoint_data_kw_only_prevents_positional_args(self):
        """CheckpointData uses kw_only=True, preventing positional construction."""

        from baldur.audit.checkpoint_manager import CheckpointData

        with pytest.raises(TypeError):
            CheckpointData(100, 1234.0, 1)  # type: ignore[misc]

        # Keyword arguments work fine
        obj = CheckpointData(last_sequence=100, timestamp=1234.0, version=1)
        assert obj.last_sequence == 100

    def test_governance_check_result_roundtrip(self):
        """GovernanceCheckResult roundtrip handles Optional[Enum] correctly."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.governance.checks import (
            BlockReason,
            GovernanceCheckResult,
        )

        # Given: with block_reason (Optional Enum)
        original = GovernanceCheckResult(
            allowed=False,
            block_reason=BlockReason.EMERGENCY_MODE,
            block_message="Emergency mode LEVEL_3 is active",
            emergency_level="LEVEL_3",
            error_budget_percent=42.5,
            threshold_percent=10.0,
        )

        # When
        data = original.to_dict()
        restored = GovernanceCheckResult.from_dict(data)

        # Then
        assert restored.allowed == original.allowed
        assert restored.block_reason == original.block_reason
        assert restored.block_message == original.block_message
        assert data["block_reason"] == "emergency_mode"

        # Given: with block_reason=None
        allowed_result = GovernanceCheckResult(allowed=True, block_reason=None)
        data2 = allowed_result.to_dict()
        restored2 = GovernanceCheckResult.from_dict(data2)

        assert restored2.block_reason is None

    def test_tier_mapping_roundtrip_with_methods(self):
        """TierMapping roundtrip serializes frozenset as sorted list."""
        from baldur.scaling.tiering.models import TierMapping

        original = TierMapping(
            pattern="/api/payments/*",
            tier_id="critical",
            methods=frozenset({"GET", "POST"}),
        )

        data = original.to_dict()

        # frozenset serialized as sorted list
        assert data["methods"] == ["GET", "POST"]
        # _compiled_pattern excluded
        assert "_compiled_pattern" not in data

        # Roundtrip via from_dict
        restored = TierMapping.from_dict(data)
        assert restored.pattern == original.pattern
        assert restored.tier_id == original.tier_id
        assert restored.methods == frozenset({"GET", "POST"})

    def test_tier_mapping_roundtrip_without_methods(self):
        """TierMapping with methods=None excludes methods key."""
        from baldur.scaling.tiering.models import TierMapping

        original = TierMapping(
            pattern="/api/health",
            tier_id="standard",
            methods=None,
        )

        data = original.to_dict()

        # methods=None -> excluded from output
        assert "methods" not in data

        # Roundtrip
        restored = TierMapping.from_dict(data)
        assert restored.methods is None


# =============================================================================
# 7. Phase 6 Override Retention Verification
# =============================================================================


class TestOverrideRetentionBehavior:
    """Phase 6: classes that gained SerializableMixin but retain explicit overrides."""

    def test_error_budget_status_has_mixin_and_override(self):
        """ErrorBudgetStatus inherits SerializableMixin and has explicit to_dict()."""
        pytest.importorskip("baldur_pro")
        from baldur.core.serializable import SerializableMixin
        from baldur_pro.services.error_budget.models import ErrorBudgetStatus

        # Given
        assert issubclass(ErrorBudgetStatus, SerializableMixin)

        # When: to_dict() still works and produces the custom nested structure
        obj = ErrorBudgetStatus(
            slo_name="availability",
            slo_target=0.999,
            window_days=30,
            budget_total_minutes=43.2,
            budget_consumed_minutes=10.0,
            budget_remaining_minutes=33.2,
            budget_remaining_percent=76.85,
        )
        data = obj.to_dict()

        # Then: custom nested structure (not flat mixin output)
        assert "slo" in data
        assert data["slo"]["name"] == "availability"
        assert "budget" in data
        assert "burn_rate" in data
        assert "health" in data

    def test_cascade_event_has_mixin_and_override(self):
        """CascadeEvent inherits SerializableMixin and has explicit to_dict()."""
        from baldur.audit.cascade_event import (
            CascadeEffect,
            CascadeEvent,
            CascadeTrigger,
        )
        from baldur.core.serializable import SerializableMixin

        assert issubclass(CascadeEvent, SerializableMixin)

        trigger = CascadeTrigger(
            trigger_type="EMERGENCY_LEVEL_CHANGED",
            event_id="evt-001",
            details={"old_level": "NORMAL", "new_level": "LEVEL_3"},
        )
        effects = [
            CascadeEffect(
                event_id="evt-002",
                action_type="GOVERNANCE_STRICT",
                caused_by="evt-001",
                success=True,
            ),
        ]
        event = CascadeEvent(
            id="cascade-abc123",
            trigger=trigger,
            effects=effects,
            namespace="seoul",
            timestamp="2026-01-21T15:30:00Z",
        )
        data = event.to_dict()

        # Custom override includes causation_chain and excludes external_trace when None
        assert "causation_chain" in data
        assert "external_trace" not in data
        assert data["total_effects"] == 1
        assert data["success_count"] == 1
