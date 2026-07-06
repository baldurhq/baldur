"""
Baldur Recovery Event Logger.

DB 붕괴 → 서킷 오픈 → DLQ 적재 → 복구 → 리플레이 완료 과정을
해시 체인으로 기록하여 감사 증적을 제공합니다.

Usage:
    recovery_logger = get_recovery_logger()

    # 복구 이벤트 체인 시작
    chain_id = recovery_logger.start_recovery_chain(
        trigger="db_connection_exhausted",
        affected_services=["database"],
    )

    # 이벤트 기록
    recovery_logger.log_event(chain_id, "circuit_opened", {...})
    recovery_logger.log_event(chain_id, "dlq_items_stored", {...})
    recovery_logger.log_event(chain_id, "system_recovered", {...})
    recovery_logger.log_event(chain_id, "dlq_replay_completed", {...})

    # 체인 완료
    summary = recovery_logger.complete_chain(chain_id)
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = structlog.get_logger()


class BaldurRecoveryLogger:
    """
    Baldur 복구 이벤트 로거.

    DB 붕괴 → 서킷 오픈 → DLQ 적재 → 복구 → 리플레이 완료 과정을
    해시 체인으로 기록하여 감사 증적을 제공합니다.
    """

    def __init__(self):
        """Initialize recovery logger."""
        self._chains: dict[str, dict[str, Any]] = {}
        self._audit_logger = None
        self._lock = None

    def _lazy_init(self) -> None:
        """Lazy initialization."""
        if self._lock is None:
            import threading

            self._lock = threading.Lock()

        if self._audit_logger is None:
            try:
                from baldur.audit import get_audit_logger

                self._audit_logger = get_audit_logger()
            except Exception:
                pass

    def start_recovery_chain(
        self,
        trigger: str,
        affected_services: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Start a new recovery event chain."""
        import uuid

        self._lazy_init()

        chain_id = f"recovery_{uuid.uuid4().hex[:12]}"

        chain_data = {
            "chain_id": chain_id,
            "trigger": trigger,
            "affected_services": affected_services,
            "started_at": utc_now().isoformat(),
            "events": [],
            "metadata": metadata or {},
            "status": "in_progress",
        }

        with self._lock:
            self._chains[chain_id] = chain_data

        # 시작 이벤트 기록
        self.log_event(
            chain_id,
            "recovery_chain_started",
            {
                "trigger": trigger,
                "affected_services": affected_services,
            },
        )

        return chain_id

    def log_event(
        self,
        chain_id: str,
        event_type: str,
        data: dict[str, Any],
        request: HttpRequest | None = None,
    ) -> bool:
        """
        Log an event to the recovery chain.

        request 파라미터 추가하여 AuditMiddleware 버퍼 패턴 지원
        (복구 체인은 대부분 비동기 컨텍스트에서 실행되므로 request가 없는 경우가 많음)
        """
        self._lazy_init()

        with self._lock:
            chain = self._chains.get(chain_id)
            if not chain:
                logger.warning(
                    "recovery_logger.chain_found",
                    chain_id=chain_id,
                )
                return False

            event = {
                "sequence": len(chain["events"]) + 1,
                "event_type": event_type,
                "timestamp": utc_now().isoformat(),
                "data": data,
            }

            chain["events"].append(event)

        # === 버퍼 패턴 우선 ===
        if request is not None:
            try:
                from baldur.audit.event_buffer import (
                    AuditEventType,
                    RequestAuditBuffer,
                )

                buffer = RequestAuditBuffer.get_or_create(request)
                buffer.add(
                    event_type=AuditEventType.RECOVERY_EVENT,
                    source="BaldurRecoveryLogger",
                    details={
                        "recovery_chain_id": chain_id,
                        "event_type": f"recovery_{event_type}",
                        "sequence": event["sequence"],
                        **data,
                    },
                    success=True,
                )
                return True
            except ImportError:
                pass

        # === Fallback: 기존 방식 ===
        try:
            if self._audit_logger:
                self._audit_logger.log(
                    {
                        "recovery_chain_id": chain_id,
                        "event_type": f"recovery_{event_type}",
                        "sequence": event["sequence"],
                        "source": "BaldurRecoveryLogger",
                        **data,
                    }
                )
        except Exception as e:
            logger.warning(
                "recovery_logger.audit_log_failed",
                error=e,
            )

        return True

    def complete_chain(
        self,
        chain_id: str,
        success: bool = True,
        summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Complete a recovery chain and return summary."""
        self._lazy_init()

        with self._lock:
            chain = self._chains.get(chain_id)
            if not chain:
                return {"error": f"Chain not found: {chain_id}"}

            chain["status"] = "completed" if success else "failed"
            chain["completed_at"] = utc_now().isoformat()
            chain["success"] = success

            # 소요 시간 계산
            started = datetime.fromisoformat(chain["started_at"].replace("Z", "+00:00"))
            completed = datetime.fromisoformat(
                chain["completed_at"].replace("Z", "+00:00")
            )
            chain["duration_seconds"] = (completed - started).total_seconds()

            if summary:
                chain["summary"] = summary

        # 완료 이벤트 기록
        self.log_event(
            chain_id,
            "recovery_chain_completed",
            {
                "success": success,
                "duration_seconds": chain["duration_seconds"],
                "total_events": len(chain["events"]),
                "summary": summary or {},
            },
        )

        return chain

    def get_chain(self, chain_id: str) -> dict[str, Any] | None:
        """Get a recovery chain by ID."""
        self._lazy_init()
        with self._lock:
            return self._chains.get(chain_id)

    def generate_audit_report(self, chain_id: str) -> dict[str, Any]:
        """Generate audit report for a recovery chain."""
        chain = self.get_chain(chain_id)
        if not chain:
            return {"error": f"Chain not found: {chain_id}"}

        # 이벤트 타임라인 생성
        timeline = []
        for event in chain.get("events", []):
            timeline.append(
                {
                    "sequence": event["sequence"],
                    "event": event["event_type"],
                    "time": event["timestamp"],
                    "data_summary": {
                        k: v
                        for k, v in event.get("data", {}).items()
                        if not k.startswith("_")
                    },
                }
            )

        return {
            "chain_id": chain_id,
            "status": chain.get("status"),
            "trigger": chain.get("trigger"),
            "affected_services": chain.get("affected_services"),
            "started_at": chain.get("started_at"),
            "completed_at": chain.get("completed_at"),
            "duration_seconds": chain.get("duration_seconds"),
            "success": chain.get("success"),
            "total_events": len(timeline),
            "timeline": timeline,
            "summary": chain.get("summary", {}),
            "hash_chain_enabled": self._audit_logger is not None,
        }


# Singleton instance
_recovery_logger: BaldurRecoveryLogger | None = None
_recovery_logger_lock = threading.Lock()


def get_recovery_logger() -> BaldurRecoveryLogger:
    """Get the singleton recovery logger instance."""
    global _recovery_logger
    if _recovery_logger is None:
        with _recovery_logger_lock:
            if _recovery_logger is None:
                _recovery_logger = BaldurRecoveryLogger()
    return _recovery_logger
