"""RuntimeConfigWatchSettings unit tests.

The poll cadence is not an implementation detail: it *is* the convergence bound
the runtime-apply declaration reports back to an operator, so the promise and
the mechanism are the same number and cannot drift apart. That makes the default
and the accepted range contract values rather than tuning preferences.

Test targets:
    - baldur.settings.runtime_config_watch.RuntimeConfigWatchSettings
    - get_runtime_config_watch_settings / reset_runtime_config_watch_settings
    - services_group cached_property accessor

Test categories:
    A. Contract — the declared default, range and env prefix
    B. Behavior — boundary validation, env override, singleton lifecycle
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings.runtime_config_watch import (
    RuntimeConfigWatchSettings,
    get_runtime_config_watch_settings,
    reset_runtime_config_watch_settings,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_runtime_config_watch_settings()
    yield
    reset_runtime_config_watch_settings()


# =============================================================================
# A. Contract
# =============================================================================


class TestRuntimeConfigWatchSettingsContract:
    """The declared surface: default, range, prefix."""

    def test_interval_default_is_thirty_seconds(self):
        """The number an operator is promised as the convergence bound."""
        assert RuntimeConfigWatchSettings().interval_seconds == 30

    def test_env_prefix_is_the_documented_one(self):
        assert (
            RuntimeConfigWatchSettings.model_config["env_prefix"]
            == "BALDUR_RUNTIME_CONFIG_WATCH_"
        )

    def test_the_field_is_exported_from_the_settings_package(self):
        from baldur.settings import RuntimeConfigWatchSettings as Exported

        assert Exported is RuntimeConfigWatchSettings

    def test_the_services_group_exposes_the_accessor(self):
        from baldur.settings.root import get_config

        assert isinstance(
            get_config().services_group.runtime_config_watch, RuntimeConfigWatchSettings
        )


# =============================================================================
# B. Behavior
# =============================================================================


class TestRuntimeConfigWatchSettingsBoundaryBehavior:
    """The range endpoints, on both sides of each one."""

    def test_zero_is_accepted_as_the_disable_value(self):
        """``0`` follows the established disable convention: the domain then
        reports itself stored-only rather than claiming a bound nothing keeps."""
        assert RuntimeConfigWatchSettings(interval_seconds=0).interval_seconds == 0

    def test_a_negative_interval_is_rejected(self):
        with pytest.raises(ValidationError):
            RuntimeConfigWatchSettings(interval_seconds=-1)

    def test_the_upper_bound_is_accepted(self):
        assert (
            RuntimeConfigWatchSettings(interval_seconds=3600).interval_seconds == 3600
        )

    def test_above_the_upper_bound_is_rejected(self):
        with pytest.raises(ValidationError):
            RuntimeConfigWatchSettings(interval_seconds=3601)


class TestRuntimeConfigWatchSettingsLifecycleBehavior:
    """Env override and the singleton pair."""

    def test_the_env_var_overrides_the_default(self, monkeypatch):
        monkeypatch.setenv("BALDUR_RUNTIME_CONFIG_WATCH_INTERVAL_SECONDS", "5")

        assert RuntimeConfigWatchSettings().interval_seconds == 5

    def test_the_accessor_caches_one_instance(self):
        assert (
            get_runtime_config_watch_settings() is get_runtime_config_watch_settings()
        )

    def test_reset_drops_the_cached_instance(self):
        before = get_runtime_config_watch_settings()

        reset_runtime_config_watch_settings()

        assert get_runtime_config_watch_settings() is not before

    def test_reset_is_safe_when_nothing_is_cached(self):
        reset_runtime_config_watch_settings()
        reset_runtime_config_watch_settings()
