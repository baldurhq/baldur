"""
339 Settings Extensions 단위 테스트.

기존 settings 클래스에 추가된 필드와 validator에 대한 테스트.

테스트 분류 (UNIT_TEST_GUIDELINES §0):
- Contract: 339 문서에 명시된 확장 필드 기본값/제약 검증
- Behavior: CellTopologySettings weight sum validator, env override

참조 소스:
- settings/recovery_shutdown.py (check_interval_seconds, max_request_age_seconds)
- settings/cell_topology.py (11 health fields + weight sum validator)
- settings/emergency_mode.py (penalty_cache_ttl_seconds)
- settings/audit_integrity.py (health_score_max_events, health_score_cache_ttl_seconds)
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings.audit_integrity import AuditIntegritySettings
from baldur.settings.cell_topology import CellTopologySettings
from baldur.settings.emergency_mode import EmergencyModeSettings
from baldur.settings.recovery_shutdown import RecoveryShutdownSettings

# =============================================================================
# RecoveryShutdownSettings — 339 확장 필드
# =============================================================================


class TestRecoveryShutdownExtensionContract:
    """RecoveryShutdownSettings 339 확장 필드 계약 검증."""

    def test_check_interval_seconds_default(self):
        """GracefulShutdownCoordinator drain loop check interval: 0.5초."""
        assert RecoveryShutdownSettings().check_interval_seconds == 0.5

    def test_max_request_age_seconds_default(self):
        """Tracked request max age before cleanup: 300.0초."""
        assert RecoveryShutdownSettings().max_request_age_seconds == 300.0

    def test_check_interval_below_minimum_rejected(self):
        """check_interval_seconds: ge=0.1 미만 → ValidationError."""
        with pytest.raises(ValidationError):
            RecoveryShutdownSettings(check_interval_seconds=0.09)

    def test_check_interval_at_minimum_accepted(self):
        """check_interval_seconds: ge=0.1 경계값 → 성공."""
        s = RecoveryShutdownSettings(check_interval_seconds=0.1)
        assert s.check_interval_seconds == pytest.approx(0.1)

    def test_max_request_age_below_minimum_rejected(self):
        """max_request_age_seconds: ge=30.0 미만 → ValidationError."""
        with pytest.raises(ValidationError):
            RecoveryShutdownSettings(max_request_age_seconds=29.9)

    def test_max_request_age_above_maximum_rejected(self):
        """max_request_age_seconds: le=3600.0 초과 → ValidationError."""
        with pytest.raises(ValidationError):
            RecoveryShutdownSettings(max_request_age_seconds=3601.0)


# =============================================================================
# CellTopologySettings — 339 확장 필드 + weight validator
# =============================================================================


class TestCellTopologyExtensionContract:
    """CellTopologySettings 339 확장 필드 기본값 계약 검증."""

    # Prometheus API settings
    def test_prometheus_timeout_seconds_default(self):
        """Prometheus API timeout default: 3.0초."""
        default = CellTopologySettings.model_fields[
            "prometheus_timeout_seconds"
        ].default
        assert default == 3.0

    def test_prometheus_max_consecutive_failures_default(self):
        """Prometheus 연속 실패 임계값: 3."""
        assert CellTopologySettings().prometheus_max_consecutive_failures == 3

    def test_prometheus_retry_after_seconds_default(self):
        """Prometheus half-open probe 재시도 대기: 60.0초."""
        assert CellTopologySettings().prometheus_retry_after_seconds == 60.0

    # Health score weights
    def test_health_weight_error_rate_default(self):
        """에러율 가중치: 0.35."""
        assert CellTopologySettings().health_weight_error_rate == pytest.approx(0.35)

    def test_health_weight_latency_default(self):
        """레이턴시 가중치: 0.25."""
        assert CellTopologySettings().health_weight_latency == pytest.approx(0.25)

    def test_health_weight_bulkhead_default(self):
        """Bulkhead 가중치: 0.20."""
        assert CellTopologySettings().health_weight_bulkhead == pytest.approx(0.20)

    def test_health_weight_cb_open_default(self):
        """CB Open 가중치: 0.20."""
        assert CellTopologySettings().health_weight_cb_open == pytest.approx(0.20)

    # Normalization & thresholds
    def test_health_max_error_rate_default(self):
        """에러율 정규화 기준: 0.5 (50%)."""
        assert CellTopologySettings().health_max_error_rate == pytest.approx(0.5)

    def test_health_max_latency_p99_default(self):
        """P99 레이턴시 정규화 기준: 5.0초."""
        assert CellTopologySettings().health_max_latency_p99 == 5.0

    def test_health_min_samples_for_penalty_default(self):
        """최소 표본 수: 10."""
        assert CellTopologySettings().health_min_samples_for_penalty == 10

    def test_health_ewma_alpha_default(self):
        """EWMA smoothing factor: 0.3."""
        assert CellTopologySettings().health_ewma_alpha == pytest.approx(0.3)


class TestCellTopologyWeightValidatorBehavior:
    """CellTopologySettings weight sum model_validator 동작 검증."""

    def test_default_weights_sum_to_one(self):
        """기본 가중치 합산 = 1.0 → 검증 통과."""
        s = CellTopologySettings()
        total = (
            s.health_weight_error_rate
            + s.health_weight_latency
            + s.health_weight_bulkhead
            + s.health_weight_cb_open
        )
        assert total == pytest.approx(1.0)

    def test_custom_weights_summing_to_one_accepted(self):
        """합산 1.0인 커스텀 가중치 → 검증 통과."""
        s = CellTopologySettings(
            health_weight_error_rate=0.4,
            health_weight_latency=0.3,
            health_weight_bulkhead=0.2,
            health_weight_cb_open=0.1,
        )
        assert s.health_weight_error_rate == pytest.approx(0.4)

    def test_weights_not_summing_to_one_rejected(self):
        """합산 ≠ 1.0 가중치 → ValueError."""
        with pytest.raises(ValidationError, match="must sum to 1.0"):
            CellTopologySettings(
                health_weight_error_rate=0.5,
                health_weight_latency=0.3,
                health_weight_bulkhead=0.2,
                health_weight_cb_open=0.2,
            )

    def test_weights_zero_sum_rejected(self):
        """모든 가중치 0.0 → ValueError."""
        with pytest.raises(ValidationError, match="must sum to 1.0"):
            CellTopologySettings(
                health_weight_error_rate=0.0,
                health_weight_latency=0.0,
                health_weight_bulkhead=0.0,
                health_weight_cb_open=0.0,
            )


class TestCellTopologyExtensionBoundaryContract:
    """CellTopologySettings 확장 필드 경계값 검증."""

    def test_prometheus_timeout_below_minimum_rejected(self):
        """prometheus_timeout_seconds: ge=0.5 미만 → ValidationError."""
        with pytest.raises(ValidationError):
            CellTopologySettings(prometheus_timeout_seconds=0.4)

    def test_health_ewma_alpha_above_maximum_rejected(self):
        """health_ewma_alpha: le=1.0 초과 → ValidationError."""
        with pytest.raises(ValidationError):
            CellTopologySettings(health_ewma_alpha=1.1)

    def test_health_min_samples_below_minimum_rejected(self):
        """health_min_samples_for_penalty: ge=1 미만 → ValidationError."""
        with pytest.raises(ValidationError):
            CellTopologySettings(health_min_samples_for_penalty=0)


# =============================================================================
# EmergencyModeSettings — 339 확장 필드
# =============================================================================


class TestEmergencyModeExtensionContract:
    """EmergencyModeSettings 339 확장 필드 계약 검증."""

    def test_penalty_cache_ttl_seconds_default(self):
        """EmergencyHealthPenalty cache TTL: 5.0초."""
        assert EmergencyModeSettings().penalty_cache_ttl_seconds == 5.0

    def test_penalty_cache_ttl_below_minimum_rejected(self):
        """penalty_cache_ttl_seconds: ge=1.0 미만 → ValidationError."""
        with pytest.raises(ValidationError):
            EmergencyModeSettings(penalty_cache_ttl_seconds=0.9)

    def test_penalty_cache_ttl_above_maximum_rejected(self):
        """penalty_cache_ttl_seconds: le=60.0 초과 → ValidationError."""
        with pytest.raises(ValidationError):
            EmergencyModeSettings(penalty_cache_ttl_seconds=61.0)


# =============================================================================
# AuditIntegritySettings — 339 확장 필드
# =============================================================================


class TestAuditIntegrityExtensionContract:
    """AuditIntegritySettings 339 확장 필드 계약 검증."""

    def test_health_score_max_events_default(self):
        """IntegrityHealthScore max events buffer: 1000."""
        assert AuditIntegritySettings().health_score_max_events == 1000

    def test_health_score_cache_ttl_seconds_default(self):
        """IntegrityHealthScore cache TTL: 10.0초."""
        assert AuditIntegritySettings().health_score_cache_ttl_seconds == 10.0

    def test_health_score_max_events_below_minimum_rejected(self):
        """health_score_max_events: ge=10 미만 → ValidationError."""
        with pytest.raises(ValidationError):
            AuditIntegritySettings(health_score_max_events=9)

    def test_health_score_cache_ttl_below_minimum_rejected(self):
        """health_score_cache_ttl_seconds: ge=1.0 미만 → ValidationError."""
        with pytest.raises(ValidationError):
            AuditIntegritySettings(health_score_cache_ttl_seconds=0.9)
