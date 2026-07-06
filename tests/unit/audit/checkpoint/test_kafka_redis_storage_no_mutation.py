"""
Unit tests for kafka_redis_storage.py fix(356) — no input data mutation.

Tests:
J. save() must not mutate the input UnifiedCheckpointData object.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from baldur.audit.checkpoint.strategy import UnifiedCheckpointData


class TestKafkaRedisStorageNoMutationBehavior:
    """save() must not mutate the caller's UnifiedCheckpointData."""

    def _make_storage(self, mock_redis=None):
        """Create KafkaRedisCheckpointStorage with mocked Redis."""
        from baldur.audit.checkpoint.kafka_redis_storage import (
            KafkaRedisCheckpointStorage,
        )

        return KafkaRedisCheckpointStorage(
            redis_client=mock_redis or MagicMock(),
            default_topic="test-topic",
        )

    def test_save_does_not_mutate_input_data_kafka_fields(self) -> None:
        """Input data's kafka_topic/partition/offset remain None after save."""
        mock_redis = MagicMock()
        storage = self._make_storage(mock_redis)

        data = UnifiedCheckpointData(
            wal_sequence=42,
            kafka_topic=None,
            kafka_partition=None,
            kafka_offset=None,
        )

        storage.save("test-ns", data)

        # Input data must NOT be mutated
        assert data.kafka_topic is None
        assert data.kafka_partition is None
        assert data.kafka_offset is None

    def test_save_does_not_mutate_input_data_with_existing_values(self) -> None:
        """Input data's existing kafka values are preserved unchanged."""
        mock_redis = MagicMock()
        storage = self._make_storage(mock_redis)

        data = UnifiedCheckpointData(
            wal_sequence=42,
            kafka_topic="my-topic",
            kafka_partition=3,
            kafka_offset=999,
        )

        storage.save("test-ns", data)

        assert data.kafka_topic == "my-topic"
        assert data.kafka_partition == 3
        assert data.kafka_offset == 999

    def test_save_uses_default_topic_for_redis_when_input_is_none(self) -> None:
        """Redis receives the default_topic when input kafka_topic is None."""
        import json

        mock_redis = MagicMock()
        storage = self._make_storage(mock_redis)

        data = UnifiedCheckpointData(
            wal_sequence=42,
            kafka_topic=None,
            kafka_partition=None,
            kafka_offset=None,
        )

        storage.save("test-ns", data)

        # Verify Redis received default values
        mock_redis.set.assert_called_once()
        redis_key, redis_value = mock_redis.set.call_args[0]
        saved_data = json.loads(redis_value)
        assert saved_data["kafka_topic"] == "test-topic"
        assert saved_data["kafka_partition"] == 0
        assert saved_data["kafka_offset"] == 0

    def test_save_preserves_input_kafka_values_in_redis(self) -> None:
        """Redis receives the caller's explicit kafka values."""
        import json

        mock_redis = MagicMock()
        storage = self._make_storage(mock_redis)

        data = UnifiedCheckpointData(
            wal_sequence=42,
            kafka_topic="custom-topic",
            kafka_partition=7,
            kafka_offset=12345,
        )

        storage.save("test-ns", data)

        mock_redis.set.assert_called_once()
        redis_key, redis_value = mock_redis.set.call_args[0]
        saved_data = json.loads(redis_value)
        assert saved_data["kafka_topic"] == "custom-topic"
        assert saved_data["kafka_partition"] == 7
        assert saved_data["kafka_offset"] == 12345
