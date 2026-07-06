"""
Post-Recovery Integrity Gate.

CB CLOSED 시 리플레이 전에 WAL↔해시체인 무결성을 검증합니다.
실패 시 리플레이를 차단하고 관리자에게 경고합니다.

EventBus 핸들러 우선순위:
    CRITICAL: 이 게이트 (리플레이보다 먼저 실행)
    NORMAL: _on_circuit_breaker_closed (리플레이)
    LOW: _on_circuit_breaker_closed_postmortem
"""

from __future__ import annotations

import time
from typing import Any

import structlog

logger = structlog.get_logger()

# 이벤트 데이터 플래그 키 (bus.py에서 import하여 참조)
INTEGRITY_GATE_KEY = "integrity_gate_result"
INTEGRITY_FAILED_KEY = "integrity_failed"


# =============================================================================
# EventBus Handler
# =============================================================================


def on_circuit_breaker_closed_integrity_gate(event: Any) -> None:
    """
    CB 복구 시 WAL 무결성 게이트.

    CRITICAL 우선순위로 등록되어 Replay 핸들러보다 먼저 실행됩니다.

    동작:
        1. 서킷이 Open이었던 시간대의 WAL 엔트리 수집
        2. 해당 구간의 해시체인 무결성 검증
        3. 실패 시 event.data에 integrity_failed=True 설정
        4. 후속 _on_circuit_breaker_closed가 이 플래그를 확인
    """
    service_name = event.data.get("service_name", "unknown")
    start = time.time()

    # 설정에서 Fail-Open/Secure 모드 로드
    try:
        from baldur.settings.audit_integrity import get_audit_integrity_settings

        fail_open = get_audit_integrity_settings().integrity_gate_fail_open
    except Exception:
        fail_open = True  # 설정 로드 실패 시 안전한 기본값

    logger.info(
        "integrity_gate.checking_wal_integrity_before",
        service_name=service_name,
        fail_open=fail_open,
    )

    try:
        result = _verify_recovery_window_integrity(service_name, event)
        duration_ms = (time.time() - start) * 1000

        event.data[INTEGRITY_GATE_KEY] = {
            "valid": result["valid"],
            "checked": result.get("checked", 0),
            "duration_ms": duration_ms,
            "strategy": result.get("strategy", "full_chain"),
        }

        if not result["valid"]:
            event.data[INTEGRITY_FAILED_KEY] = True
            logger.critical(
                "integrity_gate.integrity_violation_replay_blocked",
                service_name=service_name,
                errors=result.get("errors", []),
            )
            _send_integrity_violation_alert(service_name, result, duration_ms)
        else:
            event.data[INTEGRITY_FAILED_KEY] = False
            logger.info(
                "integrity_gate.integrity_ok_entries_ms",
                service_name=service_name,
                checked=result.get("checked", 0),
                duration_ms=duration_ms,
            )

        _update_health_score(result, duration_ms)

    except Exception as e:
        # Fail-Open/Secure 설정에 따라 분기
        if fail_open:
            logger.warning(
                "integrity_gate.gate_check_failed_proceeding",
                service_name=service_name,
                error=e,
            )
            event.data[INTEGRITY_FAILED_KEY] = False
        else:
            logger.critical(
                "integrity_gate.gate_check_failed_fail",
                service_name=service_name,
                error=e,
            )
            event.data[INTEGRITY_FAILED_KEY] = True

        event.data[INTEGRITY_GATE_KEY] = {
            "valid": None,
            "error": str(e),
            "policy": "fail_open" if fail_open else "fail_secure",
        }


# =============================================================================
# Internal Helpers
# =============================================================================


def _verify_recovery_window_integrity(
    service_name: str,
    event: Any,
) -> dict[str, Any]:
    """
    서킷 Open 기간 동안 쌓인 WAL 데이터의 해시체인 검증.

    Returns:
        {"valid": bool, "checked": int, "errors": list, "strategy": str}
    """
    from baldur.audit.integrity import HashChainVerifier

    verifier = HashChainVerifier()

    # WAL에서 미동기화 엔트리 수집
    wal_entries = _get_unsynced_wal_entries(service_name)

    if not wal_entries:
        return {"valid": True, "checked": 0, "errors": [], "strategy": "no_entries"}

    # 해시체인 검증
    is_valid, error_msg = verifier.verify_chain(wal_entries)
    issues = verifier.find_tampering(wal_entries) if not is_valid else []

    return {
        "valid": is_valid,
        "checked": len(wal_entries),
        "errors": [i["message"] for i in issues]
        if issues
        else ([error_msg] if error_msg else []),
        "strategy": "wal_chain_verify",
    }


def _get_unsynced_wal_entries(service_name: str) -> list[dict]:
    """
    WAL에서 아직 동기화되지 않은 엔트리 조회.

    wal.recover_unprocessed()를 사용하여 미처리 엔트리를 수집합니다.
    WALEntry.data 필드(dict)를 추출하여 반환합니다.
    """
    try:
        from baldur_pro.services.audit.base import _get_wal

        wal = _get_wal()
        if wal is None:
            return []

        # recover_unprocessed (wal.py L772)
        # last_processed_seq=0 → 전체 미처리 엔트리 수집
        wal_entries = wal.recover_unprocessed(last_processed_seq=0)
        # WALEntry.data 필드 추출 (wal.py L76: WALEntry.data: dict[str, Any])
        return [e.data for e in wal_entries if hasattr(e, "data")]

    except Exception as e:
        logger.warning(
            "integrity_gate.wal_read_failed",
            error=e,
        )
        return []


def _send_integrity_violation_alert(
    service_name: str,
    result: dict,
    duration_ms: float,
) -> None:
    """무결성 위반 알림 발송 및 감사 기록."""
    try:
        from baldur_pro.services.audit.base import _write_to_wal

        _write_to_wal(
            event_type="INTEGRITY_VIOLATION",
            source="PostRecoveryIntegrityGate",
            details={
                "service_name": service_name,
                "checked": result.get("checked", 0),
                "errors": result.get("errors", []),
                "duration_ms": duration_ms,
            },
            success=False,
            error_message="Hash chain integrity violation detected during post-recovery check",
        )
    except Exception as e:
        logger.exception(
            "integrity_gate.audit_write_failed",
            error=e,
        )


def _update_health_score(result: dict, duration_ms: float) -> None:
    """IntegrityHealthScore 업데이트."""
    try:
        from baldur.audit.integrity import get_integrity_health_score

        health = get_integrity_health_score()
        if result["valid"]:
            health.record_recovery(
                event_type="post_recovery_gate_ok",
                sequences_affected=result.get("checked", 0),
                recovery_time_ms=duration_ms,
            )
        else:
            health.record_chain_break()
    except Exception as e:
        logger.debug(
            "integrity_gate.health_score_update_failed",
            error=e,
        )
