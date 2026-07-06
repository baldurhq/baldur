"""
WAL → Kafka 동기화 헬퍼.

WAL 시퀀스와 Kafka 오프셋을 원자적으로 기록하여
'어디까지 Kafka로 보냈는지'를 추적합니다.

KafkaRedisCheckpointStorage (checkpoint_strategy.py)를 사용하여
체크포인트를 관리합니다.

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
# WAL → Kafka 동기화 헬퍼
# =============================================================================


def sync_wal_to_kafka_with_checkpoint(
    wal,
    producer,
    checkpoint_strategy: CheckpointStorageStrategy,
    namespace: str = "default",
) -> int:
    """
    WAL → Kafka 동기화 (체크포인트 기반).

    마지막 체크포인트 이후의 엔트리만 전송하여 중복 방지.
    KafkaAuditProducer 또는 기존 KafkaAuditAdapter 모두 지원.

    Args:
        wal: WriteAheadLog 인스턴스
        producer: KafkaAuditProducer 또는 KafkaAuditAdapter 인스턴스
        checkpoint_strategy: CheckpointStorageStrategy 인스턴스
        namespace: 네임스페이스

    Returns:
        동기화된 엔트리 수
    """
    from baldur.audit.checkpoint import UnifiedCheckpointData

    last_seq = checkpoint_strategy.get_wal_sequence(namespace)

    entries = wal.recover_unprocessed(last_processed_seq=last_seq)
    synced = 0

    # Producer 타입 감지: KafkaAuditProducer vs 기존 Adapter
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
