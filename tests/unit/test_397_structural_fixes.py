"""
397 Structural Fixes Tests

Tests for doc-397 implementation covering:
- Singleton DCL pattern (PoolMonitor, Dashboard; LearningService and
  Forecaster singleton tests moved to
  tests/dormant/unit/test_397_singleton_relocated.py per 599 D14)
- CorruptionShield enabled gate
- Canary lock timeout from settings
- ApplyOptions grace_timeout from settings
- Daily report beat schedule from settings
- SecurityConfig session fields
- PredictiveForecasterSettings enabled field (BF2)

Source files referenced in individual test docstrings.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# TestPoolMonitorSingletonBehavior — DCL singleton
# =============================================================================


# =============================================================================
# TestDashboardServiceSingletonBehavior
# =============================================================================


class TestDashboardServiceSingletonBehavior:
    """Verify reset_dashboard_service clears singleton.

    Source: src/baldur/services/dashboard_service/service.py:482-486
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        """Ensure singleton is clean before and after each test."""
        from baldur.services.dashboard_service.service import (
            reset_dashboard_service,
        )

        reset_dashboard_service()
        yield
        reset_dashboard_service()

    def test_reset_dashboard_service_clears_singleton(self):
        """After reset, get_dashboard_service must create a new instance."""
        from baldur.services.dashboard_service.service import (
            get_dashboard_service,
            reset_dashboard_service,
        )

        first = get_dashboard_service()
        reset_dashboard_service()
        second = get_dashboard_service()

        assert first is not second


# =============================================================================
# TestCorruptionShieldEnabledGateBehavior
# =============================================================================


# =============================================================================
# TestCanaryLockTimeoutFromSettingsBehavior
# =============================================================================


# =============================================================================
# TestApplyOptionsGraceTimeoutFromSettingsBehavior
# =============================================================================


class TestApplyOptionsGraceTimeoutFromSettingsBehavior:
    """Verify ApplyOptions default grace_timeout comes from settings.

    Source: src/baldur/core/apply_strategy.py:31-45
    """

    @patch(
        "baldur.core.apply_strategy.get_apply_strategy_settings",
        autospec=True,
    )
    def test_apply_options_default_grace_timeout_from_settings(self, mock_get_settings):
        """ApplyOptions() without args must have grace_timeout_seconds from settings."""
        # Given
        mock_settings = MagicMock()
        mock_settings.default_grace_timeout = 120
        mock_get_settings.return_value = mock_settings

        from baldur.core.apply_strategy import ApplyOptions

        # When
        options = ApplyOptions()

        # Then
        assert options.grace_timeout_seconds == 120


# =============================================================================
# TestDailyReportScheduleFromSettingsBehavior
# =============================================================================


class TestDailyReportScheduleFromSettingsBehavior:
    """Verify get_daily_report_beat_schedule uses settings hour/minute.

    Source: src/baldur/tasks/daily_report.py:94-119
    """

    @patch(
        "baldur.settings.daily_report.get_daily_report_settings",
        autospec=True,
    )
    def test_beat_schedule_uses_settings_hour_and_minute(self, mock_get_settings):
        """Beat schedule must reflect custom hour/minute from DailyReportSettings.

        Source: daily_report.py:112-113 — settings.default_hour/default_minute
        """
        # Given
        mock_settings = MagicMock()
        mock_settings.default_hour = 7
        mock_settings.default_minute = 30
        mock_get_settings.return_value = mock_settings

        from baldur.tasks.daily_report import get_daily_report_beat_schedule

        # When
        schedule = get_daily_report_beat_schedule()

        # Then
        entry = schedule["generate-daily-autonomous-report"]
        assert entry["schedule"]["hour"] == 7
        assert entry["schedule"]["minute"] == 30


# =============================================================================
# TestSecurityConfigSessionFieldsContract — hardcoded contract
# =============================================================================


class TestSecurityConfigSessionFieldsContract:
    """Verify SecurityConfig has session_engine and session_cookie_age fields.

    Source: src/baldur/services/security/models.py:115-116
    """

    def test_security_config_has_session_engine_field(self):
        """SecurityConfig must have session_engine field."""
        from baldur.services.security.models import SecurityConfig

        config = SecurityConfig()
        assert hasattr(config, "session_engine")
        assert config.session_engine == "django.contrib.sessions.backends.db"

    def test_security_config_has_session_cookie_age_field(self):
        """SecurityConfig must have session_cookie_age field."""
        from baldur.services.security.models import SecurityConfig

        config = SecurityConfig()
        assert hasattr(config, "session_cookie_age")
        assert config.session_cookie_age == 1209600  # 14 days


# =============================================================================
# TestPredictiveForecasterEnabledContract (BF2) — hardcoded contract
# =============================================================================


class TestPredictiveForecasterEnabledContract:
    """Verify PredictiveForecasterSettings has enabled field (BF2 fix).

    Source: src/baldur/settings/predictive_forecaster.py:44-47
    """

    def test_predictive_forecaster_settings_has_enabled_field_default_false(self):
        """PredictiveForecasterSettings has 'enabled' field defaulting to False
        (Dormant tier per V1_LAUNCH_MANIFEST, 527 D1)."""
        from baldur.settings.predictive_forecaster import (
            PredictiveForecasterSettings,
        )

        settings = PredictiveForecasterSettings()
        assert hasattr(settings, "enabled")
        assert settings.enabled is False


# =============================================================================
# TestDailyReportServiceSingletonBehavior — DCL singleton
# =============================================================================


class TestDailyReportServiceSingletonBehavior:
    """Verify get_daily_report_service / reset_daily_report_service singleton
    uses DCL pattern with threading.Lock.

    Source: src/baldur/services/daily_report/service.py:236-254
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        """Ensure singleton is clean before and after each test."""
        from baldur.services.daily_report.service import (
            reset_daily_report_service,
        )

        reset_daily_report_service()
        yield
        reset_daily_report_service()

    def test_get_daily_report_service_returns_instance(self):
        """get_daily_report_service must return a DailyReportService instance."""
        from baldur.services.daily_report.service import (
            DailyReportService,
            get_daily_report_service,
        )

        instance = get_daily_report_service()
        assert isinstance(instance, DailyReportService)

    def test_get_daily_report_service_returns_same_instance(self):
        """Repeated calls must return the same cached instance."""
        from baldur.services.daily_report.service import (
            get_daily_report_service,
        )

        first = get_daily_report_service()
        second = get_daily_report_service()
        assert first is second

    def test_reset_daily_report_service_clears_singleton(self):
        """After reset, get_daily_report_service must create a new instance."""
        from baldur.services.daily_report.service import (
            get_daily_report_service,
            reset_daily_report_service,
        )

        first = get_daily_report_service()
        reset_daily_report_service()
        second = get_daily_report_service()

        assert first is not second

    def test_singleton_registered_in_factory(self):
        """Singleton must be registered via make_singleton_factory."""
        from baldur.utils.singleton import _REGISTRY

        assert "daily_report_service" in _REGISTRY

    def test_concurrent_get_returns_same_instance(self):
        """Multiple threads calling get_daily_report_service concurrently
        must all receive the same singleton instance."""
        import threading

        from baldur.services.daily_report.service import (
            get_daily_report_service,
        )

        results = []
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            results.append(get_daily_report_service())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == 4
        assert all(r is results[0] for r in results)


# =============================================================================
# TestInvalidateDashboardCacheBehavior — CACHE_PREFIX instance access
# =============================================================================


class TestInvalidateDashboardCacheBehavior:
    """Verify invalidate_dashboard_cache uses instance property for CACHE_PREFIX,
    not class-level descriptor access.

    Source: src/baldur/services/dashboard_service/service.py:494-513
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        """Ensure dashboard singleton is clean."""
        from baldur.services.dashboard_service.service import (
            reset_dashboard_service,
        )

        reset_dashboard_service()
        yield
        reset_dashboard_service()

    @patch(
        "baldur.services.dashboard_service.service.get_dashboard_settings",
        autospec=True,
    )
    def test_invalidate_uses_settings_based_cache_prefix(self, mock_get_settings):
        """invalidate_dashboard_cache must use the instance property CACHE_PREFIX
        (which reads from settings), not DashboardService.CACHE_PREFIX
        (which returns a property descriptor object).

        Source: service.py:506
        """
        # Given
        mock_settings = MagicMock()
        mock_settings.cache_prefix = "test_prefix:"
        mock_settings.cache_ttl_seconds = 300
        mock_settings.cache_ttl_status = 60
        mock_settings.cache_ttl_activity = 120
        mock_get_settings.return_value = mock_settings

        mock_cache = MagicMock()

        from baldur.services.dashboard_service.service import (
            get_dashboard_service,
            invalidate_dashboard_cache,
        )

        get_dashboard_service(cache=mock_cache)

        # When
        invalidate_dashboard_cache()

        # Then — keys must use the settings-based prefix, not "<property ...>"
        expected_keys = [
            "test_prefix:summary",
            "test_prefix:status",
            "test_prefix:activity",
            "test_prefix:distribution",
            "test_prefix:alerts",
        ]
        actual_keys = [call.args[0] for call in mock_cache.delete.call_args_list]
        assert actual_keys == expected_keys

    def test_cache_prefix_not_accessed_as_class_attribute_in_invalidate(self):
        """invalidate_dashboard_cache source must use 'service.CACHE_PREFIX',
        not 'DashboardService.CACHE_PREFIX'.

        This is a structural regression guard.
        Source: service.py:494-513
        """
        import inspect

        from baldur.services.dashboard_service.service import (
            invalidate_dashboard_cache,
        )

        source = inspect.getsource(invalidate_dashboard_cache)
        assert "DashboardService.CACHE_PREFIX" not in source
        assert "service.CACHE_PREFIX" in source


# =============================================================================
# TestDLQReplayLogParameterBehavior — log parameter name
# =============================================================================
