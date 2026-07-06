"""DaemonWorkerSettings unit tests (impl 489 D11).

Test targets:
    - baldur.settings.daemon_worker.DaemonWorkerSettings (Pydantic v2)
    - get_daemon_worker_settings / reset_daemon_worker_settings singleton pair
    - meta_group cached_property accessor

Test Categories:
    A. Contract — design defaults from impl 489 D11
    B. Behavior — boundary validation (ge/le constraints)
    C. Behavior — env override + singleton lifecycle
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings.daemon_worker import (
    DaemonWorkerSettings,
    get_daemon_worker_settings,
    reset_daemon_worker_settings,
)

# =============================================================================
# A. Contract — defaults from impl 489 D11
# =============================================================================


class TestDaemonWorkerSettingsContract:
    """Default values declared in impl 489 D11."""

    def test_default_staleness_multiplier(self):
        assert DaemonWorkerSettings().default_staleness_multiplier == 2.0

    def test_respawn_enabled_default_is_false(self):
        """Safe-default — operators opt in after dashboard verification."""
        assert DaemonWorkerSettings().respawn_enabled is False

    def test_respawn_max_attempts_default(self):
        assert DaemonWorkerSettings().respawn_max_attempts == 3

    def test_respawn_backoff_base_seconds_default(self):
        assert DaemonWorkerSettings().respawn_backoff_base_seconds == 1.0

    def test_respawn_backoff_max_seconds_default(self):
        assert DaemonWorkerSettings().respawn_backoff_max_seconds == 60.0

    def test_respawn_count_reset_seconds_default(self):
        """D7 sustained-health window — 1 hour default."""
        assert DaemonWorkerSettings().respawn_count_reset_seconds == 3600.0


# =============================================================================
# B. Behavior — boundary validation
# =============================================================================


class TestDaemonWorkerSettingsBoundaryBehavior:
    """Field-level Pydantic ge/le constraints declared in D11."""

    def test_staleness_multiplier_below_one_rejected(self):
        with pytest.raises(ValidationError):
            DaemonWorkerSettings(default_staleness_multiplier=0.99)

    def test_staleness_multiplier_one_accepted(self):
        DaemonWorkerSettings(default_staleness_multiplier=1.0)

    def test_max_attempts_zero_rejected(self):
        with pytest.raises(ValidationError):
            DaemonWorkerSettings(respawn_max_attempts=0)

    def test_max_attempts_one_accepted(self):
        DaemonWorkerSettings(respawn_max_attempts=1)

    def test_max_attempts_above_cap_rejected(self):
        with pytest.raises(ValidationError):
            DaemonWorkerSettings(respawn_max_attempts=101)

    def test_backoff_base_negative_rejected(self):
        with pytest.raises(ValidationError):
            DaemonWorkerSettings(respawn_backoff_base_seconds=-0.1)

    def test_backoff_max_zero_rejected(self):
        with pytest.raises(ValidationError):
            DaemonWorkerSettings(respawn_backoff_max_seconds=0.0)

    def test_count_reset_below_minimum_rejected(self):
        """``respawn_count_reset_seconds`` floor is 60 — sub-minute resets are nonsense."""
        with pytest.raises(ValidationError):
            DaemonWorkerSettings(respawn_count_reset_seconds=59.0)

    def test_count_reset_at_minimum_accepted(self):
        DaemonWorkerSettings(respawn_count_reset_seconds=60.0)


# =============================================================================
# C. Behavior — env namespace + singleton lifecycle
# =============================================================================


class TestDaemonWorkerSettingsEnvBehavior:
    """``BALDUR_DAEMON_WORKER_*`` namespace + reset singleton pair."""

    def test_env_override_via_baldur_namespace(self, monkeypatch):
        """Setting ``BALDUR_DAEMON_WORKER_RESPAWN_ENABLED`` takes effect."""
        monkeypatch.setenv("BALDUR_DAEMON_WORKER_RESPAWN_ENABLED", "true")
        monkeypatch.setenv("BALDUR_DAEMON_WORKER_RESPAWN_MAX_ATTEMPTS", "7")
        s = DaemonWorkerSettings()
        assert s.respawn_enabled is True
        assert s.respawn_max_attempts == 7

    def test_singleton_returns_same_instance(self):
        """``get_daemon_worker_settings()`` is cached on the meta group."""
        reset_daemon_worker_settings()
        try:
            first = get_daemon_worker_settings()
            second = get_daemon_worker_settings()
            assert first is second
        finally:
            reset_daemon_worker_settings()

    def test_reset_clears_cache_so_new_env_picks_up(self, monkeypatch):
        """``reset_daemon_worker_settings`` invalidates the cached_property."""
        reset_daemon_worker_settings()
        try:
            initial = get_daemon_worker_settings()
            assert initial.respawn_max_attempts == 3  # default

            monkeypatch.setenv("BALDUR_DAEMON_WORKER_RESPAWN_MAX_ATTEMPTS", "9")
            reset_daemon_worker_settings()

            refreshed = get_daemon_worker_settings()
            assert refreshed.respawn_max_attempts == 9
            assert refreshed is not initial
        finally:
            monkeypatch.delenv(
                "BALDUR_DAEMON_WORKER_RESPAWN_MAX_ATTEMPTS", raising=False
            )
            reset_daemon_worker_settings()

    def test_meta_group_exposes_daemon_worker_accessor(self):
        """``MetaGroup.daemon_worker`` cached_property returns the settings."""
        from baldur.settings.groups import MetaGroup

        group = MetaGroup()
        assert isinstance(group.daemon_worker, DaemonWorkerSettings)
        # cached_property: second access returns the same instance.
        assert group.daemon_worker is group.daemon_worker
