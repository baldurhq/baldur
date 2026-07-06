"""Unit tests for the config-history rollback apply rewire (662 D2a).

Target: ``baldur.api.handlers.config_history._apply_config_values`` — the
rewired rollback apply path that delegates the full real-field snapshot to the
manager's generic ``apply_config_values`` (replacing the drifted typed-method
dispatch map whose signatures raised ``TypeError`` on every Pydantic-class
domain).

Verification techniques (§8):
  - §8.5 Dependency interaction — delegation target + actor/values passthrough
  - §8.2 Exception/edge — None-manager (OSS, no PRO backend) raises RuntimeError

The PRO manager is supplied via the real ``ProviderRegistry`` slot (register a
fake "pro" provider) rather than importing baldur_pro — ``safe_get()`` resolves
it, matching the handler's actual lookup, with no PRO dependency.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from baldur.api.handlers.config_history import _apply_config_values
from baldur.factory import ProviderRegistry


class TestConfigRollbackApply:
    """``_apply_config_values`` delegates to ``manager.apply_config_values``."""

    def test_delegates_full_snapshot_to_manager(self):
        manager = MagicMock()
        snapshot = {"max_attempts": 5, "backoff_strategy": "exponential"}

        with ProviderRegistry.runtime_config_manager.snapshot():
            ProviderRegistry.runtime_config_manager.register("pro", lambda: manager)
            _apply_config_values("retry", snapshot, changed_by="alice")

        manager.apply_config_values.assert_called_once()
        args, kwargs = manager.apply_config_values.call_args
        assert args[0] == "retry"
        assert args[1] == snapshot
        assert kwargs["changed_by"] == "alice"
        assert "retry" in kwargs["reason"]

    def test_default_changed_by_is_system(self):
        manager = MagicMock()

        with ProviderRegistry.runtime_config_manager.snapshot():
            ProviderRegistry.runtime_config_manager.register("pro", lambda: manager)
            _apply_config_values("dlq", {"max_replay_attempts": 3})

        _, kwargs = manager.apply_config_values.call_args
        assert kwargs["changed_by"] == "system"

    def test_missing_manager_raises_runtime_error(self):
        """OSS (no PRO RuntimeConfigManager registered) → RuntimeError, not a
        silent no-op rollback."""
        with ProviderRegistry.runtime_config_manager.snapshot():
            ProviderRegistry.runtime_config_manager.reset()
            with pytest.raises(RuntimeError, match="baldur_pro"):
                _apply_config_values("retry", {"max_attempts": 5})
