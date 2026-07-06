"""
Replay Service Data Models.

Provides the ReplayResult and BatchReplayResult dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from baldur.models.governance import GovernanceCheckResult

# =============================================================================
# Replay Result
# =============================================================================


@dataclass
class ReplayResult:
    """Result of a replay operation."""

    success: bool
    dlq_id: str
    message: str = ""
    error: str | None = None
    data: dict[str, Any] | None = None
    skipped: bool = False

    @classmethod
    def succeeded(
        cls, dlq_id: str, message: str = "", data: dict | None = None
    ) -> ReplayResult:
        """Factory for successful replay."""
        return cls(success=True, dlq_id=dlq_id, message=message, data=data)

    @classmethod
    def failed(cls, dlq_id: str, error: str) -> ReplayResult:
        """Factory for failed replay."""
        return cls(success=False, dlq_id=dlq_id, error=error)

    @classmethod
    def skipped_result(cls, dlq_id: str, reason: str = "") -> ReplayResult:
        """Factory for idempotency-skipped replay."""
        return cls(
            success=True,
            dlq_id=dlq_id,
            skipped=True,
            message=f"Skipped: {reason}" if reason else "Skipped",
            data={"skip_reason": reason},
        )

    @classmethod
    def blocked(
        cls, dlq_id: str, governance_result: GovernanceCheckResult
    ) -> ReplayResult:
        """Factory for governance-blocked replay."""
        return cls(
            success=False,
            dlq_id=dlq_id,
            error=governance_result.block_message,
            data={
                "blocked": True,
                "block_reason": (
                    governance_result.block_reason.value
                    if governance_result.block_reason
                    else None
                ),
            },
        )


@dataclass
class BatchReplayResult:
    """Result of a batch replay operation."""

    total: int = 0
    success_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    results: list[ReplayResult] = field(default_factory=list)
    governance_blocked: bool = False
    governance_block_reason: str = ""
    # 497 D4: True when the per-service inflight lock (setnx-based) suppressed
    # this circuit-close sweep as a duplicate. Distinct from
    # `governance_blocked` because the operator-visible category differs —
    # governance = policy block; inflight = duplicate dispatch suppression.
    inflight_skipped: bool = False
    # True when an on-recovery (circuit-close) sweep filled its per-failure-type
    # quota exactly — the `on_recovery_max_items` cap may have left eligible
    # entries undrained. No data loss: remaining entries stay PENDING and drain
    # on the next CB close or a manual/scheduled replay. Answers "why weren't all
    # recovered" without inferring it from queue depth.
    capped: bool = False
    # Domain-priority-based replay info
    priority_used: bool = False
    domains_processed: list[str] | None = None
