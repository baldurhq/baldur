"""
MetricsSettings cardinality guard fields unit tests.

Tests the 3 new Pydantic fields added for Metric Cardinality Guard:
max_distinct_endpoints, max_registered_domains, endpoint_cache_size.

Reference:
    docs/baldur/middleware_system/332_METRIC_CARDINALITY_GUARD.md §4
    src/baldur/settings/metrics.py
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from baldur.settings.metrics import (
    MetricsSettings,
    get_metrics_settings,
    reset_metrics_settings,
)

# =============================================================================
# Contract Tests
# =============================================================================


class TestMetricsCardinalitySettingsContract:
    """Contract verification: default values match design document §4."""

    def test_max_distinct_endpoints_default(self):
        """max_distinct_endpoints default is 500."""
        settings = MetricsSettings()
        assert settings.max_distinct_endpoints == 500

    def test_max_registered_domains_default(self):
        """max_registered_domains default is 50."""
        settings = MetricsSettings()
        assert settings.max_registered_domains == 50

    def test_endpoint_cache_size_default(self):
        """endpoint_cache_size default is 2048."""
        settings = MetricsSettings()
        assert settings.endpoint_cache_size == 2048


# =============================================================================
# Boundary Tests — max_distinct_endpoints (ge=50, le=5000)
# =============================================================================


class TestMaxDistinctEndpointsBoundaryContract:
    """Boundary contract: max_distinct_endpoints ge=50, le=5000."""

    def test_minimum_boundary_accepted(self):
        """Boundary value 50 (ge=50) is accepted."""
        settings = MetricsSettings(max_distinct_endpoints=50)
        assert settings.max_distinct_endpoints == 50

    def test_below_minimum_rejected(self):
        """Value 49 (below ge=50) is rejected."""
        with pytest.raises(ValidationError):
            MetricsSettings(max_distinct_endpoints=49)

    def test_maximum_boundary_accepted(self):
        """Boundary value 5000 (le=5000) is accepted."""
        settings = MetricsSettings(max_distinct_endpoints=5000)
        assert settings.max_distinct_endpoints == 5000

    def test_above_maximum_rejected(self):
        """Value 5001 (above le=5000) is rejected."""
        with pytest.raises(ValidationError):
            MetricsSettings(max_distinct_endpoints=5001)


# =============================================================================
# Boundary Tests — max_registered_domains (ge=10, le=500)
# =============================================================================


class TestMaxRegisteredDomainsBoundaryContract:
    """Boundary contract: max_registered_domains ge=10, le=500."""

    def test_minimum_boundary_accepted(self):
        """Boundary value 10 (ge=10) is accepted."""
        settings = MetricsSettings(max_registered_domains=10)
        assert settings.max_registered_domains == 10

    def test_below_minimum_rejected(self):
        """Value 9 (below ge=10) is rejected."""
        with pytest.raises(ValidationError):
            MetricsSettings(max_registered_domains=9)

    def test_maximum_boundary_accepted(self):
        """Boundary value 500 (le=500) is accepted."""
        settings = MetricsSettings(max_registered_domains=500)
        assert settings.max_registered_domains == 500

    def test_above_maximum_rejected(self):
        """Value 501 (above le=500) is rejected."""
        with pytest.raises(ValidationError):
            MetricsSettings(max_registered_domains=501)


# =============================================================================
# Boundary Tests — endpoint_cache_size (ge=256, le=65536)
# =============================================================================


class TestEndpointCacheSizeBoundaryContract:
    """Boundary contract: endpoint_cache_size ge=256, le=65536."""

    def test_minimum_boundary_accepted(self):
        """Boundary value 256 (ge=256) is accepted."""
        settings = MetricsSettings(endpoint_cache_size=256)
        assert settings.endpoint_cache_size == 256

    def test_below_minimum_rejected(self):
        """Value 255 (below ge=256) is rejected."""
        with pytest.raises(ValidationError):
            MetricsSettings(endpoint_cache_size=255)

    def test_maximum_boundary_accepted(self):
        """Boundary value 65536 (le=65536) is accepted."""
        settings = MetricsSettings(endpoint_cache_size=65536)
        assert settings.endpoint_cache_size == 65536

    def test_above_maximum_rejected(self):
        """Value 65537 (above le=65536) is rejected."""
        with pytest.raises(ValidationError):
            MetricsSettings(endpoint_cache_size=65537)


# =============================================================================
# Behavior Tests — Environment Variable Override
# =============================================================================


class TestMetricsCardinalitySettingsEnvBehavior:
    """Behavior verification: env var overrides for cardinality fields."""

    def test_max_distinct_endpoints_env_override(self):
        """BALDUR_METRICS_MAX_DISTINCT_ENDPOINTS env var sets value."""
        with patch.dict(
            "os.environ",
            {"BALDUR_METRICS_MAX_DISTINCT_ENDPOINTS": "1000"},
        ):
            settings = MetricsSettings()
            assert settings.max_distinct_endpoints == 1000

    def test_max_registered_domains_env_override(self):
        """BALDUR_METRICS_MAX_REGISTERED_DOMAINS env var sets value."""
        with patch.dict(
            "os.environ",
            {"BALDUR_METRICS_MAX_REGISTERED_DOMAINS": "100"},
        ):
            settings = MetricsSettings()
            assert settings.max_registered_domains == 100

    def test_endpoint_cache_size_env_override(self):
        """BALDUR_METRICS_ENDPOINT_CACHE_SIZE env var sets value."""
        with patch.dict(
            "os.environ",
            {"BALDUR_METRICS_ENDPOINT_CACHE_SIZE": "4096"},
        ):
            settings = MetricsSettings()
            assert settings.endpoint_cache_size == 4096


# =============================================================================
# Behavior Tests — Singleton
# =============================================================================


class TestMetricsSettingsSingletonBehavior:
    """Behavior verification: singleton get/reset for MetricsSettings."""

    def setup_method(self):
        reset_metrics_settings()

    def teardown_method(self):
        reset_metrics_settings()

    def test_get_returns_same_instance(self):
        """get_metrics_settings() returns the same cached instance."""
        first = get_metrics_settings()
        second = get_metrics_settings()
        assert first is second

    def test_reset_clears_cached_instance(self):
        """reset_metrics_settings() → next call creates new instance."""
        first = get_metrics_settings()
        reset_metrics_settings()
        second = get_metrics_settings()
        assert first is not second
