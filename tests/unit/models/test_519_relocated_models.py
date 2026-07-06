"""Unit tests for 519 PR 1 / PR 3 — relocated DTOs/enums at baldur.models.*.

PR 1 relocated ``StepResult`` to ``baldur.models.saga``; PR 3 relocated
``PassCriteria``/``CanaryState``/``CanaryStage``/``DLQConfig``/``RecoveryGateConfig``/
``OverrideType``/``ExperimentStatus``/``NotificationPriority``/``NotificationCategory``/
``NotificationPayload``/``RevisionChangeType``/``EmergencyScope``/``ScopedEmergencyState``
to their respective ``baldur.models.<svc>`` canonical homes.

These tests verify:
- Construction + default-value contracts on the OSS canonical home.
- Factory-method behavior (``StepResult.succeeded``, ``PassCriteria.for_tier``,
  ``DLQEntryResult.fallback``, etc.).
- ``from_settings()`` mock-patching behavior (``DLQConfig`` reads
  ``RuntimeConfigManager`` via ``ProviderRegistry``, ``RecoveryGateConfig``
  reads ``EmergencyModeSettings``).

Targets per 519 § Test Assessment.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

from baldur.models.canary import CanaryStage, CanaryState, PassCriteria
from baldur.models.dlq import CleanupStats, DLQConfig, DLQEntryResult
from baldur.models.emergency import (
    EmergencyLevel,
    EmergencyScope,
    ScopedEmergencyState,
)
from baldur.models.experiment import ExperimentStatus
from baldur.models.notification import (
    NotificationCategory,
    NotificationPayload,
    NotificationPriority,
)
from baldur.models.recovery import OverrideType, RecoveryGateConfig
from baldur.models.runtime_config import RevisionChangeType
from baldur.models.saga import StepResult
from baldur.utils.time import utc_now

# =============================================================================
# StepResult (PR 1 / G1)
# =============================================================================


class TestStepResultContract:
    """Contract — StepResult constructor + dataclass defaults (519 PR 1)."""

    def test_default_success_flag_propagates(self):
        result = StepResult(success=True)
        assert result.success is True
        assert result.data == {}
        assert result.error is None
        assert result.error_code is None
        assert result.retryable is False
        assert result.partial_execution is False


class TestStepResultFactoryBehavior:
    """Behavior — succeeded()/failed()/failed_with_side_effect() factories."""

    def test_succeeded_returns_success_true_and_empty_data(self):
        r = StepResult.succeeded()
        assert r.success is True
        assert r.data == {}
        assert r.partial_execution is False

    def test_succeeded_with_data_preserves_payload(self):
        r = StepResult.succeeded({"k": "v"})
        assert r.success is True
        assert r.data == {"k": "v"}

    def test_failed_returns_success_false_with_error_metadata(self):
        r = StepResult.failed(error="boom", error_code="E_BOOM", retryable=True)
        assert r.success is False
        assert r.error == "boom"
        assert r.error_code == "E_BOOM"
        assert r.retryable is True
        assert r.partial_execution is False

    def test_failed_with_side_effect_marks_partial_execution_true(self):
        """Only the side-effect factory sets ``partial_execution=True``."""
        r = StepResult.failed_with_side_effect(
            error="boom", data={"rollback": "needed"}
        )
        assert r.success is False
        assert r.partial_execution is True
        assert r.data == {"rollback": "needed"}

    def test_factory_methods_produce_independent_instances(self):
        """Idempotency — repeated calls produce equal-but-distinct instances."""
        a = StepResult.succeeded({"k": "v"})
        b = StepResult.succeeded({"k": "v"})
        assert a is not b
        assert a.success == b.success
        assert a.data == b.data
        # Mutating one does not leak into the other (no shared default dict).
        a.data["leak"] = True
        assert "leak" not in b.data


# =============================================================================
# Canary value types (PR 3 / G4)
# =============================================================================


class TestPassCriteriaContract:
    """Contract — PassCriteria default thresholds."""

    def test_defaults_match_documented_values(self):
        pc = PassCriteria()
        assert pc.error_rate_absolute_max == 0.05
        assert pc.error_rate_increase_max == 0.01
        assert pc.latency_p95_delta_ms == 50.0
        assert pc.latency_p99_delta_pct == 0.2
        assert pc.error_budget_drain_rate_max == 1.2
        assert pc.error_budget_remaining_min == 0.1
        assert pc.min_requests_required == 100
        assert pc.evaluation_window_seconds == 300

    def test_for_tier_critical_tightens_thresholds(self):
        pc = PassCriteria.for_tier("critical")
        assert pc.error_budget_drain_rate_max == 0.8
        assert pc.error_budget_remaining_min == 0.15
        assert pc.error_rate_absolute_max == 0.03

    def test_for_tier_non_essential_relaxes_thresholds(self):
        pc = PassCriteria.for_tier("non_essential")
        assert pc.error_budget_drain_rate_max == 2.0
        assert pc.error_budget_remaining_min == 0.05
        assert pc.error_rate_absolute_max == 0.10

    def test_for_tier_unknown_falls_back_to_defaults(self):
        """Unknown tier returns class defaults (graceful degradation)."""
        pc = PassCriteria.for_tier("unknown_tier_xyz")
        assert pc.error_rate_absolute_max == 0.05


class TestCanaryValueTypesContract:
    """Contract — CanaryState / CanaryStage value types."""

    def test_canary_state_enum_values(self):
        assert CanaryState.CREATED.value == "created"
        assert CanaryState.CANARY.value == "canary"
        assert CanaryState.PROMOTING.value == "promoting"
        assert CanaryState.PAUSED.value == "paused"
        assert CanaryState.COMPLETED.value == "completed"
        assert CanaryState.ROLLED_BACK.value == "rolled_back"
        assert CanaryState.FAILED.value == "failed"
        assert CanaryState.CANCELLED.value == "cancelled"

    def test_canary_stage_defaults(self):
        stage = CanaryStage(name="s1", clusters=["a", "b"], percentage=10.0)
        assert stage.duration_minutes == 5
        assert stage.auto_promote is True
        assert isinstance(stage.pass_criteria, PassCriteria)


# =============================================================================
# DLQ value types (PR 3)
# =============================================================================


class TestDLQValueTypesContract:
    """Contract — DLQConfig / DLQEntryResult / CleanupStats factories + properties."""

    def test_dlq_config_defaults(self):
        cfg = DLQConfig()
        assert cfg.enabled is True
        assert cfg.retention_days == 30
        assert cfg.max_replay_attempts == 2
        assert cfg.retry_delay == 60
        assert cfg.expiry_hours == 72
        assert cfg.batch_size == 10

    def test_dlq_entry_result_created_factory(self):
        r = DLQEntryResult.created(dlq_id=42)
        assert r.success is True
        assert r.dlq_id == 42
        assert r.fallback_path is None
        assert r.is_fallback is False

    def test_dlq_entry_result_failed_factory(self):
        r = DLQEntryResult.failed(error="db down")
        assert r.success is False
        assert r.error == "db down"
        assert r.is_fallback is False

    def test_dlq_entry_result_fallback_factory_marks_is_fallback_true(self):
        r = DLQEntryResult.fallback(error="db down", fallback_path="/tmp/dlq.log")
        assert r.success is False
        assert r.error == "db down"
        assert r.fallback_path == "/tmp/dlq.log"
        assert r.is_fallback is True

    def test_cleanup_stats_can_archive_property(self):
        stats = CleanupStats(resolved_older_than_30_days=7)
        assert stats.can_archive == 7

    def test_cleanup_stats_can_purge_property(self):
        stats = CleanupStats(archived_older_than_90_days=3)
        assert stats.can_purge == 3


class TestDLQConfigFromSettingsBehavior:
    """Behavior — ``DLQConfig.from_settings()`` reads ``RuntimeConfigManager`` first,
    falls back to ``DLQSettings`` when the manager is absent or raises."""

    def test_uses_runtime_config_when_manager_returns_dict(self):
        manager = MagicMock()
        manager.get_dlq_config.return_value = {
            "enabled": False,
            "retention_days": 7,
            "max_replay_attempts": 9,
            "retry_delay": 12,
            "expiry_hours": 5,
            "batch_size": 20,
        }
        with patch(
            "baldur.factory.registry.ProviderRegistry.runtime_config_manager.safe_get",
            return_value=manager,
        ):
            cfg = DLQConfig.from_settings()

        assert cfg.enabled is False
        assert cfg.retention_days == 7
        assert cfg.max_replay_attempts == 9
        assert cfg.retry_delay == 12
        assert cfg.expiry_hours == 5
        assert cfg.batch_size == 20

    def test_falls_back_to_pydantic_settings_when_manager_none(self):
        """``safe_get()`` returning ``None`` falls back to the static settings path."""
        with patch(
            "baldur.factory.registry.ProviderRegistry.runtime_config_manager.safe_get",
            return_value=None,
        ):
            cfg = DLQConfig.from_settings()

        # Pydantic defaults — exact values come from DLQSettings; we assert the
        # type and that defaults round-tripped without raising.
        assert isinstance(cfg, DLQConfig)


# =============================================================================
# Recovery value types (PR 3)
# =============================================================================


class TestRecoveryValueTypesContract:
    """Contract — OverrideType + RecoveryGateConfig defaults + serialization."""

    def test_override_type_enum_values(self):
        assert OverrideType.HOTFIX.value == "hotfix"
        assert OverrideType.SECURITY_PATCH.value == "security_patch"
        assert OverrideType.EXECUTIVE_APPROVAL.value == "executive_approval"
        assert OverrideType.ROLLBACK.value == "rollback"

    def test_recovery_gate_config_defaults(self):
        cfg = RecoveryGateConfig()
        assert cfg.stabilization_period_seconds == 300
        assert cfg.require_metrics_stable is True
        assert cfg.cpu_threshold_percent == 80.0
        assert cfg.error_rate_threshold == 0.05
        assert cfg.gradual_recovery is True
        assert cfg.level_step_delay_seconds == 60
        assert cfg.health_check_interval_seconds == 30
        assert cfg.auto_rollback_on_failure is True

    def test_recovery_gate_config_serialization_roundtrip(self):
        original = RecoveryGateConfig(
            stabilization_period_seconds=120,
            cpu_threshold_percent=70.0,
        )
        roundtripped = RecoveryGateConfig.from_dict(original.to_dict())
        assert roundtripped.stabilization_period_seconds == 120
        assert roundtripped.cpu_threshold_percent == 70.0


class TestRecoveryGateFromSettingsBehavior:
    """Behavior — ``RecoveryGateConfig.from_settings()`` reads EmergencyModeSettings,
    falls back to defaults when settings unavailable."""

    def test_uses_emergency_mode_settings_when_available(self):
        settings = MagicMock()
        settings.stabilization_period_seconds = 99
        settings.cpu_threshold_percent = 55.5
        settings.error_rate_threshold = 0.02
        settings.level_step_delay_seconds = 11
        settings.health_check_interval_seconds = 7

        with patch(
            "baldur.settings.emergency_mode.get_emergency_mode_settings",
            return_value=settings,
        ):
            cfg = RecoveryGateConfig.from_settings()

        assert cfg.stabilization_period_seconds == 99
        assert cfg.cpu_threshold_percent == 55.5
        assert cfg.error_rate_threshold == 0.02
        assert cfg.level_step_delay_seconds == 11
        assert cfg.health_check_interval_seconds == 7

    def test_falls_back_to_defaults_when_settings_unavailable(self):
        """If settings retrieval raises, the hardcoded defaults are returned."""
        with patch(
            "baldur.settings.emergency_mode.get_emergency_mode_settings",
            side_effect=RuntimeError("settings down"),
        ):
            cfg = RecoveryGateConfig.from_settings()

        assert cfg.stabilization_period_seconds == 300
        assert cfg.cpu_threshold_percent == 80.0


# =============================================================================
# Experiment status (PR 3)
# =============================================================================


class TestExperimentStatusContract:
    """Contract — ExperimentStatus enum values."""

    def test_enum_member_values(self):
        assert ExperimentStatus.PENDING.value == "pending"
        assert ExperimentStatus.AWAITING_APPROVAL.value == "awaiting_approval"
        assert ExperimentStatus.RUNNING.value == "running"
        assert ExperimentStatus.COMPLETED.value == "completed"
        assert ExperimentStatus.FAILED.value == "failed"
        assert ExperimentStatus.ABORTED.value == "aborted"
        assert ExperimentStatus.SKIPPED.value == "skipped"
        assert ExperimentStatus.ROLLED_BACK.value == "rolled_back"
        assert ExperimentStatus.RECOVERY_MONITORING.value == "recovery_monitoring"

    def test_enum_member_count(self):
        assert len(list(ExperimentStatus)) == 9


# =============================================================================
# Notification value types (PR 3)
# =============================================================================


class TestNotificationValueTypesContract:
    """Contract — NotificationPriority / NotificationCategory / NotificationPayload."""

    def test_priority_enum_values(self):
        assert NotificationPriority.CRITICAL.value == "critical"
        assert NotificationPriority.HIGH.value == "high"
        assert NotificationPriority.MEDIUM.value == "medium"
        assert NotificationPriority.LOW.value == "low"
        assert NotificationPriority.INFO.value == "info"

    def test_category_enum_values(self):
        assert NotificationCategory.SECURITY.value == "security"
        assert NotificationCategory.OPERATIONS.value == "operations"
        assert NotificationCategory.SLA.value == "sla"
        assert NotificationCategory.CIRCUIT_BREAKER.value == "circuit_breaker"

    def test_payload_defaults(self):
        p = NotificationPayload(title="t", message="m")
        assert p.priority is NotificationPriority.MEDIUM
        assert p.category is NotificationCategory.OPERATIONS
        assert p.source == "unknown"
        assert p.task_name is None
        assert p.task_id is None
        assert p.metadata == {}
        assert p.tags == []
        assert p.channels is None
        assert p.dedup_key is None

    def test_payload_serialization_roundtrip(self):
        original = NotificationPayload(
            title="Outage",
            message="region us-east-1 degraded",
            priority=NotificationPriority.CRITICAL,
            category=NotificationCategory.OPERATIONS,
            metadata={"region": "us-east-1"},
            tags=["paging"],
        )
        roundtripped = NotificationPayload.from_dict(original.to_dict())
        assert roundtripped.title == "Outage"
        assert roundtripped.priority is NotificationPriority.CRITICAL
        assert roundtripped.category is NotificationCategory.OPERATIONS
        assert roundtripped.metadata == {"region": "us-east-1"}


# =============================================================================
# Runtime-config revision type (PR 3)
# =============================================================================


class TestRevisionChangeTypeContract:
    def test_enum_member_values(self):
        assert RevisionChangeType.INITIAL.value == "initial"
        assert RevisionChangeType.ANALYSIS_UPDATE.value == "analysis_update"
        assert RevisionChangeType.TIMELINE_CORRECTION.value == "timeline_correction"
        assert RevisionChangeType.IMPROVEMENT_ADDED.value == "improvement_added"
        assert RevisionChangeType.ANNOTATION.value == "annotation"
        assert RevisionChangeType.CORRECTION.value == "correction"
        assert RevisionChangeType.SEALED.value == "sealed"
        assert RevisionChangeType.ROLLBACK.value == "rollback"

    def test_enum_member_count_is_8(self):
        assert len(list(RevisionChangeType)) == 8


# =============================================================================
# Emergency extended types (PR 3 — Deviation 3)
# =============================================================================


class TestEmergencyExtendedTypesContract:
    """Contract — EmergencyScope + ScopedEmergencyState live in OSS canonical."""

    def test_emergency_scope_enum_values(self):
        assert EmergencyScope.REGIONAL.value == "regional"
        assert EmergencyScope.GLOBAL.value == "global"

    def test_scoped_emergency_state_defaults(self):
        s = ScopedEmergencyState(namespace="seoul")
        assert s.emergency_level is EmergencyLevel.NORMAL
        assert s.governance_mode == "NORMAL"
        assert s.scope is EmergencyScope.REGIONAL
        assert s.activated_at is None
        assert s.expires_at is None
        assert s.metadata == {}


class TestScopedEmergencyStateBehavior:
    """Behavior — ScopedEmergencyState ``is_active`` / ``is_expired`` time-driven."""

    def test_is_active_false_when_level_is_normal(self):
        s = ScopedEmergencyState(namespace="seoul")
        assert s.is_active() is False

    def test_is_active_true_when_level_is_above_normal(self):
        s = ScopedEmergencyState(
            namespace="seoul",
            emergency_level=EmergencyLevel.LEVEL_2,
        )
        assert s.is_active() is True

    def test_is_expired_false_when_expires_at_is_none(self):
        """Boundary: ``expires_at=None`` → never expired."""
        s = ScopedEmergencyState(namespace="seoul")
        assert s.is_expired() is False

    def test_is_expired_true_when_expires_at_is_in_the_past(self):
        s = ScopedEmergencyState(
            namespace="seoul",
            expires_at=utc_now() - timedelta(seconds=10),
        )
        assert s.is_expired() is True

    def test_is_expired_false_when_expires_at_is_in_the_future(self):
        s = ScopedEmergencyState(
            namespace="seoul",
            expires_at=utc_now() + timedelta(hours=1),
        )
        assert s.is_expired() is False
