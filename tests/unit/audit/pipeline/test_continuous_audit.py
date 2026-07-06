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
    """AuditConfig 설계 계약값 검증."""

    def test_default_config_development(self):
        """개발 환경에서 기본 설정 로드."""
        with patch.dict(os.environ, {"BALDUR_ENVIRONMENT": "development"}, clear=False):
            # 기존 AUDIT_HASH_SEED 제거
            env = os.environ.copy()
            env.pop("AUDIT_HASH_SEED", None)
            with patch.dict(os.environ, env, clear=True):
                os.environ["BALDUR_ENVIRONMENT"] = "development"
                config = AuditConfig()

                # 개발 환경에서는 기본 시드 사용
                assert config.hash_seed == "dev-seed-not-for-production"
                assert config.retention_days == 365
                assert config.storage_backend == "file"

    def test_config_from_env(self):
        """환경변수에서 설정 로드."""
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
        """DNA 설정에서 로드 (환경변수 우선)."""
        dna_config = {
            "hash_seed": "dna-seed",
            "retention_days": 180,
            "storage": "loki",
            "alert_channels": ["email"],
        }

        # 환경변수가 없으면 DNA 값 사용
        with patch.dict(os.environ, {"BALDUR_ENVIRONMENT": "development"}, clear=True):
            config = AuditConfig.from_dna(dna_config)

            # hash_seed는 없으므로 DNA 값 (그러나 __post_init__에서 기본값으로 대체)
            assert config.retention_days == 180
            assert config.storage_backend == "loki"
            assert config.alert_channels == ["email"]

    def test_env_overrides_dna(self):
        """환경변수가 DNA보다 우선."""
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
            assert config.retention_days == 365  # 환경변수 우선
            assert config.storage_backend == "loki"  # DNA 값

    def test_to_dict_masks_seed(self):
        """to_dict()에서 해시 시드 마스킹."""
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
    """규정별 보존 기간 설계 계약값 검증."""

    def test_retention_days_constants(self):
        """규정별 보존 기간 상수 확인."""
        assert COMPLIANCE_RETENTION_DAYS["DORA"] == 365 * 5  # 5년
        assert COMPLIANCE_RETENTION_DAYS["PCI-DSS"] == 365  # 1년
        assert COMPLIANCE_RETENTION_DAYS["SOC2"] == 365  # 1년
        assert COMPLIANCE_RETENTION_DAYS["HIPAA"] == 365 * 6  # 6년
        assert COMPLIANCE_RETENTION_DAYS["GDPR"] is None  # 목적 달성 시

    def test_get_recommended_retention_single(self):
        """단일 규정 보존 기간."""
        assert get_recommended_retention(["DORA"]) == 365 * 5
        assert get_recommended_retention(["PCI-DSS"]) == 365

    def test_get_recommended_retention_multiple(self):
        """다중 규정 보존 기간 (최대값 반환)."""
        assert get_recommended_retention(["DORA", "PCI-DSS"]) == 365 * 5
        assert get_recommended_retention(["HIPAA", "DORA"]) == 365 * 6

    def test_get_recommended_retention_unknown(self):
        """알 수 없는 규정은 기본값."""
        assert get_recommended_retention(["UNKNOWN"]) == 365


class TestContinuousAuditRecorderBehavior:
    """ContinuousAuditRecorder 동작 검증."""

    @pytest.fixture
    def temp_log_file(self):
        """임시 로그 파일 생성."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            yield Path(f.name)
        # 정리
        Path(f.name).unlink(missing_ok=True)

    @pytest.fixture
    def recorder(self, temp_log_file):
        """ContinuousAuditRecorder 인스턴스."""
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
        """자율 조정 기록."""
        audit_id = recorder.record_auto_tuning(
            parameter="timeout_ms",
            old_value=5000,
            new_value=6000,
            reason="P99 레이턴시 증가",
            confidence=0.85,
            metrics_snapshot={"p99_latency_ms": 4200},
            safety_check={"within_bounds": True, "bounds": {"min": 100, "max": 30000}},
        )

        assert audit_id.startswith("audit-")

        # 조회 확인
        entries = recorder.query_auto_tuning_history(parameter="timeout_ms")
        assert len(entries) == 1

        entry = entries[0]
        assert entry["action"] == AuditAction.AUTO_TUNING_ADJUSTMENT.value
        assert entry["target_id"] == "timeout_ms"
        assert entry["details"]["before"]["value"] == 5000
        assert entry["details"]["after"]["value"] == 6000
        assert entry["details"]["after"]["confidence"] == 0.85

    def test_record_auto_tuning_rejected(self, recorder):
        """자율 조정 거부 기록."""
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
        """DNA Drift 감지 기록."""
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
        """Compliance 검사 기록."""
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
        """해시 체인 무결성 검증."""
        # 여러 이벤트 기록
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

        # 체인 상태 확인
        state = recorder.get_chain_state()
        assert state["sequence"] == 2
        assert "previous_hash" in state

    def test_export_jsonl(self, recorder):
        """JSON Lines 익스포트."""
        # 이벤트 기록
        recorder.record_auto_tuning(
            parameter="timeout_ms",
            old_value=5000,
            new_value=6000,
            reason="test",
            confidence=0.9,
            metrics_snapshot={},
            safety_check={},
        )

        # 익스포트
        lines = list(recorder.export_jsonl())
        assert len(lines) == 1

        data = json.loads(lines[0])
        assert data["action"] == AuditAction.AUTO_TUNING_ADJUSTMENT.value

    def test_export_csv_compatible(self, recorder):
        """CSV 호환 형식 익스포트."""
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
        # 중첩 구조 평탄화 확인
        assert "details_parameter" in row

    def test_query_with_filters(self, recorder):
        """필터를 사용한 쿼리."""
        # 다양한 이벤트 기록
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

        # 액션 필터
        auto_entries = recorder.query(action=AuditAction.AUTO_TUNING_ADJUSTMENT)
        assert len(auto_entries) == 1

        drift_entries = recorder.query(action=AuditAction.DNA_DRIFT_DETECTED)
        assert len(drift_entries) == 1

    def test_alert_callback(self, temp_log_file):
        """알림 콜백 호출."""
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
    """해시 체인 무결성 동작 검증."""

    def test_hash_chain_manager_adds_integrity(self):
        """HashChainManager가 무결성 정보를 추가."""
        manager = HashChainManager()

        entry = {"action": "test", "data": "value"}
        result = manager.add_integrity(entry)

        assert "integrity" in result
        assert result["integrity"]["sequence"] == 1
        assert result["integrity"]["previous_hash"] == "GENESIS"
        assert "current_hash" in result["integrity"]

    def test_hash_chain_sequence(self):
        """해시 체인 시퀀스 증가."""
        manager = HashChainManager()

        entry1 = manager.add_integrity({"action": "test1"})
        entry2 = manager.add_integrity({"action": "test2"})
        entry3 = manager.add_integrity({"action": "test3"})

        assert entry1["integrity"]["sequence"] == 1
        assert entry2["integrity"]["sequence"] == 2
        assert entry3["integrity"]["sequence"] == 3

        # 이전 해시 연결 확인
        assert (
            entry2["integrity"]["previous_hash"] == entry1["integrity"]["current_hash"]
        )
        assert (
            entry3["integrity"]["previous_hash"] == entry2["integrity"]["current_hash"]
        )

    def test_hash_chain_verifier_valid(self):
        """유효한 해시 체인 검증."""
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
        """수정된 엔트리 감지."""
        manager = HashChainManager()

        entries = [
            manager.add_integrity({"action": "test1"}),
            manager.add_integrity({"action": "test2"}),
            manager.add_integrity({"action": "test3"}),
        ]

        # 두 번째 엔트리 수정
        entries[1]["action"] = "modified!"

        verifier = HashChainVerifier()
        is_valid, error = verifier.verify_chain(entries)

        assert is_valid is False
        assert "hash mismatch" in error or "modified" in error.lower()

    def test_hash_chain_verifier_detects_missing(self):
        """누락된 엔트리 감지."""
        manager = HashChainManager()

        entry1 = manager.add_integrity({"action": "test1"})
        manager.add_integrity({"action": "test2"})
        entry3 = manager.add_integrity({"action": "test3"})

        # 두 번째 엔트리 제거
        entries = [entry1, entry3]

        verifier = HashChainVerifier()
        is_valid, error = verifier.verify_chain(entries)

        assert is_valid is False
        assert "Missing" in error or "sequence" in error


class TestAuditActionExtensionsContract:
    """AuditAction 확장 설계 계약값 검증."""

    def test_auto_tuning_actions_exist(self):
        """자율 조정 관련 액션 존재."""
        assert hasattr(AuditAction, "AUTO_TUNING_ADJUSTMENT")
        assert hasattr(AuditAction, "AUTO_TUNING_ENABLED")
        assert hasattr(AuditAction, "AUTO_TUNING_DISABLED")
        assert hasattr(AuditAction, "AUTO_TUNING_BOUNDS_CHANGED")
        assert hasattr(AuditAction, "AUTO_TUNING_REJECTED")
        assert hasattr(AuditAction, "AUTO_TUNING_ROLLBACK")

    def test_drift_actions_exist(self):
        """DNA Drift 관련 액션 존재."""
        assert hasattr(AuditAction, "DNA_DRIFT_DETECTED")
        assert hasattr(AuditAction, "DNA_DRIFT_RESOLVED")

    def test_compliance_actions_exist(self):
        """Compliance 관련 액션 존재."""
        assert hasattr(AuditAction, "COMPLIANCE_CHECK")
        assert hasattr(AuditAction, "COMPLIANCE_VIOLATION")

    def test_action_values(self):
        """액션 값 확인."""
        assert AuditAction.AUTO_TUNING_ADJUSTMENT.value == "auto_tuning_adjustment"
        assert AuditAction.DNA_DRIFT_DETECTED.value == "dna_drift_detected"
        assert AuditAction.COMPLIANCE_CHECK.value == "compliance_check"
