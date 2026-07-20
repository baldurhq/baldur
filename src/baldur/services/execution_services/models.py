"""
Execution Services - Result Types

Result dataclasses used by the chaos-experiment and config-apply services.
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
    """Experiment execution result."""

    checked: int = 0
    """Number of experiments checked."""

    executed: int = 0
    """Number of experiments executed."""

    skipped: int = 0
    """Number of experiments skipped."""

    blocked: int = 0
    """Number of experiments blocked."""

    errors: list[dict[str, Any]] = field(default_factory=list)
    """Errors."""

    experiments: list[dict[str, Any]] = field(default_factory=list)
    """Per-experiment results."""

    governance_blocked: bool = False
    """Whether governance blocked the whole run."""

    governance_block_reason: str = ""
    """Governance block reason."""


@dataclass
class DailyReportResult(SerializableMixin):
    """Daily report result."""

    success: bool
    report_id: str | None = None
    grade: str | None = None
    summary: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class ApprovalCleanupResult(SerializableMixin):
    """Approval cleanup result."""

    schedule_expired: int = 0
    blast_radius_expired: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class PendingApprovalCheckResult(SerializableMixin):
    """Pending-approval check result."""

    pending_schedules: int = 0
    pending_blast_radius: int = 0
    alerts_sent: int = 0
    notification_status: str = "sent"
    error: str | None = None
