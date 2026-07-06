"""
Tests for core/safe_defaults.py - Safe Default Values and Validation.
core/safe_defaults.py의 안전한 기본값 관리, 유효성 검증, Fatal 설정 분류 등에 대한 단위 테스트.

커버리지 대상:
- SAFE_DEFAULTS 딕셔너리 접근 함수 (get_safe_default, get_safe_defaults_for_type)
- VALIDATION_RULES 기반 유효성 검증 (is_valid_value, get_validation_errors)
- validate_with_safe_fallback, validate_all_with_safe_fallback
- apply_safe_defaults_to_missing
- Fatal 설정 분류 (is_fatal_config, get_all_fatal_configs)
- FatalConfigError, ConfigValidationResult
- validate_startup_config, validate_config_preflight
- validate_chaos_config (Chaos 특별 검증)
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from baldur.core.safe_defaults import (
    FATAL_CONFIGS,
    SAFE_DEFAULTS,
    VALIDATION_RULES,
    ConfigValidationResult,
    FatalConfigError,
    _finalize_validation,
    _handle_fatal_violation,
    _handle_non_fatal_violation,
    apply_safe_defaults_to_missing,
    get_all_fatal_configs,
    get_safe_default,
    get_safe_defaults_for_type,
    get_validation_errors,
    is_fatal_config,
    is_valid_value,
    validate_all_with_safe_fallback,
    validate_chaos_config,
    validate_config_preflight,
    validate_startup_config,
    validate_with_safe_fallback,
)

# =============================================================================
# get_safe_default / get_safe_defaults_for_type Tests
# =============================================================================


class TestGetSafeDefault:
    """get_safe_default 함수 테스트."""

    def test_get_existing_config_key(self):
        """Get existing config key
        존재하는 config_type과 key에 대해 올바른 기본값을 반환하는지 확인.
        """
        result = get_safe_default("circuit_breaker", "failure_threshold")
        assert result == 5

    def test_get_nonexistent_key(self):
        """Get nonexistent key
        존재하지 않는 key에 대해 None을 반환하는지 확인.
        """
        result = get_safe_default("circuit_breaker", "nonexistent_key")
        assert result is None

    def test_get_nonexistent_config_type(self):
        """Get nonexistent config type
        존재하지 않는 config_type에 대해 None을 반환하는지 확인.
        """
        result = get_safe_default("nonexistent_type", "any_key")
        assert result is None

    @pytest.mark.parametrize(
        ("config_type", "key", "expected"),
        [
            ("dlq", "max_replay_attempts", 2),
            ("retry", "backoff_strategy", "exponential"),
            ("rate_limit", "base_delay", 1.0),
            ("security", "injection_ban_hours", 24),
            ("chaos", "enabled", False),
            ("slo", "default_target", 0.999),
            ("l2_storage", "enabled", False),
        ],
    )
    def test_various_config_types(self, config_type, key, expected):
        """Various config types
        다양한 설정 유형에서 올바른 기본값을 반환하는지 확인.
        """
        result = get_safe_default(config_type, key)
        assert result == expected


class TestGetSafeDefaultsForType:
    """get_safe_defaults_for_type 함수 테스트."""

    def test_get_circuit_breaker_defaults(self):
        """Get circuit breaker defaults
        circuit_breaker 유형의 모든 기본값을 올바르게 반환하는지 확인.
        """
        result = get_safe_defaults_for_type("circuit_breaker")
        assert isinstance(result, dict)
        assert "failure_threshold" in result
        assert "recovery_timeout" in result
        assert result["enabled"] is True

    def test_returns_copy(self):
        """Returns copy (not reference)
        반환된 딕셔너리가 원본의 복사본인지 확인 (원본 불변성 보장).
        """
        result = get_safe_defaults_for_type("circuit_breaker")
        result["failure_threshold"] = 999
        # 원본이 변경되지 않아야 함
        assert SAFE_DEFAULTS["circuit_breaker"]["failure_threshold"] == 5

    def test_nonexistent_type_returns_empty(self):
        """Nonexistent type returns empty dict
        존재하지 않는 유형에 대해 빈 딕셔너리를 반환하는지 확인.
        """
        result = get_safe_defaults_for_type("nonexistent_type")
        assert result == {}


# =============================================================================
# is_valid_value Tests
# =============================================================================


class TestIsValidValue:
    """is_valid_value 함수 테스트."""

    def test_valid_numeric_value(self):
        """Valid numeric value
        유효한 숫자 값에 대해 True를 반환하는지 확인.
        """
        assert is_valid_value("circuit_breaker", "failure_threshold", 5) is True

    def test_value_below_range(self):
        """Value below minimum range
        최소값 미만의 값에 대해 False를 반환하는지 확인.
        """
        assert is_valid_value("circuit_breaker", "failure_threshold", 0) is False

    def test_value_above_range(self):
        """Value above maximum range
        최대값 초과의 값에 대해 False를 반환하는지 확인.
        """
        assert is_valid_value("circuit_breaker", "failure_threshold", 101) is False

    def test_none_value(self):
        """None value is invalid
        None 값에 대해 False를 반환하는지 확인.
        """
        assert is_valid_value("circuit_breaker", "failure_threshold", None) is False

    def test_valid_log_level(self):
        """Valid log level
        유효한 로그 레벨에 대해 True를 반환하는지 확인.
        """
        assert is_valid_value("logging", "dlq_log_level", "INFO") is True

    def test_invalid_log_level(self):
        """Invalid log level
        유효하지 않은 로그 레벨에 대해 False를 반환하는지 확인.
        """
        assert is_valid_value("logging", "dlq_log_level", "TRACE") is False

    def test_valid_backoff_strategy(self):
        """Valid backoff strategy
        유효한 backoff 전략에 대해 True를 반환하는지 확인.
        """
        assert is_valid_value("retry", "backoff_strategy", "exponential") is True

    def test_invalid_backoff_strategy(self):
        """Invalid backoff strategy
        유효하지 않은 backoff 전략에 대해 False를 반환하는지 확인.
        """
        assert is_valid_value("retry", "backoff_strategy", "random") is False

    def test_boolean_field_with_bool(self):
        """Boolean field with bool value
        Boolean 필드에 bool 값이면 True를 반환하는지 확인.
        """
        assert is_valid_value("circuit_breaker", "enabled", True) is True
        assert is_valid_value("circuit_breaker", "enabled", False) is True

    def test_boolean_field_with_non_bool(self):
        """Boolean field with non-bool value
        Boolean 필드에 bool이 아닌 값이면 False를 반환하는지 확인.
        """
        assert is_valid_value("circuit_breaker", "enabled", 1) is False
        assert is_valid_value("circuit_breaker", "enabled", "true") is False

    def test_key_without_validation_rule(self):
        """Key without validation rule
        검증 규칙이 없는 키에 대해 기본 True를 반환하는지 확인.
        """
        # "prefix"는 VALIDATION_RULES에 없음
        assert is_valid_value("metrics", "prefix", "baldur") is True

    def test_uncomparable_type(self):
        """Uncomparable type returns False
        비교 불가능한 타입에 대해 False를 반환하는지 확인.
        """
        assert (
            is_valid_value("circuit_breaker", "failure_threshold", "not_a_number")
            is False
        )

    def test_float_range_validation(self):
        """Float range validation
        float 범위 검증이 올바르게 동작하는지 확인.
        """
        assert is_valid_value("rate_limit", "base_delay", 0.1) is True
        assert is_valid_value("rate_limit", "base_delay", 60.0) is True
        assert is_valid_value("rate_limit", "base_delay", 0.05) is False
        assert is_valid_value("rate_limit", "base_delay", 61.0) is False


# =============================================================================
# validate_with_safe_fallback Tests
# =============================================================================


class TestValidateWithSafeFallback:
    """validate_with_safe_fallback 함수 테스트."""

    def test_all_valid_values(self):
        """All valid values unchanged
        모든 값이 유효한 경우 원래 값이 그대로 유지되는지 확인.
        """
        values = {"failure_threshold": 5, "recovery_timeout": 60}
        result = validate_with_safe_fallback("circuit_breaker", values)
        assert result == values

    def test_invalid_value_replaced(self):
        """Invalid value replaced with safe default
        유효하지 않은 값이 Safe Default로 대체되는지 확인.
        """
        values = {"failure_threshold": 0}  # 최소값(1) 미만
        result = validate_with_safe_fallback("circuit_breaker", values)
        assert result["failure_threshold"] == 5  # Safe Default

    def test_no_safe_default_keeps_original(self):
        """No safe default keeps original value
        Safe Default가 없는 키에 대해 원래 값을 유지하는지 확인.
        """
        values = {"custom_key": "invalid_value"}
        result = validate_with_safe_fallback(
            "circuit_breaker", values, log_changes=False
        )
        assert result["custom_key"] == "invalid_value"

    def test_log_changes_false(self):
        """Log changes disabled
        log_changes=False일 때 로깅 없이 동작하는지 확인.
        """
        values = {"failure_threshold": 0}
        result = validate_with_safe_fallback(
            "circuit_breaker", values, log_changes=False
        )
        assert result["failure_threshold"] == 5

    def test_mixed_valid_invalid_values(self):
        """Mixed valid and invalid values
        유효한 값과 유효하지 않은 값이 섞여있을 때 올바르게 처리하는지 확인.
        """
        values = {
            "failure_threshold": 10,  # 유효
            "recovery_timeout": 0,  # 최소값(1) 미만 → 대체
        }
        result = validate_with_safe_fallback("circuit_breaker", values)
        assert result["failure_threshold"] == 10
        assert result["recovery_timeout"] == 60  # Safe Default


class TestValidateAllWithSafeFallback:
    """validate_all_with_safe_fallback 함수 테스트."""

    def test_multiple_config_types(self):
        """Multiple config types validated
        여러 설정 유형이 모두 올바르게 검증되는지 확인.
        """
        config_dict = {
            "circuit_breaker": {"failure_threshold": 0},  # 유효하지 않음
            "dlq": {"max_replay_attempts": 5},  # 유효
        }
        result = validate_all_with_safe_fallback(config_dict, log_changes=False)
        assert result["circuit_breaker"]["failure_threshold"] == 5  # Safe Default
        assert result["dlq"]["max_replay_attempts"] == 5  # 유효 → 유지


# =============================================================================
# apply_safe_defaults_to_missing Tests
# =============================================================================


class TestApplySafeDefaultsToMissing:
    """apply_safe_defaults_to_missing 함수 테스트."""

    def test_missing_keys_filled(self):
        """Missing keys filled with defaults
        누락된 키에 Safe Default가 적용되는지 확인.
        """
        values = {"failure_threshold": 10}
        result = apply_safe_defaults_to_missing("circuit_breaker", values)
        assert result["failure_threshold"] == 10  # 기존 값 유지
        assert "recovery_timeout" in result  # Safe Default로 채워짐

    def test_existing_values_preserved(self):
        """Existing values preserved
        기존 값이 Safe Default로 덮어씌워지지 않는지 확인.
        """
        values = {"failure_threshold": 99}
        result = apply_safe_defaults_to_missing("circuit_breaker", values)
        assert result["failure_threshold"] == 99  # 기존 값 우선

    def test_unknown_config_type(self):
        """Unknown config type
        알 수 없는 설정 유형에서 기존 값만 반환되는지 확인.
        """
        values = {"key": "value"}
        result = apply_safe_defaults_to_missing("unknown_type", values)
        assert result == {"key": "value"}


# =============================================================================
# get_validation_errors Tests
# =============================================================================


class TestGetValidationErrors:
    """get_validation_errors 함수 테스트."""

    def test_no_errors(self):
        """No validation errors
        모든 값이 유효할 때 오류가 없는지 확인.
        """
        values = {"failure_threshold": 5, "recovery_timeout": 60}
        errors = get_validation_errors("circuit_breaker", values)
        assert errors == {}

    def test_value_below_minimum(self):
        """Value below minimum
        최소값 미만일 때 적절한 에러 메시지가 반환되는지 확인.
        """
        values = {"failure_threshold": 0}
        errors = get_validation_errors("circuit_breaker", values)
        assert "failure_threshold" in errors
        assert "below minimum" in errors["failure_threshold"]

    def test_value_above_maximum(self):
        """Value above maximum
        최대값 초과일 때 적절한 에러 메시지가 반환되는지 확인.
        """
        values = {"failure_threshold": 200}
        errors = get_validation_errors("circuit_breaker", values)
        assert "failure_threshold" in errors
        assert "exceeds maximum" in errors["failure_threshold"]

    def test_none_value(self):
        """None value error
        None 값에 대해 'cannot be None' 에러가 반환되는지 확인.
        """
        values = {"failure_threshold": None}
        errors = get_validation_errors("circuit_breaker", values)
        assert "failure_threshold" in errors
        assert "None" in errors["failure_threshold"]

    def test_invalid_type(self):
        """Invalid type error
        비교 불가능한 타입에 대해 에러가 반환되는지 확인.
        """
        values = {"failure_threshold": "string"}
        errors = get_validation_errors("circuit_breaker", values)
        assert "failure_threshold" in errors
        assert "not a valid number" in errors["failure_threshold"]

    def test_invalid_log_level(self):
        """Invalid log level error
        유효하지 않은 로그 레벨에 대해 에러가 반환되는지 확인.
        """
        values = {"dlq_log_level": "TRACE"}
        errors = get_validation_errors("logging", values)
        assert "dlq_log_level" in errors

    def test_invalid_backoff_strategy(self):
        """Invalid backoff strategy error
        유효하지 않은 backoff 전략에 대해 에러가 반환되는지 확인.
        """
        values = {"backoff_strategy": "random_strategy"}
        errors = get_validation_errors("retry", values)
        assert "backoff_strategy" in errors


# =============================================================================
# Fatal Config Tests
# =============================================================================


class TestFatalConfig:
    """Fatal 설정 분류 테스트."""

    def test_security_injection_ban_hours_is_fatal(self):
        """Security injection_ban_hours is fatal
        security.injection_ban_hours가 Fatal 설정인지 확인.
        """
        assert is_fatal_config("security", "injection_ban_hours") is True

    def test_chaos_blast_radius_is_fatal(self):
        """Chaos max_blast_radius is fatal
        chaos.max_blast_radius가 Fatal 설정인지 확인.
        """
        assert is_fatal_config("chaos", "max_blast_radius") is True

    def test_non_fatal_config(self):
        """Non-fatal config
        Fatal이 아닌 설정에 대해 False를 반환하는지 확인.
        """
        assert is_fatal_config("circuit_breaker", "failure_threshold") is False

    def test_unknown_type(self):
        """Unknown config type
        알 수 없는 config_type에 대해 False를 반환하는지 확인.
        """
        assert is_fatal_config("unknown", "any_key") is False

    def test_get_all_fatal_configs(self):
        """Get all fatal configs
        get_all_fatal_configs가 모든 Fatal 설정의 복사본을 반환하는지 확인.
        """
        result = get_all_fatal_configs()
        assert "security" in result
        assert "chaos" in result
        assert "error_budget" in result
        # 반환된 dict가 복사본인지 확인
        result["security"].add("test_key")
        assert "test_key" not in FATAL_CONFIGS["security"]

    def test_error_budget_is_fatal(self):
        """Error budget fatal configs
        error_budget의 Fatal 설정들이 올바르게 분류되는지 확인.
        """
        assert is_fatal_config("error_budget", "threshold_critical") is True
        assert is_fatal_config("error_budget", "burn_rate_fast_critical") is True


# =============================================================================
# FatalConfigError Tests
# =============================================================================


class TestFatalConfigError:
    """FatalConfigError 예외 테스트."""

    def test_error_message(self):
        """Error message format
        FatalConfigError의 메시지 형식이 올바른지 확인.
        """
        violations = {
            "security": {"injection_ban_hours": "Value 0 is below minimum 1"},
        }
        error = FatalConfigError(violations)
        assert "security.injection_ban_hours" in str(error)
        assert "Fatal config violations" in str(error)
        assert error.violations == violations

    def test_multiple_violations(self):
        """Multiple violations in message
        여러 위반 사항이 모두 메시지에 포함되는지 확인.
        """
        violations = {
            "security": {"injection_ban_hours": "too low"},
            "chaos": {"max_blast_radius": "exceeds limit"},
        }
        error = FatalConfigError(violations)
        assert "security.injection_ban_hours" in str(error)
        assert "chaos.max_blast_radius" in str(error)


# =============================================================================
# ConfigValidationResult Tests
# =============================================================================


class TestConfigValidationResult:
    """ConfigValidationResult 클래스 테스트."""

    def test_initial_state(self):
        """Initial state
        초기 상태에서 has_fatal_violations=False, is_valid=True인지 확인.
        """
        result = ConfigValidationResult()
        assert result.has_fatal_violations is False
        assert result.is_valid is True
        assert result.changes_count == 0

    def test_add_fatal_violation(self):
        """Add fatal violation
        Fatal 위반 추가 후 상태가 올바르게 변경되는지 확인.
        """
        result = ConfigValidationResult()
        result.add_fatal_violation("security", "key1", "error msg")
        assert result.has_fatal_violations is True
        assert result.is_valid is False
        assert "security" in result.fatal_violations

    def test_add_non_fatal_warning(self):
        """Add non-fatal warning
        Non-fatal 경고 추가 후 상태가 올바르게 유지되는지 확인.
        """
        result = ConfigValidationResult()
        result.add_non_fatal_warning("circuit_breaker", "key1", "warning msg")
        assert result.has_fatal_violations is False
        assert result.is_valid is True
        assert "circuit_breaker" in result.non_fatal_warnings

    def test_multiple_fatal_violations_same_type(self):
        """Multiple fatal violations same type
        같은 유형에 여러 Fatal 위반을 추가할 수 있는지 확인.
        """
        result = ConfigValidationResult()
        result.add_fatal_violation("security", "key1", "error1")
        result.add_fatal_violation("security", "key2", "error2")
        assert len(result.fatal_violations["security"]) == 2


# =============================================================================
# validate_startup_config Tests
# =============================================================================


class TestValidateStartupConfig:
    """validate_startup_config 함수 테스트."""

    def test_valid_config(self):
        """Valid config no changes
        모든 설정이 유효할 때 변경 수가 0인지 확인.
        """
        config = MagicMock()
        cb_config = MagicMock()
        cb_config.failure_threshold = 5
        cb_config.recovery_timeout = 60
        cb_config.success_threshold = 2
        cb_config.half_open_max_calls = 3
        cb_config.rate_limit_cascade_threshold = 10
        cb_config.rate_limit_cascade_window_seconds = 60
        cb_config.rate_limit_cascade_rate = 10.0
        cb_config.rate_limit_cascade_minimum_calls = 20
        cb_config.self_ddos_protection_enabled = True
        cb_config.self_ddos_rps_limit = 200
        cb_config.self_ddos_window_seconds = 10
        cb_config.self_ddos_backoff_multiplier = 2.0
        cb_config.enabled = True
        config.circuit_breaker = cb_config
        # 나머지 config type은 None으로 처리
        config.dlq = None
        config.retry = None
        config.sla = None
        config.security = None
        config.forensic = None
        config.metrics = None
        config.notification = None
        config.rate_limit = None
        config.idempotency = None
        config.chaos = None
        config.error_budget = None

        changes = validate_startup_config(config, log_changes=False)
        assert changes == 0

    def test_invalid_config_gets_fixed(self):
        """Invalid config gets fixed
        유효하지 않은 설정이 Safe Default로 교체되는지 확인.
        """
        config = MagicMock()
        cb_config = MagicMock()
        cb_config.failure_threshold = 0  # 유효하지 않음
        # 나머지는 유효한 값
        cb_config.recovery_timeout = 60
        cb_config.success_threshold = 2
        cb_config.half_open_max_calls = 3
        cb_config.rate_limit_cascade_threshold = 10
        cb_config.rate_limit_cascade_window_seconds = 60
        cb_config.rate_limit_cascade_rate = 10.0
        cb_config.rate_limit_cascade_minimum_calls = 20
        cb_config.self_ddos_protection_enabled = True
        cb_config.self_ddos_rps_limit = 200
        cb_config.self_ddos_window_seconds = 10
        cb_config.self_ddos_backoff_multiplier = 2.0
        cb_config.enabled = True
        config.circuit_breaker = cb_config
        config.dlq = None
        config.retry = None
        config.sla = None
        config.security = None
        config.forensic = None
        config.metrics = None
        config.notification = None
        config.rate_limit = None
        config.idempotency = None
        config.chaos = None
        config.error_budget = None

        changes = validate_startup_config(config, log_changes=False)
        assert changes >= 1

    def test_fatal_violation_raises(self):
        """Fatal violation raises FatalConfigError
        Fatal 설정 위반 시 raise_on_fatal=True이면 FatalConfigError가 발생하는지 확인.
        """
        config = MagicMock()
        # security 설정에 유효하지 않은 Fatal 값
        security_config = MagicMock()
        security_config.injection_ban_hours = 0  # 최소값(1) 미만, Fatal
        security_config.temporary_ban_hours = 1
        security_config.permanent_ban_threshold = 5
        security_config.suspicious_ip_cache_timeout = 86400
        config.security = security_config
        config.circuit_breaker = None
        config.dlq = None
        config.retry = None
        config.sla = None
        config.forensic = None
        config.metrics = None
        config.notification = None
        config.rate_limit = None
        config.idempotency = None
        config.chaos = None
        config.error_budget = None

        with pytest.raises(FatalConfigError):
            validate_startup_config(config, log_changes=False, raise_on_fatal=True)

    def test_none_sub_config_skipped(self):
        """None sub-config skipped
        sub_config가 None이면 건너뛰는지 확인.
        """
        config = MagicMock()
        for attr in [
            "circuit_breaker",
            "dlq",
            "retry",
            "sla",
            "security",
            "forensic",
            "metrics",
            "notification",
            "rate_limit",
            "idempotency",
            "chaos",
            "error_budget",
        ]:
            setattr(config, attr, None)

        changes = validate_startup_config(config, log_changes=False)
        assert changes == 0


# =============================================================================
# validate_config_preflight Tests
# =============================================================================


class TestValidateConfigPreflight:
    """validate_config_preflight 함수 테스트."""

    def test_preflight_with_valid_config(self):
        """Preflight with valid config
        유효한 설정에 대해 위반이 없는 결과를 반환하는지 확인.
        """
        config = MagicMock()
        cb = MagicMock()
        cb.failure_threshold = 5
        cb.recovery_timeout = 60
        cb.success_threshold = 2
        cb.half_open_max_calls = 3
        cb.rate_limit_cascade_threshold = 10
        cb.rate_limit_cascade_window_seconds = 60
        cb.rate_limit_cascade_rate = 10.0
        cb.rate_limit_cascade_minimum_calls = 20
        cb.self_ddos_protection_enabled = True
        cb.self_ddos_rps_limit = 200
        cb.self_ddos_window_seconds = 10
        cb.self_ddos_backoff_multiplier = 2.0
        cb.enabled = True
        config.circuit_breaker = cb
        config.dlq = None
        config.retry = None
        config.sla = None
        config.security = None
        config.forensic = None
        config.metrics = None
        config.notification = None
        config.rate_limit = None
        config.idempotency = None
        config.chaos = None
        config.error_budget = None

        result = validate_config_preflight(config)
        assert result.is_valid is True

    def test_preflight_detects_fatal(self):
        """Preflight detects fatal violations
        Preflight에서 Fatal 위반을 감지하는지 확인 (설정은 수정하지 않음).
        """
        config = MagicMock()
        security = MagicMock()
        security.injection_ban_hours = 0  # Fatal!
        security.temporary_ban_hours = 1
        security.permanent_ban_threshold = 5
        security.suspicious_ip_cache_timeout = 86400
        config.security = security
        config.circuit_breaker = None
        config.dlq = None
        config.retry = None
        config.sla = None
        config.forensic = None
        config.metrics = None
        config.notification = None
        config.rate_limit = None
        config.idempotency = None
        config.chaos = None
        config.error_budget = None

        result = validate_config_preflight(config)
        assert result.has_fatal_violations is True
        assert "security" in result.fatal_violations


# =============================================================================
# validate_chaos_config Tests
# =============================================================================


class TestValidateChaosConfig:
    """validate_chaos_config 함수 테스트."""

    def test_valid_chaos_config(self):
        """Valid chaos config
        유효한 Chaos 설정이 변경 없이 반환되는지 확인.
        """
        values = {"max_blast_radius": 0.05, "failure_rate": 0.01, "dry_run": True}
        result = validate_chaos_config(values)
        assert result == values

    def test_blast_radius_clamped_to_50_percent(self):
        """Blast radius clamped to 50%
        max_blast_radius가 50%를 초과할 때 0.5로 제한되는지 확인.
        """
        values = {"max_blast_radius": 0.8}
        result = validate_chaos_config(values)
        assert result["max_blast_radius"] == 0.5

    def test_negative_blast_radius_clamped_to_zero(self):
        """Negative blast radius clamped to 0
        음수 blast radius가 0으로 제한되는지 확인.
        """
        values = {"max_blast_radius": -0.1}
        result = validate_chaos_config(values)
        assert result["max_blast_radius"] == 0.0

    def test_failure_rate_clamped_to_50_percent(self):
        """Failure rate clamped to 50%
        failure_rate가 50%를 초과할 때 0.5로 제한되는지 확인.
        """
        values = {"failure_rate": 0.9}
        result = validate_chaos_config(values)
        assert result["failure_rate"] == 0.5

    def test_negative_failure_rate_clamped_to_zero(self):
        """Negative failure rate clamped to 0
        음수 failure_rate가 0으로 제한되는지 확인.
        """
        values = {"failure_rate": -0.5}
        result = validate_chaos_config(values)
        assert result["failure_rate"] == 0.0

    @patch.dict(os.environ, {"DJANGO_SETTINGS_MODULE": "myproject.settings.production"})
    def test_production_forces_dry_run(self):
        """Production forces dry_run
        프로덕션 환경에서 dry_run=False가 True로 강제되는지 확인.
        """
        values = {"dry_run": False}
        result = validate_chaos_config(values)
        assert result["dry_run"] is True

    @patch.dict(
        os.environ, {"DJANGO_SETTINGS_MODULE": "myproject.settings.development"}
    )
    def test_non_production_allows_dry_run_false(self):
        """Non-production allows dry_run=False
        비프로덕션 환경에서 dry_run=False가 허용되는지 확인.
        """
        values = {"dry_run": False}
        result = validate_chaos_config(values)
        assert result["dry_run"] is False


# =============================================================================
# Internal Helper Functions Tests
# =============================================================================


class TestInternalHelpers:
    """내부 헬퍼 함수 테스트."""

    def test_handle_fatal_violation(self):
        """_handle_fatal_violation records violation
        Fatal 위반이 result에 올바르게 기록되는지 확인.
        """
        result = ConfigValidationResult()
        _handle_fatal_violation(
            result, "security", "key1", "bad_val", "error", log_changes=False
        )
        assert "security" in result.fatal_violations
        assert "key1" in result.fatal_violations["security"]

    def test_handle_non_fatal_violation(self):
        """_handle_non_fatal_violation applies safe default
        Non-fatal 위반이 Safe Default로 교체되고 result에 기록되는지 확인.
        """
        result = ConfigValidationResult()
        sub_config = MagicMock()
        _handle_non_fatal_violation(
            result,
            sub_config,
            "circuit_breaker",
            "key1",
            "bad",
            "good",
            "error",
            log_changes=False,
        )
        assert "circuit_breaker" in result.non_fatal_warnings

    def test_handle_non_fatal_frozen_dataclass(self):
        """_handle_non_fatal_violation with frozen dataclass
        Frozen dataclass에서 setattr 실패 시에도 경고만 출력하는지 확인.
        """
        result = ConfigValidationResult()

        class FrozenLike:
            def __setattr__(self, name, value):
                raise AttributeError("frozen")

        sub_config = FrozenLike.__new__(FrozenLike)
        # frozen이면 changes_count가 증가하지 않아야 함
        _handle_non_fatal_violation(
            result,
            sub_config,
            "circuit_breaker",
            "key1",
            "bad",
            "good",
            "error",
            log_changes=False,
        )
        assert "circuit_breaker" in result.non_fatal_warnings

    def test_finalize_with_fatal_and_raise(self):
        """_finalize_validation raises on fatal
        Fatal 위반이 있고 raise_on_fatal=True일 때 FatalConfigError가 발생하는지 확인.
        """
        result = ConfigValidationResult()
        result.add_fatal_violation("security", "key1", "error")
        with pytest.raises(FatalConfigError):
            _finalize_validation(result, log_changes=False, raise_on_fatal=True)

    def test_finalize_without_fatal(self):
        """_finalize_validation without fatal does not raise
        Fatal 위반이 없을 때 예외가 발생하지 않는지 확인.
        """
        result = ConfigValidationResult()
        result.changes_count = 3
        _finalize_validation(result, log_changes=False, raise_on_fatal=True)
        # 예외 없이 완료


# =============================================================================
# SAFE_DEFAULTS and VALIDATION_RULES Consistency Tests
# =============================================================================


class TestSafeDefaultsConsistency:
    """SAFE_DEFAULTS와 VALIDATION_RULES 일관성 테스트."""

    def test_all_validation_rules_have_defaults(self):
        """All validation rules have corresponding safe defaults
        VALIDATION_RULES에 정의된 모든 키가 SAFE_DEFAULTS에도 존재하는지 확인.
        """
        for config_type, rules in VALIDATION_RULES.items():
            defaults = SAFE_DEFAULTS.get(config_type, {})
            for key in rules:
                assert key in defaults, (
                    f"{config_type}.{key} is in VALIDATION_RULES but not in SAFE_DEFAULTS"
                )

    def test_safe_defaults_values_are_valid(self):
        """Safe default values pass validation
        SAFE_DEFAULTS의 모든 값이 자체 검증을 통과하는지 확인.
        """
        for config_type, defaults in SAFE_DEFAULTS.items():
            for key, value in defaults.items():
                assert is_valid_value(config_type, key, value), (
                    f"SAFE_DEFAULTS[{config_type}][{key}]={value!r} fails validation"
                )

    def test_fatal_configs_have_validation_rules(self):
        """Fatal configs should have validation rules
        FATAL_CONFIGS에 정의된 모든 키가 VALIDATION_RULES에도 존재하는지 확인.
        """
        for config_type, keys in FATAL_CONFIGS.items():
            rules = VALIDATION_RULES.get(config_type, {})
            for key in keys:
                assert key in rules, (
                    f"Fatal config {config_type}.{key} has no validation rule"
                )
