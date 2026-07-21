"""
Gap-coverage tests for config.py → settings/ migration.

Covers:
- ForensicSettings: masking fields (Contract)
- MetricsSettings: sync/drift fields (Contract)
- L2StorageSettings: 50ms default, boundary (Contract)
- Safe getters: drift detection integration (Behavior)
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

# =============================================================================
# ForensicSettings — masking fields Contract
# =============================================================================


class TestForensicMaskingFieldsContract:
    """ForensicSettings masking field design contract verification."""

    def test_mask_sensitive_fields_default_true(self):
        """mask_sensitive_fields default: True."""
        from baldur.settings.forensic import ForensicSettings

        settings = ForensicSettings()
        assert settings.mask_sensitive_fields is True

    def test_sensitive_field_patterns_contains_required_entries(self):
        """sensitive_field_patterns contains auth, payment, infra patterns."""
        from baldur.settings.forensic import ForensicSettings

        settings = ForensicSettings()
        patterns = settings.sensitive_field_patterns
        # Authentication patterns
        assert "password" in patterns
        assert "secret" in patterns
        assert "token" in patterns
        assert "api_key" in patterns
        assert "authorization" in patterns
        assert "private_key" in patterns
        # Payment patterns
        assert "card_number" in patterns
        assert "cvv" in patterns
        # Infrastructure patterns
        assert "db_password" in patterns
        assert "connection_string" in patterns

    def test_mask_internal_ip_default_true(self):
        """mask_internal_ip default: True."""
        from baldur.settings.forensic import ForensicSettings

        settings = ForensicSettings()
        assert settings.mask_internal_ip is True

    def test_internal_ip_patterns_cover_rfc1918_ranges(self):
        """internal_ip_patterns cover 10.x, 172.16-31.x, 192.168.x ranges."""
        from baldur.settings.forensic import ForensicSettings

        settings = ForensicSettings()
        patterns = settings.internal_ip_patterns
        assert len(patterns) == 3
        assert any("10\\." in p for p in patterns)
        assert any("172\\." in p for p in patterns)
        assert any("192\\.168" in p for p in patterns)

    def test_mask_server_paths_default_true(self):
        """mask_server_paths default: True."""
        from baldur.settings.forensic import ForensicSettings

        settings = ForensicSettings()
        assert settings.mask_server_paths is True

    def test_server_path_patterns_present(self):
        """server_path_patterns cover Linux and Windows paths."""
        from baldur.settings.forensic import ForensicSettings

        settings = ForensicSettings()
        patterns = settings.server_path_patterns
        assert any("/home/" in p for p in patterns)
        assert any("Users" in p for p in patterns)  # Windows

    def test_collect_request_body_default_false(self):
        """collect_request_body default: False (security default)."""
        from baldur.settings.forensic import ForensicSettings

        settings = ForensicSettings()
        assert settings.collect_request_body is False

    def test_collect_response_body_default_false(self):
        """collect_response_body default: False (security default)."""
        from baldur.settings.forensic import ForensicSettings

        settings = ForensicSettings()
        assert settings.collect_response_body is False

    def test_max_stacktrace_length_default_10000(self):
        """max_stacktrace_length default: 10000."""
        from baldur.settings.forensic import ForensicSettings

        settings = ForensicSettings()
        assert settings.max_stacktrace_length == 10000

    def test_max_stacktrace_length_boundary_minimum(self):
        """max_stacktrace_length minimum boundary: ge=1000."""
        from baldur.settings.forensic import ForensicSettings

        settings = ForensicSettings(max_stacktrace_length=1000)
        assert settings.max_stacktrace_length == 1000

        with pytest.raises(ValidationError):
            ForensicSettings(max_stacktrace_length=999)


# =============================================================================
# MetricsSettings — sync/drift fields Contract
# =============================================================================


class TestMetricsSyncDriftFieldsContract:
    """MetricsSettings sync/drift field design contract verification."""

    def test_adapter_type_default_null(self):
        """adapter_type default: 'null'."""
        from baldur.settings.metrics import MetricsSettings

        settings = MetricsSettings()
        assert settings.adapter_type == "null"

    def test_redis_prefix_default(self):
        """redis_prefix default: 'sh:metrics:'."""
        from baldur.settings.metrics import MetricsSettings

        settings = MetricsSettings()
        assert settings.redis_prefix == "sh:metrics:"

    def test_drift_thresholds_contract_values(self):
        """Drift thresholds: warning=5%, critical=20%, incident=50%."""
        from baldur.settings.metrics import MetricsSettings

        settings = MetricsSettings()
        assert settings.drift_warning_threshold == 0.05
        assert settings.drift_critical_threshold == 0.20
        assert settings.drift_incident_threshold == 0.50

    def test_drift_threshold_boundary_maximum(self):
        """Drift thresholds: le=1.0."""
        from baldur.settings.metrics import MetricsSettings

        settings = MetricsSettings(drift_warning_threshold=1.0)
        assert settings.drift_warning_threshold == 1.0

        with pytest.raises(ValidationError):
            MetricsSettings(drift_warning_threshold=1.01)


# =============================================================================
# L2StorageSettings — 200ms timeout Contract (478 D4)
# =============================================================================


class TestL2StorageTimeoutContract:
    """L2StorageSettings redis_timeout_ms 1000ms design contract (479 D1)."""

    def test_redis_timeout_ms_default_1000(self):
        """redis_timeout_ms default: 1000ms (479 D1 — cold-start cluster-cap headroom)."""
        from baldur.settings.l2_storage import L2StorageSettings

        settings = L2StorageSettings()
        assert settings.redis_timeout_ms == 1000

    def test_redis_timeout_ms_minimum_boundary_10(self):
        """redis_timeout_ms minimum boundary: ge=10."""
        from baldur.settings.l2_storage import L2StorageSettings

        settings = L2StorageSettings(redis_timeout_ms=10)
        assert settings.redis_timeout_ms == 10

        with pytest.raises(ValidationError):
            L2StorageSettings(redis_timeout_ms=9)

    def test_redis_timeout_ms_maximum_boundary_1000(self):
        """redis_timeout_ms maximum boundary: le=1000."""
        from baldur.settings.l2_storage import L2StorageSettings

        settings = L2StorageSettings(redis_timeout_ms=1000)
        assert settings.redis_timeout_ms == 1000

        with pytest.raises(ValidationError):
            L2StorageSettings(redis_timeout_ms=1001)

    def test_fallback_timeout_ms_minimum_boundary_10(self):
        """fallback_timeout_ms minimum boundary: ge=10."""
        from baldur.settings.l2_storage import L2StorageSettings

        settings = L2StorageSettings(fallback_timeout_ms=10)
        assert settings.fallback_timeout_ms == 10

        with pytest.raises(ValidationError):
            L2StorageSettings(fallback_timeout_ms=9)


# =============================================================================
# Safe Getters — Drift detection Behavior
# =============================================================================


class TestSafeGetterDriftBehavior:
    """Safe getter drift detection and reset behavior."""

    @pytest.fixture(autouse=True)
    def _reset_drift_monitor(self):
        """Reset drift monitor singleton before each test."""
        from baldur.settings.drift_monitor import reset_config_drift_monitor

        reset_config_drift_monitor()
        yield
        reset_config_drift_monitor()

    def test_notification_safe_getter_resets_on_drift(self):
        """get_notification_settings_safe() resets cache when env changes."""
        from baldur.settings.drift_monitor import get_config_drift_monitor
        from baldur.settings.notification import get_notification_settings_safe

        # Given — prime the hash
        get_notification_settings_safe()

        # When — env changes
        monitor = get_config_drift_monitor()
        mock_reset = MagicMock(spec=[])
        monitor.register_cache_function("notification", mock_reset)

        with patch.dict(os.environ, {"BALDUR_NOTIFICATION_NEW_VAR": "x"}):
            get_notification_settings_safe()

        # Then — reset was called
        mock_reset.assert_called_once()

    def test_forensic_safe_getter_returns_settings(self):
        """get_forensic_settings_safe() returns ForensicSettings instance."""
        from baldur.settings.forensic import (
            ForensicSettings,
            get_forensic_settings_safe,
        )

        result = get_forensic_settings_safe()
        assert isinstance(result, ForensicSettings)

    def test_l2_storage_safe_getter_returns_settings(self):
        """get_l2_storage_settings_safe() returns L2StorageSettings instance."""
        from baldur.settings.l2_storage import (
            L2StorageSettings,
            get_l2_storage_settings_safe,
        )

        result = get_l2_storage_settings_safe()
        assert isinstance(result, L2StorageSettings)

    def test_metrics_safe_getter_returns_settings(self):
        """get_metric_collection_settings_safe() returns MetricsSettings instance."""
        from baldur.settings.metrics import (
            MetricsSettings,
            get_metric_collection_settings_safe,
        )

        result = get_metric_collection_settings_safe()
        assert isinstance(result, MetricsSettings)

    def test_safe_getter_no_drift_does_not_reset(self):
        """Safe getter without env change does not trigger reset."""
        from baldur.settings.drift_monitor import get_config_drift_monitor
        from baldur.settings.notification import get_notification_settings_safe

        monitor = get_config_drift_monitor()
        mock_reset = MagicMock(spec=[])
        monitor.register_cache_function("notification", mock_reset)

        # Two calls with same env — no reset expected
        get_notification_settings_safe()
        get_notification_settings_safe()

        mock_reset.assert_not_called()


# =============================================================================
# ApiRateLimitSettings — ping timeout field Contract
# =============================================================================


class TestApiRateLimitPingTimeoutContract:
    """ApiRateLimitSettings redis_ping_timeout_ms field contract."""

    def test_redis_ping_timeout_ms_default_100(self):
        """redis_ping_timeout_ms default: 100ms."""
        from baldur.settings.api_rate_limit import ApiRateLimitSettings

        settings = ApiRateLimitSettings()
        assert settings.redis_ping_timeout_ms == 100

    def test_redis_ping_timeout_ms_minimum_boundary(self):
        """redis_ping_timeout_ms minimum boundary: ge=10."""
        from baldur.settings.api_rate_limit import ApiRateLimitSettings

        settings = ApiRateLimitSettings(redis_ping_timeout_ms=10)
        assert settings.redis_ping_timeout_ms == 10

        with pytest.raises(ValidationError):
            ApiRateLimitSettings(redis_ping_timeout_ms=9)

    def test_redis_ping_timeout_ms_maximum_boundary(self):
        """redis_ping_timeout_ms maximum boundary: le=1000."""
        from baldur.settings.api_rate_limit import ApiRateLimitSettings

        settings = ApiRateLimitSettings(redis_ping_timeout_ms=1000)
        assert settings.redis_ping_timeout_ms == 1000

        with pytest.raises(ValidationError):
            ApiRateLimitSettings(redis_ping_timeout_ms=1001)
