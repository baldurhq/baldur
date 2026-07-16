"""
DLQ Entry Operations Mixin.

Provides methods for retry, resolve, force-redrive, and entry management
operations. Uses Repository pattern for domain-free architecture.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.trace import extract_origin_trace
from baldur.core.exceptions import (
    DLQEntryNotFoundError,
    DLQError,
    DLQReplayError,
    DLQStateConflictError,
)
from baldur.interfaces.repositories import FailedOperationStatus
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    pass

logger = structlog.get_logger()

__all__ = ["EntryOperationsMixin"]


class EntryOperationsMixin:
    """Mixin providing DLQ entry operations using Repository pattern."""

    def retry_entry(self, pk: str, reason: str | None = None) -> dict[str, Any]:
        """
        Retry a single DLQ entry by re-executing it through its replay handler.

        Applies the same per-entry pipeline as the batch ``replay()`` path to
        the single operator-selected entry: cap-gate, atomic acquire,
        handler execution, then a cap-aware terminal transition. ``success``
        means the replay succeeded — not merely that the counter advanced.

        Args:
            pk: Entry primary key
            reason: Optional operator justification — persisted into the
                resolution_note when the retry resolves the entry

        Returns:
            Dict with operation details:
                - success: bool (True only if the replay handler succeeded)
                - id: str
                - retry_count: int (post-attempt count)
                - previous_retry_count: int
                - status: str (resulting entry status)
                - message: str

        Raises:
            DLQEntryNotFoundError: If entry not found (-> HTTP 404)
            DLQStateConflictError: If entry is resolved/archived, has exhausted
                its replay attempts (at cap), or is not in a replayable state
                (-> HTTP 409)
            DLQReplayError: If the replay raised an unexpected exception
                (-> HTTP 500)
        """
        entry = self.repository.get_by_id(pk)

        if entry is None:
            raise DLQEntryNotFoundError(f"DLQ entry {pk} not found")

        if entry.status == "resolved":
            raise DLQStateConflictError("Cannot retry an already resolved entry")

        if entry.status == "archived":
            raise DLQStateConflictError("Cannot retry an archived entry")

        # D3: hard-block a past-cap entry before attempting replay.
        if not entry.can_retry:
            raise DLQStateConflictError(
                f"Cannot retry: entry {pk} has exhausted replay attempts "
                f"({entry.retry_count}/{entry.max_retries})"
            )

        old_count = entry.retry_count

        # D8: atomic cap-gate + retry_count++ + PENDING->REPLAYING. None covers
        # the under-cap-but-non-PENDING case (e.g. REVIEWING) and lost races,
        # which the can_retry gate alone would let through.
        acquired = self.repository.try_acquire_for_replay(
            pk, self.config.max_replay_attempts
        )
        if acquired is None:
            raise DLQStateConflictError(
                f"Cannot retry: entry {pk} is not in a replayable state "
                f"(not pending, over cap, or lost a concurrent acquire)"
            )

        # 679 D5: origin trace captured at store time — retry_entry emits no
        # per-entry replay audit / DLQ_REPLAY_* event, so origin surfaces on the
        # retry log lines (the centralized span link lives in _execute_replay).
        origin_trace_id = extract_origin_trace(acquired.metadata)["origin_trace_id"]
        origin_log_fields = (
            {"origin_trace_id": origin_trace_id} if origin_trace_id else {}
        )

        # Manual retry does NOT go through AdaptiveThrottle — throttle
        # backpressure governs automatic sweeps, not a deliberate operator action.
        try:
            replay_success = self._execute_replay(acquired)
        except Exception as e:
            # Never strand the entry in REPLAYING; release via complete_replay.
            self.repository.complete_replay(pk, success=False, note=str(e))
            self._emit_replay_exhausted(acquired)
            logger.warning(
                "dlq.entry_retry_failed",
                record_pk=pk,
                entry_domain=acquired.domain,
                failure_type=acquired.failure_type,
                error=e,
                **origin_log_fields,
            )
            raise DLQReplayError(f"Retry failed for entry {pk}: {e}") from e

        if replay_success:
            self.resolve_entry(
                pk,
                notes=reason or "manual_retry",
                resolution_type="manual_retry",
            )
            logger.info(
                "dlq.entry_retry_triggered",
                record_pk=pk,
                entry_domain=acquired.domain,
                failure_type=acquired.failure_type,
                **origin_log_fields,
            )
            return {
                "success": True,
                "id": pk,
                "retry_count": acquired.retry_count,
                "previous_retry_count": old_count,
                "status": FailedOperationStatus.RESOLVED.value,
                "message": f"Replay succeeded for entry {pk}",
            }

        # Handler ran but failed: cap-aware terminal transition (REQUIRES_REVIEW
        # at cap, PENDING under cap) — mirrors the batch path.
        self.repository.complete_replay(
            pk, success=False, note="Replay handler returned failure"
        )
        self._emit_replay_exhausted(acquired)
        terminal = acquired.retry_count >= acquired.max_retries
        resulting_status = (
            FailedOperationStatus.REQUIRES_REVIEW.value
            if terminal
            else FailedOperationStatus.PENDING.value
        )
        logger.warning(
            "dlq.entry_retry_failed",
            record_pk=pk,
            entry_domain=acquired.domain,
            failure_type=acquired.failure_type,
            **origin_log_fields,
        )
        return {
            "success": False,
            "id": pk,
            "retry_count": acquired.retry_count,
            "previous_retry_count": old_count,
            "status": resulting_status,
            "message": f"Replay failed for entry {pk}",
        }

    def force_redrive_entry(
        self,
        pk: str,
        *,
        actor_id: str | None = None,
        reason: str = "",
        ticket_url: str | None = None,
    ) -> dict[str, Any]:
        """
        Force-redrive an at-cap DLQ entry past the cap gate (operator override).

        A deliberate, ADMIN-gated escape hatch: after diagnosing and fixing a
        root cause, re-drive an entry that exhausted its cap *because of* that
        now-fixed cause. Mirrors ``retry_entry()`` but acquires with
        ``force=True`` (bypassing the cap gate) and grants a fresh retry budget.
        The poison-pill convergence guarantee is preserved: a still-broken entry
        re-converges to REQUIRES_REVIEW within ``max_replay_attempts`` further
        automatic attempts.

        The normal ``retry_entry()`` hard block on at-cap entries is untouched —
        force is purely additive, and only this ADMIN-gated, audited path grants
        the fresh budget.

        Args:
            pk: Entry primary key
            actor_id: Acting operator (recorded in the force-redrive audit)
            reason: Operator justification (recorded in the audit)
            ticket_url: Optional change/incident ticket reference (recorded)

        Returns:
            Dict mirroring ``retry_entry()``:
                - success: bool (True only if the replay handler succeeded)
                - id: str
                - retry_count: int (post-acquire count — 1 under the fresh budget)
                - previous_retry_count: int (pre-acquire count)
                - status: str (resulting entry status)
                - message: str

        Raises:
            DLQEntryNotFoundError: If entry not found (-> HTTP 404)
            DLQStateConflictError: If entry is resolved/archived or not in a
                force-redrivable state (-> HTTP 409)
            DLQReplayError: If the replay raised an unexpected exception
                (-> HTTP 500)
        """
        entry = self.repository.get_by_id(pk)

        if entry is None:
            raise DLQEntryNotFoundError(f"DLQ entry {pk} not found")

        if entry.status == "resolved":
            raise DLQStateConflictError(
                "Cannot force-redrive an already resolved entry"
            )

        if entry.status == "archived":
            raise DLQStateConflictError("Cannot force-redrive an archived entry")

        old_count = entry.retry_count

        # Force-acquire bypasses the cap gate, resets to a fresh budget, and
        # stamps the metadata history scar (previous_total_retries /
        # force_redrive_count). None covers a non-force-redrivable state
        # (resolved/archived/replaying/...) and a lost concurrent acquire.
        acquired = self.repository.try_acquire_for_replay(
            pk, self.config.max_replay_attempts, force=True
        )
        if acquired is None:
            raise DLQStateConflictError(
                f"Cannot force-redrive: entry {pk} is not in a force-redrivable "
                f"state (not pending/requires_review, or lost a concurrent acquire)"
            )

        previous_total_retries = (acquired.metadata or {}).get(
            "previous_total_retries", old_count
        )

        # 679 D5: origin trace captured at store time — folded into the
        # force-redrive audit details and surfaced on the force-redrive logs.
        origin_trace_id = extract_origin_trace(acquired.metadata)["origin_trace_id"]
        origin_log_fields = (
            {"origin_trace_id": origin_trace_id} if origin_trace_id else {}
        )

        # D4: distinct, privileged audit event recording who force-redrove what,
        # why, and what budget was overridden — fires on the override act
        # itself, independent of the subsequent replay outcome.
        self._log_dlq_audit(
            action="force_redrive",
            dlq_id=pk,
            domain=acquired.domain,
            actor_id=actor_id,
            reason=reason,
            ticket_url=ticket_url,
            previous_total_retries=previous_total_retries,
            origin_trace_id=origin_trace_id,
        )

        # D7: force-redrive occurrence signal (fail-open) — SRE can alert on
        # force frequency as a systemic-problem signal.
        try:
            from baldur.metrics.prometheus import get_metrics

            metrics = get_metrics()
            if metrics and hasattr(metrics, "dlq"):
                metrics.dlq.record_force_redrive(acquired.domain)
        except Exception:
            pass

        try:
            replay_success = self._execute_replay(acquired)
        except Exception as e:
            # Never strand the entry in REPLAYING; release via complete_replay.
            self.repository.complete_replay(pk, success=False, note=str(e))
            self._emit_replay_exhausted(acquired)
            logger.warning(
                "dlq.entry_force_redrive_failed",
                record_pk=pk,
                entry_domain=acquired.domain,
                failure_type=acquired.failure_type,
                error=e,
                **origin_log_fields,
            )
            raise DLQReplayError(f"Force-redrive failed for entry {pk}: {e}") from e

        if replay_success:
            self.resolve_entry(
                pk, notes="force_redrive", resolution_type="force_redrive"
            )
            logger.info(
                "dlq.entry_force_redriven",
                record_pk=pk,
                entry_domain=acquired.domain,
                failure_type=acquired.failure_type,
                **origin_log_fields,
            )
            return {
                "success": True,
                "id": pk,
                "retry_count": acquired.retry_count,
                "previous_retry_count": old_count,
                "status": FailedOperationStatus.RESOLVED.value,
                "message": f"Force-redrive succeeded for entry {pk}",
            }

        # Handler ran but failed: cap-aware terminal transition. Under the fresh
        # budget this reverts to PENDING (re-eligible for automatic replay),
        # re-converging within cap further attempts.
        self.repository.complete_replay(
            pk, success=False, note="Force-redrive handler returned failure"
        )
        self._emit_replay_exhausted(acquired)
        terminal = acquired.retry_count >= acquired.max_retries
        resulting_status = (
            FailedOperationStatus.REQUIRES_REVIEW.value
            if terminal
            else FailedOperationStatus.PENDING.value
        )
        logger.warning(
            "dlq.entry_force_redrive_failed",
            record_pk=pk,
            entry_domain=acquired.domain,
            failure_type=acquired.failure_type,
            **origin_log_fields,
        )
        return {
            "success": False,
            "id": pk,
            "retry_count": acquired.retry_count,
            "previous_retry_count": old_count,
            "status": resulting_status,
            "message": f"Force-redrive failed for entry {pk}",
        }

    def resolve_entry(
        self,
        pk: str,
        notes: str = "",
        resolution_type: str = "manual",
        status: str = "resolved",
    ) -> dict[str, Any]:
        """
        Resolve a DLQ entry.

        Args:
            pk: Entry primary key
            notes: Resolution notes (optional)
            resolution_type: How the entry was resolved (default: "manual")
            status: Target status (default: "resolved")

        Returns:
            Dict with operation details:
                - success: bool
                - id: str
                - previous_status: str
                - current_status: str
                - resolved_at: str (ISO format)
                - notes: str

        Raises:
            DLQEntryNotFoundError: If entry not found (-> HTTP 404)
            DLQStateConflictError: If entry is already resolved/archived
                (-> HTTP 409)
            DLQError: If the resolve operation fails (-> HTTP 400)
        """
        entry = self.repository.get_by_id(pk)

        if entry is None:
            raise DLQEntryNotFoundError(f"DLQ entry {pk} not found")

        if entry.status == "resolved":
            raise DLQStateConflictError("Entry is already resolved")

        if entry.status == "archived":
            raise DLQStateConflictError("Cannot resolve an archived entry")

        old_status = entry.status

        # Use repository method
        success = self.repository.mark_as_resolved(
            id=pk,
            resolution_type=resolution_type,
            resolution_note=notes,
        )

        if not success:
            raise DLQError(f"Failed to resolve entry {pk}")

        # Update status if different from default "resolved"
        if status != "resolved":
            self.repository.update_status(pk, status=status)

        resolved_at = utc_now()

        logger.info(
            "dlq.entry_resolved",
            record_pk=pk,
            notes=notes,
            resolution_type=resolution_type,
            status=status,
        )

        # Metrics update (Fail-Open)
        try:
            from baldur.metrics.event_handlers import DLQMetricEventHandler

            DLQMetricEventHandler.on_item_resolved(
                domain=entry.domain,
                resolution_type=resolution_type,
                duration_seconds=None,
            )
        except ImportError:
            pass

        return {
            "success": True,
            "id": pk,
            "previous_status": old_status,
            "current_status": status,
            "resolved_at": resolved_at.isoformat(),
            "notes": notes,
        }

    def get_entry(self, pk: str) -> dict[str, Any] | None:
        """
        Get detailed info for a single DLQ entry.

        Args:
            pk: Entry primary key

        Returns:
            Dictionary with entry details or None if not found
        """
        entry = self.repository.get_by_id(pk)

        if entry is None:
            return None

        return {
            "id": entry.id,
            "domain": entry.domain,
            "failure_type": entry.failure_type,
            "status": entry.status,
            "retry_count": entry.retry_count,
            "max_retries": entry.max_retries,
            "entity_type": entry.entity_type,
            "entity_id": entry.entity_id,
            "error_code": entry.error_code,
            "error_message": entry.error_message,
            "snapshot_data": entry.snapshot_data,
            "request_data": entry.request_data,
            "response_data": entry.response_data,
            "metadata": entry.metadata,
            "resolution_note": entry.resolution_note,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
            "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
            "resolved_at": entry.resolved_at.isoformat() if entry.resolved_at else None,
        }
