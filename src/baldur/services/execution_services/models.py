"""
Execution Services - Result Types

Chaos 실험 및 설정 적용 서비스에서 사용하는 결과 데이터 클래스.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from baldur.core.serializable import SerializableMixin

# =============================================================================
# Result Types
# =============================================================================


@dataclass
class ExperimentExecutionResult(SerializableMixin):
    """실험 실행 결과."""

    checked: int = 0
    """체크된 실험 수."""

    executed: int = 0
    """실행된 실험 수."""

    skipped: int = 0
    """스킵된 실험 수."""

    blocked: int = 0
    """차단된 실험 수."""

    errors: list[dict[str, Any]] = field(default_factory=list)
    """에러 목록."""

    experiments: list[dict[str, Any]] = field(default_factory=list)
    """개별 실험 결과."""

    governance_blocked: bool = False
    """거버넌스에 의해 전체 차단되었는지."""

    governance_block_reason: str = ""
    """거버넌스 차단 사유."""


@dataclass
class DailyReportResult(SerializableMixin):
    """일일 보고서 결과."""

    success: bool
    report_id: str | None = None
    grade: str | None = None
    summary: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class ApprovalCleanupResult(SerializableMixin):
    """승인 정리 결과."""

    schedule_expired: int = 0
    blast_radius_expired: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class PendingApprovalCheckResult(SerializableMixin):
    """대기 중인 승인 체크 결과."""

    pending_schedules: int = 0
    pending_blast_radius: int = 0
    alerts_sent: int = 0
    notification_status: str = "sent"
    error: str | None = None
