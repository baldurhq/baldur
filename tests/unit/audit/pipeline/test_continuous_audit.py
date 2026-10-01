"""
Tests for Continuous Audit System.

Tests cover:
- AuditConfig configuration loading
- ContinuousAuditRecorder recording and querying
- Hash chain integrity
- Export functionality
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from baldur.adapters.audit.file_adapter import FileAuditLogAdapter
from baldur.audit.config import (
    COMPLIANCE_RETENTION_DAYS,
    AuditConfig,
    get_recommended_retention,
)
from baldur.audit.continuous_audit import ContinuousAuditRecorder
from baldur.audit.integrity import HashChainManager, HashChainVerifier
from baldur.interfaces.audit_adapter import AuditAction


class TestAuditConfigContract:
    """AuditConfig design contract values."""

    def test_default_config_development(self):
        """Load the default config in development."""
        with patch.dict(os.environ, {"BALDUR_ENVIRONMENT": "development"}, clear=False):
            # Remove any existing AUDIT_HASH_SEED
            env = os.environ.copy()
            env.pop("AUDIT_HASH_SEED", None)
            with patch.dict(os.environ, env, clear=True):
                os.environ["BALDUR_ENVIRONMENT"] = "development"
                config = AuditConfig()

                # Development uses the default seed
                assert config.hash_seed == "dev-seed-not-for-production"
                assert config.retention_days == 365
                assert config.storage_backend == "file"

    def test_config_from_env(self):
        """Load the config from environment variables."""
        env = {
            "AUDIT_HASH_SEED": "test-seed-12345",
            "AUDIT_RETENTION_DAYS": "730",
            "AUDIT_STORAGE": "s3",
            "AUDIT_S3_BUCKET": "my-audit-bucket",
            "AUDIT_S3_WORM": "true",
            "AUDIT_ALERT_CHANNELS": "slack,pagerduty",
            "BALDUR_ENVIRONMENT": "development",
        }

        with patch.dict(os.environ, env, clear=True):
            config = AuditConfig()

            assert config.hash_seed == "test-seed-12345"
            assert config.retention_days == 730
            assert config.storage_backend == "s3"
            assert config.s3_bucket == "my-audit-bucket"
            assert config.s3_worm_enabled is True
            assert config.alert_channels == ["slack", "pagerduty"]

    def test_production_requires_hash_seed(self):
        """In production, AUDIT_HASH_SEED must be set."""
        from baldur.runtime import reset_runtime

        with patch.dict(os.environ, {"BALDUR_ENVIRONMENT": "production"}, clear=True):
            # AuditConfig.__post_init__ delegates to runtime.is_production()
            # which is eager-read at runtime construction. Reset so the
            # patched env is visible.
            reset_runtime()
            with pytest.raises(ValueError) as exc_info:
                AuditConfig()

            assert "AUDIT_HASH_SEED" in str(exc_info.value)

    def test_from_dna(self):
        """Load from the DNA config (environment variables win)."""
        dna_config = {
            "hash_seed": "dna-seed",
            "retention_days": 180,
            "storage": "loki",
            "alert_channels": ["email"],
        }

        # Without environment variables the DNA values apply
        with patch.dict(os.environ, {"BALDUR_ENVIRONMENT": "development"}, clear=True):
            config = AuditConfig.from_dna(dna_config)

            # No hash_seed, so the DNA value applies (replaced by the default in __post_init__)
            assert config.retention_days == 180
            assert config.storage_backend == "loki"
            assert config.alert_channels == ["email"]

    def test_env_overrides_dna(self):
        """Environment variables win over DNA."""
        dna_config = {
            "retention_days": 180,
            "storage": "loki",
        }

        env = {
            "AUDIT_HASH_SEED": "env-seed",
            "AUDIT_RETENTION_DAYS": "365",
            "BALDUR_ENVIRONMENT": "development",
        }

        with patch.dict(os.environ, env, clear=True):
            config = AuditConfig.from_dna(dna_config)

            assert config.hash_seed == "env-seed"
            assert config.retention_days == 365  # environment variable wins
            assert config.storage_backend == "loki"  # DNA value

    def test_to_dict_masks_seed(self):
        """to_dict() masks the hash seed."""
        with patch.dict(
            os.environ,
            {
                "AUDIT_HASH_SEED": "secret-seed",
                "BALDUR_ENVIRONMENT": "development",
            },
            clear=True,
        ):
            config = AuditConfig()
            config_dict = config.to_dict()

            assert config_dict["hash_seed"] == "***"
            assert config_dict["retention_days"] == 365


class TestComplianceRetentionContract:
    """Per-regulation retention design contract values."""

    def test_retention_days_constants(self):
        """Per-regulation retention constants."""
        assert COMPLIANCE_RETENTION_DAYS["DORA"] == 365 * 5  # 5 years
        assert COMPLIANCE_RETENTION_DAYS["PCI-DSS"] == 365  # 1 year
        assert COMPLIANCE_RETENTION_DAYS["SOC2"] == 365  # 1 year
        assert COMPLIANCE_RETENTION_DAYS["HIPAA"] == 365 * 6  # 6 years
        assert (
            COMPLIANCE_RETENTION_DAYS["GDPR"] is None
        )  # until the purpose is fulfilled

    def test_get_recommended_retention_single(self):
        """Retention for a single regulation."""
        assert get_recommended_retention(["DORA"]) == 365 * 5
        assert get_recommended_retention(["PCI-DSS"]) == 365

    def test_get_recommended_retention_multiple(self):
        """Retention for several regulations (the maximum)."""
        assert get_recommended_retention(["DORA", "PCI-DSS"]) == 365 * 5
        assert get_recommended_retention(["HIPAA", "DORA"]) == 365 * 6

    def test_get_recommended_retention_unknown(self):
        """An unknown regulation gets the default."""
        assert get_recommended_retention(["UNKNOWN"]) == 365


class TestContinuousAuditRecorderBehavior:
    """ContinuousAuditRecorder behaviour."""

    @pytest.fixture
    def temp_log_file(self):
        """Create a temporary log file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            yield Path(f.name)
        # Clean up
        Path(f.name).unlink(missing_ok=True)

    @pytest.fixture
    def recorder(self, temp_log_file):
        """A ContinuousAuditRecorder instance."""
        adapter = FileAuditLogAdapter(temp_log_file)

        with patch.dict(
            os.environ,
            {
                "AUDIT_HASH_SEED": "test-seed",
                "BALDUR_ENVIRONMENT": "development",
                "SERVICE_NAME": "test-service",
                "SERVICE_VERSION": "1.0.0",
            },
            clear=False,
        ):
            config = AuditConfig()
            return ContinuousAuditRecorder(
                audit_adapter=adapter,
                config=config,
            )

    def test_record_auto_tuning(self, recorder):
        """Record an auto-tuning adjustment."""
        audit_id = recorder.record_auto_tuning(
            parameter="timeout_ms",
            old_value=5000,
            new_value=6000,
            reason="P99 latency increase",
            confidence=0.85,
            metrics_snapshot={"p99_latency_ms": 4200},
            safety_check={"within_bounds": True, "bounds": {"min": 100, "max": 30000}},
        )

        assert audit_id.startswith("audit-")

        # Query it back
        entries = recorder.query_auto_tuning_history(parameter="timeout_ms")
        assert len(entries) == 1

        entry = entries[0]
        assert entry["action"] == AuditAction.AUTO_TUNING_ADJUSTMENT.value
        assert entry["target_id"] == "timeout_ms"
        assert entry["details"]["before"]["value"] == 5000
        assert entry["details"]["after"]["value"] == 6000
        assert entry["details"]["after"]["confidence"] == 0.85

    def test_record_auto_tuning_rejected(self, recorder):
        """Record a rejected auto-tuning adjustment."""
        recorder.record_auto_tuning_rejected(
            parameter="timeout_ms",
            requested_value=50000,
            current_value=5000,
            rejection_reason="Exceeds maximum bound",
            safety_bounds={"min": 100, "max": 30000},
        )

        entries = recorder.query(action=AuditAction.AUTO_TUNING_REJECTED)
        assert len(entries) == 1
        assert entries[0]["success"] is False

    def test_record_drift_detected(self, recorder):
        """Record a DNA drift detection."""
        recorder.record_drift_detected(
            resource_id="stage14_dlq_api_test",
            declared={"timeout_ms": 5000, "retry_count": 3},
            actual={"timeout_ms": 6000, "retry_count": 3},
            drifted_fields=["timeout_ms"],
            severity="medium",
        )

        entries = recorder.query_drift_history(resource_id="stage14_dlq_api_test")
        assert len(entries) == 1

        entry = entries[0]
        assert entry["action"] == AuditAction.DNA_DRIFT_DETECTED.value
        assert entry["details"]["drifted_fields"] == ["timeout_ms"]
        assert entry["details"]["severity"] == "medium"

    def test_record_compliance_check(self, recorder):
        """Record a compliance check."""
        results = {
            "DORA": {"status": "compliant"},
            "PCI-DSS": {"status": "compliant"},
            "SOC2": {"status": "warning"},
        }

        recorder.record_compliance_check(
            standards_checked=["DORA", "PCI-DSS", "SOC2"],
            results=results,
            overall_status="compliant_with_warnings",
        )

        entries = recorder.query_compliance_history()
        assert len(entries) == 1
        assert entries[0]["details"]["overall_status"] == "compliant_with_warnings"

    def test_hash_chain_integrity(self, recorder):
        """A plain file adapter keeps no chain, so no chain state is reported."""
        # Record several events
        recorder.record_auto_tuning(
            parameter="param1",
            old_value=1,
            new_value=2,
            reason="test",
            confidence=0.9,
            metrics_snapshot={},
            safety_check={},
        )
        recorder.record_auto_tuning(
            parameter="param2",
            old_value=10,
            new_value=20,
            reason="test2",
            confidence=0.95,
            metrics_snapshot={},
            safety_check={},
        )

        # The recorder's own sequence numbers only name audit ids
        state = recorder.get_chain_state()
        assert state["source"] == "no_hash_chain"
        assert state["sequence"] is None

    def test_export_jsonl(self, recorder):
        """JSON Lines export."""
        # Record an event
        recorder.record_auto_tuning(
            parameter="timeout_ms",
            old_value=5000,
            new_value=6000,
            reason="test",
            confidence=0.9,
            metrics_snapshot={},
            safety_check={},
        )

        # Export
        lines = list(recorder.export_jsonl())
        assert len(lines) == 1

        data = json.loads(lines[0])
        assert data["action"] == AuditAction.AUTO_TUNING_ADJUSTMENT.value

    def test_export_csv_compatible(self, recorder):
        """CSV-compatible export."""
        recorder.record_auto_tuning(
            parameter="timeout_ms",
            old_value=5000,
            new_value=6000,
            reason="test",
            confidence=0.9,
            metrics_snapshot={"p99": 4200},
            safety_check={"ok": True},
        )

        data = list(recorder.export_csv_compatible())
        assert len(data) == 1

        row = data[0]
        assert "timestamp" in row
        assert "action" in row
        assert row["target_id"] == "timeout_ms"
        # Nested structure is flattened
        assert "details_parameter" in row

    def test_query_with_filters(self, recorder):
        """Query with filters."""
        # Record several kinds of event
        recorder.record_auto_tuning(
            parameter="timeout_ms",
            old_value=5000,
            new_value=6000,
            reason="test1",
            confidence=0.9,
            metrics_snapshot={},
            safety_check={},
        )
        recorder.record_drift_detected(
            resource_id="stage14",
            declared={},
            actual={},
            drifted_fields=["x"],
            severity="low",
        )

        # Action filter
        auto_entries = recorder.query(action=AuditAction.AUTO_TUNING_ADJUSTMENT)
        assert len(auto_entries) == 1

        drift_entries = recorder.query(action=AuditAction.DNA_DRIFT_DETECTED)
        assert len(drift_entries) == 1

    def test_alert_callback(self, temp_log_file):
        """The alert callback is called."""
        adapter = FileAuditLogAdapter(temp_log_file)

        alerts = []

        def capture_alert(channel, data):
            alerts.append((channel, data))

        with patch.dict(
            os.environ,
            {
                "AUDIT_HASH_SEED": "test-seed",
                "BALDUR_ENVIRONMENT": "development",
            },
            clear=False,
        ):
            config = AuditConfig()
            recorder = ContinuousAuditRecorder(
                audit_adapter=adapter,
                config=config,
                alert_callback=capture_alert,
            )

        recorder.record_auto_tuning(
            parameter="timeout_ms",
            old_value=5000,
            new_value=6000,
            reason="test",
            confidence=0.9,
            metrics_snapshot={},
            safety_check={},
        )

        assert len(alerts) == 1
        assert alerts[0][0] == "auto_tuning"
        assert alerts[0][1]["parameter"] == "timeout_ms"


class TestHashChainIntegrityBehavior:
    """Hash chain integrity behaviour."""

    def test_hash_chain_manager_adds_integrity(self):
        """HashChainManager adds integrity information."""
        manager = HashChainManager()

        entry = {"action": "test", "data": "value"}
        result = manager.add_integrity(entry)

        assert "integrity" in result
        assert result["integrity"]["sequence"] == 1
        assert result["integrity"]["previous_hash"] == "GENESIS"
        assert "current_hash" in result["integrity"]

    def test_hash_chain_sequence(self):
        """The hash chain sequence increments."""
        manager = HashChainManager()

        entry1 = manager.add_integrity({"action": "test1"})
        entry2 = manager.add_integrity({"action": "test2"})
        entry3 = manager.add_integrity({"action": "test3"})

        assert entry1["integrity"]["sequence"] == 1
        assert entry2["integrity"]["sequence"] == 2
        assert entry3["integrity"]["sequence"] == 3

        # Each entry links to the previous hash
        assert (
            entry2["integrity"]["previous_hash"] == entry1["integrity"]["current_hash"]
        )
        assert (
            entry3["integrity"]["previous_hash"] == entry2["integrity"]["current_hash"]
        )

    def test_hash_chain_verifier_valid(self):
        """A valid hash chain verifies."""
        manager = HashChainManager()

        entries = [
            manager.add_integrity({"action": "test1"}),
            manager.add_integrity({"action": "test2"}),
            manager.add_integrity({"action": "test3"}),
        ]

        verifier = HashChainVerifier()
        is_valid, error = verifier.verify_chain(entries)

        assert is_valid is True
        assert error is None

    def test_hash_chain_verifier_detects_modification(self):
        """A modified entry is detected."""
        manager = HashChainManager()

        entries = [
            manager.add_integrity({"action": "test1"}),
            manager.add_integrity({"action": "test2"}),
            manager.add_integrity({"action": "test3"}),
        ]

        # Modify the second entry
        entries[1]["action"] = "modified!"

        verifier = HashChainVerifier()
        is_valid, error = verifier.verify_chain(entries)

        assert is_valid is False
        assert "hash mismatch" in error or "modified" in error.lower()

    def test_hash_chain_verifier_detects_missing(self):
        """A missing entry is detected."""
        manager = HashChainManager()

        entry1 = manager.add_integrity({"action": "test1"})
        manager.add_integrity({"action": "test2"})
        entry3 = manager.add_integrity({"action": "test3"})

        # Remove the second entry
        entries = [entry1, entry3]

        verifier = HashChainVerifier()
        is_valid, error = verifier.verify_chain(entries)

        assert is_valid is False
        assert "Missing" in error or "sequence" in error


class TestAuditActionExtensionsContract:
    """AuditAction extension design contract values."""

    def test_auto_tuning_actions_exist(self):
        """Auto-tuning actions exist."""
        assert hasattr(AuditAction, "AUTO_TUNING_ADJUSTMENT")
        assert hasattr(AuditAction, "AUTO_TUNING_ENABLED")
        assert hasattr(AuditAction, "AUTO_TUNING_DISABLED")
        assert hasattr(AuditAction, "AUTO_TUNING_BOUNDS_CHANGED")
        assert hasattr(AuditAction, "AUTO_TUNING_REJECTED")
        assert hasattr(AuditAction, "AUTO_TUNING_ROLLBACK")

    def test_drift_actions_exist(self):
        """DNA drift actions exist."""
        assert hasattr(AuditAction, "DNA_DRIFT_DETECTED")
        assert hasattr(AuditAction, "DNA_DRIFT_RESOLVED")

    def test_compliance_actions_exist(self):
        """Compliance actions exist."""
        assert hasattr(AuditAction, "COMPLIANCE_CHECK")
        assert hasattr(AuditAction, "COMPLIANCE_VIOLATION")

    def test_action_values(self):
        """Action values."""
        assert AuditAction.AUTO_TUNING_ADJUSTMENT.value == "auto_tuning_adjustment"
        assert AuditAction.DNA_DRIFT_DETECTED.value == "dna_drift_detected"
        assert AuditAction.COMPLIANCE_CHECK.value == "compliance_check"
