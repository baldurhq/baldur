"""
Contract tests for checkpoint event names.

Verifies fix(356) corrections:
- checkpoint_registry.strategy_registered (was cell_registry.bulkheads_registered)
- *_unavailable in ImportError context (was *_available)
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

from baldur.audit.checkpoint.strategy import (
    CheckpointStorageStrategy,
)


class TestCheckpointRegistryEventNameContract:
    """CheckpointStrategyRegistry event names follow logging standard."""

    def test_strategy_registered_event_name(self) -> None:
        """Registry logs 'checkpoint_registry.strategy_registered' (not cell_registry.bulkheads_registered)."""
        from baldur.audit.checkpoint import CheckpointStrategyRegistry

        class _DummyStrategy(CheckpointStorageStrategy):
            def save(self, ns, data):
                pass

            def load(self, ns):
                return None

            def commit(self, ns):
                pass

            def delete(self, ns):
                return False

            def exists(self, ns):
                return False

        with patch(
            "baldur.audit.checkpoint.logger",
        ) as mock_logger:
            CheckpointStrategyRegistry.register("test_dummy", _DummyStrategy)

        mock_logger.info.assert_called_once()
        assert (
            mock_logger.info.call_args[0][0]
            == "checkpoint_registry.strategy_registered"
        )
        assert mock_logger.info.call_args[1]["strategy_name"] == "test_dummy"

        # Cleanup
        CheckpointStrategyRegistry._strategies.pop("test_dummy", None)

    def test_registry_source_has_no_cell_registry_event_name(self) -> None:
        """Checkpoint registry source does not contain old copy-paste event name."""
        from baldur.audit import checkpoint as checkpoint_init

        source = inspect.getsource(checkpoint_init)
        assert '"cell_registry.bulkheads_registered"' not in source
        assert '"checkpoint_registry.strategy_registered"' in source


class TestRedisCheckpointEventNameSourceContract:
    """Redis checkpoint storage event names: _unavailable on ImportError."""

    def test_distributed_lock_unavailable_in_source(self) -> None:
        """Source contains 'redis_checkpoint.distributed_lock_unavailable' (not *_available)."""
        from baldur.audit.checkpoint import redis_storage

        source = inspect.getsource(redis_storage)
        assert '"redis_checkpoint.distributed_lock_unavailable"' in source
        assert '"redis_checkpoint.distributedrecoverylock_available"' not in source

    def test_notification_manager_unavailable_in_source(self) -> None:
        """Source contains 'redis_checkpoint.notification_manager_unavailable' (not *_available)."""
        from baldur.audit.checkpoint import redis_storage

        source = inspect.getsource(redis_storage)
        assert '"redis_checkpoint.notification_manager_unavailable"' in source
        assert '"redis_checkpoint.unifiednotificationmanager_available"' not in source

    def test_checksum_module_unavailable_in_source(self) -> None:
        """Source contains 'redis_checkpoint.checksum_module_unavailable' (not *_available_skipping)."""
        from baldur.audit.checkpoint import redis_storage

        source = inspect.getsource(redis_storage)
        assert '"redis_checkpoint.checksum_module_unavailable"' in source
        assert '"redis_checkpoint.checksum_module_available_skipping"' not in source


class TestKafkaRedisCheckpointEventNameContract:
    """Kafka Redis checkpoint event names: _unavailable on ImportError."""

    def test_checksum_module_unavailable_in_source(self) -> None:
        """Source contains 'kafka_redis_checkpoint.checksum_module_unavailable'."""
        from baldur.audit.checkpoint import kafka_redis_storage

        source = inspect.getsource(kafka_redis_storage)
        assert '"kafka_redis_checkpoint.checksum_module_unavailable"' in source
        assert '"kafka_redis_checkpoint.checksum_module_available"' not in source
