"""
Tests for Pydantic Settings Module.

Tests for the 5 core settings classes:
- CircuitBreakerSettings
- DLQSettings
- RetrySettings
- RateLimitSettings
- SecuritySettings
"""

import pytest
from pydantic import ValidationError


class TestCircuitBreakerSettings:
    """Tests for CircuitBreakerSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.circuit_breaker import reset_circuit_breaker_settings

        reset_circuit_breaker_settings()
        yield
        reset_circuit_breaker_settings()

    def test_default_values(self):
        """기본값이 core/config.py:CircuitBreakerConfig와 일치하는지 검증."""
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        settings = CircuitBreakerSettings()

        # Core settings (from core/config.py lines 17-23)
        assert settings.enabled is True
        assert settings.failure_threshold == 5
        assert settings.recovery_timeout == 60
        assert settings.success_threshold == 2
        assert settings.half_open_max_calls == 3
        # 476 D8: HALF_OPEN stuck-window auto-reset threshold. Default
        # 60s == 2× recovery_timeout default to avoid false-positive
        # resets during legitimately slow downstream recovery (R5).
        assert settings.half_open_stuck_timeout_seconds == 60
        # Rate limit cascade (lines 25-27)
        assert settings.rate_limit_cascade_threshold == 10
        assert settings.rate_limit_cascade_window_seconds == 60

        # Self-DDoS protection (lines 29-33)
        assert settings.self_ddos_protection_enabled is True
        assert settings.self_ddos_rps_limit == 200
        assert settings.self_ddos_window_seconds == 10
        assert settings.self_ddos_backoff_multiplier == 2.0

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        monkeypatch.setenv("BALDUR_CB_FAILURE_THRESHOLD", "10")
        monkeypatch.setenv("BALDUR_CB_RECOVERY_TIMEOUT", "120")
        monkeypatch.setenv("BALDUR_CB_ENABLED", "false")

        settings = CircuitBreakerSettings()

        assert settings.failure_threshold == 10
        assert settings.recovery_timeout == 120
        assert settings.enabled is False

    def test_validation_min_failure_threshold(self):
        """failure_threshold 최소값(1) 검증."""
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        with pytest.raises(ValidationError) as exc_info:
            CircuitBreakerSettings(failure_threshold=0)

        assert "failure_threshold" in str(exc_info.value)

    def test_validation_max_failure_threshold(self):
        """failure_threshold 최대값(100) 검증."""
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        with pytest.raises(ValidationError) as exc_info:
            CircuitBreakerSettings(failure_threshold=101)

        assert "failure_threshold" in str(exc_info.value)

    def test_validation_recovery_timeout_range(self):
        """recovery_timeout 범위 (1-3600) 검증."""
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        # Too low
        with pytest.raises(ValidationError):
            CircuitBreakerSettings(recovery_timeout=0)

        # Too high
        with pytest.raises(ValidationError):
            CircuitBreakerSettings(recovery_timeout=3601)

        # Valid edge cases
        settings_min = CircuitBreakerSettings(recovery_timeout=1)
        assert settings_min.recovery_timeout == 1

        settings_max = CircuitBreakerSettings(recovery_timeout=3600)
        assert settings_max.recovery_timeout == 3600

    def test_type_coercion(self):
        """문자열이 정수로 자동 변환되는지 검증."""
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        settings = CircuitBreakerSettings(failure_threshold="5")  # type: ignore
        assert settings.failure_threshold == 5
        assert isinstance(settings.failure_threshold, int)

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.circuit_breaker import (
            get_circuit_breaker_settings,
            reset_circuit_breaker_settings,
        )

        settings1 = get_circuit_breaker_settings()
        settings2 = get_circuit_breaker_settings()

        assert settings1 is settings2

        reset_circuit_breaker_settings()
        settings3 = get_circuit_breaker_settings()

        assert settings1 is not settings3

    def test_json_schema_generation(self):
        """JSON Schema가 올바르게 생성되는지 검증."""
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        schema = CircuitBreakerSettings.model_json_schema()

        assert "properties" in schema
        assert "failure_threshold" in schema["properties"]

        ft_schema = schema["properties"]["failure_threshold"]
        assert ft_schema.get("minimum") == 1
        assert ft_schema.get("maximum") == 100

    # ---- 439: New hybrid cascade / distributed fields ----

    def test_hybrid_cascade_defaults(self):
        """439 hybrid cascade new field defaults."""
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        settings = CircuitBreakerSettings()

        assert settings.rate_limit_cascade_rate == 10.0
        assert settings.rate_limit_cascade_minimum_calls == 20
        assert settings.rate_limit_distributed is False

    def test_rate_limit_cascade_rate_validation_lower_bound(self):
        """rate_limit_cascade_rate rejects below 0.0."""
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        with pytest.raises(ValidationError):
            CircuitBreakerSettings(rate_limit_cascade_rate=-0.1)

    def test_rate_limit_cascade_rate_validation_upper_bound(self):
        """rate_limit_cascade_rate rejects above 100.0."""
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        with pytest.raises(ValidationError):
            CircuitBreakerSettings(rate_limit_cascade_rate=100.1)

    def test_rate_limit_cascade_rate_boundary_values_accepted(self):
        """rate_limit_cascade_rate accepts 0.0 and 100.0."""
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        s_min = CircuitBreakerSettings(rate_limit_cascade_rate=0.0)
        assert s_min.rate_limit_cascade_rate == 0.0

        s_max = CircuitBreakerSettings(rate_limit_cascade_rate=100.0)
        assert s_max.rate_limit_cascade_rate == 100.0

    def test_rate_limit_cascade_minimum_calls_validation(self):
        """rate_limit_cascade_minimum_calls uses MediumCount (1-100)."""
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        with pytest.raises(ValidationError):
            CircuitBreakerSettings(rate_limit_cascade_minimum_calls=0)

        with pytest.raises(ValidationError):
            CircuitBreakerSettings(rate_limit_cascade_minimum_calls=101)

    def test_env_override_new_fields(self, monkeypatch):
        """439 new fields can be overridden via env vars."""
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        monkeypatch.setenv("BALDUR_CB_RATE_LIMIT_CASCADE_RATE", "25.5")
        monkeypatch.setenv("BALDUR_CB_RATE_LIMIT_CASCADE_MINIMUM_CALLS", "50")
        monkeypatch.setenv("BALDUR_CB_RATE_LIMIT_DISTRIBUTED", "true")

        settings = CircuitBreakerSettings()

        assert settings.rate_limit_cascade_rate == 25.5
        assert settings.rate_limit_cascade_minimum_calls == 50
        assert settings.rate_limit_distributed is True


class TestDLQSettings:
    """Tests for DLQSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.dlq import reset_dlq_settings

        reset_dlq_settings()
        yield
        reset_dlq_settings()

    def test_default_values(self):
        """기본값이 core/config.py:DLQConfig와 일치하는지 검증."""
        from baldur.settings.dlq import DLQSettings

        settings = DLQSettings()

        # From core/config.py lines 38-45
        assert settings.enabled is True
        assert settings.retry_delay == 60
        assert settings.expiry_hours == 72
        assert settings.retention_days == 30
        assert settings.batch_size == 10
        assert settings.max_replay_attempts == 2

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.dlq import DLQSettings

        monkeypatch.setenv("BALDUR_DLQ_MAX_REPLAY_ATTEMPTS", "5")
        monkeypatch.setenv("BALDUR_DLQ_RETENTION_DAYS", "60")

        settings = DLQSettings()

        assert settings.max_replay_attempts == 5
        assert settings.retention_days == 60

    def test_validation_max_replay_attempts_range(self):
        """max_replay_attempts 범위 (1-10) 검증."""
        from baldur.settings.dlq import DLQSettings

        with pytest.raises(ValidationError):
            DLQSettings(max_replay_attempts=0)

        with pytest.raises(ValidationError):
            DLQSettings(max_replay_attempts=11)

    def test_validation_retention_days_range(self):
        """retention_days 범위 (1-365) 검증."""
        from baldur.settings.dlq import DLQSettings

        with pytest.raises(ValidationError):
            DLQSettings(retention_days=0)

        with pytest.raises(ValidationError):
            DLQSettings(retention_days=366)

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.dlq import get_dlq_settings

        settings1 = get_dlq_settings()
        settings2 = get_dlq_settings()

        assert settings1 is settings2


class TestRetrySettings:
    """Tests for RetrySettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.retry import reset_retry_settings

        reset_retry_settings()
        yield
        reset_retry_settings()

    def test_default_values(self):
        """기본값이 core/config.py:RetryConfig와 일치하는지 검증."""
        from baldur.settings.retry import RetrySettings

        settings = RetrySettings()

        # From core/config.py lines 50-56
        # Note: backoff_base, min_delay, jitter_percent moved to BackoffSettings
        # (doc 359 Option B)
        assert settings.max_attempts == 3
        assert settings.backoff_strategy == "exponential"
        assert settings.base_delay == 1.0
        assert settings.max_delay == 60.0

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.retry import RetrySettings

        monkeypatch.setenv("BALDUR_RETRY_MAX_ATTEMPTS", "5")
        monkeypatch.setenv("BALDUR_RETRY_BACKOFF_STRATEGY", "linear")

        settings = RetrySettings()

        assert settings.max_attempts == 5
        assert settings.backoff_strategy == "linear"

    def test_validation_backoff_strategy(self):
        """backoff_strategy 유효값 검증."""
        from baldur.settings.retry import RetrySettings

        # Valid strategies
        for strategy in ["exponential", "linear", "constant", "decorrelated_jitter"]:
            settings = RetrySettings(backoff_strategy=strategy)
            assert settings.backoff_strategy == strategy

        # Invalid strategy
        with pytest.raises(ValidationError) as exc_info:
            RetrySettings(backoff_strategy="invalid_strategy")

        assert "backoff_strategy" in str(exc_info.value)

    def test_validation_max_attempts_range(self):
        """max_attempts 범위 (1-20) 검증."""
        from baldur.settings.retry import RetrySettings

        with pytest.raises(ValidationError):
            RetrySettings(max_attempts=0)

        with pytest.raises(ValidationError):
            RetrySettings(max_attempts=21)

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.retry import get_retry_settings

        settings1 = get_retry_settings()
        settings2 = get_retry_settings()

        assert settings1 is settings2


class TestRateLimitSettings:
    """Tests for RateLimitSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.rate_limit import reset_rate_limit_settings

        reset_rate_limit_settings()
        yield
        reset_rate_limit_settings()

    def test_default_values(self):
        """기본값이 core/config.py:RateLimitConfig와 일치하는지 검증."""
        from baldur.settings.rate_limit import RateLimitSettings

        settings = RateLimitSettings()

        # Control API rate limiting (the outbound 429-backoff family moved
        # to RateLimitBackoffSettings)
        assert settings.control_api_rate_limit == 100
        assert settings.control_api_window_seconds == 60
        assert settings.emergency_rate_limit == 10
        assert settings.emergency_window_seconds == 60

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.rate_limit import RateLimitSettings

        monkeypatch.setenv("BALDUR_RATE_LIMIT_CONTROL_API_RATE_LIMIT", "200")
        monkeypatch.setenv("BALDUR_RATE_LIMIT_EMERGENCY_RATE_LIMIT", "20")

        settings = RateLimitSettings()

        assert settings.control_api_rate_limit == 200
        assert settings.emergency_rate_limit == 20

    def test_validation_emergency_rate_limit(self):
        """emergency_rate_limit 범위 (1-100) 검증."""
        from baldur.settings.rate_limit import RateLimitSettings

        with pytest.raises(ValidationError):
            RateLimitSettings(emergency_rate_limit=0)

        with pytest.raises(ValidationError):
            RateLimitSettings(emergency_rate_limit=101)

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.rate_limit import (
            get_rate_limit_settings,
        )

        settings1 = get_rate_limit_settings()
        settings2 = get_rate_limit_settings()

        assert settings1 is settings2


class TestSecuritySettings:
    """Tests for SecuritySettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.security import reset_security_settings

        reset_security_settings()
        yield
        reset_security_settings()

    def test_default_values(self):
        """기본값이 core/config.py:SecurityConfig와 일치하는지 검증."""
        from baldur.settings.security import SecuritySettings

        settings = SecuritySettings()

        # From core/config.py lines 176-187
        assert settings.temporary_ban_hours == 1
        assert settings.permanent_ban_threshold == 5
        assert settings.suspicious_ip_cache_timeout == 86400
        assert settings.injection_ban_hours == 24
        assert settings.suspicious_ip_cache_prefix == "security:suspicious_ip:"
        assert settings.banned_ip_cache_prefix == "security:banned_ip:"

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.security import SecuritySettings

        monkeypatch.setenv("BALDUR_SECURITY_TEMPORARY_BAN_HOURS", "12")
        monkeypatch.setenv("BALDUR_SECURITY_INJECTION_BAN_HOURS", "48")

        settings = SecuritySettings()

        assert settings.temporary_ban_hours == 12
        assert settings.injection_ban_hours == 48

    def test_validation_injection_ban_hours(self):
        """injection_ban_hours 범위 (1-720) 검증."""
        from baldur.settings.security import SecuritySettings

        with pytest.raises(ValidationError):
            SecuritySettings(injection_ban_hours=0)

        with pytest.raises(ValidationError):
            SecuritySettings(injection_ban_hours=721)

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.security import (
            get_security_settings,
        )

        settings1 = get_security_settings()
        settings2 = get_security_settings()

        assert settings1 is settings2

    def test_json_schema_generation(self):
        """JSON Schema가 올바르게 생성되는지 검증."""
        from baldur.settings.security import SecuritySettings

        schema = SecuritySettings.model_json_schema()

        assert "properties" in schema
        assert "temporary_ban_hours" in schema["properties"]
        assert "injection_ban_hours" in schema["properties"]


class TestSettingsConsistencyWithLegacy:
    """
    기존 dataclass 설정과 Pydantic Settings의 일관성 검증.

    core/config.py의 dataclass 기본값과
    settings/*.py의 Pydantic 기본값이 동일한지 확인.
    """

    def test_circuit_breaker_consistency(self):
        """CircuitBreakerConfig와 CircuitBreakerSettings 기본값 일치."""
        from baldur.core.config import CircuitBreakerConfig
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        legacy = CircuitBreakerConfig()
        pydantic = CircuitBreakerSettings()

        assert pydantic.enabled == legacy.enabled
        assert pydantic.failure_threshold == legacy.failure_threshold
        assert pydantic.recovery_timeout == legacy.recovery_timeout
        assert pydantic.success_threshold == legacy.success_threshold
        assert pydantic.half_open_max_calls == legacy.half_open_max_calls
        assert (
            pydantic.rate_limit_cascade_threshold == legacy.rate_limit_cascade_threshold
        )
        assert (
            pydantic.rate_limit_cascade_window_seconds
            == legacy.rate_limit_cascade_window_seconds
        )
        assert (
            pydantic.self_ddos_protection_enabled == legacy.self_ddos_protection_enabled
        )
        assert pydantic.self_ddos_rps_limit == legacy.self_ddos_rps_limit
        assert pydantic.self_ddos_window_seconds == legacy.self_ddos_window_seconds
        assert (
            pydantic.self_ddos_backoff_multiplier == legacy.self_ddos_backoff_multiplier
        )

    def test_dlq_consistency(self):
        """DLQConfig와 DLQSettings 기본값 일치."""
        from baldur.core.config import DLQConfig
        from baldur.settings.dlq import DLQSettings

        legacy = DLQConfig()
        pydantic = DLQSettings()

        assert pydantic.enabled == legacy.enabled
        assert pydantic.retry_delay == legacy.retry_delay
        assert pydantic.expiry_hours == legacy.expiry_hours
        assert pydantic.retention_days == legacy.retention_days
        assert pydantic.batch_size == legacy.batch_size
        assert pydantic.max_replay_attempts == legacy.max_replay_attempts

    def test_retry_consistency(self):
        """RetrySettings와 BackoffSettings legacy 기본값 검증."""
        from baldur.settings.backoff import BackoffSettings
        from baldur.settings.retry import RetrySettings

        pydantic = RetrySettings()
        backoff = BackoffSettings()

        assert pydantic.max_attempts == 3
        assert pydantic.backoff_strategy == "exponential"
        assert pydantic.base_delay == 1.0
        assert pydantic.max_delay == 60.0
        # Legacy backoff fields moved to BackoffSettings (doc 359 Option B)
        assert backoff.legacy_base == 4
        assert backoff.legacy_min_delay == 1
        assert backoff.legacy_jitter_percent == 25

    def test_rate_limit_consistency(self):
        """RateLimitConfig와 RateLimitSettings 기본값 일치."""
        from baldur.core.config import RateLimitConfig
        from baldur.settings.rate_limit import RateLimitSettings

        legacy = RateLimitConfig()
        pydantic = RateLimitSettings()

        # Quota family only — the backoff family moved to
        # RateLimitBackoffSettings and is no longer on RateLimitSettings.
        assert pydantic.control_api_rate_limit == legacy.control_api_rate_limit
        assert pydantic.control_api_window_seconds == legacy.control_api_window_seconds
        assert pydantic.emergency_rate_limit == legacy.emergency_rate_limit
        assert pydantic.emergency_window_seconds == legacy.emergency_window_seconds

    def test_security_consistency(self):
        """SecurityConfig와 SecuritySettings 기본값 일치."""
        from baldur.core.config import SecurityConfig
        from baldur.settings.security import SecuritySettings

        legacy = SecurityConfig()
        pydantic = SecuritySettings()

        assert pydantic.temporary_ban_hours == legacy.temporary_ban_hours
        assert pydantic.permanent_ban_threshold == legacy.permanent_ban_threshold
        assert (
            pydantic.suspicious_ip_cache_timeout == legacy.suspicious_ip_cache_timeout
        )
        assert pydantic.injection_ban_hours == legacy.injection_ban_hours
        assert pydantic.suspicious_ip_cache_prefix == legacy.suspicious_ip_cache_prefix
        assert pydantic.banned_ip_cache_prefix == legacy.banned_ip_cache_prefix


class TestValidationRulesConsistency:
    """
    core/safe_defaults.py의 VALIDATION_RULES와
    Pydantic Field constraints의 일관성 검증.
    """

    def test_circuit_breaker_validation_rules(self):
        """CircuitBreakerSettings 검증 규칙이 VALIDATION_RULES와 일치."""
        from baldur.core.safe_defaults import VALIDATION_RULES
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        schema = CircuitBreakerSettings.model_json_schema()
        props = schema["properties"]
        rules = VALIDATION_RULES["circuit_breaker"]

        # failure_threshold: (1, 100)
        assert props["failure_threshold"]["minimum"] == rules["failure_threshold"][0]
        assert props["failure_threshold"]["maximum"] == rules["failure_threshold"][1]

        # recovery_timeout: (1, 3600)
        assert props["recovery_timeout"]["minimum"] == rules["recovery_timeout"][0]
        assert props["recovery_timeout"]["maximum"] == rules["recovery_timeout"][1]

        # success_threshold: (1, 100)
        assert props["success_threshold"]["minimum"] == rules["success_threshold"][0]
        assert props["success_threshold"]["maximum"] == rules["success_threshold"][1]

    def test_dlq_validation_rules(self):
        """DLQSettings 검증 규칙이 VALIDATION_RULES와 일치."""
        from baldur.core.safe_defaults import VALIDATION_RULES
        from baldur.settings.dlq import DLQSettings

        schema = DLQSettings.model_json_schema()
        props = schema["properties"]
        rules = VALIDATION_RULES["dlq"]

        # retention_days: (1, 365)
        assert props["retention_days"]["minimum"] == rules["retention_days"][0]
        assert props["retention_days"]["maximum"] == rules["retention_days"][1]

    def test_retry_validation_rules(self):
        """RetrySettings 검증 규칙이 VALIDATION_RULES와 일치."""
        from baldur.core.safe_defaults import VALIDATION_RULES
        from baldur.settings.backoff import BackoffSettings
        from baldur.settings.retry import RetrySettings

        retry_schema = RetrySettings.model_json_schema()
        retry_props = retry_schema["properties"]
        rules = VALIDATION_RULES["retry"]

        # max_attempts: (1, 20)
        assert retry_props["max_attempts"]["minimum"] == rules["max_attempts"][0]
        assert retry_props["max_attempts"]["maximum"] == rules["max_attempts"][1]

        # jitter_percent moved to BackoffSettings.legacy_jitter_percent (doc 359)
        backoff_schema = BackoffSettings.model_json_schema()
        backoff_props = backoff_schema["properties"]
        assert (
            backoff_props["legacy_jitter_percent"]["minimum"]
            == rules["jitter_percent"][0]
        )
        assert (
            backoff_props["legacy_jitter_percent"]["maximum"]
            == rules["jitter_percent"][1]
        )

    def test_rate_limit_validation_rules(self):
        """RateLimitSettings 검증 규칙이 VALIDATION_RULES와 일치."""
        from baldur.core.safe_defaults import VALIDATION_RULES
        from baldur.settings.rate_limit import RateLimitSettings

        schema = RateLimitSettings.model_json_schema()
        props = schema["properties"]
        rules = VALIDATION_RULES["rate_limit"]

        # control_api_rate_limit: (1, 10000)
        assert (
            props["control_api_rate_limit"]["minimum"]
            == rules["control_api_rate_limit"][0]
        )
        assert (
            props["control_api_rate_limit"]["maximum"]
            == rules["control_api_rate_limit"][1]
        )

    def test_security_validation_rules(self):
        """SecuritySettings 검증 규칙이 VALIDATION_RULES와 일치."""
        from baldur.core.safe_defaults import VALIDATION_RULES
        from baldur.settings.security import SecuritySettings

        schema = SecuritySettings.model_json_schema()
        props = schema["properties"]
        rules = VALIDATION_RULES["security"]

        # temporary_ban_hours: (1, 168)
        assert (
            props["temporary_ban_hours"]["minimum"] == rules["temporary_ban_hours"][0]
        )
        assert (
            props["temporary_ban_hours"]["maximum"] == rules["temporary_ban_hours"][1]
        )

        # injection_ban_hours: (1, 720)
        assert (
            props["injection_ban_hours"]["minimum"] == rules["injection_ban_hours"][0]
        )
        assert (
            props["injection_ban_hours"]["maximum"] == rules["injection_ban_hours"][1]
        )
