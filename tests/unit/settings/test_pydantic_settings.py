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
        """Defaults match core/config.py:CircuitBreakerConfig."""
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
        """Environment variables override the defaults."""
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        monkeypatch.setenv("BALDUR_CB_FAILURE_THRESHOLD", "10")
        monkeypatch.setenv("BALDUR_CB_RECOVERY_TIMEOUT", "120")
        monkeypatch.setenv("BALDUR_CB_ENABLED", "false")

        settings = CircuitBreakerSettings()

        assert settings.failure_threshold == 10
        assert settings.recovery_timeout == 120
        assert settings.enabled is False

    def test_validation_min_failure_threshold(self):
        """failure_threshold accepts its minimum (1) and rejects below it."""
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        with pytest.raises(ValidationError) as exc_info:
            CircuitBreakerSettings(failure_threshold=0)

        assert "failure_threshold" in str(exc_info.value)

    def test_validation_max_failure_threshold(self):
        """failure_threshold accepts its maximum (100) and rejects above it."""
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        with pytest.raises(ValidationError) as exc_info:
            CircuitBreakerSettings(failure_threshold=101)

        assert "failure_threshold" in str(exc_info.value)

    def test_validation_recovery_timeout_range(self):
        """recovery_timeout is bounded to 1-3600 seconds."""
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
        """String environment values coerce to integers."""
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        settings = CircuitBreakerSettings(failure_threshold="5")  # type: ignore
        assert settings.failure_threshold == 5
        assert isinstance(settings.failure_threshold, int)

    def test_singleton_pattern(self):
        """The singleton getter returns the same instance."""
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
        """The model produces a well-formed JSON Schema."""
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
        """Defaults match core/config.py:DLQConfig."""
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
        """Environment variables override the defaults."""
        from baldur.settings.dlq import DLQSettings

        monkeypatch.setenv("BALDUR_DLQ_MAX_REPLAY_ATTEMPTS", "5")
        monkeypatch.setenv("BALDUR_DLQ_RETENTION_DAYS", "60")

        settings = DLQSettings()

        assert settings.max_replay_attempts == 5
        assert settings.retention_days == 60

    def test_validation_max_replay_attempts_range(self):
        """max_replay_attempts is bounded to 1-10."""
        from baldur.settings.dlq import DLQSettings

        with pytest.raises(ValidationError):
            DLQSettings(max_replay_attempts=0)

        with pytest.raises(ValidationError):
            DLQSettings(max_replay_attempts=11)

    def test_validation_retention_days_range(self):
        """retention_days is bounded to 1-365."""
        from baldur.settings.dlq import DLQSettings

        with pytest.raises(ValidationError):
            DLQSettings(retention_days=0)

        with pytest.raises(ValidationError):
            DLQSettings(retention_days=366)

    def test_singleton_pattern(self):
        """The singleton getter returns the same instance."""
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
        """Defaults match core/config.py:RetryConfig."""
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
        """Environment variables override the defaults."""
        from baldur.settings.retry import RetrySettings

        monkeypatch.setenv("BALDUR_RETRY_MAX_ATTEMPTS", "5")
        monkeypatch.setenv("BALDUR_RETRY_BACKOFF_STRATEGY", "linear")

        settings = RetrySettings()

        assert settings.max_attempts == 5
        assert settings.backoff_strategy == "linear"

    def test_validation_backoff_strategy(self):
        """backoff_strategy accepts only the known strategy names."""
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
        """max_attempts is bounded to 1-20."""
        from baldur.settings.retry import RetrySettings

        with pytest.raises(ValidationError):
            RetrySettings(max_attempts=0)

        with pytest.raises(ValidationError):
            RetrySettings(max_attempts=21)

    def test_singleton_pattern(self):
        """The singleton getter returns the same instance."""
        from baldur.settings.retry import get_retry_settings

        settings1 = get_retry_settings()
        settings2 = get_retry_settings()

        assert settings1 is settings2


class TestRetrySettingsBaseAboveMaxWarningBehavior:
    """``base_delay > max_delay`` warns instead of raising or clamping.

    The pair became reachable *in effect* only once ``base_delay`` started
    reaching the ladder: while the field was inert, the two could not disagree
    about anything observable. Every strategy enforces the cap when it computes
    a delay, so the combination is suboptimal rather than unsafe -- the ladder
    simply starts saturated and stays there. Only the silence was the defect.
    """

    BASE_ABOVE_MAX_EVENT = "settings.retry_base_exceeds_max_delay"

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        from baldur.settings.retry import reset_retry_settings

        reset_retry_settings()
        yield
        reset_retry_settings()

    def _warnings(self, logs):
        return [e for e in logs if e.get("event") == self.BASE_ABOVE_MAX_EVENT]

    def test_a_base_above_the_cap_emits_the_warning_with_both_values(self):
        """The operator is told which two numbers disagree."""
        from structlog.testing import capture_logs

        from baldur.settings.retry import RetrySettings

        with capture_logs() as logs:
            RetrySettings(base_delay=30.0, max_delay=10.0)

        warnings = self._warnings(logs)
        assert len(warnings) == 1
        assert warnings[0]["log_level"] == "warning"
        assert warnings[0]["base_delay"] == 30.0
        assert warnings[0]["max_delay"] == 10.0

    def test_the_conflicting_values_come_back_unmodified(self):
        """Warn, never clamp -- both halves asserted, not just the warning.

        Rewriting ``base_delay`` down to ``max_delay`` would mutate operator
        intent to fix behavior the strategies already handle, and would sit a
        second clamp behind the real one. Without this assertion a future
        editor could turn the warning into a clamp and the warning test above
        would still pass.
        """
        from baldur.settings.retry import RetrySettings

        settings = RetrySettings(base_delay=30.0, max_delay=10.0)

        assert settings.base_delay == 30.0
        assert settings.max_delay == 10.0

    @pytest.mark.parametrize(
        ("base_delay", "max_delay"),
        [(1.0, 60.0), (10.0, 10.0), (9.9, 10.0)],
        ids=["default_pair", "at_boundary", "just_below"],
    )
    def test_a_base_at_or_below_the_cap_stays_silent(self, base_delay, max_delay):
        """The equal case is the boundary that matters.

        A ``>`` to ``>=`` drift would warn on the perfectly ordinary "one
        sleep, always the cap" configuration.
        """
        from structlog.testing import capture_logs

        from baldur.settings.retry import RetrySettings

        with capture_logs() as logs:
            RetrySettings(base_delay=base_delay, max_delay=max_delay)

        assert not self._warnings(logs)

    def test_the_warning_fires_for_env_supplied_values(self, monkeypatch):
        """The validator runs on env-sourced settings, not only on kwargs."""
        from structlog.testing import capture_logs

        from baldur.settings.retry import RetrySettings

        monkeypatch.setenv("BALDUR_RETRY_BASE_DELAY", "30.0")
        monkeypatch.setenv("BALDUR_RETRY_MAX_DELAY", "10.0")

        with capture_logs() as logs:
            settings = RetrySettings()

        assert settings.base_delay == 30.0
        assert self._warnings(logs)


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
        """Defaults match core/config.py:RateLimitConfig."""
        from baldur.settings.rate_limit import RateLimitSettings

        settings = RateLimitSettings()

        # Control API rate limiting (the outbound 429-backoff family moved
        # to RateLimitBackoffSettings)
        assert settings.control_api_rate_limit == 100
        assert settings.control_api_window_seconds == 60
        assert settings.emergency_rate_limit == 10
        assert settings.emergency_window_seconds == 60

    def test_env_override(self, monkeypatch):
        """Environment variables override the defaults."""
        from baldur.settings.rate_limit import RateLimitSettings

        monkeypatch.setenv("BALDUR_RATE_LIMIT_CONTROL_API_RATE_LIMIT", "200")
        monkeypatch.setenv("BALDUR_RATE_LIMIT_EMERGENCY_RATE_LIMIT", "20")

        settings = RateLimitSettings()

        assert settings.control_api_rate_limit == 200
        assert settings.emergency_rate_limit == 20

    def test_validation_emergency_rate_limit(self):
        """emergency_rate_limit is bounded to 1-100."""
        from baldur.settings.rate_limit import RateLimitSettings

        with pytest.raises(ValidationError):
            RateLimitSettings(emergency_rate_limit=0)

        with pytest.raises(ValidationError):
            RateLimitSettings(emergency_rate_limit=101)

    def test_singleton_pattern(self):
        """The singleton getter returns the same instance."""
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
        """Defaults match core/config.py:SecurityConfig."""
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
        """Environment variables override the defaults."""
        from baldur.settings.security import SecuritySettings

        monkeypatch.setenv("BALDUR_SECURITY_TEMPORARY_BAN_HOURS", "12")
        monkeypatch.setenv("BALDUR_SECURITY_INJECTION_BAN_HOURS", "48")

        settings = SecuritySettings()

        assert settings.temporary_ban_hours == 12
        assert settings.injection_ban_hours == 48

    def test_validation_injection_ban_hours(self):
        """injection_ban_hours is bounded to 1-720."""
        from baldur.settings.security import SecuritySettings

        with pytest.raises(ValidationError):
            SecuritySettings(injection_ban_hours=0)

        with pytest.raises(ValidationError):
            SecuritySettings(injection_ban_hours=721)

    def test_singleton_pattern(self):
        """The singleton getter returns the same instance."""
        from baldur.settings.security import (
            get_security_settings,
        )

        settings1 = get_security_settings()
        settings2 = get_security_settings()

        assert settings1 is settings2

    def test_json_schema_generation(self):
        """The model produces a well-formed JSON Schema."""
        from baldur.settings.security import SecuritySettings

        schema = SecuritySettings.model_json_schema()

        assert "properties" in schema
        assert "temporary_ban_hours" in schema["properties"]
        assert "injection_ban_hours" in schema["properties"]


class TestSettingsConsistencyWithLegacy:
    """
    Consistency between the legacy dataclass configs and Pydantic Settings.

    Confirms the dataclass defaults in core/config.py and the Pydantic defaults
    in settings/*.py are the same values.
    """

    def test_circuit_breaker_consistency(self):
        """CircuitBreakerConfig and CircuitBreakerSettings share their defaults."""
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
        """DLQConfig and DLQSettings share their defaults."""
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
        """RetrySettings carries the documented retry ladder defaults."""
        from baldur.settings.retry import RetrySettings

        pydantic = RetrySettings()

        assert pydantic.max_attempts == 3
        assert pydantic.backoff_strategy == "exponential"
        assert pydantic.base_delay == 1.0
        assert pydantic.max_delay == 60.0

    def test_rate_limit_consistency(self):
        """RateLimitConfig and RateLimitSettings share their defaults."""
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
        """SecurityConfig and SecuritySettings share their defaults."""
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
    Consistency between VALIDATION_RULES in core/safe_defaults.py and the
    Pydantic Field constraints.
    """

    def test_circuit_breaker_validation_rules(self):
        """CircuitBreakerSettings constraints match VALIDATION_RULES."""
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
        """DLQSettings constraints match VALIDATION_RULES."""
        from baldur.core.safe_defaults import VALIDATION_RULES
        from baldur.settings.dlq import DLQSettings

        schema = DLQSettings.model_json_schema()
        props = schema["properties"]
        rules = VALIDATION_RULES["dlq"]

        # retention_days: (1, 365)
        assert props["retention_days"]["minimum"] == rules["retention_days"][0]
        assert props["retention_days"]["maximum"] == rules["retention_days"][1]

    def test_retry_validation_rules(self):
        """RetrySettings constraints match VALIDATION_RULES."""
        from baldur.core.safe_defaults import VALIDATION_RULES
        from baldur.settings.retry import RetrySettings

        retry_schema = RetrySettings.model_json_schema()
        retry_props = retry_schema["properties"]
        rules = VALIDATION_RULES["retry"]

        # max_attempts: (1, 20)
        assert retry_props["max_attempts"]["minimum"] == rules["max_attempts"][0]
        assert retry_props["max_attempts"]["maximum"] == rules["max_attempts"][1]

    def test_rate_limit_validation_rules(self):
        """RateLimitSettings constraints match VALIDATION_RULES."""
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
        """SecuritySettings constraints match VALIDATION_RULES."""
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
