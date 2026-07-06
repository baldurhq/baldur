"""
Redis TTL 및 클래스 레벨 상수의 Settings 연동 테스트.

환경 변수를 통해 런타임에 변경 가능한 설정값들의 연동을 검증합니다.

테스트 대상 Settings 필드:
- RateLimitSettings.redis_ttl: Rate Limit 상태 Redis 저장 TTL
- AirGapSettings.redis_ttl: Air-Gap 중간 저장소 TTL
- AuditSettings.buffer_redis_ttl: 감사 버퍼 Redis TTL
- ErrorBudgetSettings.multiplier_cache_ttl: 위기 가중치 캐시 TTL
- ErrorBudgetSettings.multiplier_max: 최대 위기 가중치
- DashboardSettings.stale_threshold_minutes: 데이터 방치 임계치
- DashboardSettings.max_regional_status: 리전 상태 표시 수
- MetricsSettings.snapshot_max_age: 메트릭 스냅샷 최대 유효 기간
- SafeGaugeSettings.max_label_combinations: Prometheus 레이블 조합 제한
- AuditIntegritySettings.archive_threshold_days: Cold Storage 아카이브 임계치
- AuditIntegritySettings.cold_retention_years: Cold Storage 보관 기간

테스트 범위:
1. 기본값 검증
2. 환경 변수 로드 검증
3. 헬퍼 함수 동작 검증
4. 하위 호환성 (레거시 상수 유지) 검증
"""

import os
from unittest import mock

import pytest

# =============================================================================
# 1. RateLimitSettings.redis_ttl
# =============================================================================


class TestRateLimitSettingsRedisTtl:
    """Rate Limit Redis 저장소 TTL 설정 테스트."""

    def test_default_redis_ttl(self):
        """기본 Redis TTL 값 (3600초)."""
        from baldur.settings.rate_limit import (
            RateLimitSettings,
            reset_rate_limit_settings,
        )

        reset_rate_limit_settings()
        settings = RateLimitSettings()
        assert settings.redis_ttl == 3600  # 1시간

    def test_redis_ttl_from_env(self):
        """환경 변수에서 Redis TTL 로드."""
        from baldur.settings.rate_limit import (
            RateLimitSettings,
            reset_rate_limit_settings,
        )

        reset_rate_limit_settings()
        with mock.patch.dict(os.environ, {"BALDUR_RATE_LIMIT_REDIS_TTL": "7200"}):
            settings = RateLimitSettings()
            assert settings.redis_ttl == 7200  # 2시간

    def test_redis_ttl_validation_bounds(self):
        """Redis TTL 범위 검증 (60 <= x <= 86400)."""
        from pydantic import ValidationError

        from baldur.settings.rate_limit import RateLimitSettings

        # 너무 작은 값
        with pytest.raises(ValidationError):
            RateLimitSettings(redis_ttl=30)

        # 너무 큰 값
        with pytest.raises(ValidationError):
            RateLimitSettings(redis_ttl=90000)


# =============================================================================
# 2. AirGapSettings
# =============================================================================


class TestAirGapSettings:
    """AirGapSettings 테스트."""

    def test_default_values(self):
        """기본값 테스트."""
        from baldur.settings.airgap import AirGapSettings, reset_airgap_settings

        reset_airgap_settings()
        settings = AirGapSettings()
        assert settings.redis_ttl == 3600
        assert settings.key_prefix == "sh:airgap:"

    def test_redis_ttl_from_env(self):
        """환경 변수에서 Redis TTL 로드."""
        from baldur.settings.airgap import AirGapSettings, reset_airgap_settings

        reset_airgap_settings()
        with mock.patch.dict(os.environ, {"BALDUR_AIRGAP_REDIS_TTL": "1800"}):
            settings = AirGapSettings()
            assert settings.redis_ttl == 1800

    def test_key_prefix_auto_colon(self):
        """키 접두사 자동 콜론 추가."""
        from baldur.settings.airgap import AirGapSettings, reset_airgap_settings

        reset_airgap_settings()
        settings = AirGapSettings(key_prefix="test")
        assert settings.key_prefix == "test:"


# =============================================================================
# 3. AuditSettings.buffer_redis_ttl
# =============================================================================


class TestAuditSettingsBufferRedisTtl:
    """AuditSettings.buffer_redis_ttl 필드 테스트."""

    def test_default_buffer_redis_ttl(self):
        """기본 Buffer Redis TTL 값 (86400초 = 24시간)."""
        from baldur.settings.audit import (
            AuditSettings,
            reset_audit_settings,
        )

        reset_audit_settings()
        settings = AuditSettings()
        assert settings.buffer_redis_ttl == 86400

    def test_buffer_redis_ttl_from_env(self):
        """환경 변수에서 Buffer Redis TTL 로드."""
        from baldur.settings.audit import (
            AuditSettings,
            reset_audit_settings,
        )

        reset_audit_settings()
        with mock.patch.dict(os.environ, {"BALDUR_AUDIT_BUFFER_REDIS_TTL": "172800"}):
            settings = AuditSettings()
            assert settings.buffer_redis_ttl == 172800  # 2일


# =============================================================================
# 4. ErrorBudgetSettings.multiplier_cache_ttl, multiplier_max
# =============================================================================


class TestErrorBudgetSettingsMultiplier:
    """ErrorBudgetSettings 위기 가중치 설정 테스트."""

    def test_default_multiplier_settings(self):
        """기본 위기 가중치 설정값."""
        from baldur.settings.error_budget import (
            ErrorBudgetSettings,
            reset_error_budget_settings,
        )

        reset_error_budget_settings()
        settings = ErrorBudgetSettings()
        assert settings.multiplier_cache_ttl == 30.0
        assert settings.multiplier_max == 10.0

    def test_multiplier_settings_from_env(self):
        """환경 변수에서 위기 가중치 설정 로드."""
        from baldur.settings.error_budget import (
            ErrorBudgetSettings,
            reset_error_budget_settings,
        )

        reset_error_budget_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_ERROR_BUDGET_MULTIPLIER_CACHE_TTL": "60.0",
                "BALDUR_ERROR_BUDGET_MULTIPLIER_MAX": "20.0",
            },
        ):
            settings = ErrorBudgetSettings()
            assert settings.multiplier_cache_ttl == 60.0
            assert settings.multiplier_max == 20.0


# =============================================================================
# 5. DashboardSettings (기존, 연동 확인)
# =============================================================================


class TestDashboardSettings:
    """DashboardSettings 테스트."""

    def test_default_dashboard_settings(self):
        """기본 대시보드 설정값."""
        from baldur.settings.dashboard import (
            DashboardSettings,
            reset_dashboard_settings,
        )

        reset_dashboard_settings()
        settings = DashboardSettings()
        assert settings.stale_threshold_minutes == 30
        assert settings.max_regional_status == 5

    def test_dashboard_settings_from_env(self):
        """환경 변수에서 대시보드 설정 로드."""
        from baldur.settings.dashboard import (
            DashboardSettings,
            reset_dashboard_settings,
        )

        reset_dashboard_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_DASHBOARD_STALE_THRESHOLD_MINUTES": "60",
                "BALDUR_DASHBOARD_MAX_REGIONAL_STATUS": "10",
            },
        ):
            settings = DashboardSettings()
            assert settings.stale_threshold_minutes == 60
            assert settings.max_regional_status == 10


# =============================================================================
# 6. MetricsSettings.snapshot_max_age
# =============================================================================


class TestMetricsSettingsSnapshotMaxAge:
    """MetricsSettings.snapshot_max_age 필드 테스트."""

    def test_default_snapshot_max_age(self):
        """기본 스냅샷 최대 유효 기간 (3600초)."""
        from baldur.settings.metrics import (
            MetricsSettings,
            reset_metrics_settings,
        )

        reset_metrics_settings()
        settings = MetricsSettings()
        assert settings.snapshot_max_age == 3600

    def test_snapshot_max_age_from_env(self):
        """환경 변수에서 스냅샷 최대 유효 기간 로드."""
        from baldur.settings.metrics import (
            MetricsSettings,
            reset_metrics_settings,
        )

        reset_metrics_settings()
        with mock.patch.dict(os.environ, {"BALDUR_METRICS_SNAPSHOT_MAX_AGE": "7200"}):
            settings = MetricsSettings()
            assert settings.snapshot_max_age == 7200


# =============================================================================
# 7. SafeGaugeSettings
# =============================================================================


class TestSafeGaugeSettings:
    """SafeGaugeSettings 테스트."""

    def test_default_max_label_combinations(self):
        """기본 최대 레이블 조합 수 (1000)."""
        from baldur.settings.safe_gauge import (
            SafeGaugeSettings,
            reset_safe_gauge_settings,
        )

        reset_safe_gauge_settings()
        settings = SafeGaugeSettings()
        assert settings.max_label_combinations == 1000

    def test_max_label_combinations_from_env(self):
        """환경 변수에서 최대 레이블 조합 수 로드."""
        from baldur.settings.safe_gauge import (
            SafeGaugeSettings,
            reset_safe_gauge_settings,
        )

        reset_safe_gauge_settings()
        with mock.patch.dict(
            os.environ, {"BALDUR_SAFE_GAUGE_MAX_LABEL_COMBINATIONS": "500"}
        ):
            settings = SafeGaugeSettings()
            assert settings.max_label_combinations == 500

    def test_eviction_warning_threshold(self):
        """Eviction 경고 임계치 기본값."""
        from baldur.settings.safe_gauge import (
            SafeGaugeSettings,
            reset_safe_gauge_settings,
        )

        reset_safe_gauge_settings()
        settings = SafeGaugeSettings()
        assert settings.eviction_warning_threshold == 0.8


# =============================================================================
# 8. AuditIntegritySettings (기존, 연동 확인)
# =============================================================================


class TestAuditIntegritySettings:
    """AuditIntegritySettings 테스트."""

    def test_default_cold_storage_settings(self):
        """기본 Cold Storage 설정값."""
        from baldur.settings.audit_integrity import (
            AuditIntegritySettings,
            reset_audit_integrity_settings,
        )

        reset_audit_integrity_settings()
        settings = AuditIntegritySettings()
        assert settings.archive_threshold_days == 7
        assert settings.cold_retention_years == 7

    def test_cold_storage_settings_from_env(self):
        """환경 변수에서 Cold Storage 설정 로드."""
        from baldur.settings.audit_integrity import (
            AuditIntegritySettings,
            reset_audit_integrity_settings,
        )

        reset_audit_integrity_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_AUDIT_INTEGRITY_ARCHIVE_THRESHOLD_DAYS": "14",
                "BALDUR_AUDIT_INTEGRITY_COLD_RETENTION_YEARS": "10",
            },
        ):
            settings = AuditIntegritySettings()
            assert settings.archive_threshold_days == 14
            assert settings.cold_retention_years == 10


# =============================================================================
# 9. 헬퍼 함수 테스트 (클래스 연동)
# =============================================================================


# =============================================================================
# 10. 하위 호환성 테스트
# =============================================================================


# =============================================================================
# 11. Mock Redis로 TTL 적용 검증 테스트
# =============================================================================


class TestRedisTtlAppliedToMock:
    """
    Settings에서 가져온 TTL 값이 실제 Redis 호출에 적용되는지 검증.

    Mock Redis를 사용하여 setex, expire 등의 호출 시 TTL 값이 올바르게 전달되는지 확인합니다.
    """

    def test_rate_limit_storage_uses_settings_ttl(self):
        """RedisRateLimitStorage가 Settings TTL로 Redis 호출하는지 확인."""
        from unittest.mock import MagicMock

        from baldur.adapters.rate_limit.redis_adapter import RedisRateLimitStorage
        from baldur.settings.rate_limit import reset_rate_limit_settings

        reset_rate_limit_settings()

        # Mock Redis
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe

        # 어댑터 생성 (Settings에서 TTL 가져옴)
        storage = RedisRateLimitStorage(redis_client=mock_redis)

        # TTL이 Settings에서 가져온 값인지 확인
        assert storage._ttl == 3600  # 기본값

    def test_rate_limit_storage_custom_ttl_override(self):
        """생성자에서 TTL 오버라이드 가능."""
        from unittest.mock import MagicMock

        from baldur.adapters.rate_limit.redis_adapter import RedisRateLimitStorage

        mock_redis = MagicMock()
        storage = RedisRateLimitStorage(redis_client=mock_redis, ttl=7200)

        assert storage._ttl == 7200

    def test_rate_limit_storage_ttl_from_env(self):
        """환경 변수로 설정된 TTL이 적용되는지 확인."""
        from unittest.mock import MagicMock

        from baldur.adapters.rate_limit.redis_adapter import RedisRateLimitStorage
        from baldur.settings.rate_limit import reset_rate_limit_settings

        reset_rate_limit_settings()

        with mock.patch.dict(os.environ, {"BALDUR_RATE_LIMIT_REDIS_TTL": "1800"}):
            reset_rate_limit_settings()
            mock_redis = MagicMock()
            storage = RedisRateLimitStorage(redis_client=mock_redis)

            assert storage._ttl == 1800

        reset_rate_limit_settings()

    def test_airgap_adapter_uses_settings_ttl(self):
        """RedisAirGapAdapter가 Settings TTL로 Redis 호출하는지 확인."""
        from unittest.mock import MagicMock

        from baldur.adapters.airgap.redis_adapter import RedisAirGapAdapter
        from baldur.settings.airgap import reset_airgap_settings

        reset_airgap_settings()

        mock_redis = MagicMock()
        adapter = RedisAirGapAdapter(redis_client=mock_redis)

        # Settings에서 가져온 TTL 확인
        assert adapter.default_ttl == 3600

        # write_summary 호출 시 TTL 적용 확인
        adapter.write_summary("test_key", "test_value")
        mock_redis.setex.assert_called_once()
        call_args = mock_redis.setex.call_args
        assert call_args[0][1] == 3600  # TTL 인자

    def test_airgap_adapter_ttl_from_env(self):
        """환경 변수로 설정된 AirGap TTL이 적용되는지 확인."""
        from unittest.mock import MagicMock

        from baldur.adapters.airgap.redis_adapter import RedisAirGapAdapter
        from baldur.settings.airgap import reset_airgap_settings

        reset_airgap_settings()

        with mock.patch.dict(os.environ, {"BALDUR_AIRGAP_REDIS_TTL": "1800"}):
            reset_airgap_settings()
            mock_redis = MagicMock()
            adapter = RedisAirGapAdapter(redis_client=mock_redis)

            assert adapter.default_ttl == 1800

            adapter.write_summary("test_key", "test_value")
            call_args = mock_redis.setex.call_args
            assert call_args[0][1] == 1800

        reset_airgap_settings()

    def test_audit_buffer_uses_settings_ttl(self):
        """RedisAuditBuffer가 Settings TTL로 Redis expire 호출하는지 확인."""
        from unittest.mock import MagicMock

        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer
        from baldur.settings.audit import reset_audit_settings

        reset_audit_settings()

        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe

        buffer = RedisAuditBuffer(redis_client=mock_redis)

        # Settings에서 가져온 TTL 확인
        assert buffer._ttl_seconds == 86400

        # log 호출 시 expire에 TTL 전달 확인
        buffer.log({"event": "test"}, domain="test")
        mock_pipe.expire.assert_called_once()
        call_args = mock_pipe.expire.call_args
        assert call_args[0][1] == 86400  # TTL 인자

    def test_audit_buffer_ttl_from_env(self):
        """환경 변수로 설정된 Audit Buffer TTL이 적용되는지 확인."""
        from unittest.mock import MagicMock

        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer
        from baldur.settings.audit import reset_audit_settings

        reset_audit_settings()

        with mock.patch.dict(os.environ, {"BALDUR_AUDIT_BUFFER_REDIS_TTL": "172800"}):
            reset_audit_settings()
            mock_redis = MagicMock()
            mock_pipe = MagicMock()
            mock_redis.pipeline.return_value = mock_pipe

            buffer = RedisAuditBuffer(redis_client=mock_redis)

            assert buffer._ttl_seconds == 172800

            buffer.log({"event": "test"}, domain="test")
            call_args = mock_pipe.expire.call_args
            assert call_args[0][1] == 172800

        reset_audit_settings()
