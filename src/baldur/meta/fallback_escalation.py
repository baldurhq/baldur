"""
Fallback Escalation Handler.

Slack/PagerDuty 전송 실패 시 로컬 디스크에 기록.
나중에 drain하거나 수동으로 처리할 수 있도록 합니다.

기능:
- 실패한 에스컬레이션을 JSONL 파일에 기록
- 메모리 버퍼 폴백 (디스크 쓰기도 실패 시)
- 대기 중인 에스컬레이션 조회 및 카운트
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import structlog

from baldur.core.file_utils import safe_unlink
from baldur.utils.serialization import fast_dumps_str, fast_loads
from baldur.utils.time import utc_now

logger = structlog.get_logger()

# 폴백 로그 경로
DEFAULT_ESCALATION_LOG_PATH = Path(
    os.environ.get(
        "BALDUR_EMERGENCY_ESCALATION_LOG",
        "/var/log/baldur/emergency_escalation.jsonl",
    )
)


class FallbackEscalationHandler:
    """
    폴백 에스컬레이션 핸들러.

    Slack/PagerDuty API 장애 시 로컬 디스크에 기록합니다.
    나중에 drain하거나 수동으로 처리 가능합니다.

    저장 구조:
    - 파일: JSONL (줄당 하나의 JSON 객체)
    - 메모리 버퍼: 디스크 쓰기 실패 시 폴백

    사용 예시:
        handler = FallbackEscalationHandler()

        handler.record_failed_escalation(
            component="dlq",
            title="DLQ Stuck",
            description="DLQ consumer stopped",
            level="critical",
            details={"pending_count": 1500},
            failed_channels=["pagerduty", "slack"],
            error_message="Connection timeout",
        )

        # 대기 중인 에스컬레이션 확인
        count = handler.get_pending_count()
        entries = handler.get_pending_escalations()
    """

    def __init__(
        self,
        log_path: Path | str | None = None,
        max_buffer_size: int = 1000,
    ):
        """
        초기화.

        Args:
            log_path: 로그 파일 경로 (None이면 기본값)
            max_buffer_size: 메모리 버퍼 최대 크기
        """
        self._log_path = Path(log_path) if log_path else DEFAULT_ESCALATION_LOG_PATH
        self._lock = (
            threading.RLock()
        )  # 재진입 가능 락 (drain_to_file에서 _write_to_file 호출 시 필요)
        self._memory_buffer: list[dict[str, Any]] = []
        self._max_buffer_size = max_buffer_size

    def _ensure_directory(self) -> bool:
        """
        로그 디렉토리 생성.

        Returns:
            디렉토리 생성 성공 여부
        """
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            return True
        except Exception as e:
            logger.exception(
                "fallback_escalation.cannot_create_directory",
                error=e,
            )
            return False

    def record_failed_escalation(
        self,
        component: str,
        title: str,
        description: str,
        level: str,
        details: dict[str, Any],
        failed_channels: list[str],
        error_message: str,
    ) -> bool:
        """
        실패한 에스컬레이션 기록.

        Args:
            component: 컴포넌트 이름
            title: 에스컬레이션 제목
            description: 설명
            level: 심각도 레벨
            details: 상세 정보
            failed_channels: 실패한 채널 목록
            error_message: 에러 메시지

        Returns:
            기록 성공 여부
        """
        entry = {
            "timestamp": utc_now().isoformat(),
            "type": "EMERGENCY_ESCALATION_FAILED",
            "component": component,
            "title": title,
            "description": description,
            "level": level,
            "details": details,
            "failed_channels": failed_channels,
            "error": error_message,
            "requires_manual_review": True,
        }

        # 1. 파일에 기록 시도
        if self._write_to_file(entry):
            return True

        # 2. 메모리 버퍼에 저장 (폴백)
        return self._write_to_memory(entry)

    def _write_to_file(self, entry: dict[str, Any]) -> bool:
        """
        파일에 기록.

        Args:
            entry: 기록할 엔트리

        Returns:
            기록 성공 여부
        """
        if not self._ensure_directory():
            return False

        try:
            with self._lock, open(self._log_path, "a", encoding="utf-8") as f:
                f.write(fast_dumps_str(entry) + "\n")

            logger.warning(
                "emergency_escalation_log.recorded",
                component_name=entry["component"],
                title=entry["title"],
            )
            return True
        except Exception as e:
            logger.exception(
                "fallback_escalation.file_write_failed",
                error=e,
            )
            return False

    def _write_to_memory(self, entry: dict[str, Any]) -> bool:
        """
        메모리 버퍼에 저장.

        Args:
            entry: 저장할 엔트리

        Returns:
            저장 성공 여부
        """
        with self._lock:
            self._memory_buffer.append(entry)
            # 버퍼 크기 제한
            if len(self._memory_buffer) > self._max_buffer_size:
                self._memory_buffer = self._memory_buffer[-self._max_buffer_size :]

        logger.warning(
            "fallback_escalation.stored_memory_buffer_size",
            component_name=entry["component"],
            memory_buffer_count=len(self._memory_buffer),
        )
        return True

    def get_pending_escalations(self) -> list[dict[str, Any]]:
        """
        대기 중인 에스컬레이션 조회.

        파일과 메모리 버퍼 모두에서 조회합니다.

        Returns:
            대기 중인 에스컬레이션 목록
        """
        entries: list[dict[str, Any]] = []

        # 파일에서 읽기
        if self._log_path.exists():
            try:
                with open(self._log_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            entries.append(fast_loads(line))
            except Exception as e:
                logger.exception(
                    "fallback_escalation.file_read_failed",
                    error=e,
                )

        # 메모리 버퍼 추가
        with self._lock:
            entries.extend(self._memory_buffer)

        return entries

    def get_pending_count(self) -> int:
        """
        대기 중인 에스컬레이션 수.

        Returns:
            대기 중인 에스컬레이션 수
        """
        count = 0

        # 파일에서 라인 수 카운트
        if self._log_path.exists():
            try:
                with open(self._log_path, encoding="utf-8") as f:
                    count = sum(1 for line in f if line.strip())
            except Exception:
                pass

        # 메모리 버퍼 카운트
        with self._lock:
            count += len(self._memory_buffer)

        return count

    def drain_to_file(self) -> int:
        """
        메모리 버퍼를 파일로 drain.

        Returns:
            drain된 엔트리 수
        """
        with self._lock:
            if not self._memory_buffer:
                return 0

            drained = 0
            for entry in self._memory_buffer:
                if self._write_to_file(entry):
                    drained += 1

            self._memory_buffer.clear()
            return drained

    def clear_file(self) -> None:
        """파일 초기화 (처리 완료 후)."""
        if safe_unlink(self._log_path):
            logger.info("fallback_escalation.file_cleared")

    def clear_memory(self) -> None:
        """메모리 버퍼 초기화."""
        with self._lock:
            self._memory_buffer.clear()

    def clear_all(self) -> None:
        """파일과 메모리 버퍼 모두 초기화."""
        self.clear_file()
        self.clear_memory()


# =============================================================================
# 싱글톤
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

(
    get_fallback_escalation_handler,
    configure_fallback_escalation_handler,
    reset_fallback_escalation_handler,
) = make_singleton_factory("fallback_escalation_handler", FallbackEscalationHandler)
