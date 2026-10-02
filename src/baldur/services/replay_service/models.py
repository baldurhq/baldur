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
    # Set when the replay handler was called (it returned or raised): the
    # dependency may have been reached. False on every exit that never called
    # it — a gate refusal, a lost acquisition, a duplicate or in-progress key.
    handler_ran: bool = False
    # Set when the handler's work may still be running (a timeout gave up
    # waiting for it): the entry is left REPLAYING instead of completed.
    work_may_continue: bool = False

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
    # Where each selection lane of an on-recovery sweep stopped, keyed
    # `"{failure_type}|{domain or ''}"`. A follow-up pass hands these straight
    # back so it resumes instead of re-walking the prefix it already crossed;
    # they are strings because the follow-up arrives over a broker message.
    lane_cursors: dict[str, str] = field(default_factory=dict)
    # Lanes whose selector stopped on its scan bound rather than on an empty
    # pool. Keeps "there is nothing left" distinguishable from "there is more
    # behind a prefix of another failure type", which an empty result alone
    # cannot say.
    scan_exhausted_lanes: list[str] = field(default_factory=list)
    # The entry an on-recovery pass's deadline cut mid-replay, if one was. It is
    # neither counted nor processed (it stays PENDING ahead of the returned
    # cursors, to be replayed first by the next pass), but the pass did move
    # the backlog: the cut used one of that entry's replay attempts. A pass
    # whose first replay is cut completes nothing and leaves every cursor
    # where it was, so this is the only sign that it got anywhere.
    deadline_cut_dlq_id: str | None = None
    # True when the pass ended because a replay's own breaker refused the call
    # before the job began (its entry is the ``deadline_cut_dlq_id``): the
    # chain queues its next pass after a pause instead of at once.
    ended_by_breaker_refusal: bool = False
    # Domain-priority-based replay info
    priority_used: bool = False
    domains_processed: list[str] | None = None

    @property
    def scan_exhausted(self) -> bool:
        """True when any lane stopped on its scan bound."""
        return bool(self.scan_exhausted_lanes)
