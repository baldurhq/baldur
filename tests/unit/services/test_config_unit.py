"""
Tests for migrated config module classes.

Tests for NotificationSettings, ForensicSettings, EventLoggingConfig,
MetricsSettings, L2StorageSettings, L2StorageRuntimeConfig,
ConfigDriftMonitor — all migrated from config.py to settings/.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from baldur.settings.drift_monitor import (
    ConfigDriftMonitor,
    reset_config_drift_monitor,
)
from baldur.settings.event_logging import (
    EventLoggingConfig,
    reset_event_logging_config,
)
from baldur.settings.forensic import ForensicSettings
from baldur.settings.l2_storage import (
    L2StorageRuntimeConfig,
    L2StorageSettings,
    reset_l2_storage_runtime_config,
)
from baldur.settings.metrics import MetricsSettings
from baldur.settings.notification import NotificationSettings

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset singletons before and after each test."""
    # Reset EventLoggingConfig
    reset_event_logging_config()

    # Reset L2StorageRuntimeConfig
    reset_l2_storage_runtime_config()

    # Reset ConfigDriftMonitor
    reset_config_drift_monitor()

    yield

    reset_event_logging_config()
    reset_l2_storage_runtime_config()
    reset_config_drift_monitor()


# =============================================================================
# NotificationSettings Tests (was NotificationLimits)
# =============================================================================


class TestNotificationLimits:
    """NotificationSettings BaseSettings tests."""

    def test_default_values(self):
        """Default values are correctly initialized."""
        assert (
            NotificationSettings.model_fields["description_max_length"].default == 500
        )
        assert (
            NotificationSettings.model_fields["notification_timeout_seconds"].default
            == 10
        )


# =============================================================================
# ForensicSettings Tests (was ForensicContextConfig)
# =============================================================================


class TestForensicContextConfig:
    """ForensicSettings BaseSettings tests."""

    def test_default_values(self):
        """Default values are correctly initialized."""
        config = ForensicSettings()
        assert config.max_stack_frames == 50
        assert config.mask_sensitive_fields is True
        assert config.collect_request_body is False

    def test_sensitive_field_patterns(self):
        """Sensitive field patterns are correctly configured."""
        config = ForensicSettings()
        assert "password" in config.sensitive_field_patterns
        assert "token" in config.sensitive_field_patterns
        assert "card_number" in config.sensitive_field_patterns

    def test_get_forensic_settings_from_env(self):
        """Environment variables are loaded correctly."""
        env = {"BALDUR_FORENSIC_MAX_STACK_FRAMES": "100"}
        with patch.dict(os.environ, env):
            config = ForensicSettings()
            assert config.max_stack_frames == 100


# =============================================================================
# EventLoggingConfig Tests
# =============================================================================


class TestEventLoggingConfig:
    """EventLoggingConfig singleton tests."""

    def test_singleton(self):
        """Two instances are the same object."""
        c1 = EventLoggingConfig()
        c2 = EventLoggingConfig()
        assert c1 is c2

    def test_default_log_levels(self):
        """Default log levels are correct."""
        config = EventLoggingConfig()
        assert config.get_dlq_log_level() == "INFO"
        assert config.get_cb_log_level() == "WARNING"
        assert config.get_replay_log_level() == "INFO"
        assert config.get_sla_log_level() == "WARNING"

    def test_update_log_levels(self):
        """Log levels can be changed at runtime."""
        config = EventLoggingConfig()
        config.update(dlq_log_level="DEBUG", cb_log_level="ERROR")
        assert config.get_dlq_log_level() == "DEBUG"
        assert config.get_cb_log_level() == "ERROR"

    def test_update_invalid_level_raises(self):
        """Invalid log levels raise ValueError."""
        config = EventLoggingConfig()
        with pytest.raises(ValueError, match="Invalid log level"):
            config.update(dlq_log_level="INVALID")

    def test_reset(self):
        """reset() clears runtime settings."""
        config = EventLoggingConfig()
        config.update(dlq_log_level="DEBUG")
        config.reset()
        assert config.get_dlq_log_level() == "INFO"

    def test_to_dict(self):
        """to_dict() includes all expected keys."""
        config = EventLoggingConfig()
        d = config.to_dict()
        assert "dlq_log_level" in d
        assert "cb_log_level" in d
        assert "replay_log_level" in d
        assert "sla_log_level" in d
        assert "last_updated" in d

    def test_get_log_level_int(self):
        """String log levels convert to integers."""
        import logging

        config = EventLoggingConfig()
        assert config.get_log_level_int("INFO") == logging.INFO
        assert config.get_log_level_int("ERROR") == logging.ERROR

    def test_update_records_audit_trail(self):
        """update() records audit information in last_updated."""
        config = EventLoggingConfig()
        result = config.update(dlq_log_level="DEBUG", updated_by="admin")
        assert result["last_updated"]["updated_by"] == "admin"
        assert "timestamp" in result["last_updated"]

    def test_env_override(self):
        """Environment variables override hardcoded defaults."""
        config = EventLoggingConfig()
        config.reset()
        # env_defaults are set during __init__, so manually override for test
        config._env_defaults["dlq_log_level"] = "ERROR"
        assert config.get_dlq_log_level() == "ERROR"


# =============================================================================
# MetricsSettings Tests (was MetricCollectionSettings)
# =============================================================================


class TestMetricCollectionSettings:
    """MetricsSettings BaseSettings tests."""

    def test_default_values(self):
        """Default values are correctly initialized."""
        settings = MetricsSettings()
        assert settings.jitter_enabled is True
        assert settings.adapter_type == "null"

    def test_drift_thresholds(self):
        """Drift thresholds are correctly configured."""
        settings = MetricsSettings()
        assert settings.drift_warning_threshold == 0.05
        assert settings.drift_critical_threshold == 0.20
        assert settings.drift_incident_threshold == 0.50


# =============================================================================
# L2StorageSettings Tests (was L2StorageConfig)
# =============================================================================


class TestL2StorageConfig:
    """L2StorageSettings BaseSettings tests."""

    def test_default_values(self):
        """Default values are correctly initialized (479 D1: redis 200→1000)."""
        config = L2StorageSettings()
        assert config.redis_timeout_ms == 1000
        assert config.database_timeout_ms == 200

    def test_timeout_range_validation(self):
        """Redis timeout range is 10-1000ms."""
        # Valid boundary
        config = L2StorageSettings(redis_timeout_ms=10)
        assert config.redis_timeout_ms == 10

        config = L2StorageSettings(redis_timeout_ms=1000)
        assert config.redis_timeout_ms == 1000

        # Out of range
        with pytest.raises(Exception):
            L2StorageSettings(redis_timeout_ms=5)

        with pytest.raises(Exception):
            L2StorageSettings(redis_timeout_ms=1001)


# =============================================================================
# L2StorageRuntimeConfig Tests
# =============================================================================


class TestL2StorageRuntimeConfig:
    """L2StorageRuntimeConfig singleton tests."""

    def test_singleton(self):
        """Two instances are the same object."""
        c1 = L2StorageRuntimeConfig()
        c2 = L2StorageRuntimeConfig()
        assert c1 is c2

    def test_default_values(self):
        """Default values are correctly initialized (479 D1: redis 200→1000)."""
        config = L2StorageRuntimeConfig()
        assert config.get_redis_timeout_ms() == 1000
        assert config.get_database_timeout_ms() == 200

    def test_update(self):
        """Runtime settings can be changed."""
        config = L2StorageRuntimeConfig()
        config.update(redis_timeout_ms=100)
        assert config.get_redis_timeout_ms() == 100

    def test_update_validation_rejected(self):
        """Out-of-range values are rejected."""
        config = L2StorageRuntimeConfig()
        with pytest.raises(ValueError, match="redis_timeout_ms"):
            config.update(redis_timeout_ms=5)  # min is 10

    def test_reset(self):
        """reset() clears runtime settings (479 D1: default 1000)."""
        config = L2StorageRuntimeConfig()
        config.update(redis_timeout_ms=100)
        config.reset()
        assert config.get_redis_timeout_ms() == 1000

    def test_to_dict(self):
        """to_dict() includes all expected keys."""
        config = L2StorageRuntimeConfig()
        d = config.to_dict()
        assert "redis_timeout_ms" in d
        assert "database_timeout_ms" in d
        assert "last_updated" in d

    def test_get_timeout_for_adapter(self):
        """Adapter-type timeout lookup works correctly (479 D1: redis 1.0s)."""
        config = L2StorageRuntimeConfig()
        assert config.get_timeout_for_adapter("redis") == 1.0


# =============================================================================
# ConfigDriftMonitor Tests
# =============================================================================


class TestConfigDriftMonitor:
    """ConfigDriftMonitor singleton tests."""

    def test_singleton(self):
        """Two instances are the same object."""
        m1 = ConfigDriftMonitor()
        m2 = ConfigDriftMonitor()
        assert m1 is m2

    def test_no_drift_on_first_call(self):
        """First call does not detect drift."""
        monitor = ConfigDriftMonitor()
        result = monitor.check_and_invalidate("test_config", "BALDUR_TEST_")
        assert result is False

    def test_drift_detected_on_env_change(self):
        """Environment variable change triggers drift detection."""
        monitor = ConfigDriftMonitor()

        # First call: record hash
        monitor.check_and_invalidate("test_drift", "BALDUR_DRIFT_THRESHOLD_TEST_")

        # Change environment variable
        with patch.dict(os.environ, {"BALDUR_DRIFT_THRESHOLD_TEST_VALUE": "changed"}):
            result = monitor.check_and_invalidate(
                "test_drift", "BALDUR_DRIFT_THRESHOLD_TEST_"
            )
            assert result is True

    def test_register_cache_function(self):
        """Cache function registration and invalidation works."""
        monitor = ConfigDriftMonitor()
        mock_fn = MagicMock()
        mock_fn.cache_clear = MagicMock()
        monitor.register_cache_function("test_type", mock_fn)

        # First call: record hash
        monitor.check_and_invalidate("test_type", "BALDUR_XXX_")
        # Simulate environment change
        with patch.dict(os.environ, {"BALDUR_XXX_VAL": "new"}):
            monitor.check_and_invalidate("test_type", "BALDUR_XXX_")
            mock_fn.cache_clear.assert_called_once()

    def test_register_plain_callable(self):
        """Plain callable (without cache_clear) is called directly."""
        monitor = ConfigDriftMonitor()
        mock_reset = MagicMock(spec=[])  # spec=[] prevents auto-creating cache_clear
        monitor.register_cache_function("plain_test", mock_reset)

        monitor.check_and_invalidate("plain_test", "BALDUR_PLAIN_")
        with patch.dict(os.environ, {"BALDUR_PLAIN_VAL": "changed"}):
            monitor.check_and_invalidate("plain_test", "BALDUR_PLAIN_")
            mock_reset.assert_called_once()

    def test_get_stats(self):
        """get_stats() returns stored hash values."""
        monitor = ConfigDriftMonitor()
        monitor.check_and_invalidate("stat_test", "BALDUR_STAT_")
        stats = monitor.get_stats()
        assert "stat_test" in stats

    def test_no_drift_same_env(self):
        """No drift detected when environment is unchanged."""
        monitor = ConfigDriftMonitor()
        monitor.check_and_invalidate("stable", "BALDUR_STABLE_")
        result = monitor.check_and_invalidate("stable", "BALDUR_STABLE_")
        assert result is False
