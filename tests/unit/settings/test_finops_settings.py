"""
Unit tests for FinOpsSettings.

검증 항목:
- 설계 계약값 (기본값, 필드 수)
- 환경 변수 오버라이드
- 경계값 분석 (ge=0, le=1.0, ge=1.0)
- 모델 검증기 (tier ordering)
- 싱글톤 캐싱/리셋
- build_operation_costs() 동작

테스트 대상: baldur.settings.finops
"""

import os
from decimal import Decimal
from unittest import mock

import pytest
from pydantic import ValidationError


class TestFinOpsSettingsContract:
    """FinOpsSettings 설계 계약값 검증.

    337_SETTINGS_GAP_FINOPS_COMPLIANCE.md에 명시된 기본값을 검증한다.
    """

    def test_field_count(self):
        """FinOpsSettings는 19개 필드로 구성된다."""
        from baldur.settings.finops import FinOpsSettings

        assert len(FinOpsSettings.model_fields) == 19

    def test_enabled_default_is_false(self):
        """FinOps 서비스는 기본 비활성 (v1.1 deferred per impl 527)."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = FinOpsSettings()
            assert settings.enabled is False

    def test_cost_retry_default(self):
        """Retry 작업 비용 기본값: 0.001 USD."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = FinOpsSettings()
            assert settings.cost_retry == Decimal("0.001")

    def test_cost_circuit_breaker_check_default(self):
        """Circuit Breaker 체크 비용 기본값: 0.0001 USD."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = FinOpsSettings()
            assert settings.cost_circuit_breaker_check == Decimal("0.0001")

    def test_cost_dlq_enqueue_default(self):
        """DLQ Enqueue 비용 기본값: 0.005 USD."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = FinOpsSettings()
            assert settings.cost_dlq_enqueue == Decimal("0.005")

    def test_cost_dlq_replay_default(self):
        """DLQ Replay 비용 기본값: 0.01 USD."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = FinOpsSettings()
            assert settings.cost_dlq_replay == Decimal("0.01")

    def test_cost_health_check_default(self):
        """Health Check 비용 기본값: 0.0001 USD."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = FinOpsSettings()
            assert settings.cost_health_check == Decimal("0.0001")

    def test_cost_rollback_default(self):
        """Rollback 비용 기본값: 0.05 USD."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = FinOpsSettings()
            assert settings.cost_rollback == Decimal("0.05")

    def test_cost_emergency_mode_default(self):
        """Emergency Mode 비용 기본값: 0.10 USD."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = FinOpsSettings()
            assert settings.cost_emergency_mode == Decimal("0.10")

    def test_cost_chaos_test_default(self):
        """Chaos Test 비용 기본값: 0.02 USD."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = FinOpsSettings()
            assert settings.cost_chaos_test == Decimal("0.02")

    def test_cost_fallback_default(self):
        """Fallback 비용 기본값: 0.001 USD."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = FinOpsSettings()
            assert settings.cost_fallback == Decimal("0.001")

    def test_default_max_budget_default(self):
        """기본 최대 예산: 10.00 USD."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = FinOpsSettings()
            assert settings.default_max_budget == Decimal("10.00")

    def test_default_alert_threshold_default(self):
        """기본 알림 임계값: 0.8 (80%)."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = FinOpsSettings()
            assert settings.default_alert_threshold == 0.8

    def test_default_hard_limit_default(self):
        """기본 예산 초과 차단: True."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = FinOpsSettings()
            assert settings.default_hard_limit is True

    def test_default_reset_period_default(self):
        """기본 예산 리셋 주기: daily."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = FinOpsSettings()
            assert settings.default_reset_period == "daily"

    def test_tier_low_threshold_default(self):
        """LOW tier 임계값: 0.001."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = FinOpsSettings()
            assert settings.tier_low_threshold == Decimal("0.001")

    def test_tier_medium_threshold_default(self):
        """MEDIUM tier 임계값: 0.01."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = FinOpsSettings()
            assert settings.tier_medium_threshold == Decimal("0.01")

    def test_tier_high_threshold_default(self):
        """HIGH tier 임계값: 0.10."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = FinOpsSettings()
            assert settings.tier_high_threshold == Decimal("0.10")

    def test_max_chaos_weight_multiplier_default(self):
        """Chaos 가중치 최대 배수: 10.0."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = FinOpsSettings()
            assert settings.max_chaos_weight_multiplier == 10.0

    def test_alert_dedup_count_default(self):
        """알림 중복 억제 최근 개수: 10."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = FinOpsSettings()
            assert settings.alert_dedup_count == 10


class TestFinOpsSettingsBehavior:
    """FinOpsSettings 동작 검증."""

    # === Environment Variable Override ===

    def test_env_override_enabled(self):
        """환경 변수로 enabled를 비활성화할 수 있다."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_ENABLED": "false"},
            clear=True,
        ):
            settings = FinOpsSettings()
            assert settings.enabled is False

    def test_env_override_cost_retry(self):
        """환경 변수로 retry 비용을 변경할 수 있다."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_COST_RETRY": "0.005"},
            clear=True,
        ):
            settings = FinOpsSettings()
            assert settings.cost_retry == Decimal("0.005")

    def test_env_override_default_max_budget(self):
        """환경 변수로 기본 최대 예산을 변경할 수 있다."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_DEFAULT_MAX_BUDGET": "50.00"},
            clear=True,
        ):
            settings = FinOpsSettings()
            assert settings.default_max_budget == Decimal("50.00")

    def test_env_override_alert_threshold(self):
        """환경 변수로 알림 임계값을 변경할 수 있다."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_DEFAULT_ALERT_THRESHOLD": "0.9"},
            clear=True,
        ):
            settings = FinOpsSettings()
            assert settings.default_alert_threshold == 0.9

    def test_env_override_tier_thresholds(self):
        """환경 변수로 tier 임계값들을 변경할 수 있다."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_FINOPS_TIER_LOW_THRESHOLD": "0.01",
                "BALDUR_FINOPS_TIER_MEDIUM_THRESHOLD": "0.1",
                "BALDUR_FINOPS_TIER_HIGH_THRESHOLD": "1.0",
            },
            clear=True,
        ):
            settings = FinOpsSettings()
            assert settings.tier_low_threshold == Decimal("0.01")
            assert settings.tier_medium_threshold == Decimal("0.1")
            assert settings.tier_high_threshold == Decimal("1.0")

    def test_env_override_alert_dedup_count(self):
        """환경 변수로 알림 중복 억제 개수를 변경할 수 있다."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_ALERT_DEDUP_COUNT": "20"},
            clear=True,
        ):
            settings = FinOpsSettings()
            assert settings.alert_dedup_count == 20

    # === Boundary Analysis: default_reset_period Literal constraint ===

    def test_env_override_reset_period_weekly(self):
        """환경 변수로 reset_period를 weekly로 변경할 수 있다."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_DEFAULT_RESET_PERIOD": "weekly"},
            clear=True,
        ):
            settings = FinOpsSettings()
            assert settings.default_reset_period == "weekly"

    def test_env_override_reset_period_monthly(self):
        """환경 변수로 reset_period를 monthly로 변경할 수 있다."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_DEFAULT_RESET_PERIOD": "monthly"},
            clear=True,
        ):
            settings = FinOpsSettings()
            assert settings.default_reset_period == "monthly"

    def test_reset_period_invalid_value_rejected(self):
        """유효하지 않은 reset_period 값이면 ValidationError 발생."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_DEFAULT_RESET_PERIOD": "hourly"},
            clear=True,
        ):
            with pytest.raises(ValidationError):
                FinOpsSettings()

    def test_reset_period_empty_string_rejected(self):
        """빈 문자열 reset_period이면 ValidationError 발생."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_DEFAULT_RESET_PERIOD": ""},
            clear=True,
        ):
            with pytest.raises(ValidationError):
                FinOpsSettings()

    # === Boundary Analysis: cost fields ge=0 ===

    def test_cost_retry_negative_rejected(self):
        """cost_retry에 음수를 설정하면 ValidationError 발생."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_COST_RETRY": "-0.001"},
            clear=True,
        ):
            with pytest.raises(ValidationError):
                FinOpsSettings()

    def test_cost_zero_accepted(self):
        """cost 필드에 0을 설정할 수 있다 (ge=0 경계)."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_COST_RETRY": "0"},
            clear=True,
        ):
            settings = FinOpsSettings()
            assert settings.cost_retry == Decimal("0")

    def test_cost_rollback_negative_rejected(self):
        """cost_rollback에 음수를 설정하면 ValidationError 발생."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_COST_ROLLBACK": "-1"},
            clear=True,
        ):
            with pytest.raises(ValidationError):
                FinOpsSettings()

    def test_default_max_budget_negative_rejected(self):
        """default_max_budget에 음수를 설정하면 ValidationError 발생."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_DEFAULT_MAX_BUDGET": "-10"},
            clear=True,
        ):
            with pytest.raises(ValidationError):
                FinOpsSettings()

    # === Boundary Analysis: alert_threshold 0.0~1.0 ===

    def test_alert_threshold_zero_accepted(self):
        """alert_threshold에 0.0을 설정할 수 있다 (ge=0.0 경계)."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_DEFAULT_ALERT_THRESHOLD": "0.0"},
            clear=True,
        ):
            settings = FinOpsSettings()
            assert settings.default_alert_threshold == 0.0

    def test_alert_threshold_one_accepted(self):
        """alert_threshold에 1.0을 설정할 수 있다 (le=1.0 경계)."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_DEFAULT_ALERT_THRESHOLD": "1.0"},
            clear=True,
        ):
            settings = FinOpsSettings()
            assert settings.default_alert_threshold == 1.0

    def test_alert_threshold_below_zero_rejected(self):
        """alert_threshold에 음수를 설정하면 ValidationError 발생."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_DEFAULT_ALERT_THRESHOLD": "-0.1"},
            clear=True,
        ):
            with pytest.raises(ValidationError):
                FinOpsSettings()

    def test_alert_threshold_above_one_rejected(self):
        """alert_threshold에 1.0 초과를 설정하면 ValidationError 발생."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_DEFAULT_ALERT_THRESHOLD": "1.1"},
            clear=True,
        ):
            with pytest.raises(ValidationError):
                FinOpsSettings()

    # === Boundary Analysis: max_chaos_weight_multiplier ge=1.0 ===

    def test_chaos_multiplier_one_accepted(self):
        """max_chaos_weight_multiplier에 1.0을 설정할 수 있다 (ge=1.0 경계)."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_MAX_CHAOS_WEIGHT_MULTIPLIER": "1.0"},
            clear=True,
        ):
            settings = FinOpsSettings()
            assert settings.max_chaos_weight_multiplier == 1.0

    def test_chaos_multiplier_below_one_rejected(self):
        """max_chaos_weight_multiplier에 1.0 미만을 설정하면 ValidationError 발생."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_MAX_CHAOS_WEIGHT_MULTIPLIER": "0.5"},
            clear=True,
        ):
            with pytest.raises(ValidationError):
                FinOpsSettings()

    # === Boundary Analysis: alert_dedup_count ge=1 ===

    def test_alert_dedup_count_one_accepted(self):
        """alert_dedup_count에 1을 설정할 수 있다 (ge=1 경계)."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_ALERT_DEDUP_COUNT": "1"},
            clear=True,
        ):
            settings = FinOpsSettings()
            assert settings.alert_dedup_count == 1

    def test_alert_dedup_count_zero_rejected(self):
        """alert_dedup_count에 0을 설정하면 ValidationError 발생."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_ALERT_DEDUP_COUNT": "0"},
            clear=True,
        ):
            with pytest.raises(ValidationError):
                FinOpsSettings()

    # === Model Validator: tier ordering ===

    def test_tier_ordering_reversed_rejected(self):
        """Tier 임계값이 역순이면 ValueError 발생 (low > medium)."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_FINOPS_TIER_LOW_THRESHOLD": "0.5",
                "BALDUR_FINOPS_TIER_MEDIUM_THRESHOLD": "0.1",
                "BALDUR_FINOPS_TIER_HIGH_THRESHOLD": "0.01",
            },
            clear=True,
        ):
            with pytest.raises(
                ValidationError, match="Tier thresholds must be ordered"
            ):
                FinOpsSettings()

    def test_tier_ordering_equal_rejected(self):
        """Tier 임계값이 동일하면 ValueError 발생 (strict ordering 위반)."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_FINOPS_TIER_LOW_THRESHOLD": "0.01",
                "BALDUR_FINOPS_TIER_MEDIUM_THRESHOLD": "0.01",
                "BALDUR_FINOPS_TIER_HIGH_THRESHOLD": "0.10",
            },
            clear=True,
        ):
            with pytest.raises(
                ValidationError, match="Tier thresholds must be ordered"
            ):
                FinOpsSettings()

    def test_tier_ordering_medium_equals_high_rejected(self):
        """medium == high이면 ValueError 발생."""
        from baldur.settings.finops import FinOpsSettings, reset_finops_settings

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_FINOPS_TIER_LOW_THRESHOLD": "0.001",
                "BALDUR_FINOPS_TIER_MEDIUM_THRESHOLD": "0.10",
                "BALDUR_FINOPS_TIER_HIGH_THRESHOLD": "0.10",
            },
            clear=True,
        ):
            with pytest.raises(
                ValidationError, match="Tier thresholds must be ordered"
            ):
                FinOpsSettings()

    # === build_operation_costs() ===

    def test_build_operation_costs_returns_correct_keys(self):
        """build_operation_costs()는 8개 operation key를 반환한다."""
        from baldur.settings.finops import (
            FinOpsSettings,
            build_operation_costs,
            reset_finops_settings,
        )

        reset_finops_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = FinOpsSettings()
            costs = build_operation_costs(settings)

        expected_keys = {
            "retry",
            "circuit_breaker_check",
            "dlq_enqueue",
            "dlq_replay",
            "health_check",
            "rollback",
            "emergency_mode",
            "chaos_test",
        }
        assert set(costs.keys()) == expected_keys

    def test_build_operation_costs_maps_values_from_settings(self):
        """build_operation_costs()는 settings 필드값을 정확히 매핑한다."""
        from baldur.settings.finops import (
            FinOpsSettings,
            build_operation_costs,
            reset_finops_settings,
        )

        reset_finops_settings()
        with mock.patch.dict(
            os.environ,
            {"BALDUR_FINOPS_COST_RETRY": "0.999"},
            clear=True,
        ):
            settings = FinOpsSettings()
            costs = build_operation_costs(settings)

        assert costs["retry"] == Decimal("0.999")
        assert costs["circuit_breaker_check"] == settings.cost_circuit_breaker_check
        assert costs["dlq_enqueue"] == settings.cost_dlq_enqueue
        assert costs["rollback"] == settings.cost_rollback


class TestFinOpsSettingsSingletonBehavior:
    """FinOpsSettings 싱글톤 캐싱/리셋 동작 검증."""

    def test_get_returns_same_instance(self):
        """get_finops_settings()는 동일 인스턴스를 반환."""
        from baldur.settings.finops import (
            get_finops_settings,
            reset_finops_settings,
        )

        reset_finops_settings()
        first = get_finops_settings()
        second = get_finops_settings()
        assert first is second

    def test_reset_clears_cached_instance(self):
        """reset 후 get_finops_settings()는 새 인스턴스를 반환."""
        from baldur.settings.finops import (
            get_finops_settings,
            reset_finops_settings,
        )

        reset_finops_settings()
        first = get_finops_settings()
        reset_finops_settings()
        second = get_finops_settings()
        assert first is not second

    def test_settings_importable_from_package(self):
        """settings 패키지에서 FinOpsSettings를 import할 수 있다."""
        from baldur.settings import (
            FinOpsSettings,
            get_finops_settings,
            reset_finops_settings,
        )

        assert FinOpsSettings is not None
        assert callable(get_finops_settings)
        assert callable(reset_finops_settings)
