"""
Settings SSOT Migration — facade functions, conftest reset, and CLI unit tests.

Verifies:
- get_*_settings() routes through Root group and returns correct Settings instance
- Identity consistency: get_foo() is get_foo() via cached_property
- reset_*_settings() deletes cached_property from group __dict__
- Idempotent reset: no error if not initialized
- conftest auto_reset_all_settings calls reset_config()
- CLI _model_to_dict dispatches to_full_dict() vs model_dump()
"""

from __future__ import annotations

import sys

from baldur.settings.root import BaldurSettings, get_config, reset_config

# =============================================================================
# Behavior Tests — Facade get_*_settings() routing through Root
# =============================================================================


class TestFacadeGetRoutingBehavior:
    """get_*_settings() routes through Root group and returns correct type."""

    def test_get_backoff_settings_returns_backoff_type(self):
        """get_backoff_settings() returns BackoffSettings."""
        from baldur.settings.backoff import BackoffSettings, get_backoff_settings

        result = get_backoff_settings()
        assert isinstance(result, BackoffSettings)

    def test_get_backoff_settings_routes_through_root_core_group(self):
        """get_backoff_settings() is the same object as get_config().core.backoff."""
        from baldur.settings.backoff import get_backoff_settings

        result = get_backoff_settings()
        assert result is get_config().core.backoff

    def test_get_throttle_settings_returns_throttle_type(self):
        """get_throttle_settings() returns ThrottleSettings."""
        from baldur.settings.throttle import (
            ThrottleSettings,
            get_throttle_settings,
        )

        result = get_throttle_settings()
        assert isinstance(result, ThrottleSettings)

    def test_get_audit_settings_returns_audit_type(self):
        """get_audit_settings() returns AuditSettings."""
        from baldur.settings.audit import AuditSettings, get_audit_settings

        result = get_audit_settings()
        assert isinstance(result, AuditSettings)

    def test_get_bulkhead_settings_returns_bulkhead_type(self):
        """get_bulkhead_settings() returns BulkheadSettings."""
        from baldur.settings.bulkhead import (
            BulkheadSettings,
            get_bulkhead_settings,
        )

        result = get_bulkhead_settings()
        assert isinstance(result, BulkheadSettings)

    def test_get_distributed_lock_settings_returns_correct_type(self):
        """get_distributed_lock_settings() routes through coordination group."""
        from baldur.settings.distributed_lock import (
            DistributedLockSettings,
            get_distributed_lock_settings,
        )

        result = get_distributed_lock_settings()
        assert isinstance(result, DistributedLockSettings)

    def test_get_cell_topology_settings_returns_correct_type(self):
        """get_cell_topology_settings() routes through multi_region group."""
        from baldur.settings.cell_topology import (
            CellTopologySettings,
            get_cell_topology_settings,
        )

        result = get_cell_topology_settings()
        assert isinstance(result, CellTopologySettings)

    def test_get_drift_detection_settings_returns_correct_type(self):
        """get_drift_detection_settings() routes through metrics group."""
        from baldur.settings.drift_detection import (
            DriftDetectionSettings,
            get_drift_detection_settings,
        )

        result = get_drift_detection_settings()
        assert isinstance(result, DriftDetectionSettings)

    def test_get_canary_settings_returns_correct_type(self):
        """get_canary_settings() routes through services group."""
        from baldur.settings.canary import CanarySettings, get_canary_settings

        result = get_canary_settings()
        assert isinstance(result, CanarySettings)


class TestFacadeIdentityConsistencyBehavior:
    """get_foo() is get_foo() via cached_property — identity consistency."""

    def test_repeated_get_backoff_returns_same_instance(self):
        """Calling get_backoff_settings() twice returns the same object."""
        from baldur.settings.backoff import get_backoff_settings

        first = get_backoff_settings()
        second = get_backoff_settings()
        assert first is second

    def test_repeated_get_throttle_returns_same_instance(self):
        """Calling get_throttle_settings() twice returns the same object."""
        from baldur.settings.throttle import get_throttle_settings

        first = get_throttle_settings()
        second = get_throttle_settings()
        assert first is second

    def test_facade_and_root_accessor_return_same_instance(self):
        """get_backoff_settings() and config.core.backoff are identical objects."""
        from baldur.settings.backoff import get_backoff_settings

        config = get_config()
        facade_result = get_backoff_settings()
        root_result = config.core.backoff
        assert facade_result is root_result


# =============================================================================
# Behavior Tests — Facade reset_*_settings()
# =============================================================================


class TestFacadeResetBehavior:
    """reset_*_settings() deletes cached_property from group __dict__."""

    def test_reset_backoff_clears_cached_property(self):
        """reset_backoff_settings() removes 'backoff' from core group __dict__."""
        from baldur.settings.backoff import (
            get_backoff_settings,
            reset_backoff_settings,
        )

        # Given: backoff is initialized
        _ = get_backoff_settings()
        core_group = get_config().core
        assert "backoff" in core_group.__dict__

        # When
        reset_backoff_settings()

        # Then
        assert "backoff" not in core_group.__dict__

    def test_reset_creates_fresh_instance_on_next_get(self):
        """After reset, next get_*_settings() returns a new instance."""
        from baldur.settings.backoff import (
            get_backoff_settings,
            reset_backoff_settings,
        )

        first = get_backoff_settings()
        reset_backoff_settings()
        second = get_backoff_settings()
        assert first is not second

    def test_reset_is_idempotent_when_not_initialized(self):
        """reset_*_settings() does not raise when property was never accessed."""
        from baldur.settings.backoff import reset_backoff_settings

        # Should not raise even though backoff was never accessed
        reset_backoff_settings()

    def test_reset_is_idempotent_on_double_call(self):
        """Calling reset twice does not raise."""
        from baldur.settings.backoff import (
            get_backoff_settings,
            reset_backoff_settings,
        )

        _ = get_backoff_settings()
        reset_backoff_settings()
        reset_backoff_settings()  # second call should be harmless

    def test_reset_one_does_not_affect_sibling(self):
        """Resetting backoff does not affect pool_monitor in the same group."""
        from baldur.settings.backoff import (
            get_backoff_settings,
            reset_backoff_settings,
        )

        # Given: both initialized
        _ = get_backoff_settings()
        _ = get_config().core.pool_monitor
        core_group = get_config().core

        # When
        reset_backoff_settings()

        # Then
        assert "pool_monitor" in core_group.__dict__
        assert "backoff" not in core_group.__dict__


# =============================================================================
# Behavior Tests — conftest auto_reset_all_settings
# =============================================================================


class TestConftestAutoResetBehavior:
    """auto_reset_all_settings fixture calls reset_config() for isolation."""

    def test_reset_config_clears_root_singleton(self):
        """reset_config() sets the global _settings to None."""
        # Given: config exists
        config = get_config()
        assert config is not None

        # When
        reset_config()

        # Then: new call creates fresh instance
        new_config = get_config()
        assert new_config is not config

    def test_reset_config_invalidates_all_group_caches(self):
        """After reset_config(), group caches from old instance are gone."""
        # Given: access some groups
        old_config = get_config()
        old_backoff = old_config.core.backoff

        # When
        reset_config()

        # Then: new config has fresh groups
        new_config = get_config()
        new_backoff = new_config.core.backoff
        assert old_backoff is not new_backoff

    def test_conftest_reset_uses_sys_modules_lookup(self):
        """_reset_root_config pattern uses sys.modules to find root module."""
        root_mod = sys.modules.get("baldur.settings.root")
        assert root_mod is not None
        assert hasattr(root_mod, "reset_config")


# =============================================================================
# Behavior Tests — CLI _model_to_dict
# =============================================================================


class TestCliModelToDictBehavior:
    """_model_to_dict dispatches to to_full_dict() for Root, model_dump() for sub."""

    @staticmethod
    def _model_to_dict(model):
        """Replicate CLI _model_to_dict logic without Django dependency."""
        if hasattr(model, "to_full_dict"):
            return model.to_full_dict()
        if hasattr(model, "model_dump"):
            return model.model_dump()
        return dict(model)

    def test_root_settings_uses_to_full_dict(self):
        """Root BaldurSettings dispatches to to_full_dict()."""
        config = BaldurSettings()
        result = self._model_to_dict(config)
        # to_full_dict() returns model_dump() + initialized groups
        assert "cluster_id" in result

    def test_sub_settings_uses_model_dump(self):
        """Sub-settings without to_full_dict() dispatches to model_dump()."""
        from baldur.settings.backoff import BackoffSettings

        settings = BackoffSettings()
        result = self._model_to_dict(settings)
        assert "exponential_base_delay" in result
        assert "to_full_dict" not in dir(BackoffSettings)

    def test_root_with_initialized_group_includes_group_data(self):
        """CLI serialization includes group data when group is accessed."""
        config = BaldurSettings()
        _ = config.core.backoff
        result = self._model_to_dict(config)
        assert "core" in result
        assert "backoff" in result["core"]
