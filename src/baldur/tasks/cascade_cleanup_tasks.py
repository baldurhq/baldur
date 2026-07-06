"""
Cascade Cleanup Tasks - Cascade 데이터 정리 태스크.

Cascade Event 데이터의 보관/정리/아카이브 관련 Celery 태스크를 정의합니다.

Tasks:
- archive_cascade_events: Redis → PostgreSQL 이관
- purge_old_cascade_events: 오래된 이벤트 영구 삭제
- create_cascade_daily_checkpoint: 일일 체크포인트 생성
- verify_cascade_chain_integrity: 체인 무결성 검증
- recover_cascade_from_wal: 로컬 WAL 복구

WAL 용어 통일:
    시스템 전반에서 wal_dir, WriteAheadLog 등 WAL 용어 사용.
    (backend.py, services/audit/base.py, audit/wal.py 참조)

Reference:
    docs/baldur/middleware_system/76_CASCADE_EVENT_AUDIT.md
    tasks/cleanup_tasks.py (archive_old_dlq_entries 패턴)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from baldur.utils.serialization import fast_dumps_str, fast_loads
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# =============================================================================
# Archive Task
# =============================================================================


def archive_cascade_events(
    namespace: str = "global",
    older_than_days: int = 7,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Redis에서 PostgreSQL로 Cascade 이벤트 이관.

    Hot tier(Redis)에서 Warm tier(PostgreSQL)로 오래된 이벤트를 이관합니다.

    Args:
        namespace: 네임스페이스
        older_than_days: 이 일수보다 오래된 이벤트 이관
        dry_run: True면 실제 이관 없이 대상만 확인

    Returns:
        이관 결과 통계

    Code reference:
        tasks/cleanup_tasks.py (archive_old_dlq_entries 패턴)
    """
    from baldur.audit.cascade_auditor import get_cascade_event_auditor

    auditor = get_cascade_event_auditor()
    cutoff_time = utc_now() - timedelta(days=older_than_days)
    cutoff_str = cutoff_time.isoformat()

    # 모든 이벤트 조회
    events = auditor.get_recent_events(namespace, limit=10000)

    # older_than_days보다 오래된 이벤트 필터링
    to_archive = []
    for event in events:
        try:
            event_time = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
            if event_time < cutoff_time:
                to_archive.append(event)
        except ValueError:
            continue

    if dry_run:
        logger.info(
            "cascade_cleanup.archive_dry_run_found",
            to_archive_count=len(to_archive),
            namespace=namespace,
            older_than_days=older_than_days,
        )
        return {
            "status": "dry_run",
            "namespace": namespace,
            "events_to_archive": len(to_archive),
            "cutoff_time": cutoff_str,
            "archived": 0,
        }

    # PostgreSQL로 이관 (실제 Django ORM 사용 시)
    archived_count = 0
    failed_count = 0

    for event in to_archive:
        try:
            # PostgreSQL 저장 (실제 구현 시 Django ORM 사용)
            _archive_single_event_to_db(event)
            archived_count += 1
        except Exception as e:
            logger.exception(
                "cascade_cleanup.archive_failed",
                cascade_event_id=event.id,
                error=e,
            )
            failed_count += 1

    logger.info(
        "cascade_cleanup.archive_completed",
        archived_count=archived_count,
        failed_count=failed_count,
        namespace=namespace,
    )

    return {
        "status": "completed",
        "namespace": namespace,
        "archived": archived_count,
        "failed": failed_count,
        "cutoff_time": cutoff_str,
    }


def _archive_single_event_to_db(event: Any) -> None:
    """
    단일 Cascade Event를 Repository를 통해 저장.

    ProviderRegistry에서 cascade_event_repo를 가져와 저장합니다.
    Repository가 없는 환경에서는 로컬 파일에 폴백합니다.
    """
    try:
        from baldur.factory.registry import ProviderRegistry
        from baldur.models.cascade_event import CascadeEventData

        repo = ProviderRegistry.get_cascade_event_repo()
        data = CascadeEventData.from_cascade_event(event)
        repo.save(data)
    except Exception:
        # Repository 사용 불가 시 로컬 파일에 저장
        _archive_single_event_to_file(event)


def _archive_single_event_to_file(event: Any) -> None:
    """
    단일 Cascade Event를 로컬 파일에 저장.

    PostgreSQL을 사용할 수 없는 환경에서 폴백으로 사용합니다.
    """
    archive_dir = Path("/tmp/cascade_archive")
    archive_dir.mkdir(parents=True, exist_ok=True)

    # 월별 파일로 저장
    try:
        event_date = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
        month_str = event_date.strftime("%Y_%m")
    except ValueError:
        month_str = utc_now().strftime("%Y_%m")

    archive_file = archive_dir / f"cascade_{event.namespace}_{month_str}.jsonl"

    with open(archive_file, "a", encoding="utf-8") as f:
        f.write(fast_dumps_str(event.to_dict()) + "\n")


# =============================================================================
# Purge Task
# =============================================================================


def purge_old_cascade_events(
    namespace: str = "global",
    older_than_days: int = 365,
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    오래된 Cascade 이벤트 영구 삭제.

    ⚠️ 고위험 작업: 기본값 dry_run=True

    아카이브된 이벤트 중 보관 기간이 지난 이벤트를 영구 삭제합니다.

    Args:
        namespace: 네임스페이스
        older_than_days: 이 일수보다 오래된 이벤트 삭제
        dry_run: True면 실제 삭제 없이 대상만 확인 (기본값)

    Returns:
        삭제 결과 통계
    """
    from baldur.audit.cascade_auditor import get_cascade_event_auditor

    auditor = get_cascade_event_auditor()
    cutoff_time = utc_now() - timedelta(days=older_than_days)
    cutoff_str = cutoff_time.isoformat()

    # 모든 이벤트 조회
    events = auditor.get_recent_events(namespace, limit=10000)

    # older_than_days보다 오래된 이벤트 필터링
    to_purge = []
    for event in events:
        try:
            event_time = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
            if event_time < cutoff_time:
                to_purge.append(event)
        except ValueError:
            continue

    if dry_run:
        logger.warning(
            "cascade_cleanup.purge_dry_run_found",
            to_purge_count=len(to_purge),
            namespace=namespace,
            older_than_days=older_than_days,
        )
        return {
            "status": "dry_run",
            "namespace": namespace,
            "events_to_purge": len(to_purge),
            "cutoff_time": cutoff_str,
            "purged": 0,
        }

    # 실제 삭제
    purged_count = 0
    failed_count = 0

    for event in to_purge:
        try:
            _delete_cascade_event(event.id, namespace)
            purged_count += 1
        except Exception as e:
            logger.exception(
                "cascade_cleanup.purge_failed",
                cascade_event_id=event.id,
                error=e,
            )
            failed_count += 1

    logger.warning(
        "cascade_cleanup.purge_completed",
        purged_count=purged_count,
        failed_count=failed_count,
        namespace=namespace,
    )

    return {
        "status": "completed",
        "namespace": namespace,
        "purged": purged_count,
        "failed": failed_count,
        "cutoff_time": cutoff_str,
    }


def _delete_cascade_event(cascade_id: str, namespace: str) -> None:
    """
    단일 Cascade Event 삭제.

    Redis에서 이벤트를 삭제합니다.
    """
    from baldur.core.state_backend import get_state_backend

    backend = get_state_backend()

    # 이벤트 삭제
    event_key = f"baldur:{namespace}:audit:cascade:{cascade_id}"
    backend.delete(event_key)

    # 인덱스에서 제거
    index_key = f"baldur:{namespace}:audit:cascade_index"
    index_data = backend.get(index_key)

    if index_data:
        ids = index_data if isinstance(index_data, list) else index_data.get("ids", [])
        if cascade_id in ids:
            ids.remove(cascade_id)
            backend.set(index_key, {"ids": ids})


# =============================================================================
# Checkpoint Task
# =============================================================================


def create_cascade_daily_checkpoint(
    namespace: str = "global",
) -> dict[str, Any]:
    """
    일일 체크포인트 생성.

    Daily Celery Beat에서 호출되어 Hash Chain 체크포인트를 생성합니다.
    이후 무결성 검증 시 체크포인트 이후만 검증하여 효율성을 높입니다.

    Args:
        namespace: 네임스페이스

    Returns:
        생성된 체크포인트 정보

    Code reference:
        audit/integrity/anchor.py (DailyHashAnchor 패턴)
    """
    from baldur.audit.cascade_auditor import get_cascade_event_auditor

    auditor = get_cascade_event_auditor()
    checkpoint = auditor.create_checkpoint(namespace)

    logger.info(
        "cascade_cleanup.daily_checkpoint_created",
        namespace=namespace,
        checkpoint=checkpoint.get("event_count"),
    )

    # 머클 블록 루트도 함께 빌드 (스팟체크 기준선)
    try:
        from baldur.adapters.redis import get_redis_client
        from baldur.audit.integrity.merkle_spot_checker import MerkleSpotChecker
        from baldur.settings.audit_integrity import get_audit_integrity_settings

        settings = get_audit_integrity_settings()
        redis_client = get_redis_client()
        checker = MerkleSpotChecker(
            block_size=settings.merkle_block_size,
            redis_client=redis_client,
            namespace=namespace,
        )
        events = auditor.get_recent_events(namespace, limit=100000)
        merkle_result = checker.build_merkle_roots(
            [
                e.to_dict() if hasattr(e, "to_dict") else e  # type: ignore[misc]
                for e in events
            ]
        )
        checkpoint["merkle_blocks"] = merkle_result["blocks_stored"]
    except Exception as e:
        logger.warning(
            "cascade_cleanup.merkle_root_build_failed",
            error=e,
        )

    return checkpoint


# =============================================================================
# Integrity Verification Task
# =============================================================================


def verify_cascade_chain_integrity(
    namespace: str = "global",
    use_checkpoint: bool = True,
) -> dict[str, Any]:
    """
    Hash Chain 무결성 검증.

    Cascade Event의 Hash Chain 무결성을 검증합니다.
    체크포인트가 있으면 체크포인트 이후만 검증하여 효율성을 높입니다.

    Args:
        namespace: 네임스페이스
        use_checkpoint: 체크포인트 기반 검증 사용 여부

    Returns:
        검증 결과
    """
    from baldur.audit.cascade_auditor import get_cascade_event_auditor

    auditor = get_cascade_event_auditor()

    if use_checkpoint:
        result = auditor.verify_chain_integrity_from_checkpoint(namespace)
    else:
        result = auditor.verify_chain_integrity(namespace)

    if result["valid"]:
        logger.info(
            "cascade_cleanup.chain_integrity_verified",
            namespace=namespace,
            checked_count=result["checked"],
        )
    else:
        logger.error(
            "cascade_cleanup.chain_integrity_failed",
            namespace=namespace,
            errors_count=len(result["errors"]),
        )

    return result


# =============================================================================
# WAL Recovery Task
# =============================================================================


LOCAL_CASCADE_WAL_DIR = Path("/var/log/baldur/cascade_wal")
"""로컬 WAL 디렉토리 경로 (시스템 전반 WAL 용어 통일)."""

LOCAL_CASCADE_WAL_PATH = LOCAL_CASCADE_WAL_DIR / "cascade_audit_wal.jsonl"
"""로컬 WAL 파일 경로."""

# 하위 호환성
LOCAL_FALLBACK_PATH = LOCAL_CASCADE_WAL_PATH


def recover_cascade_from_wal(
    namespace: str = "global",
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    로컬 WAL에서 Redis로 복구.

    Redis 장애 복구 후 로컬 WAL에 쌓인 엔트리를 Redis로 이관합니다.

    WAL 용어 통일:
        시스템 전반에서 wal_dir, WriteAheadLog 등 WAL 용어 사용.
        (backend.py, services/audit/base.py, audit/wal.py 참조)

    Args:
        namespace: 네임스페이스
        dry_run: True면 실제 복구 없이 대상만 확인

    Returns:
        복구 결과 통계

    Code reference:
        audit/graceful_degradation/manager.py#L180-220 (reconcile 패턴)
    """
    from baldur.audit.cascade_auditor import get_cascade_event_auditor
    from baldur.audit.cascade_event import CascadeEvent

    if not LOCAL_CASCADE_WAL_PATH.exists():
        return {
            "status": "no_wal_data",
            "namespace": namespace,
            "recovered": 0,
            "failed": 0,
        }

    auditor = get_cascade_event_auditor()
    entries = []

    # WAL 파일에서 해당 네임스페이스 엔트리 읽기
    with open(LOCAL_CASCADE_WAL_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                entry = fast_loads(line.strip())
                if entry.get("namespace") == namespace:
                    entries.append(entry)
            except (ValueError, TypeError):
                continue

    if dry_run:
        logger.info(
            "cascade_cleanup.wal_recovery_dry_run",
            entries_count=len(entries),
            namespace=namespace,
        )
        return {
            "status": "dry_run",
            "namespace": namespace,
            "entries_to_recover": len(entries),
            "recovered": 0,
        }

    # Redis로 복구
    recovered = 0
    failed = 0

    for entry in entries:
        try:
            event = CascadeEvent.from_dict(entry)
            auditor._save_cascade_event(event)
            auditor._add_to_index(namespace, event.id)
            recovered += 1
        except Exception as e:
            logger.exception(
                "watchdog.recovery_failed",
                error=e,
            )
            failed += 1

    # 복구 완료 후 WAL 파일에서 해당 네임스페이스 엔트리 제거
    if recovered > 0 and failed == 0:
        _remove_namespace_from_wal(namespace)

    logger.info(
        "cascade_cleanup.wal_recovery_completed",
        recovered=recovered,
        failed=failed,
        namespace=namespace,
    )

    return {
        "status": "completed",
        "namespace": namespace,
        "recovered": recovered,
        "failed": failed,
    }


# 하위 호환성
recover_cascade_from_fallback = recover_cascade_from_wal


def _remove_namespace_from_wal(namespace: str) -> None:
    """WAL 파일에서 특정 네임스페이스 엔트리 제거."""
    if not LOCAL_CASCADE_WAL_PATH.exists():
        return

    remaining = []

    with open(LOCAL_CASCADE_WAL_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                entry = fast_loads(line.strip())
                if entry.get("namespace") != namespace:
                    remaining.append(line)
            except (ValueError, TypeError):
                remaining.append(line)

    if remaining:
        with open(LOCAL_CASCADE_WAL_PATH, "w", encoding="utf-8") as f:
            f.writelines(remaining)
    else:
        LOCAL_CASCADE_WAL_PATH.unlink(missing_ok=True)


# 하위 호환성
_remove_namespace_from_fallback = _remove_namespace_from_wal


# =============================================================================
# Celery Beat Schedule
# =============================================================================


CASCADE_CLEANUP_SCHEDULE = {
    "archive-cascade-to-postgres": {
        "task": "baldur.tasks.cascade_cleanup_tasks.archive_cascade_events",
        "schedule_description": "daily at 03:00",  # crontab(hour=3, minute=0)
        "kwargs": {"older_than_days": 7},
    },
    "create-cascade-daily-checkpoint": {
        "task": "baldur.tasks.cascade_cleanup_tasks.create_cascade_daily_checkpoint",
        "schedule_description": "daily at 00:05",  # crontab(hour=0, minute=5)
    },
    "verify-cascade-chain-integrity": {
        "task": "baldur.tasks.cascade_cleanup_tasks.verify_cascade_chain_integrity",
        "schedule_description": "daily at 04:00",  # crontab(hour=4, minute=0)
    },
}
"""
Celery Beat 스케줄 정의.

실제 등록은 celery_app.py 또는 Django settings에서 수행합니다.
"""
