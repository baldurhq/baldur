"""
Tests for Circuit Breaker Config

Covers:
- CircuitState constants
- CircuitBreakerConfig dataclass
- from_settings class method
- CircuitBreakerResult
- CircuitBreakerFallbackResult
"""

from dataclasses import asdict
from unittest.mock import MagicMock, patch

import pytest


class TestCircuitState:
    """Tests for CircuitState constants."""

    def test_closed_state_value(self):
        """Test CLOSED state value."""
        from baldur.services.circuit_breaker.config import CircuitState

        assert CircuitState.CLOSED == "closed"

    def test_open_state_value(self):
        """Test OPEN state value."""
        from baldur.services.circuit_breaker.config import CircuitState

        assert CircuitState.OPEN == "open"

    def test_half_open_state_value(self):
        """Test HALF_OPEN state value."""
        from baldur.services.circuit_breaker.config import CircuitState

        assert CircuitState.HALF_OPEN == "half_open"


class TestCircuitBreakerConfig:
    """Tests for CircuitBreakerConfig dataclass."""

    def test_default_values(self):
        """Test default configuration values."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig

        config = CircuitBreakerConfig()

        assert config.enabled is False
        assert config.failure_threshold == 5
        assert config.recovery_timeout == 60
        assert config.success_threshold == 2
        assert config.minimum_calls == 10
        assert config.sliding_window_size == 100
        assert config.failure_rate_threshold == 50.0
        assert config.fallback_strategy == "cache"

    def test_custom_values(self):
        """Test custom configuration values."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig

        config = CircuitBreakerConfig(
            enabled=True,
            failure_threshold=10,
            recovery_timeout=120,
            success_threshold=5,
        )

        assert config.enabled is True
        assert config.failure_threshold == 10
        assert config.recovery_timeout == 120
        assert config.success_threshold == 5

    def test_error_budget_integration_settings(self):
        """Test error budget integration configuration."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig

        config = CircuitBreakerConfig()
        assert config.cb_open_burn_rate_multiplier == 10.0

    def test_governance_parameters(self):
        """Test governance parameters."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig

        config = CircuitBreakerConfig()
        assert config.manual_override_ttl_minutes == 90
        assert config.half_open_max_calls == 3
        assert config.max_pending_duration_hours == 4
        assert config.max_retry_lifetime_hours == 24

    def test_rate_limit_cascade_settings(self):
        """Test rate limit cascade detection settings."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig

        config = CircuitBreakerConfig()
        assert config.rate_limit_cascade_threshold == 10
        assert config.rate_limit_cascade_window_seconds == 60

    def test_self_ddos_protection_settings(self):
        """Test self-DDoS protection settings."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig

        config = CircuitBreakerConfig()
        assert config.self_ddos_protection_enabled is True
        assert config.self_ddos_rps_limit == 200
        assert config.self_ddos_window_seconds == 10
        assert config.self_ddos_backoff_multiplier == 2.0

    def test_from_settings_fallback(self):
        """Test from_settings with fallback to defaults."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig

        # Patch to simulate runtime config not available
        with patch.dict("sys.modules", {"baldur_pro.services.runtime_config": None}):
            # Should use core config fallback
            config = CircuitBreakerConfig.from_settings()
            assert config is not None

    def test_from_settings_with_runtime_config(self):
        """Test from_settings with runtime config."""
        pytest.importorskip("baldur_pro")
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig

        mock_manager = MagicMock()
        mock_manager.get_circuit_breaker_config.return_value = {
            "enabled": True,
            "failure_threshold": 15,
            "recovery_timeout": 90,
        }

        with patch(
            "baldur_pro.services.runtime_config.get_runtime_config_manager",
            return_value=mock_manager,
        ):
            config = CircuitBreakerConfig.from_settings()
            assert config.enabled is True
            assert config.failure_threshold == 15
            assert config.recovery_timeout == 90


class TestSelfDdosBackoffDriftContract:
    """687 D9 (G1) — CircuitBreakerConfig dataclass defaults must not drift from
    the CircuitBreakerSettings field defaults for the self-DDoS backoff trio.

    Compares two *source-of-truth* defaults (dataclass mirror vs settings model),
    not an instance against its own field default, so this is a genuine drift
    guard rather than the tautology UNIT_TEST_GUIDELINES §9 forbids.
    """

    def test_dataclass_defaults_match_settings_defaults(self):
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        config = CircuitBreakerConfig()
        settings_fields = CircuitBreakerSettings.model_fields
        for name in (
            "self_ddos_backoff_base_seconds",
            "self_ddos_backoff_max_seconds",
            "self_ddos_backoff_jitter_factor",
        ):
            assert getattr(config, name) == settings_fields[name].default


class TestCircuitBreakerConfigMappingBehavior:
    """687 D9 (G1) — a non-default self-DDoS backoff value must survive both
    ``from_settings()`` mapping paths (runtime-dict and static-settings).

    Default-equality alone cannot catch a dropped mapping line (both defaults
    stay equal while an operator's override is silently ignored); pushing a
    non-default value through each path proves the wiring end-to-end.
    """

    def test_runtime_dict_path_propagates_non_default_backoff(self):
        # Given: the runtime config manager returns a non-default backoff base
        from baldur.factory.registry import ProviderRegistry
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig

        mock_manager = MagicMock()
        mock_manager.get_circuit_breaker_config.return_value = {
            "self_ddos_backoff_base_seconds": 2.5,
            "self_ddos_backoff_max_seconds": 45.0,
            "self_ddos_backoff_jitter_factor": 0.4,
        }

        # When: from_settings resolves through the runtime-dict path
        with patch.object(
            ProviderRegistry.runtime_config_manager,
            "safe_get",
            return_value=mock_manager,
        ):
            config = CircuitBreakerConfig.from_settings()

        # Then: the non-default values land on the dataclass
        assert config.self_ddos_backoff_base_seconds == 2.5
        assert config.self_ddos_backoff_max_seconds == 45.0
        assert config.self_ddos_backoff_jitter_factor == 0.4

    def test_static_settings_path_propagates_non_default_backoff(self):
        # Given: no runtime manager (forces the static fallback) and a
        # CircuitBreakerSettings carrying a non-default backoff base
        from baldur.factory.registry import ProviderRegistry
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        cb_settings = CircuitBreakerSettings(
            self_ddos_backoff_base_seconds=2.5,
            self_ddos_backoff_max_seconds=45.0,
            self_ddos_backoff_jitter_factor=0.4,
        )
        mock_config = MagicMock()
        mock_config.core.circuit_breaker = cb_settings

        # When: from_settings falls through to the static-settings path
        with (
            patch.object(
                ProviderRegistry.runtime_config_manager, "safe_get", return_value=None
            ),
            patch(
                "baldur.services.circuit_breaker.config.get_config",
                return_value=mock_config,
            ),
        ):
            config = CircuitBreakerConfig.from_settings()

        # Then: the non-default values propagate through the static mapping
        assert config.self_ddos_backoff_base_seconds == 2.5
        assert config.self_ddos_backoff_max_seconds == 45.0
        assert config.self_ddos_backoff_jitter_factor == 0.4


class TestCircuitBreakerResult:
    """Tests for CircuitBreakerResult dataclass."""

    def test_succeeded_result(self):
        """Test creating a succeeded result."""
        from baldur.services.circuit_breaker.config import CircuitBreakerResult

        result = CircuitBreakerResult.succeeded(
            service_name="test_service",
            previous_state="closed",
            new_state="open",
            message="Circuit opened",
        )

        assert result.success is True
        assert result.service_name == "test_service"
        assert result.previous_state == "closed"
        assert result.new_state == "open"
        assert result.message == "Circuit opened"
        assert result.error is None

    def test_failed_result(self):
        """Test creating a failed result."""
        from baldur.services.circuit_breaker.config import CircuitBreakerResult

        result = CircuitBreakerResult.failed(
            service_name="test_service",
            error="Connection failed",
        )

        assert result.success is False
        assert result.service_name == "test_service"
        assert result.error == "Connection failed"

    def test_result_dataclass_fields(self):
        """Test result dataclass fields."""
        from baldur.services.circuit_breaker.config import CircuitBreakerResult

        result = CircuitBreakerResult.succeeded(
            service_name="test_service",
            previous_state="closed",
            new_state="open",
            message="Test",
        )

        d = asdict(result)
        assert isinstance(d, dict)
        assert d["success"] is True
        assert d["service_name"] == "test_service"


class TestCircuitBreakerFallbackResult:
    """Tests for CircuitBreakerFallbackResult dataclass."""

    def test_fallback_result_allow(self):
        """Test CircuitBreakerFallbackResult.allow() factory."""
        from baldur.services.circuit_breaker.config import (
            CircuitBreakerFallbackResult,
        )

        result = CircuitBreakerFallbackResult.allow()

        assert result.allowed is True
        assert result.fallback_used is False

    def test_fallback_result_block(self):
        """Test CircuitBreakerFallbackResult.block() factory."""
        from baldur.services.circuit_breaker.config import (
            CircuitBreakerFallbackResult,
        )

        result = CircuitBreakerFallbackResult.block(message="Circuit breaker is open")

        assert result.allowed is False
        assert result.fallback_used is False
        assert result.message == "Circuit breaker is open"

    def test_fallback_result_from_cache(self):
        """Test CircuitBreakerFallbackResult.from_cache() factory."""
        from baldur.services.circuit_breaker.config import (
            CircuitBreakerFallbackResult,
        )

        cached_data = {"data": "cached"}
        result = CircuitBreakerFallbackResult.from_cache(cached_data)

        assert result.allowed is False
        assert result.fallback_used is True
        assert result.fallback_type == "cache"
        assert result.fallback_data == cached_data

    def test_fallback_result_to_dlq(self):
        """Test CircuitBreakerFallbackResult.to_dlq() factory."""
        from baldur.services.circuit_breaker.config import (
            CircuitBreakerFallbackResult,
        )

        result = CircuitBreakerFallbackResult.to_dlq()

        assert result.allowed is False
        assert result.fallback_used is True
        assert result.fallback_type == "dlq"

    def test_fallback_result_default_response(self):
        """Test CircuitBreakerFallbackResult.default_response() factory."""
        from baldur.services.circuit_breaker.config import (
            CircuitBreakerFallbackResult,
        )

        default_data = {"status": "unavailable"}
        result = CircuitBreakerFallbackResult.default_response(default_data)

        assert result.allowed is False
        assert result.fallback_used is True
        assert result.fallback_type == "default"
        assert result.fallback_data == default_data


class TestFallbackStrategies:
    """Tests for fallback strategy options."""

    def test_valid_fallback_strategies(self):
        """Test valid fallback strategy values."""
        valid_strategies = ["block", "cache", "dlq", "default_response"]

        from baldur.services.circuit_breaker.config import CircuitBreakerConfig

        for strategy in valid_strategies:
            config = CircuitBreakerConfig(fallback_strategy=strategy)
            assert config.fallback_strategy == strategy

    def test_fallback_cache_ttl_default(self):
        """Test default fallback cache TTL."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig

        config = CircuitBreakerConfig()
        assert config.fallback_cache_ttl_seconds == 300  # 5 minutes


class TestCircuitBreakerConfigBehavior:
    """719 D7 — the three rate-trigger fields resolve through settings.

    ``from_settings`` used to read them via ``getattr(cb_settings, name,
    <literal>)``. The literals silently won whenever the settings model lacked
    the field, so ``BALDUR_CB_FAILURE_RATE_THRESHOLD`` had no effect at all.
    These tests fail if a fallback is reintroduced.
    """

    @pytest.fixture(autouse=True)
    def _reset_settings(self):
        from baldur.settings.circuit_breaker import reset_circuit_breaker_settings
        from baldur.settings.root import reset_config

        reset_circuit_breaker_settings()
        reset_config()
        yield
        reset_circuit_breaker_settings()
        reset_config()

    @staticmethod
    def _from_static_settings():
        """Resolve the config through the static-settings path.

        The runtime-config path belongs to PRO and is covered separately; this
        forces the OSS branch so the env override is what is being measured.
        """
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig

        with patch.dict("sys.modules", {"baldur_pro.services.runtime_config": None}):
            return CircuitBreakerConfig.from_settings()

    def test_failure_rate_threshold_env_override_reaches_the_config(self, monkeypatch):
        """BALDUR_CB_FAILURE_RATE_THRESHOLD=0 disables the rate trigger."""
        from baldur.settings.circuit_breaker import reset_circuit_breaker_settings
        from baldur.settings.root import reset_config

        monkeypatch.setenv("BALDUR_CB_FAILURE_RATE_THRESHOLD", "0")
        reset_circuit_breaker_settings()
        reset_config()

        assert self._from_static_settings().failure_rate_threshold == 0.0

    def test_sliding_window_size_env_override_reaches_the_config(self, monkeypatch):
        """BALDUR_CB_SLIDING_WINDOW_SIZE resizes the outcome window."""
        from baldur.settings.circuit_breaker import reset_circuit_breaker_settings
        from baldur.settings.root import reset_config

        monkeypatch.setenv("BALDUR_CB_SLIDING_WINDOW_SIZE", "250")
        reset_circuit_breaker_settings()
        reset_config()

        assert self._from_static_settings().sliding_window_size == 250

    def test_minimum_calls_env_override_reaches_the_config(self, monkeypatch):
        """BALDUR_CB_MINIMUM_CALLS moves the rate trigger's traffic gate."""
        from baldur.settings.circuit_breaker import reset_circuit_breaker_settings
        from baldur.settings.root import reset_config

        monkeypatch.setenv("BALDUR_CB_MINIMUM_CALLS", "3")
        reset_circuit_breaker_settings()
        reset_config()

        assert self._from_static_settings().minimum_calls == 3

    def test_unset_env_yields_the_settings_defaults(self):
        """Without overrides the config matches the settings model's defaults."""
        from baldur.settings.circuit_breaker import CircuitBreakerSettings

        config = self._from_static_settings()
        settings = CircuitBreakerSettings()

        assert config.failure_rate_threshold == settings.failure_rate_threshold
        assert config.sliding_window_size == settings.sliding_window_size
        assert config.minimum_calls == settings.minimum_calls
