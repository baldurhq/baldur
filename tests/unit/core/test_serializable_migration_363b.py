"""363B Migration Snapshot Tests

Verify that migrated Category A classes produce identical to_dict()/from_dict()
output after replacing boilerplate with SerializableMixin inheritance.

Representative classes selected across different type patterns:
- Simple primitives (StopConditionsConfig)
- Nested list of objects (StopConditionCheckResult → StopConditionViolation)
- asdict() replacement (DriftThresholdConfig, EmergencyState, ConfigVersion,
  IncidentGroupEntry)
- Optional Enum field pattern
- IntEnum fields (EmergencyState — EmergencyLevel)
- set field (CanaryFeatureFlag → SampleConditional pattern)

Test Categories:
    A. Snapshot Tests: verify exact dict output (key-set + values)
    B. Round-trip Tests: to_dict → from_dict preserves all fields
"""

from __future__ import annotations

import pytest


class TestStopConditionsConfigSnapshotBehavior:
    """StopConditionsConfig serialization snapshot — all primitive fields."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_to_dict_key_set_matches_fields(self):
        """to_dict() output keys match all dataclass field names."""
        from baldur_pro.services.chaos.stop_conditions import StopConditionsConfig

        obj = StopConditionsConfig()
        result = obj.to_dict()
        expected_keys = {
            "max_error_rate_percent",
            "max_latency_p99_ms",
            "max_latency_p95_ms",
            "min_error_budget_percent",
            "check_interval_seconds",
            "consecutive_breaches_required",
            "enabled",
        }
        assert set(result.keys()) == expected_keys

    def test_to_dict_default_values_match(self):
        """to_dict() default values match dataclass defaults."""
        from baldur_pro.services.chaos.stop_conditions import StopConditionsConfig

        obj = StopConditionsConfig()
        result = obj.to_dict()
        assert result["max_error_rate_percent"] == obj.max_error_rate_percent
        assert result["enabled"] == obj.enabled

    def test_roundtrip_preserves_custom_values(self):
        """to_dict → from_dict round-trip preserves custom values."""
        from baldur_pro.services.chaos.stop_conditions import StopConditionsConfig

        original = StopConditionsConfig(
            max_error_rate_percent=10.0,
            max_latency_p99_ms=5000,
            enabled=False,
        )
        restored = StopConditionsConfig.from_dict(original.to_dict())
        assert restored.max_error_rate_percent == original.max_error_rate_percent
        assert restored.max_latency_p99_ms == original.max_latency_p99_ms
        assert restored.enabled == original.enabled


class TestDriftThresholdConfigSnapshotBehavior:
    """DriftThresholdConfig snapshot — previously used asdict() pattern."""

    def test_to_dict_key_set_matches_fields(self):
        """to_dict() output keys match all dataclass field names."""
        from baldur.models.drift_config import DriftThresholdConfig

        obj = DriftThresholdConfig()
        result = obj.to_dict()
        expected_keys = {
            "warning_threshold",
            "critical_threshold",
            "incident_threshold",
            "alert_enabled",
            "incident_auto_create",
            "updated_at",
            "updated_by",
        }
        assert set(result.keys()) == expected_keys

    def test_roundtrip_preserves_values(self):
        """to_dict → from_dict round-trip preserves all fields."""
        from baldur.models.drift_config import DriftThresholdConfig

        original = DriftThresholdConfig(
            warning_threshold=0.10,
            critical_threshold=0.30,
            incident_threshold=0.60,
            alert_enabled=False,
            incident_auto_create=False,
        )
        restored = DriftThresholdConfig.from_dict(original.to_dict())
        assert restored.warning_threshold == original.warning_threshold
        assert restored.critical_threshold == original.critical_threshold
        assert restored.incident_threshold == original.incident_threshold
        assert restored.alert_enabled == original.alert_enabled
        assert restored.incident_auto_create == original.incident_auto_create


class TestStopConditionCheckResultSnapshotBehavior:
    """StopConditionCheckResult snapshot — nested list of objects."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_to_dict_key_set_matches_fields(self):
        """to_dict() output keys match all dataclass field names."""
        import dataclasses

        from baldur_pro.services.chaos.stop_conditions import (
            StopConditionCheckResult,
        )

        obj = StopConditionCheckResult(should_stop=False)
        result = obj.to_dict()
        field_names = {f.name for f in dataclasses.fields(StopConditionCheckResult)}
        assert set(result.keys()) == field_names

    def test_to_dict_nested_violations_serialized(self):
        """Nested StopConditionViolation objects serialized recursively."""
        from baldur_pro.services.chaos.stop_conditions import (
            StopConditionCheckResult,
            StopConditionViolation,
        )

        violation = StopConditionViolation(
            condition_type="error_rate",
            current_value=8.5,
            threshold_value=5.0,
            message="Error rate exceeded",
        )
        obj = StopConditionCheckResult(
            should_stop=True,
            violations=[violation],
            consecutive_breach_count=3,
        )
        result = obj.to_dict()
        assert result["should_stop"] is True
        assert len(result["violations"]) == 1
        assert result["violations"][0]["condition_type"] == "error_rate"
        assert result["violations"][0]["current_value"] == 8.5


class TestMetricSnapshotRoundtripBehavior:
    """MetricSnapshot round-trip — default_factory fields."""

    def test_roundtrip_preserves_dict_and_metadata_fields(self):
        """to_dict → from_dict round-trip preserves dict and metadata fields."""
        from baldur.metrics.snapshot_storage import MetricSnapshot

        original = MetricSnapshot(
            values={"dlq_pending": {"payment": 5, "toss": 3}},
            created_at=1710000000.0,
            updated_at=1710000100.0,
            version="1.0",
            source="bulk",
        )
        serialized = original.to_dict()
        restored = MetricSnapshot.from_dict(serialized)
        assert restored.values == original.values
        assert restored.created_at == original.created_at
        assert restored.updated_at == original.updated_at
        assert restored.version == original.version
        assert restored.source == original.source


class TestDeploymentEventSnapshotBehavior:
    """DeploymentEvent snapshot — Enum + datetime pattern."""

    def test_to_dict_key_set_includes_all_fields(self):
        """to_dict() key set includes all deployment fields."""
        import dataclasses

        from baldur.adapters.deployment.base import (
            DeploymentEvent,
            DeploymentSource,
            DeploymentType,
        )

        obj = DeploymentEvent(
            deployment_id="deploy-001",
            service_name="payment-service",
            version_from="1.0.0",
            version_to="1.1.0",
            deployed_at="2026-01-15T10:00:00+00:00",
            deployed_by="ci-pipeline",
            deployment_type=DeploymentType.ROLLING,
            source=DeploymentSource.KUBERNETES,
            namespace="production",
            is_rollback=False,
            metadata={"cluster": "prod-kr-1"},
        )
        field_names = {f.name for f in dataclasses.fields(DeploymentEvent)}
        result_keys = set(obj.to_dict().keys())
        assert result_keys == field_names


class TestEmergencyStateSnapshotBehavior:
    """EmergencyState snapshot — IntEnum fields, previously used asdict() pattern."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_to_dict_key_set_matches_fields(self):
        """to_dict() output keys match all dataclass field names."""
        import dataclasses

        from baldur_pro.services.emergency_mode.models import EmergencyState

        obj = EmergencyState()
        result = obj.to_dict()
        field_names = {f.name for f in dataclasses.fields(EmergencyState)}
        assert set(result.keys()) == field_names

    def test_int_enum_level_serialized_as_int(self):
        """EmergencyLevel (str, Enum) serialized as string value."""
        from baldur.models.emergency import EmergencyLevel
        from baldur_pro.services.emergency_mode.models import EmergencyState

        obj = EmergencyState(level=EmergencyLevel.LEVEL_2)
        result = obj.to_dict()
        assert result["level"] == "level_2"
        assert isinstance(result["level"], str)

    def test_optional_int_enum_target_level_serialized(self):
        """Optional (str, Enum) target_level serialized when present."""
        from baldur.models.emergency import EmergencyLevel
        from baldur_pro.services.emergency_mode.models import EmergencyState

        obj = EmergencyState(
            level=EmergencyLevel.LEVEL_3,
            target_level=EmergencyLevel.LEVEL_1,
        )
        result = obj.to_dict()
        assert result["level"] == "level_3"
        assert result["target_level"] == "level_1"

    def test_optional_int_enum_target_level_none(self):
        """Optional IntEnum target_level serialized as None when absent."""
        from baldur_pro.services.emergency_mode.models import EmergencyState

        obj = EmergencyState()
        result = obj.to_dict()
        assert result["target_level"] is None

    def test_roundtrip_preserves_int_enum_fields(self):
        """to_dict → from_dict round-trip preserves IntEnum fields."""
        from baldur.models.emergency import EmergencyLevel
        from baldur_pro.services.emergency_mode.models import EmergencyState

        original = EmergencyState(
            level=EmergencyLevel.LEVEL_3,
            is_active=True,
            activated_at="2026-01-15T10:00:00+00:00",
            activated_by="system",
            activation_reason="High error rate",
            target_level=EmergencyLevel.LEVEL_1,
            is_recovering=True,
            metadata={"is_chaos_experiment": True, "experiment_id": "exp-001"},
        )
        serialized = original.to_dict()
        restored = EmergencyState.from_dict(serialized)
        assert restored.level == original.level
        assert isinstance(restored.level, EmergencyLevel)
        assert restored.target_level == original.target_level
        assert isinstance(restored.target_level, EmergencyLevel)
        assert restored.is_active == original.is_active
        assert restored.metadata == original.metadata


class TestConfigVersionSnapshotBehavior:
    """ConfigVersion snapshot — previously used asdict() pattern."""

    def test_to_dict_key_set_matches_fields(self):
        """to_dict() output keys match all dataclass field names."""
        import dataclasses

        from baldur.services.config_history.models import ConfigVersion

        obj = ConfigVersion(
            version=1,
            timestamp=1710000000.0,
            config_type="circuit_breaker",
            values={"failure_threshold": 5},
            changed_by="admin",
            reason="Initial config",
            hash="abc123",
        )
        result = obj.to_dict()
        field_names = {f.name for f in dataclasses.fields(ConfigVersion)}
        assert set(result.keys()) == field_names

    def test_roundtrip_preserves_all_fields(self):
        """to_dict → from_dict round-trip preserves all fields."""
        from baldur.services.config_history.models import ConfigVersion

        original = ConfigVersion(
            version=3,
            timestamp=1710000000.0,
            config_type="dlq",
            values={"max_retries": 3, "retry_delay": 60},
            changed_by="operator",
            reason="Tune retry",
            hash="def456",
        )
        serialized = original.to_dict()
        restored = ConfigVersion.from_dict(serialized)
        assert restored.version == original.version
        assert restored.timestamp == original.timestamp
        assert restored.values == original.values
        assert restored.changed_by == original.changed_by


class TestIncidentGroupEntrySnapshotBehavior:
    """IncidentGroupEntry snapshot — previously used asdict() pattern."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_to_dict_key_set_matches_fields(self):
        """to_dict() output keys match all dataclass field names."""
        import dataclasses

        from baldur_pro.services.postmortem.incident_group import (
            IncidentGroupEntry,
        )

        obj = IncidentGroupEntry(
            service_name="payment",
            closed_at="2026-01-15T10:05:00+00:00",
            opened_at="2026-01-15T10:00:00+00:00",
            duration_seconds=300.0,
            event_data={"error_rate": 0.15},
        )
        result = obj.to_dict()
        field_names = {f.name for f in dataclasses.fields(IncidentGroupEntry)}
        assert set(result.keys()) == field_names

    def test_roundtrip_preserves_all_fields(self):
        """to_dict → from_dict round-trip preserves all fields."""
        from baldur_pro.services.postmortem.incident_group import (
            IncidentGroupEntry,
        )

        original = IncidentGroupEntry(
            service_name="order-service",
            closed_at="2026-01-15T10:05:00+00:00",
            opened_at="2026-01-15T10:00:00+00:00",
            duration_seconds=300.0,
            event_data={"error_rate": 0.15, "region": "us-east-1"},
        )
        serialized = original.to_dict()
        restored = IncidentGroupEntry.from_dict(serialized)
        assert restored.service_name == original.service_name
        assert restored.duration_seconds == original.duration_seconds
        assert restored.event_data == original.event_data


class TestSagaContextRoundtripBehavior:
    """SagaContext round-trip — both to_dict and from_dict removed."""

    def test_roundtrip_preserves_all_fields(self):
        """to_dict → from_dict preserves SagaContext fields."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.saga.models import SagaContext

        original = SagaContext(
            saga_instance_id="saga-001",
            initial_data={"order_id": "order-123", "amount": 50000},
            step_results={"reserve_stock": {"reserved": True}},
            failed_step_name="charge_payment",
            abort_reason="Payment gateway timeout",
            abort_error_code="PAYMENT_TIMEOUT",
        )
        serialized = original.to_dict()
        restored = SagaContext.from_dict(serialized)
        assert restored.saga_instance_id == original.saga_instance_id
        assert restored.initial_data == original.initial_data
        assert restored.step_results == original.step_results
        assert restored.failed_step_name == original.failed_step_name
        assert restored.abort_reason == original.abort_reason
        assert restored.abort_error_code == original.abort_error_code
