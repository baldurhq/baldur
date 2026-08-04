"""
Tests for core/safe_defaults.py - Safe Default Values and Validation.
Unit tests for safe-default management, validation, and Fatal-config classification in core/safe_defaults.py.

Coverage targets:
- SAFE_DEFAULTS dictionary accessors (get_safe_default, get_safe_defaults_for_type)
- VALIDATION_RULES-based validation (is_valid_value, get_validation_errors)
- validate_with_safe_fallback, validate_all_with_safe_fallback
- apply_safe_defaults_to_missing
- Fatal-config classification (is_fatal_config, get_all_fatal_configs)
- FatalConfigError, ConfigValidationResult
- validate_startup_config, validate_config_preflight
- validate_chaos_config (chaos-specific validation)
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
    """Tests for the get_safe_default function."""

    def test_get_existing_config_key(self):
        """Get existing config key
        An existing config_type/key pair returns the correct default.
        """
        result = get_safe_default("circuit_breaker", "failure_threshold")
        assert result == 5

    def test_get_nonexistent_key(self):
        """Get nonexistent key
        A nonexistent key returns None.
        """
        result = get_safe_default("circuit_breaker", "nonexistent_key")
        assert result is None

    def test_get_nonexistent_config_type(self):
        """Get nonexistent config type
        A nonexistent config_type returns None.
        """
        result = get_safe_default("nonexistent_type", "any_key")
        assert result is None

    @pytest.mark.parametrize(
        ("config_type", "key", "expected"),
        [
            ("dlq", "max_replay_attempts", 2),
            ("retry", "backoff_strategy", "exponential"),
            ("rate_limit", "control_api_rate_limit", 100),
            ("security", "injection_ban_hours", 24),
            ("chaos", "enabled", False),
            ("slo", "default_target", 0.999),
            ("l2_storage", "enabled", False),
        ],
    )
    def test_various_config_types(self, config_type, key, expected):
        """Various config types
        Various config types return their correct defaults.
        """
        result = get_safe_default(config_type, key)
        assert result == expected


class TestGetSafeDefaultsForType:
    """Tests for the get_safe_defaults_for_type function."""

    def test_get_circuit_breaker_defaults(self):
        """Get circuit breaker defaults
        All circuit_breaker defaults are returned correctly.
        """
        result = get_safe_defaults_for_type("circuit_breaker")
        assert isinstance(result, dict)
        assert "failure_threshold" in result
        assert "recovery_timeout" in result
        assert result["enabled"] is True

    def test_returns_copy(self):
        """Returns copy (not reference)
        The returned dictionary is a copy (original stays immutable).
        """
        result = get_safe_defaults_for_type("circuit_breaker")
        result["failure_threshold"] = 999
        # The original must remain unchanged
        assert SAFE_DEFAULTS["circuit_breaker"]["failure_threshold"] == 5

    def test_nonexistent_type_returns_empty(self):
        """Nonexistent type returns empty dict
        A nonexistent type returns an empty dictionary.
        """
        result = get_safe_defaults_for_type("nonexistent_type")
        assert result == {}


# =============================================================================
# is_valid_value Tests
# =============================================================================


class TestIsValidValue:
    """Tests for the is_valid_value function."""

    def test_valid_numeric_value(self):
        """Valid numeric value
        A valid numeric value returns True.
        """
        assert is_valid_value("circuit_breaker", "failure_threshold", 5) is True

    def test_value_below_range(self):
        """Value below minimum range
        A value below the minimum returns False.
        """
        assert is_valid_value("circuit_breaker", "failure_threshold", 0) is False

    def test_value_above_range(self):
        """Value above maximum range
        A value above the maximum returns False.
        """
        assert is_valid_value("circuit_breaker", "failure_threshold", 101) is False

    def test_none_value(self):
        """None value is invalid
        A None value returns False.
        """
        assert is_valid_value("circuit_breaker", "failure_threshold", None) is False

    def test_valid_log_level(self):
        """Valid log level
        A valid log level returns True.
        """
        assert is_valid_value("logging", "dlq_log_level", "INFO") is True

    def test_invalid_log_level(self):
        """Invalid log level
        An invalid log level returns False.
        """
        assert is_valid_value("logging", "dlq_log_level", "TRACE") is False

    def test_valid_backoff_strategy(self):
        """Valid backoff strategy
        A valid backoff strategy returns True.
        """
        assert is_valid_value("retry", "backoff_strategy", "exponential") is True

    def test_invalid_backoff_strategy(self):
        """Invalid backoff strategy
        An invalid backoff strategy returns False.
        """
        assert is_valid_value("retry", "backoff_strategy", "random") is False

    def test_boolean_field_with_bool(self):
        """Boolean field with bool value
        A bool value on a boolean field returns True.
        """
        assert is_valid_value("circuit_breaker", "enabled", True) is True
        assert is_valid_value("circuit_breaker", "enabled", False) is True

    def test_boolean_field_with_non_bool(self):
        """Boolean field with non-bool value
        A non-bool value on a boolean field returns False.
        """
        assert is_valid_value("circuit_breaker", "enabled", 1) is False
        assert is_valid_value("circuit_breaker", "enabled", "true") is False

    def test_key_without_validation_rule(self):
        """Key without validation rule
        A key without a validation rule defaults to True.
        """
        # "prefix" is not in VALIDATION_RULES
        assert is_valid_value("metrics", "prefix", "baldur") is True

    def test_uncomparable_type(self):
        """Uncomparable type returns False
        An uncomparable type returns False.
        """
        assert (
            is_valid_value("circuit_breaker", "failure_threshold", "not_a_number")
            is False
        )

    def test_float_range_validation(self):
        """Float range validation
        Float range validation behaves correctly.
        """
        assert is_valid_value("retry", "base_delay", 0.1) is True
        assert is_valid_value("retry", "base_delay", 60.0) is True
        assert is_valid_value("retry", "base_delay", 0.05) is False
        assert is_valid_value("retry", "base_delay", 61.0) is False


# =============================================================================
# validate_with_safe_fallback Tests
# =============================================================================


class TestValidateWithSafeFallback:
    """Tests for the validate_with_safe_fallback function."""

    def test_all_valid_values(self):
        """All valid values unchanged
        When every value is valid, the originals are kept as-is.
        """
        values = {"failure_threshold": 5, "recovery_timeout": 60}
        result = validate_with_safe_fallback("circuit_breaker", values)
        assert result == values

    def test_invalid_value_replaced(self):
        """Invalid value replaced with safe default
        An invalid value is replaced with its safe default.
        """
        values = {"failure_threshold": 0}  # below minimum (1)
        result = validate_with_safe_fallback("circuit_breaker", values)
        assert result["failure_threshold"] == 5  # Safe Default

    def test_no_safe_default_keeps_original(self):
        """No safe default keeps original value
        A key without a safe default keeps its original value.
        """
        values = {"custom_key": "invalid_value"}
        result = validate_with_safe_fallback(
            "circuit_breaker", values, log_changes=False
        )
        assert result["custom_key"] == "invalid_value"

    def test_log_changes_false(self):
        """Log changes disabled
        log_changes=False operates without logging.
        """
        values = {"failure_threshold": 0}
        result = validate_with_safe_fallback(
            "circuit_breaker", values, log_changes=False
        )
        assert result["failure_threshold"] == 5

    def test_mixed_valid_invalid_values(self):
        """Mixed valid and invalid values
        A mix of valid and invalid values is handled correctly.
        """
        values = {
            "failure_threshold": 10,  # valid
            "recovery_timeout": 0,  # below minimum (1) -> replaced
        }
        result = validate_with_safe_fallback("circuit_breaker", values)
        assert result["failure_threshold"] == 10
        assert result["recovery_timeout"] == 60  # Safe Default


class TestValidateAllWithSafeFallback:
    """Tests for the validate_all_with_safe_fallback function."""

    def test_multiple_config_types(self):
        """Multiple config types validated
        Multiple config types are all validated correctly.
        """
        config_dict = {
            "circuit_breaker": {"failure_threshold": 0},  # invalid
            "dlq": {"max_replay_attempts": 5},  # valid
        }
        result = validate_all_with_safe_fallback(config_dict, log_changes=False)
        assert result["circuit_breaker"]["failure_threshold"] == 5  # Safe Default
        assert result["dlq"]["max_replay_attempts"] == 5  # valid -> kept


# =============================================================================
# apply_safe_defaults_to_missing Tests
# =============================================================================


class TestApplySafeDefaultsToMissing:
    """Tests for the apply_safe_defaults_to_missing function."""

    def test_missing_keys_filled(self):
        """Missing keys filled with defaults
        Missing keys are filled with safe defaults.
        """
        values = {"failure_threshold": 10}
        result = apply_safe_defaults_to_missing("circuit_breaker", values)
        assert result["failure_threshold"] == 10  # existing value kept
        assert "recovery_timeout" in result  # filled from safe defaults

    def test_existing_values_preserved(self):
        """Existing values preserved
        Existing values are not overwritten by safe defaults.
        """
        values = {"failure_threshold": 99}
        result = apply_safe_defaults_to_missing("circuit_breaker", values)
        assert result["failure_threshold"] == 99  # existing value wins

    def test_unknown_config_type(self):
        """Unknown config type
        An unknown config type returns only the existing values.
        """
        values = {"key": "value"}
        result = apply_safe_defaults_to_missing("unknown_type", values)
        assert result == {"key": "value"}


# =============================================================================
# get_validation_errors Tests
# =============================================================================


class TestGetValidationErrors:
    """Tests for the get_validation_errors function."""

    def test_no_errors(self):
        """No validation errors
        No errors when every value is valid.
        """
        values = {"failure_threshold": 5, "recovery_timeout": 60}
        errors = get_validation_errors("circuit_breaker", values)
        assert errors == {}

    def test_value_below_minimum(self):
        """Value below minimum
        A below-minimum value yields the proper error message.
        """
        values = {"failure_threshold": 0}
        errors = get_validation_errors("circuit_breaker", values)
        assert "failure_threshold" in errors
        assert "below minimum" in errors["failure_threshold"]

    def test_value_above_maximum(self):
        """Value above maximum
        An above-maximum value yields the proper error message.
        """
        values = {"failure_threshold": 200}
        errors = get_validation_errors("circuit_breaker", values)
        assert "failure_threshold" in errors
        assert "exceeds maximum" in errors["failure_threshold"]

    def test_none_value(self):
        """None value error
        A None value yields a 'cannot be None' error.
        """
        values = {"failure_threshold": None}
        errors = get_validation_errors("circuit_breaker", values)
        assert "failure_threshold" in errors
        assert "None" in errors["failure_threshold"]

    def test_invalid_type(self):
        """Invalid type error
        An uncomparable type yields an error.
        """
        values = {"failure_threshold": "string"}
        errors = get_validation_errors("circuit_breaker", values)
        assert "failure_threshold" in errors
        assert "not a valid number" in errors["failure_threshold"]

    def test_invalid_log_level(self):
        """Invalid log level error
        An invalid log level yields an error.
        """
        values = {"dlq_log_level": "TRACE"}
        errors = get_validation_errors("logging", values)
        assert "dlq_log_level" in errors

    def test_invalid_backoff_strategy(self):
        """Invalid backoff strategy error
        An invalid backoff strategy yields an error.
        """
        values = {"backoff_strategy": "random_strategy"}
        errors = get_validation_errors("retry", values)
        assert "backoff_strategy" in errors


# =============================================================================
# Fatal Config Tests
# =============================================================================


class TestFatalConfig:
    """Fatal-config classification tests."""

    def test_security_injection_ban_hours_is_fatal(self):
        """Security injection_ban_hours is fatal
        security.injection_ban_hours is classified as fatal.
        """
        assert is_fatal_config("security", "injection_ban_hours") is True

    def test_chaos_blast_radius_is_fatal(self):
        """Chaos max_blast_radius is fatal
        chaos.max_blast_radius is classified as fatal.
        """
        assert is_fatal_config("chaos", "max_blast_radius") is True

    def test_non_fatal_config(self):
        """Non-fatal config
        A non-fatal setting returns False.
        """
        assert is_fatal_config("circuit_breaker", "failure_threshold") is False

    def test_unknown_type(self):
        """Unknown config type
        An unknown config_type returns False.
        """
        assert is_fatal_config("unknown", "any_key") is False

    def test_get_all_fatal_configs(self):
        """Get all fatal configs
        get_all_fatal_configs returns a copy of every fatal config.
        """
        result = get_all_fatal_configs()
        assert "security" in result
        assert "chaos" in result
        assert "error_budget" in result
        # The returned dict must be a copy
        result["security"].add("test_key")
        assert "test_key" not in FATAL_CONFIGS["security"]

    def test_error_budget_is_fatal(self):
        """Error budget fatal configs
        error_budget's fatal settings are classified correctly.
        """
        assert is_fatal_config("error_budget", "threshold_critical") is True
        assert is_fatal_config("error_budget", "burn_rate_fast_critical") is True


# =============================================================================
# FatalConfigError Tests
# =============================================================================


class TestFatalConfigError:
    """Tests for the FatalConfigError exception."""

    def test_error_message(self):
        """Error message format
        FatalConfigError's message format is correct.
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
        Every violation appears in the message.
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
    """Tests for the ConfigValidationResult class."""

    def test_initial_state(self):
        """Initial state
        Initial state has has_fatal_violations=False and is_valid=True.
        """
        result = ConfigValidationResult()
        assert result.has_fatal_violations is False
        assert result.is_valid is True
        assert result.changes_count == 0

    def test_add_fatal_violation(self):
        """Add fatal violation
        State updates correctly after adding a fatal violation.
        """
        result = ConfigValidationResult()
        result.add_fatal_violation("security", "key1", "error msg")
        assert result.has_fatal_violations is True
        assert result.is_valid is False
        assert "security" in result.fatal_violations

    def test_add_non_fatal_warning(self):
        """Add non-fatal warning
        State stays valid after adding a non-fatal warning.
        """
        result = ConfigValidationResult()
        result.add_non_fatal_warning("circuit_breaker", "key1", "warning msg")
        assert result.has_fatal_violations is False
        assert result.is_valid is True
        assert "circuit_breaker" in result.non_fatal_warnings

    def test_multiple_fatal_violations_same_type(self):
        """Multiple fatal violations same type
        Multiple fatal violations can be added to one type.
        """
        result = ConfigValidationResult()
        result.add_fatal_violation("security", "key1", "error1")
        result.add_fatal_violation("security", "key2", "error2")
        assert len(result.fatal_violations["security"]) == 2


# =============================================================================
# validate_startup_config Tests
# =============================================================================


class TestValidateStartupConfig:
    """Tests for the validate_startup_config function."""

    def test_valid_config(self):
        """Valid config no changes
        A fully valid config yields zero changes.
        """
        config = MagicMock()
        cb_config = MagicMock()
        cb_config.failure_threshold = 5
        cb_config.recovery_timeout = 60
        cb_config.success_threshold = 2
        cb_config.half_open_max_calls = 3
        cb_config.manual_override_ttl_minutes = 90
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
        # The remaining config types are treated as None
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
        An invalid setting is replaced with its safe default.
        """
        config = MagicMock()
        cb_config = MagicMock()
        cb_config.failure_threshold = 0  # invalid
        # The rest are valid values
        cb_config.recovery_timeout = 60
        cb_config.success_threshold = 2
        cb_config.half_open_max_calls = 3
        cb_config.manual_override_ttl_minutes = 90
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
        A fatal violation raises FatalConfigError when raise_on_fatal=True.
        """
        config = MagicMock()
        # An invalid fatal value on the security config
        security_config = MagicMock()
        security_config.injection_ban_hours = 0  # below minimum (1), fatal
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
        A None sub-config is skipped.
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
    """Tests for the validate_config_preflight function."""

    def test_preflight_with_valid_config(self):
        """Preflight with valid config
        A valid config yields a violation-free result.
        """
        config = MagicMock()
        cb = MagicMock()
        cb.failure_threshold = 5
        cb.recovery_timeout = 60
        cb.success_threshold = 2
        cb.half_open_max_calls = 3
        cb.manual_override_ttl_minutes = 90
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
        Preflight detects fatal violations (without mutating the config).
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
    """Tests for the validate_chaos_config function."""

    def test_valid_chaos_config(self):
        """Valid chaos config
        A valid chaos config is returned unchanged.
        """
        values = {"max_blast_radius": 0.05, "failure_rate": 0.01, "dry_run": True}
        result = validate_chaos_config(values)
        assert result == values

    def test_blast_radius_clamped_to_50_percent(self):
        """Blast radius clamped to 50%
        max_blast_radius above 50% is clamped to 0.5.
        """
        values = {"max_blast_radius": 0.8}
        result = validate_chaos_config(values)
        assert result["max_blast_radius"] == 0.5

    def test_negative_blast_radius_clamped_to_zero(self):
        """Negative blast radius clamped to 0
        A negative blast radius is clamped to 0.
        """
        values = {"max_blast_radius": -0.1}
        result = validate_chaos_config(values)
        assert result["max_blast_radius"] == 0.0

    def test_failure_rate_clamped_to_50_percent(self):
        """Failure rate clamped to 50%
        failure_rate above 50% is clamped to 0.5.
        """
        values = {"failure_rate": 0.9}
        result = validate_chaos_config(values)
        assert result["failure_rate"] == 0.5

    def test_negative_failure_rate_clamped_to_zero(self):
        """Negative failure rate clamped to 0
        A negative failure_rate is clamped to 0.
        """
        values = {"failure_rate": -0.5}
        result = validate_chaos_config(values)
        assert result["failure_rate"] == 0.0

    @patch.dict(os.environ, {"DJANGO_SETTINGS_MODULE": "myproject.settings.production"})
    def test_production_forces_dry_run(self):
        """Production forces dry_run
        Production forces dry_run=False to True.
        """
        values = {"dry_run": False}
        result = validate_chaos_config(values)
        assert result["dry_run"] is True

    @patch.dict(
        os.environ, {"DJANGO_SETTINGS_MODULE": "myproject.settings.development"}
    )
    def test_non_production_allows_dry_run_false(self):
        """Non-production allows dry_run=False
        Non-production allows dry_run=False.
        """
        values = {"dry_run": False}
        result = validate_chaos_config(values)
        assert result["dry_run"] is False


# =============================================================================
# Internal Helper Functions Tests
# =============================================================================


class TestInternalHelpers:
    """Tests for the internal helper functions."""

    def test_handle_fatal_violation(self):
        """_handle_fatal_violation records violation
        A fatal violation is recorded on the result correctly.
        """
        result = ConfigValidationResult()
        _handle_fatal_violation(
            result, "security", "key1", "bad_val", "error", log_changes=False
        )
        assert "security" in result.fatal_violations
        assert "key1" in result.fatal_violations["security"]

    def test_handle_non_fatal_violation(self):
        """_handle_non_fatal_violation applies safe default
        A non-fatal violation is replaced with its safe default and recorded.
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
        A setattr failure on a frozen dataclass still only warns.
        """
        result = ConfigValidationResult()

        class FrozenLike:
            def __setattr__(self, name, value):
                raise AttributeError("frozen")

        sub_config = FrozenLike.__new__(FrozenLike)
        # frozen means changes_count must not grow
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
        FatalConfigError is raised when fatal violations exist and raise_on_fatal=True.
        """
        result = ConfigValidationResult()
        result.add_fatal_violation("security", "key1", "error")
        with pytest.raises(FatalConfigError):
            _finalize_validation(result, log_changes=False, raise_on_fatal=True)

    def test_finalize_without_fatal(self):
        """_finalize_validation without fatal does not raise
        No exception without fatal violations.
        """
        result = ConfigValidationResult()
        result.changes_count = 3
        _finalize_validation(result, log_changes=False, raise_on_fatal=True)
        # completes without raising


# =============================================================================
# SAFE_DEFAULTS and VALIDATION_RULES Consistency Tests
# =============================================================================


class TestSafeDefaultsConsistency:
    """Consistency tests between SAFE_DEFAULTS and VALIDATION_RULES."""

    def test_all_validation_rules_have_defaults(self):
        """All validation rules have corresponding safe defaults
        Every key in VALIDATION_RULES also exists in SAFE_DEFAULTS.
        """
        for config_type, rules in VALIDATION_RULES.items():
            defaults = SAFE_DEFAULTS.get(config_type, {})
            for key in rules:
                assert key in defaults, (
                    f"{config_type}.{key} is in VALIDATION_RULES but not in SAFE_DEFAULTS"
                )

    def test_safe_defaults_values_are_valid(self):
        """Safe default values pass validation
        Every SAFE_DEFAULTS value passes its own validation.
        """
        for config_type, defaults in SAFE_DEFAULTS.items():
            for key, value in defaults.items():
                assert is_valid_value(config_type, key, value), (
                    f"SAFE_DEFAULTS[{config_type}][{key}]={value!r} fails validation"
                )

    def test_fatal_configs_have_validation_rules(self):
        """Fatal configs should have validation rules
        Every key in FATAL_CONFIGS also exists in VALIDATION_RULES.
        """
        for config_type, keys in FATAL_CONFIGS.items():
            rules = VALIDATION_RULES.get(config_type, {})
            for key in keys:
                assert key in rules, (
                    f"Fatal config {config_type}.{key} has no validation rule"
                )
