"""
339 Settings Integration unit tests.

Behavior verification for the settings-connection bug fixes and code
changes.

Test classification (UNIT_TEST_GUIDELINES §0):
- Behavior: verify that code reads values from settings correctly

Referenced sources:
- core/shutdown_coordinator.py — RecoveryShutdownSettings connection
- core/state_backend.py — SystemControlSettings-driven factory
- adapters/health_checker.py — HealthCheckSettings defaults
- meta/health_probe.py — HealthCheckSettings thresholds
- services/config/propagation_health.py — PropagationSettings injection
- audit/integrity/health_score.py — AuditIntegritySettings cache parameters

The multiregion health_monitor slice (339 §5.2) moved to
``tests/dormant/unit/multiregion/test_health_monitor_settings_339.py``
per doc 599 D5/D14 (its SUT relocated to the private distribution).
"""

from __future__ import annotations

import pytest

from baldur.settings.recovery_shutdown import (
    RecoveryShutdownSettings,
    reset_recovery_shutdown_settings,
)


@pytest.fixture(autouse=True)
def _reset_all():
    """Reset for test isolation."""
    reset_recovery_shutdown_settings()
    yield
    reset_recovery_shutdown_settings()


# =============================================================================
# GracefulShutdownCoordinator — settings connection (339 §6.3)
# =============================================================================


class TestShutdownCoordinatorSettingsConnectionBehavior:
    """Verify GracefulShutdownCoordinator references RecoveryShutdownSettings."""

    def test_coordinator_uses_settings_drain_timeout_when_none(self):
        """drain_timeout=None uses settings.default_drain_timeout_seconds."""
        from baldur.core.shutdown_coordinator import (
            GracefulShutdownCoordinator,
            RequestTracker,
        )

        # Given
        settings = RecoveryShutdownSettings()
        tracker = RequestTracker()

        # When — drain_timeout unspecified
        coordinator = GracefulShutdownCoordinator(request_tracker=tracker)

        # Then — the settings default is used
        assert coordinator._drain_timeout == settings.default_drain_timeout_seconds

    def test_coordinator_uses_settings_check_interval_when_none(self):
        """check_interval=None uses settings.check_interval_seconds."""
        from baldur.core.shutdown_coordinator import (
            GracefulShutdownCoordinator,
            RequestTracker,
        )

        settings = RecoveryShutdownSettings()
        tracker = RequestTracker()
        coordinator = GracefulShutdownCoordinator(request_tracker=tracker)
        assert coordinator._check_interval == settings.check_interval_seconds

    def test_coordinator_explicit_values_override_settings(self):
        """Explicit values take precedence over settings."""
        from baldur.core.shutdown_coordinator import (
            GracefulShutdownCoordinator,
            RequestTracker,
        )

        tracker = RequestTracker()
        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            drain_timeout=99.0,
            check_interval=1.5,
        )
        assert coordinator._drain_timeout == 99.0
        assert coordinator._check_interval == 1.5

    def test_request_tracker_uses_settings_max_age_when_none(self):
        """max_request_age_seconds=None uses the settings default."""
        from baldur.core.shutdown_coordinator import RequestTracker

        settings = RecoveryShutdownSettings()
        tracker = RequestTracker()
        assert tracker._max_age == settings.max_request_age_seconds

    def test_request_tracker_explicit_max_age_overrides(self):
        """An explicit max_request_age_seconds takes precedence over settings."""
        from baldur.core.shutdown_coordinator import RequestTracker

        tracker = RequestTracker(max_request_age_seconds=600.0)
        assert tracker._max_age == 600.0


# =============================================================================
# get_state_backend — SystemControlSettings-driven factory (339 §7.2)
# =============================================================================


class TestStateBackendFactorySettingsBehavior:
    """Verify get_state_backend() reads values from SystemControlSettings."""

    def test_file_backend_uses_settings_state_dir(self, monkeypatch):
        """file backend: uses settings.state_dir."""
        from baldur.core.state_backend import (
            FileStateBackend,
            get_state_backend,
            reset_state_backend,
        )

        reset_state_backend()
        monkeypatch.setenv("BALDUR_SYSTEM_CONTROL_BACKEND", "file")
        monkeypatch.setenv("BALDUR_SYSTEM_CONTROL_DIR", "/tmp/test_state_339")

        try:
            from baldur.settings.system_control import (
                reset_system_control_settings,
            )

            reset_system_control_settings()
            backend = get_state_backend()
            assert isinstance(backend, FileStateBackend)
        finally:
            reset_state_backend()

    def test_memory_backend_selection(self, monkeypatch):
        """memory backend selection."""
        from baldur.core.state_backend import (
            MemoryStateBackend,
            get_state_backend,
            reset_state_backend,
        )
        from baldur.settings.system_control import reset_system_control_settings

        reset_state_backend()
        reset_system_control_settings()
        monkeypatch.setenv("BALDUR_SYSTEM_CONTROL_BACKEND", "memory")

        try:
            backend = get_state_backend()
            assert isinstance(backend, MemoryStateBackend)
        finally:
            reset_state_backend()
            reset_system_control_settings()


# =============================================================================
# HealthChecker — settings default connection
# =============================================================================


class TestHealthCheckerSettingsConnectionBehavior:
    """Verify the health checker adapters use settings defaults."""

    def test_ttl_cache_strategy_uses_settings_ttl_when_none(self):
        """TTLCacheStrategy: ttl=None → HealthCheckSettings default."""
        from baldur.adapters.health_checker import TTLCacheStrategy
        from baldur.settings.health_check import HealthCheckSettings

        strategy = TTLCacheStrategy(ttl=None)
        assert (
            strategy._ttl_cache._ttl_seconds
            == HealthCheckSettings().checker_cache_ttl_seconds
        )

    def test_ttl_cache_strategy_explicit_ttl_overrides(self):
        """TTLCacheStrategy: explicit ttl → settings ignored."""
        from baldur.adapters.health_checker import TTLCacheStrategy

        strategy = TTLCacheStrategy(ttl=15.0)
        assert strategy._ttl_cache._ttl_seconds == 15.0

    def test_simple_socket_strategy_uses_settings_timeout_when_none(self):
        """SimpleSocketStrategy: timeout=None → HealthCheckSettings default."""
        from baldur.adapters.health_checker import SimpleSocketStrategy
        from baldur.settings.health_check import HealthCheckSettings

        strategy = SimpleSocketStrategy(timeout=None)
        assert strategy._timeout == HealthCheckSettings().socket_timeout_seconds

    def test_portable_health_checker_uses_settings_ttl_when_none(self):
        """PortableHealthChecker: ttl=None → HealthCheckSettings default."""
        from baldur.adapters.health_checker import PortableHealthChecker
        from baldur.settings.health_check import HealthCheckSettings

        checker = PortableHealthChecker(ttl=None)
        assert checker._ttl == HealthCheckSettings().checker_cache_ttl_seconds


# =============================================================================
# PropagationHealthMonitor — PropagationSettings connection (339 §5.4)
# =============================================================================


class TestPropagationHealthMonitorSettingsBehavior:
    """Verify PropagationHealthMonitor uses PropagationSettings."""

    def test_uses_propagation_settings_sla_thresholds(self):
        """SLA thresholds are provided by PropagationSettings."""
        from baldur.services.config.propagation_health import (
            PropagationHealthMonitor,
        )
        from baldur.settings.propagation import PropagationSettings

        settings = PropagationSettings()
        monitor = PropagationHealthMonitor(settings=settings)
        assert monitor._propagation_settings is settings

    def test_custom_settings_override_defaults(self):
        """Custom settings values are reflected."""
        from baldur.services.config.propagation_health import (
            PropagationHealthMonitor,
        )
        from baldur.settings.propagation import PropagationSettings

        custom_settings = PropagationSettings(
            tier1_max_latency_ms=500,
            tier1_penalty_points=10,
        )
        monitor = PropagationHealthMonitor(settings=custom_settings)
        assert monitor._propagation_settings.tier1_max_latency_ms == 500
        assert monitor._propagation_settings.tier1_penalty_points == 10


# =============================================================================
# IntegrityHealthScore — AuditIntegritySettings cache parameters (339 §5.4)
# =============================================================================


class TestIntegrityHealthScoreSettingsBehavior:
    """Verify IntegrityHealthScore reads cache parameters from AuditIntegritySettings."""

    def test_max_events_from_settings(self):
        """_max_events is provided by AuditIntegritySettings."""
        from baldur.audit.integrity.health_score import IntegrityHealthScore
        from baldur.settings.audit_integrity import AuditIntegritySettings

        health = IntegrityHealthScore()
        expected = AuditIntegritySettings().health_score_max_events
        assert health._max_events == expected

    def test_cache_ttl_from_settings(self):
        """_cache_ttl_seconds is provided by AuditIntegritySettings."""
        from baldur.audit.integrity.health_score import IntegrityHealthScore
        from baldur.settings.audit_integrity import AuditIntegritySettings

        health = IntegrityHealthScore()
        expected = AuditIntegritySettings().health_score_cache_ttl_seconds
        assert health._cache_ttl_seconds == expected
