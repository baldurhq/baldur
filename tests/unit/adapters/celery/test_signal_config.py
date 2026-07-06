"""
Unit tests for SignalHooksSettings and domain/service resolution helpers.

Tests configuration defaults, singleton lifecycle, environment variable
override, domain extraction priority, and service name detection.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from baldur.adapters.celery.signal_config import (
    _DEFAULT_DOMAIN_PATTERNS,
    SignalHooksSettings,
    extract_domain_from_task_name,
    extract_service_name,
    get_signal_hooks_settings,
    reset_signal_hooks_settings,
)

# =========================================================================
# Contract Tests
# =========================================================================


class TestSignalHooksSettingsContract:
    """Design-contract values for SignalHooksSettings defaults."""

    def test_enabled_default_is_true(self) -> None:
        """Master switch default: True."""
        settings = SignalHooksSettings()
        assert settings.enabled is True

    def test_cb_enabled_default_is_true(self) -> None:
        """Circuit breaker toggle default: True."""
        settings = SignalHooksSettings()
        assert settings.cb_enabled is True

    def test_dlq_enabled_default_is_true(self) -> None:
        """DLQ toggle default: True."""
        settings = SignalHooksSettings()
        assert settings.dlq_enabled is True

    def test_metrics_enabled_default_is_true(self) -> None:
        """Metrics toggle default: True."""
        settings = SignalHooksSettings()
        assert settings.metrics_enabled is True

    def test_forensics_enabled_default_is_true(self) -> None:
        """Forensics toggle default: True."""
        settings = SignalHooksSettings()
        assert settings.forensics_enabled is True

    def test_cb_failure_threshold_default_is_5(self) -> None:
        """CB failure threshold default: 5."""
        settings = SignalHooksSettings()
        assert settings.cb_failure_threshold == 5

    def test_cb_recovery_timeout_default_is_60(self) -> None:
        """CB recovery timeout default: 60."""
        settings = SignalHooksSettings()
        assert settings.cb_recovery_timeout == 60

    def test_cb_success_threshold_default_is_2(self) -> None:
        """CB success threshold default: 2."""
        settings = SignalHooksSettings()
        assert settings.cb_success_threshold == 2

    def test_excluded_tasks_has_6_entries(self) -> None:
        """Default excluded_tasks contains exactly 6 entries."""
        settings = SignalHooksSettings()
        assert len(settings.excluded_tasks) == 6

    def test_excluded_tasks_contains_celery_builtin_tasks(self) -> None:
        """Default excluded_tasks includes Celery built-in tasks."""
        settings = SignalHooksSettings()
        assert "celery.backend_cleanup" in settings.excluded_tasks
        assert "celery.chord_unlock" in settings.excluded_tasks

    def test_default_domain_patterns_has_8_entries(self) -> None:
        """_DEFAULT_DOMAIN_PATTERNS has 8 domain entries."""
        assert len(_DEFAULT_DOMAIN_PATTERNS) == 8

    def test_default_domain_patterns_keys(self) -> None:
        """_DEFAULT_DOMAIN_PATTERNS includes all expected domains."""
        expected_domains = {
            "payment",
            "order",
            "inventory",
            "notification",
            "user",
            "cart",
            "shipping",
            "refund",
        }
        assert set(_DEFAULT_DOMAIN_PATTERNS.keys()) == expected_domains


# =========================================================================
# Behavior Tests — extract_domain_from_task_name
# =========================================================================


class TestExtractDomainFromTaskNameBehavior:
    """Domain extraction priority and pattern matching behavior."""

    @pytest.fixture
    def default_config(self) -> SignalHooksSettings:
        """Fresh settings instance with defaults."""
        return SignalHooksSettings()

    def test_explicit_mapping_takes_priority(
        self, default_config: SignalHooksSettings
    ) -> None:
        """Explicit task_domain_mapping overrides pattern matching."""
        config = SignalHooksSettings(
            task_domain_mapping={"myapp.tasks.do_work": "custom_domain"},
        )
        result = extract_domain_from_task_name("myapp.tasks.do_work", config)
        assert result == "custom_domain"

    def test_pattern_matching_detects_payment_domain(
        self, default_config: SignalHooksSettings
    ) -> None:
        """Task name containing 'payment' maps to 'payment' domain."""
        result = extract_domain_from_task_name(
            "myapp.tasks.process_payment",
            default_config,
        )
        assert result == "payment"

    def test_pattern_matching_detects_order_domain(
        self, default_config: SignalHooksSettings
    ) -> None:
        """Task name containing 'order' maps to 'order' domain."""
        result = extract_domain_from_task_name(
            "shop.tasks.create_order",
            default_config,
        )
        assert result == "order"

    def test_pattern_matching_detects_notification_domain(
        self, default_config: SignalHooksSettings
    ) -> None:
        """Task name containing 'email' maps to 'notification' domain."""
        result = extract_domain_from_task_name(
            "comms.tasks.send_email",
            default_config,
        )
        assert result == "notification"

    def test_fallback_uses_first_meaningful_segment(
        self, default_config: SignalHooksSettings
    ) -> None:
        """Task name with no matching pattern falls back to first meaningful segment."""
        result = extract_domain_from_task_name(
            "analytics.tasks.compute_stats",
            default_config,
        )
        assert result == "analytics"

    def test_fallback_skips_noise_segments(
        self, default_config: SignalHooksSettings
    ) -> None:
        """Segments like 'tasks', 'celery', 'app' are skipped in fallback."""
        result = extract_domain_from_task_name(
            "tasks.myservice.run",
            default_config,
        )
        assert result == "myservice"

    def test_unrecognizable_single_segment_returns_unknown(
        self, default_config: SignalHooksSettings
    ) -> None:
        """Single-segment task name that matches nothing returns 'unknown'."""
        result = extract_domain_from_task_name("tasks", default_config)
        assert result == "unknown"

    def test_custom_domain_patterns_override_defaults(self) -> None:
        """When domain_patterns is set, it overrides _DEFAULT_DOMAIN_PATTERNS."""
        config = SignalHooksSettings(
            domain_patterns={"custom": ["xyz"]},
        )
        result = extract_domain_from_task_name("service.tasks.xyz_handler", config)
        assert result == "custom"

    def test_custom_domain_patterns_no_default_fallback(self) -> None:
        """When domain_patterns is set, default patterns are not used."""
        config = SignalHooksSettings(
            domain_patterns={"custom": ["xyz"]},
        )
        # 'payment' pattern is only in defaults, not in custom
        result = extract_domain_from_task_name(
            "myapp.tasks.process_payment",
            config,
        )
        # Should fall back to first meaningful segment, not match 'payment'
        assert result == "myapp"


# =========================================================================
# Behavior Tests — extract_service_name
# =========================================================================


class TestExtractServiceNameBehavior:
    """Service name extraction based on exception and task name."""

    @pytest.fixture
    def default_config(self) -> SignalHooksSettings:
        """Fresh settings instance with defaults."""
        return SignalHooksSettings()

    def test_redis_connection_error_returns_redis(
        self, default_config: SignalHooksSettings
    ) -> None:
        """Exception message containing 'redis' returns 'redis' service."""
        exc = ConnectionError("Redis connection refused")
        result = extract_service_name("app.tasks.cache_update", default_config, exc)
        assert result == "redis"

    def test_timeout_exception_returns_external_timeout(
        self, default_config: SignalHooksSettings
    ) -> None:
        """Exception message containing 'timeout' returns 'external_timeout'."""
        exc = TimeoutError("Request timeout after 30s")
        result = extract_service_name("app.tasks.fetch_data", default_config, exc)
        assert result == "external_timeout"

    def test_connection_error_returns_external_connection(
        self, default_config: SignalHooksSettings
    ) -> None:
        """Exception message containing 'connection' returns 'external_connection'."""
        exc = Exception("connection reset by peer")
        result = extract_service_name("app.tasks.sync", default_config, exc)
        assert result == "external_connection"

    def test_payment_gateway_exception_returns_payment_gateway(
        self,
        default_config: SignalHooksSettings,
    ) -> None:
        """Exception message containing 'gateway' returns 'payment_gateway'."""
        exc = Exception("Payment gateway error")
        result = extract_service_name("app.tasks.charge", default_config, exc)
        assert result == "payment_gateway"

    def test_no_exception_falls_back_to_domain_extraction(
        self,
        default_config: SignalHooksSettings,
    ) -> None:
        """Without exception, service name falls back to domain extraction."""
        result = extract_service_name(
            "myapp.tasks.process_order",
            default_config,
            None,
        )
        assert result == "order"

    def test_unrecognized_exception_falls_back_to_domain(
        self,
        default_config: SignalHooksSettings,
    ) -> None:
        """Unrecognized exception message falls back to domain extraction."""
        exc = ValueError("some internal bug")
        result = extract_service_name(
            "myapp.tasks.process_order",
            default_config,
            exc,
        )
        assert result == "order"


# =========================================================================
# Behavior Tests — Singleton
# =========================================================================


class TestSignalHooksSettingsSingletonBehavior:
    """Singleton lifecycle for get/reset_signal_hooks_settings."""

    def setup_method(self) -> None:
        """Clear singleton before each test."""
        reset_signal_hooks_settings()

    def teardown_method(self) -> None:
        """Clear singleton after each test."""
        reset_signal_hooks_settings()

    def test_get_returns_same_instance(self) -> None:
        """get_signal_hooks_settings returns the same cached instance."""
        first = get_signal_hooks_settings()
        second = get_signal_hooks_settings()
        assert first is second

    def test_reset_clears_cached_instance(self) -> None:
        """reset_signal_hooks_settings clears the cached singleton."""
        first = get_signal_hooks_settings()
        reset_signal_hooks_settings()
        second = get_signal_hooks_settings()
        assert first is not second


# =========================================================================
# Behavior Tests — Environment Variable Override
# =========================================================================


class TestSignalHooksSettingsEnvOverrideBehavior:
    """Environment variable overrides for SignalHooksSettings."""

    def setup_method(self) -> None:
        """Clear singleton before each test."""
        reset_signal_hooks_settings()

    def teardown_method(self) -> None:
        """Clear singleton after each test."""
        reset_signal_hooks_settings()

    def test_env_var_disables_master_switch(self) -> None:
        """BALDUR_SIGNAL_HOOKS_ENABLED=false disables the master switch."""
        with patch.dict(os.environ, {"BALDUR_SIGNAL_HOOKS_ENABLED": "false"}):
            settings = SignalHooksSettings()
            assert settings.enabled is False

    def test_env_var_overrides_cb_failure_threshold(self) -> None:
        """BALDUR_SIGNAL_HOOKS_CB_FAILURE_THRESHOLD overrides default."""
        with patch.dict(
            os.environ,
            {"BALDUR_SIGNAL_HOOKS_CB_FAILURE_THRESHOLD": "10"},
        ):
            settings = SignalHooksSettings()
            assert settings.cb_failure_threshold == 10

    def test_env_var_disables_dlq(self) -> None:
        """BALDUR_SIGNAL_HOOKS_DLQ_ENABLED=false disables DLQ."""
        with patch.dict(os.environ, {"BALDUR_SIGNAL_HOOKS_DLQ_ENABLED": "false"}):
            settings = SignalHooksSettings()
            assert settings.dlq_enabled is False


# =========================================================================
# Boundary Tests
# =========================================================================


class TestSignalHooksSettingsBoundaryBehavior:
    """Boundary validation for CB threshold fields."""

    def test_cb_failure_threshold_minimum_boundary(self) -> None:
        """cb_failure_threshold minimum boundary: ge=1."""
        # Boundary value (1) -> success
        settings = SignalHooksSettings(cb_failure_threshold=1)
        assert settings.cb_failure_threshold == 1

        # Below boundary (0) -> failure
        with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
            SignalHooksSettings(cb_failure_threshold=0)

    def test_cb_recovery_timeout_minimum_boundary(self) -> None:
        """cb_recovery_timeout minimum boundary: ge=1."""
        settings = SignalHooksSettings(cb_recovery_timeout=1)
        assert settings.cb_recovery_timeout == 1

        with pytest.raises(Exception):  # noqa: B017
            SignalHooksSettings(cb_recovery_timeout=0)

    def test_cb_success_threshold_minimum_boundary(self) -> None:
        """cb_success_threshold minimum boundary: ge=1."""
        settings = SignalHooksSettings(cb_success_threshold=1)
        assert settings.cb_success_threshold == 1

        with pytest.raises(Exception):  # noqa: B017
            SignalHooksSettings(cb_success_threshold=0)
