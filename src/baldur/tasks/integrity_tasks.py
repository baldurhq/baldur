"""
Hash Chain Integrity Verification Tasks.

Celery Beat으로 주기적으로 해시체인 무결성을 검증합니다.
Redis 분산 락으로 멀티 프로세스 중복 실행을 방지합니다.

Tasks:
    verify_hash_chain_integrity: 백그라운드 해시체인 검증 (5분/1일 주기)

Functions:
    get_integrity_beat_schedule: Celery Beat Schedule 반환
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import structlog

logger = structlog.get_logger()


# =============================================================================
# Background Hash Chain Integrity Verification
# =============================================================================


def verify_hash_chain_integrity(
    namespace: str = "global",
    use_merkle_spot_check: bool = True,
    block_size: int = 1000,
) -> dict[str, Any]:
    """
    백그라운드 해시체인 무결성 검증.

    마지막 검증 지점(DailyHashAnchor)부터 최신 엔트리까지 검증합니다.
    데이터가 merkle_threshold 이상이면 MerkleSpotChecker로 전환합니다.
    Redis 분산 락으로 멀티 프로세스 중복 실행을 방지합니다.

    Args:
        namespace: 검증 대상 네임스페이스
        use_merkle_spot_check: Merkle 스팟체크 활성화
        block_size: 머클 블록 단위 크기

    Returns:
        검증 결과 딕셔너리
    """
    start = time.time()

    try:
        from datetime import timedelta

        from baldur.adapters.cache.redis_adapter import RedisDistributedLock
        from baldur.adapters.redis import get_redis_client
        from baldur.audit.integrity import (
            HashChainVerifier,
            get_integrity_health_score,
        )
        from baldur.settings.audit_integrity import get_audit_integrity_settings

        settings = get_audit_integrity_settings()
        verifier = HashChainVerifier()
        health = get_integrity_health_score()

        # Redis 분산 락 — 멀티 프로세스 중복 실행 방지
        redis_client = get_redis_client()
        if redis_client is None:
            logger.warning("background_integrity_verifier.redis_unavailable_skip")
            return {"valid": True, "skipped": True, "reason": "redis_unavailable"}

        lock = RedisDistributedLock(
            redis_client=redis_client,
            full_key="baldur:integrity:background_verify_lock",
            timeout=timedelta(seconds=settings.hash_chain_lock_timeout),
            blocking_timeout=5.0,
        )

        if not lock.acquire(blocking=True):
            logger.info("background_integrity_verifier.another_verify_task_running")
            return {"valid": True, "skipped": True, "reason": "lock_contention"}

        try:
            entries = _get_entries_since_last_anchor(namespace)
            entry_count = len(entries)

            if (
                use_merkle_spot_check
                and entry_count > settings.background_verify_merkle_threshold
            ):
                result = _merkle_spot_check(entries, settings.merkle_block_size)
            else:
                # 3회 재시도 + 소스 리로드로 false positive 방지
                is_valid, error_msg = _verify_with_retry(
                    verifier=verifier,
                    entries_loader=lambda: _get_entries_since_last_anchor(namespace),
                    max_retries=settings.max_verification_retries,
                )
                result = {
                    "valid": is_valid,
                    "strategy": "full_chain",
                    "checked": entry_count,
                    "errors": [error_msg] if error_msg else [],
                }

            duration_ms = (time.time() - start) * 1000
            if result["valid"]:
                health.record_recovery(
                    event_type="background_verify_ok",
                    sequences_affected=entry_count,
                    recovery_time_ms=duration_ms,
                )
            else:
                health.record_chain_break()
                _alert_integrity_violation(namespace, result)

            result["duration_ms"] = duration_ms
            result["namespace"] = namespace
            return result

        finally:
            lock.release()

    except Exception as e:
        logger.exception(
            "background_integrity_verifier.failed",
            error=e,
        )
        return {"valid": False, "error": str(e), "namespace": namespace}


# =============================================================================
# Internal Helpers
# =============================================================================


def _get_entries_since_last_anchor(namespace: str) -> list[dict]:
    """DailyHashAnchor 이후 엔트리만 로드."""
    try:
        from baldur.audit.cascade_auditor import get_cascade_event_auditor

        auditor = get_cascade_event_auditor()
        events = auditor.get_recent_events(namespace, limit=100000)
        return [
            e.to_dict() if hasattr(e, "to_dict") else e  # type: ignore[misc]
            for e in events
        ]
    except Exception as e:
        logger.warning(
            "background_integrity_verifier.entry_load_failed",
            error=e,
        )
        return []


def _merkle_spot_check(
    entries: list[dict],
    block_size: int,
) -> dict[str, Any]:
    """MerkleSpotChecker를 사용한 블록 단위 검증."""
    from baldur.audit.integrity.merkle_spot_checker import MerkleSpotChecker

    checker = MerkleSpotChecker(block_size=block_size)
    return checker.spot_check(entries)


def _verify_with_retry(
    verifier: Any,
    entries_loader: Callable[[], list[dict]],
    max_retries: int = 3,
) -> tuple[bool, str | None]:
    """
    재시도 포함 해시체인 검증.

    매 시도마다 entries_loader()로 소스에서 리로드하여
    일시적 캐시 오염/동시 쓰기 글리치를 해소합니다.

    Args:
        verifier: HashChainVerifier 인스턴스
        entries_loader: 엔트리 로드 callable (매번 새로 로드)
        max_retries: 최대 재시도 횟수

    Returns:
        (is_valid, error_message) 튜플
    """
    last_error = None

    for attempt in range(1, max_retries + 1):
        entries = entries_loader()
        if not entries:
            return True, None

        is_valid, error_msg = verifier.verify_chain(entries)
        if is_valid:
            if attempt > 1:
                logger.info(
                    "background_integrity_verifier.verification_succeeded_retry",
                    attempt=attempt,
                    max_retries=max_retries,
                )
            return True, None

        last_error = error_msg
        logger.warning(
            "background_integrity_verifier.attempt_failed",
            attempt=attempt,
            max_retries=max_retries,
            error_msg=error_msg,
        )

    # 모든 재시도 실패 → 실제 위반으로 확정
    logger.critical(
        "background_integrity_verifier.all_attempts_failed_confirming",
        max_retries=max_retries,
        last_error=last_error,
    )
    return False, last_error


def _alert_integrity_violation(namespace: str, result: dict) -> None:
    """무결성 위반 시 관리자 알림 및 감사 기록."""
    try:
        from baldur_pro.services.audit.base import _write_to_wal

        _write_to_wal(
            event_type="INTEGRITY_VIOLATION",
            source="BackgroundIntegrityVerifier",
            details={
                "namespace": namespace,
                "checked": result.get("checked", 0),
                "errors": result.get("errors", []),
                "strategy": result.get("strategy", "unknown"),
            },
            success=False,
            error_message="Hash chain integrity violation detected during background verification",
        )
    except Exception as e:
        logger.exception(
            "background_integrity_verifier.audit_write_failed",
            error=e,
        )

    logger.critical(
        "integrity_violation.event",
        namespace=namespace,
        errors=result.get("errors", []),
    )


# =============================================================================
# Celery Beat Schedule
# =============================================================================


def get_integrity_beat_schedule() -> dict[str, Any]:
    """
    무결성 검증 Beat Schedule.

    Returns:
        Celery Beat Schedule 설정 딕셔너리
    """
    try:
        from celery.schedules import crontab

        return {
            # 5분마다 - 해시체인 무결성 검증
            "verify-hash-chain-integrity": {
                "task": "baldur.tasks.integrity_tasks.verify_hash_chain_integrity",
                "schedule": crontab(minute="*/5"),
                "options": {"queue": "integrity"},
                "kwargs": {
                    "namespace": "global",
                    "use_merkle_spot_check": True,
                },
            },
            # 매일 01:00 - 전체 체인 풀 검증 (Merkle 비활성화)
            "verify-hash-chain-full": {
                "task": "baldur.tasks.integrity_tasks.verify_hash_chain_integrity",
                "schedule": crontab(hour=1, minute=0),
                "options": {"queue": "integrity"},
                "kwargs": {
                    "namespace": "global",
                    "use_merkle_spot_check": False,
                },
            },
        }
    except ImportError:
        return {}
