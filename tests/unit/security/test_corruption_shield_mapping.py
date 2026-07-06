"""
CorruptionShield ViolationType 매핑 테스트

순위 3 구현 테스트:
- _map_to_violation_type 메서드 테스트
- L1/L2/L3 → 표준 ViolationType 매핑
"""

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


@dataclass
class MockViolation:
    """테스트용 Violation mock 객체."""

    code: str
    layer: str
    message: str = ""
    field: str = ""
    severity: str = "critical"


class TestCorruptionShieldMapping:
    """CorruptionShield ViolationType 매핑 테스트."""

    @pytest.fixture
    def shield(self):
        """CorruptionShield 인스턴스 생성."""
        from baldur_pro.services.corruption_shield.shield import (
            CorruptionShield,
            CorruptionShieldConfig,
        )

        config = CorruptionShieldConfig(log_to_security_incident=False)
        return CorruptionShield(config=config)

    def test_l1_maps_to_schema_violation(self, shield):
        """
        Purpose:
            L1 위반이 SCHEMA_VIOLATION으로 매핑되는지 확인.
        """
        from baldur.services.security import ViolationType

        violation = MockViolation(code="missing_field", layer="L1")
        result = shield._map_to_violation_type(violation)

        assert result == ViolationType.SCHEMA_VIOLATION.value

    def test_l2_maps_to_business_rule_violation(self, shield):
        """
        Purpose:
            L2 위반이 BUSINESS_RULE_VIOLATION으로 매핑되는지 확인.
        """
        from baldur.services.security import ViolationType

        violation = MockViolation(code="invalid_amount", layer="L2")
        result = shield._map_to_violation_type(violation)

        assert result == ViolationType.BUSINESS_RULE_VIOLATION.value

    def test_l3_maps_to_anomaly_statistical(self, shield):
        """
        Purpose:
            L3 위반이 ANOMALY_STATISTICAL으로 매핑되는지 확인.
        """
        from baldur.services.security import ViolationType

        violation = MockViolation(code="zscore_exceeded", layer="L3")
        result = shield._map_to_violation_type(violation)

        assert result == ViolationType.ANOMALY_STATISTICAL.value

    def test_behavioral_anomaly_maps_correctly(self, shield):
        """
        Purpose:
            behavioral anomaly가 ANOMALY_BEHAVIORAL으로 매핑되는지 확인.
        """
        from baldur.services.security import ViolationType

        violation = MockViolation(code="behavioral_anomaly_detected", layer="L3")
        result = shield._map_to_violation_type(violation)

        assert result == ViolationType.ANOMALY_BEHAVIORAL.value

    def test_statistical_anomaly_maps_correctly(self, shield):
        """
        Purpose:
            anomaly 키워드가 있으면 ANOMALY_STATISTICAL으로 매핑되는지 확인.
        """
        from baldur.services.security import ViolationType

        violation = MockViolation(code="anomaly_detected", layer="L3")
        result = shield._map_to_violation_type(violation)

        assert result == ViolationType.ANOMALY_STATISTICAL.value

    def test_unknown_layer_maps_to_suspicious_activity(self, shield):
        """
        Purpose:
            알 수 없는 layer가 SUSPICIOUS_ACTIVITY로 폴백되는지 확인.
        """
        from baldur.services.security import ViolationType

        violation = MockViolation(code="unknown", layer="L4")  # 존재하지 않는 레이어
        result = shield._map_to_violation_type(violation)

        assert result == ViolationType.SUSPICIOUS_ACTIVITY.value

    def test_maybe_create_security_incident_uses_mapping(self, shield):
        """
        Purpose:
            _maybe_create_security_incident이 매핑된 ViolationType을 사용하는지 확인.
        """
        from baldur_pro.services.corruption_shield.shield import ValidationResult

        with patch(
            "baldur.services.security.SecurityViolationService"
        ) as mock_svc_class:
            mock_service = MagicMock()
            mock_svc_class.return_value = mock_service

            # 테스트용 ValidationResult - 실제 dataclass 필드명 사용
            violation = MockViolation(
                code="injection_attempt",
                layer="L1",
                message="Possible injection",
                field="query",
                severity="critical",
            )
            result = MagicMock(spec=ValidationResult)
            result.is_valid = False
            result.blocked = True
            result.violations = [violation]
            result.l1_passed = False
            result.l2_passed = True
            result.l3_passed = True

            # log_to_security_incident 활성화
            shield.config.log_to_security_incident = True
            shield._maybe_create_security_incident({"query": "test"}, result)

            # record_violation이 호출되었는지 확인
            mock_service.record_violation.assert_called_once()
            call_args = mock_service.record_violation.call_args

            # violation_type이 표준 값으로 전달되었는지 확인
            assert call_args.kwargs["violation_type"] == "schema_violation"


class TestCorruptionShieldIntegration:
    """CorruptionShield 통합 테스트."""

    def test_shield_records_violation_with_correct_type(self):
        """
        Purpose:
            CorruptionShield가 보안 위반을 올바른 ViolationType으로 기록하는지 확인.

        통합 테스트: _maybe_create_security_incident가 실제 ViolationType 매핑을 사용.
        """
        from baldur.services.security import ViolationType
        from baldur_pro.services.corruption_shield.shield import (
            CorruptionShield,
            CorruptionShieldConfig,
            ValidationResult,
        )

        # Mock SecurityViolationService
        with patch(
            "baldur.services.security.SecurityViolationService"
        ) as mock_svc_class:
            mock_service = MagicMock()
            mock_svc_class.return_value = mock_service

            # 테스트용 config
            config = CorruptionShieldConfig(
                log_to_security_incident=True,
                l1_enabled=True,
            )
            shield = CorruptionShield(config=config)

            # L2 critical 위반 시뮬레이션
            l2_violation = MagicMock()
            l2_violation.code = "negative_amount"
            l2_violation.layer = "L2"
            l2_violation.message = "Amount cannot be negative"
            l2_violation.field = "amount"
            l2_violation.severity = "critical"

            # 가짜 ValidationResult 생성
            result = MagicMock(spec=ValidationResult)
            result.is_valid = False
            result.blocked = True
            result.violations = [l2_violation]
            result.l1_passed = True
            result.l2_passed = False
            result.l3_passed = True

            # _maybe_create_security_incident 호출
            shield._maybe_create_security_incident({"amount": -100}, result)

            # record_violation이 호출되었는지 확인
            mock_service.record_violation.assert_called_once()
            call_args = mock_service.record_violation.call_args

            # L2 → BUSINESS_RULE_VIOLATION으로 매핑되었는지 확인
            assert (
                call_args.kwargs["violation_type"]
                == ViolationType.BUSINESS_RULE_VIOLATION.value
            )
