"""CheckpointStrategyRegistry and factory singleton unit tests.

Tests registry operations, factory function routing, and singleton
lifecycle for the checkpoint storage strategy package.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.audit.checkpoint import (
    CheckpointStrategyRegistry,
    FileCheckpointStorage,
    get_checkpoint_strategy,
    get_default_checkpoint_strategy,
    reset_default_checkpoint_strategy,
)
from baldur.audit.checkpoint.strategy import CheckpointStorageStrategy

# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset registry and singleton before and after each test."""
    CheckpointStrategyRegistry.clear()
    reset_default_checkpoint_strategy()
    yield
    CheckpointStrategyRegistry.clear()
    reset_default_checkpoint_strategy()


# =============================================================================
# Behavior: CheckpointStrategyRegistry register / get / clear
# =============================================================================


class TestCheckpointStrategyRegistryBehavior:
    """CheckpointStrategyRegistry core operations behavior tests."""

    def test_register_adds_strategy(self):
        """register() makes a strategy available via get()."""
        mock_cls = MagicMock(spec=type)
        mock_cls.return_value = MagicMock(spec=CheckpointStorageStrategy)

        CheckpointStrategyRegistry.register("test_strategy", mock_cls)
        result = CheckpointStrategyRegistry.get("test_strategy")

        assert result is not None
        mock_cls.assert_called_once()

    def test_get_returns_registered_strategy_instance(self):
        """get() returns an instance of the registered strategy class."""
        mock_instance = MagicMock(spec=CheckpointStorageStrategy)
        mock_cls = MagicMock(spec=type, return_value=mock_instance)

        CheckpointStrategyRegistry.register("custom", mock_cls)
        result = CheckpointStrategyRegistry.get("custom")

        assert result is mock_instance

    def test_list_strategies_auto_registers_builtins(self):
        """list_strategies() triggers auto-registration of built-in strategies."""
        strategies = CheckpointStrategyRegistry.list_strategies()

        assert "file" in strategies
        assert "redis" in strategies
        assert "kafka_redis" in strategies
        assert "composite" in strategies

    def test_list_strategies_includes_custom(self):
        """list_strategies() includes both built-in and custom strategies."""
        mock_cls = MagicMock(spec=type)
        CheckpointStrategyRegistry.register("my_custom", mock_cls)

        strategies = CheckpointStrategyRegistry.list_strategies()

        assert "my_custom" in strategies
        assert "file" in strategies  # Built-in also present

    def test_clear_empties_registry(self):
        """clear() removes all registered strategies and instances."""
        # Given — register something
        mock_cls = MagicMock(spec=type)
        mock_cls.return_value = MagicMock(spec=CheckpointStorageStrategy)
        CheckpointStrategyRegistry.register("to_clear", mock_cls)
        CheckpointStrategyRegistry.get("to_clear")

        # When
        CheckpointStrategyRegistry.clear()

        # Then — auto-register will re-add builtins, but "to_clear" is gone
        strategies = CheckpointStrategyRegistry.list_strategies()
        assert "to_clear" not in strategies

    def test_get_unknown_strategy_raises_value_error(self):
        """get() raises ValueError for unknown strategy name."""
        with pytest.raises(ValueError, match="Unknown checkpoint strategy"):
            CheckpointStrategyRegistry.get("nonexistent_strategy")

    def test_get_with_force_new_creates_fresh_instance(self):
        """get(force_new=True) creates a new instance each time."""
        mock_cls = MagicMock(spec=type)
        instance1 = MagicMock(spec=CheckpointStorageStrategy)
        instance2 = MagicMock(spec=CheckpointStorageStrategy)
        mock_cls.side_effect = [instance1, instance2]

        CheckpointStrategyRegistry.register("refresh", mock_cls)
        result1 = CheckpointStrategyRegistry.get("refresh")
        result2 = CheckpointStrategyRegistry.get("refresh", force_new=True)

        assert result1 is instance1
        assert result2 is instance2

    def test_get_without_force_new_caches_instance(self):
        """get() without force_new returns same cached instance."""
        mock_cls = MagicMock(spec=type)
        mock_instance = MagicMock(spec=CheckpointStorageStrategy)
        mock_cls.return_value = mock_instance

        CheckpointStrategyRegistry.register("cached", mock_cls)
        result1 = CheckpointStrategyRegistry.get("cached")
        result2 = CheckpointStrategyRegistry.get("cached")

        assert result1 is result2
        mock_cls.assert_called_once()


# =============================================================================
# Behavior: get_checkpoint_strategy() factory function
# =============================================================================


class TestGetCheckpointStrategyBehavior:
    """get_checkpoint_strategy() factory function behavior tests."""

    def test_file_storage_type_returns_file_checkpoint(self):
        """storage_type='file' returns FileCheckpointStorage instance."""
        result = get_checkpoint_strategy(storage_type="file")
        assert isinstance(result, FileCheckpointStorage)

    def test_redis_storage_type_without_client_raises(self):
        """storage_type='redis' without redis_client raises ValueError."""
        with pytest.raises(ValueError, match="redis_client is required"):
            get_checkpoint_strategy(storage_type="redis")

    def test_kafka_redis_storage_type_without_client_raises(self):
        """storage_type='kafka_redis' without redis_client raises ValueError."""
        with pytest.raises(ValueError, match="redis_client is required"):
            get_checkpoint_strategy(storage_type="kafka_redis")

    def test_unknown_storage_type_raises_value_error(self):
        """Unknown storage_type not in registry raises ValueError."""
        with pytest.raises(ValueError, match="Unknown storage_type"):
            get_checkpoint_strategy(storage_type="totally_unknown")

    def test_composite_without_redis_client_raises(self):
        """storage_type='composite' with redis primary and no client raises."""
        with pytest.raises(ValueError, match="redis_client is required"):
            get_checkpoint_strategy(storage_type="composite", primary_type="redis")

    @patch.dict(
        "os.environ",
        {"BALDUR_CHECKPOINT_STORAGE": "file"},
        clear=False,
    )
    def test_none_storage_type_reads_env_var(self):
        """storage_type=None reads from BALDUR_CHECKPOINT_STORAGE env var."""
        result = get_checkpoint_strategy(storage_type=None)
        assert isinstance(result, FileCheckpointStorage)


# =============================================================================
# Behavior: Singleton lifecycle
# =============================================================================


class TestCheckpointSingletonBehavior:
    """get_default_checkpoint_strategy / reset singleton lifecycle tests."""

    @patch.dict(
        "os.environ",
        {"BALDUR_CHECKPOINT_STORAGE": "file"},
        clear=False,
    )
    def test_get_default_returns_same_instance(self):
        """get_default_checkpoint_strategy() returns same instance on repeated calls."""
        first = get_default_checkpoint_strategy()
        second = get_default_checkpoint_strategy()
        assert first is second

    @patch.dict(
        "os.environ",
        {"BALDUR_CHECKPOINT_STORAGE": "file"},
        clear=False,
    )
    def test_reset_clears_singleton(self):
        """After reset, get_default_checkpoint_strategy() returns new instance."""
        first = get_default_checkpoint_strategy()
        reset_default_checkpoint_strategy()
        second = get_default_checkpoint_strategy()
        assert first is not second

    @patch.dict(
        "os.environ",
        {"BALDUR_CHECKPOINT_STORAGE": "file"},
        clear=False,
    )
    def test_default_strategy_is_file_by_default(self):
        """Default strategy is FileCheckpointStorage when env is 'file'."""
        result = get_default_checkpoint_strategy()
        assert isinstance(result, FileCheckpointStorage)
