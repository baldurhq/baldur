"""
WAL → Kafka sync helper.

Atomically records the WAL sequence together with the Kafka offset to track
"how far we have sent to Kafka".

Uses KafkaRedisCheckpointStorage (checkpoint_strategy.py) to manage
checkpoints.

Usage:
    from baldur.audit.kafka_checkpoint import sync_wal_to_kafka_with_checkpoint
    from baldur.audit.checkpoint import (
        KafkaRedisCheckpointStorage,
        UnifiedCheckpointData,
    )

    strategy = KafkaRedisCheckpointStorage(redis_client=redis)

    synced = sync_wal_to_kafka_with_checkpoint(
        wal=wal,
        producer=producer,
        checkpoint_strategy=strategy,
        namespace="default",
    )

Version: 2.0.0
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from baldur.audit.checkpoint import CheckpointStorageStrategy

logger = structlog.get_logger()


# =============================================================================
# WAL → Kafka sync helper
# =============================================================================


def sync_wal_to_kafka_with_checkpoint(
    wal,
    producer,
    checkpoint_strategy: CheckpointStorageStrategy,
    namespace: str = "default",
) -> int:
    """
    WAL → Kafka sync (checkpoint-based).

    Sends only the entries after the last checkpoint to avoid duplicates.
    Supports both KafkaAuditProducer and the existing KafkaAuditAdapter.

    Args:
        wal: WriteAheadLog instance
        producer: KafkaAuditProducer or KafkaAuditAdapter instance
        checkpoint_strategy: CheckpointStorageStrategy instance
        namespace: Namespace

    Returns:
        Number of entries synced
    """
    from baldur.audit.checkpoint import UnifiedCheckpointData

    last_seq = checkpoint_strategy.get_wal_sequence(namespace)

    entries = wal.recover_unprocessed(last_processed_seq=last_seq)
    synced = 0

    # Detect the producer type: KafkaAuditProducer vs the existing Adapter
    is_new_producer = hasattr(producer, "publish_audit_event")

    for entry in entries:
        try:
            if is_new_producer:
                success = producer.publish_audit_event(
                    event=entry.data,
                    domain=namespace,
                )
                if not success:
                    logger.error(
                        "wal.kafka_publish_failed",
                        entry_sequence=entry.sequence,
                    )
                    break

                remaining = producer.flush(timeout=5.0)
                if remaining > 0:
                    logger.warning(
                        "wal.kafka_messages_pending",
                        remaining=remaining,
                    )

                kafka_topic = producer._settings.full_audit_topic
            else:
                from baldur.interfaces.audit_adapter import AuditEntry

                audit_entry = AuditEntry(**entry.data)
                producer.log(audit_entry)
                producer.flush(timeout=5.0)
                kafka_topic = producer._settings.topic

            checkpoint_strategy.save(
                namespace,
                UnifiedCheckpointData(
                    wal_sequence=entry.sequence,
                    kafka_topic=kafka_topic,
                    kafka_partition=0,
                    kafka_offset=0,
                    checksum=entry.checksum,
                ),
            )
            synced += 1

        except Exception as e:
            logger.exception(
                "wal.kafka_sync_failed",
                entry_sequence=entry.sequence,
                error=e,
            )
            break

    return synced
