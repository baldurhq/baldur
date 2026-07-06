"""BulkheadSettings unit tests.

Verifies the behavior of the Pydantic v2 bulkhead settings:
- Default values
- Environment-variable loading
- Validation constraints
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from baldur.settings.bulkhead import (
    BulkheadSettings,
    get_bulkhead_settings,
    reset_bulkhead_settings,
)


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset the singleton before and after each test."""
    reset_bulkhead_settings()
    yield
    reset_bulkhead_settings()


class TestBulkheadSettingsDefaults:
    """Default-value tests."""

    def test_default_database_max_concurrent(self):
        """DATABASE default concurrency."""
        settings = BulkheadSettings()
        assert settings.database_max_concurrent == 10

    def test_default_cache_max_concurrent(self):
        """CACHE default concurrency."""
        settings = BulkheadSettings()
        assert settings.cache_max_concurrent == 20

    def test_default_external_api_settings(self):
        """EXTERNAL_API defaults."""
        settings = BulkheadSettings()
        assert settings.external_api_max_workers == 5
        assert settings.external_api_queue_size == 10

    def test_default_message_queue_max_concurrent(self):
        """MESSAGE_QUEUE default concurrency."""
        settings = BulkheadSettings()
        assert settings.message_queue_max_concurrent == 15

    def test_default_max_concurrent(self):
        """Custom-domain default concurrency."""
        settings = BulkheadSettings()
        assert settings.default_max_concurrent == 10


class TestBulkheadSettingsEnvironmentVariables:
    """Environment-variable loading tests."""

    def test_load_from_env_database_max_concurrent(self):
        """Load DATABASE concurrency from an environment variable."""
        with patch.dict(os.environ, {"BALDUR_BULKHEAD_DATABASE_MAX_CONCURRENT": "25"}):
            settings = BulkheadSettings()
            assert settings.database_max_concurrent == 25

    def test_load_from_env_cache_max_concurrent(self):
        """Load CACHE concurrency from an environment variable."""
        with patch.dict(os.environ, {"BALDUR_BULKHEAD_CACHE_MAX_CONCURRENT": "50"}):
            settings = BulkheadSettings()
            assert settings.cache_max_concurrent == 50

    def test_load_from_env_external_api_workers(self):
        """Load EXTERNAL_API worker count from an environment variable."""
        with patch.dict(os.environ, {"BALDUR_BULKHEAD_EXTERNAL_API_MAX_WORKERS": "15"}):
            settings = BulkheadSettings()
            assert settings.external_api_max_workers == 15


class TestBulkheadSettingsValidation:
    """Validation tests."""

    def test_database_max_concurrent_min_value(self):
        """DATABASE concurrency minimum-value validation."""
        with pytest.raises(ValueError):
            BulkheadSettings(database_max_concurrent=0)

    def test_database_max_concurrent_max_value(self):
        """DATABASE concurrency maximum-value validation."""
        with pytest.raises(ValueError):
            BulkheadSettings(database_max_concurrent=101)

    def test_cache_max_concurrent_max_value(self):
        """CACHE concurrency maximum-value validation."""
        with pytest.raises(ValueError):
            BulkheadSettings(cache_max_concurrent=201)

    def test_external_api_max_workers_min_value(self):
        """EXTERNAL_API worker-count minimum-value validation."""
        with pytest.raises(ValueError):
            BulkheadSettings(external_api_max_workers=0)


class TestBulkheadSettingsMultiInstance:
    """Multi-instance settings tests."""

    def test_default_database_aliases(self):
        """Default DB-alias settings."""
        settings = BulkheadSettings()
        assert "default" in settings.database_aliases
        assert "replica" in settings.database_aliases
        assert settings.database_aliases["default"] == 10
        assert settings.database_aliases["replica"] == 15

    def test_default_cache_instances(self):
        """Default cache-instance settings."""
        settings = BulkheadSettings()
        assert "default" in settings.cache_instances
        assert "session" in settings.cache_instances
        assert settings.cache_instances["default"] == 20
        assert settings.cache_instances["session"] == 10

    def test_custom_database_aliases(self):
        """Custom DB-alias settings."""
        settings = BulkheadSettings(database_aliases={"default": 20, "analytics": 30})
        assert settings.database_aliases["default"] == 20
        assert settings.database_aliases["analytics"] == 30


class TestBulkheadSettingsSingleton:
    """Singleton tests."""

    def test_get_bulkhead_settings_returns_same_instance(self):
        """The singleton accessor returns the same instance."""
        settings1 = get_bulkhead_settings()
        settings2 = get_bulkhead_settings()
        assert settings1 is settings2

    def test_reset_clears_singleton(self):
        """reset() clears the singleton."""
        settings1 = get_bulkhead_settings()
        reset_bulkhead_settings()
        settings2 = get_bulkhead_settings()
        assert settings1 is not settings2


class TestBulkheadSettingsContract:
    """615 D5 Contract: the Prometheus metrics-updater fields gate and tune the
    BulkheadMetricsUpdater that ``baldur.init()`` auto-starts on every framework.

    Hardcoded against the 615 D5 settings table:
      BALDUR_BULKHEAD_METRICS_ENABLED        bool   default True
      BALDUR_BULKHEAD_METRICS_UPDATE_INTERVAL float  default 10.0  ge=1.0 le=300.0
    """

    def test_metrics_enabled_defaults_true(self):
        """Default True preserves today's unconditional Django start."""
        assert BulkheadSettings().metrics_enabled is True

    def test_metrics_update_interval_defaults_to_ten_seconds(self):
        assert BulkheadSettings().metrics_update_interval == 10.0

    def test_metrics_enabled_loads_from_env(self):
        with patch.dict(os.environ, {"BALDUR_BULKHEAD_METRICS_ENABLED": "false"}):
            assert BulkheadSettings().metrics_enabled is False

    def test_metrics_update_interval_loads_from_env(self):
        with patch.dict(
            os.environ, {"BALDUR_BULKHEAD_METRICS_UPDATE_INTERVAL": "30.0"}
        ):
            assert BulkheadSettings().metrics_update_interval == 30.0

    def test_metrics_update_interval_at_lower_bound_is_accepted(self):
        """Boundary: ge=1.0 — exactly 1.0 passes."""
        assert BulkheadSettings(
            metrics_update_interval=1.0
        ).metrics_update_interval == (1.0)

    def test_metrics_update_interval_below_lower_bound_is_rejected(self):
        """Boundary: just below ge=1.0 fails."""
        with pytest.raises(ValueError):
            BulkheadSettings(metrics_update_interval=0.9)

    def test_metrics_update_interval_at_upper_bound_is_accepted(self):
        """Boundary: le=300.0 — exactly 300.0 passes (the staleness-detection
        cap that keeps the DaemonWorkerHandle watchdog tick meaningful)."""
        assert (
            BulkheadSettings(metrics_update_interval=300.0).metrics_update_interval
            == 300.0
        )

    def test_metrics_update_interval_above_upper_bound_is_rejected(self):
        """Boundary: just above le=300.0 fails."""
        with pytest.raises(ValueError):
            BulkheadSettings(metrics_update_interval=300.1)
