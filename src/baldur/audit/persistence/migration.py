"""
Disk Buffer Drain-on-Startup 마이그레이션.

Pod 재시작 시 이전에 영속된 이벤트를 주 스토리지로 플러시합니다.
애플리케이션 시작 시 호출하여 이전 세션의 미처리 이벤트를 복구합니다.

사용법:
    from baldur.audit.persistence import (
        DiskPersistentBuffer,
        drain_on_startup,
    )

    buffer = DiskPersistentBuffer()

    def send_to_primary(entries: list[dict]) -> bool:
        # 주 스토리지 (Kafka, DB 등)로 전송
        return True

    result = drain_on_startup(
        buffer=buffer,
        flush_handler=send_to_primary,
    )

    print(f"Drained: {result.drained}, Failed: {result.failed}")
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from baldur.audit.persistence.disk_buffer import (
        DiskPersistentBuffer,
    )

logger = structlog.get_logger()


@dataclass
class DrainResult:
    """Drain 작업 결과."""

    drained: int = 0
    """성공적으로 플러시된 엔트리 수."""

    failed: int = 0
    """플러시 실패한 엔트리 수."""

    skipped: int = 0
    """건너뛴 엔트리 수."""

    duration_seconds: float = 0.0
    """작업 소요 시간 (초)."""

    errors: list[str] = field(default_factory=list)
    """발생한 에러 메시지 목록."""


def drain_on_startup(
    buffer: DiskPersistentBuffer,
    flush_handler: Callable[[list[dict[str, Any]]], bool],
    batch_size: int = 100,
    max_batches: int | None = None,
    fail_fast: bool = False,
) -> DrainResult:
    """
    시작 시 버퍼에 남은 이벤트를 주 스토리지로 플러시.

    Pod 재시작 후 이전 세션에서 저장된 미처리 이벤트를
    주 스토리지 (Kafka, DB 등)로 전송합니다.

    Args:
        buffer: DiskPersistentBuffer 인스턴스
        flush_handler: 이벤트 배치 처리 핸들러 (성공 시 True 반환)
        batch_size: 배치 크기
        max_batches: 최대 처리 배치 수 (None=무제한)
        fail_fast: 실패 시 즉시 중단

    Returns:
        DrainResult

    Usage:
        result = drain_on_startup(
            buffer=buffer,
            flush_handler=lambda entries: kafka_producer.send_batch(entries),
            batch_size=100,
        )
    """
    start_time = time.time()
    result = DrainResult(errors=[])

    entry_count = buffer.count()
    if entry_count == 0:
        logger.info("drain_on_startup.no_pending_entries_drain")
        return result

    logger.info(
        "drain_on_startup.draining_pending_entries",
        entry_count=entry_count,
    )

    batches_processed = 0

    while True:
        if max_batches and batches_processed >= max_batches:
            logger.warning(
                "drain_on_startup.max_batches_reached",
                max_batches=max_batches,
            )
            break

        # 배치 조회
        entries = list(buffer.iter_entries(limit=batch_size))
        if not entries:
            break

        # 핸들러 호출
        try:
            # BufferEntry → dict 변환
            entry_dicts = [e.data for e in entries]
            success = flush_handler(entry_dicts)
        except Exception as e:
            error_msg = f"Handler error: {e}"
            logger.exception(
                "drain_on_startup.event",
                error_msg=error_msg,
            )
            result.errors.append(error_msg)
            result.failed += len(entries)

            if fail_fast:
                break
            continue

        if success:
            # 성공 시 삭제
            keys = [e.key for e in entries]
            deleted = buffer.delete_batch(keys)
            result.drained += deleted
            logger.debug(
                "drain_on_startup.drained_batch_entries",
                deleted=deleted,
            )
        else:
            # 실패 시 해당 배치 스킵 (다음 배치 시도)
            result.skipped += len(entries)
            logger.warning(
                "drain_on_startup.batch_failed_skipping_entries",
                entries_count=len(entries),
            )

            if fail_fast:
                break

        batches_processed += 1

    result.duration_seconds = time.time() - start_time

    logger.info(
        "drain_on_startup.complete",
        drained_count=result.drained,
        failed=result.failed,
        skipped=result.skipped,
        duration_seconds=result.duration_seconds,
    )

    return result


async def async_drain_on_startup(
    buffer: DiskPersistentBuffer,
    async_flush_handler: Callable[[list[dict[str, Any]]], Any],
    batch_size: int = 100,
    max_batches: int | None = None,
) -> DrainResult:
    """
    비동기 버전의 drain_on_startup.

    비동기 주 스토리지 핸들러를 사용하는 경우에 적합합니다.

    Args:
        buffer: DiskPersistentBuffer 인스턴스
        async_flush_handler: 비동기 이벤트 핸들러
        batch_size: 배치 크기
        max_batches: 최대 배치 수

    Returns:
        DrainResult

    Usage:
        async def send_to_kafka(entries):
            await kafka_producer.send_batch(entries)
            return True

        result = await async_drain_on_startup(
            buffer=buffer,
            async_flush_handler=send_to_kafka,
        )
    """
    import asyncio

    start_time = time.time()
    result = DrainResult(errors=[])

    entry_count = buffer.count()
    if entry_count == 0:
        return result

    logger.info(
        "async_drain_on_startup.draining_entries",
        entry_count=entry_count,
    )

    batches_processed = 0

    while True:
        if max_batches and batches_processed >= max_batches:
            break

        entries = list(buffer.iter_entries(limit=batch_size))
        if not entries:
            break

        try:
            entry_dicts = [e.data for e in entries]
            success = await async_flush_handler(entry_dicts)
        except Exception as e:
            result.errors.append(str(e))
            result.failed += len(entries)
            continue

        if success:
            keys = [e.key for e in entries]
            deleted = buffer.delete_batch(keys)
            result.drained += deleted
        else:
            result.skipped += len(entries)

        batches_processed += 1
        await asyncio.sleep(0)  # 이벤트 루프 양보

    result.duration_seconds = time.time() - start_time

    logger.info(
        "async_drain_on_startup.complete",
        drained_count=result.drained,
        failed=result.failed,
        skipped=result.skipped,
    )

    return result
