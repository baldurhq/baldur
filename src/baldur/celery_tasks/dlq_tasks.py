"""
DLQ Celery Tasks

Tasks for replaying failed operations from the Dead Letter Queue.
"""

import time
from typing import Any

import structlog
from celery import shared_task

from baldur.utils.time import utc_now

logger = structlog.get_logger(__name__)

# Bounds for the compressed-entry lifecycle drain. One run processes at most
# _COMPRESSED_DRAIN_PAGE_SIZE * _COMPRESSED_DRAIN_MAX_ITERATIONS entries per
# transition; whatever is left is picked up by the next daily run.
_COMPRESSED_DRAIN_PAGE_SIZE = 500
_COMPRESSED_DRAIN_MAX_ITERATIONS = 200

# How far below its own soft time limit an on-recovery pass stops selecting and
# replaying. The pass must end by RETURNING, not by having SoftTimeLimitExceeded
# raised through it: that exception is an ordinary Exception, the task's blanket
# handler swallows it into an error dict, and the continuation — dispatched
# after the service call returns — would never run. The margin covers the
# in-flight replay the deadline check cannot interrupt plus the completion
# event, audit and daily-report writes that follow the loop.
_CIRCUIT_CLOSE_DEADLINE_MARGIN_SECONDS = 30


def _affirm_circuit_closed(service_name: str) -> tuple[bool, str | None]:
    """Is every circuit that could have parked this domain's work now CLOSED?

    Returns ``(proceed, offending_circuit_name)``.

    The sweep selects by the *stored* domain, and the projection from a
    ``protect()`` name onto that domain is many-to-one — ``Payment-API``,
    ``payment-api`` and ``payment_api`` all land in one bucket. Affirming the
    one raw name that closed would therefore let a chain walk a peer circuit's
    whole backlog into a dependency that is still down, one entry at a time,
    escalating each on its first failure. So the affirmation asks the same
    question the drain does: project every circuit forward into stored-domain
    space and require all of them to be CLOSED.

    Three outcomes, and the two permissive ones are deliberate:

    - Nothing projects onto the domain: proceed. On an in-memory circuit store
      the worker is a different process from the one whose circuit closed and
      holds none of its rows, so treating an empty projection as "not affirmed"
      would stop every sweep on such a deployment before its first pass. The
      CLOSED event that dispatched this task is the evidence for that case.
    - The read failed: proceed. The sweep performed no circuit read at all
      before this affirmation existed, so a failed read reproduces the previous
      behaviour rather than inventing a stop.

    ``get_state`` is never used: it is get-or-create and would fabricate a
    CLOSED row inside the worker for a name it has never seen.
    """
    from baldur.services.circuit_breaker import (
        CircuitState,
        get_circuit_breaker_service,
    )
    from baldur.utils.domain_validation import FALLBACK_DOMAIN, resolve_stored_domain

    try:
        cb_service = get_circuit_breaker_service()
        repository = cb_service.repository
        # A worker's L1 is whatever it hydrated at boot plus whatever it has
        # touched, and there is no periodic pull — so a circuit a web pod
        # re-opened in the shared store stays CLOSED here for the whole chain
        # unless the pass refreshes first.
        force_sync = getattr(repository, "force_sync_from_l2", None)
        if callable(force_sync) and not force_sync():
            # False covers two things: "no L2 is configured" — the
            # single-layer store, where L1 IS the store — and "the load
            # failed". Only the second means the affirmation below is about
            # to read a copy the shared store has already moved past. The
            # pass proceeds either way (the CLOSED event is prior evidence,
            # and before this affirmation existed the sweep read no circuit
            # state at all), but proceeding on a read that could not be
            # refreshed is worth saying out loud rather than swallowing.
            health = getattr(repository, "get_l2_health", None)
            if callable(health) and (health() or {}).get("adapter_type") is not None:
                logger.warning(
                    "dlq.circuit_affirmation_refresh_failed",
                    service_name=service_name,
                )

        stored_domain = resolve_stored_domain(service_name)
        if stored_domain == FALLBACK_DOMAIN:
            # The unclassifiable bucket pools unrelated names, so "every
            # circuit projecting onto it" would range over strangers. It is
            # also the case in which no open-circuit lane exists at all, so
            # only operator-mapped lanes run — affirm the raw name alone.
            row = repository.get_by_service_name(service_name)
            if row is not None and row.state != CircuitState.CLOSED.value:
                return False, service_name
            return True, None

        projecting = [
            row
            for row in cb_service.get_all_states()
            if resolve_stored_domain(row.get("service_name", "")) == stored_domain
        ]
        for row in projecting:
            if row.get("state") != CircuitState.CLOSED.value:
                return False, row.get("service_name")
        if not projecting:
            logger.debug(
                "dlq.circuit_state_unknown",
                service_name=service_name,
                healing_domain=stored_domain,
            )
        return True, None
    except Exception as e:
        logger.warning(
            "dlq.circuit_affirmation_failed",
            service_name=service_name,
            error=str(e),
        )
        return True, None


def _circuit_close_pass_deadline(task: Any) -> float | None:
    """``time.monotonic()`` value a pass must return by, or None if unbounded.

    Celery's per-call override wins over the decorator value, matching the
    limit that would actually kill this pass.
    """
    timelimit = getattr(task.request, "timelimit", None) or (None, None)
    soft_limit = timelimit[1] or getattr(task, "soft_time_limit", None)
    if not soft_limit:
        return None
    return time.monotonic() + max(
        1.0, soft_limit - _CIRCUIT_CLOSE_DEADLINE_MARGIN_SECONDS
    )


def _chain_progress(result: Any, carried_cursors: dict) -> tuple[bool, bool]:
    """``(work is still reachable, this pass moved toward it)``.

    The two halves are separate because they route to different endings: a
    pass that reached nothing is a finished drain, while a pass that left work
    reachable and moved nothing toward it is a chain that has to stop AND say
    so — continuing would re-dispatch forever over the same page.
    """
    reachable = bool(result.capped) or bool(getattr(result, "scan_exhausted", False))
    advanced = result.total > 0 or dict(result.lane_cursors) != dict(carried_cursors)
    return reachable, advanced


def _should_continue_chain(result: Any, carried_cursors: dict) -> bool:
    """Did the pass leave work reachable, and did it make progress reaching it?

    Reachability is ``capped`` (a lane filled its quota or the deadline cut the
    pass short) or ``scan_exhausted`` (a selector stopped on its scan bound
    rather than on an empty pool) — an empty page means neither on its own.

    Progress is the guard against a chain that re-dispatches forever without
    moving: either the pass acquired entries (every selected entry leaves
    PENDING before any skip branch runs, so none of them is selectable again),
    or a lane's cursor advanced past members it examined and rejected.
    """
    reachable, advanced = _chain_progress(result, carried_cursors)
    return reachable and advanced


@shared_task(
    bind=True,
    name="baldur.celery_tasks.conditional_replay_on_circuit_close",
    queue="dlq_processing",
    max_retries=0,
    time_limit=300,
    soft_time_limit=290,
    acks_late=True,
)
def conditional_replay_on_circuit_close(  # noqa: C901, PLR0911
    self,
    service_name: str,
    max_items: int = 50,
    max_continuations: int = 1,
    continuation: int = 0,
    cursors: dict | None = None,
) -> dict:
    """
    Trigger conditional replay when a circuit breaker closes.

    This task is called by CircuitBreakerService.force_close() when
    trigger_replay=True is specified, and by itself: one run is one pass over
    the recovered service's backlog, and the task re-dispatches itself while
    the pass it just ran left work reachable. The chain is what makes the
    on-recovery guarantee about a *backlog* rather than about one budget of it.

    The re-dispatch lives here rather than in the service because the service
    releases its per-service inflight lock in a ``finally``: a continuation
    queued from inside would meet its own predecessor's lock and end the drain
    silently.

    Args:
        service_name: Name of the service that recovered
        max_items: Maximum number of items to replay per pass
        max_continuations: Maximum passes to chain after this one. Resolved
            once by the dispatching handler and carried unchanged, so a chain
            runs to the budget it started with.
        continuation: How many passes preceded this one.
        cursors: Per-lane selection positions the previous pass stopped at.

    Returns:
        Dictionary with replay result summary
    """
    from baldur.services import get_replay_service
    from baldur.services.replay_service.service import (
        REASON_CIRCUIT_REOPENED,
        REASON_PASS_ERRORED,
    )

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)
    carried_cursors: dict = dict(cursors or {})

    bound_logger.info(
        "dlq.circuit_recovery_started",
        service_name=service_name,
        max_items=max_items,
        continuation=continuation,
    )

    service = get_replay_service()

    # Affirmed at the start of EVERY pass, not once before dispatch: a
    # continuation queued while the circuit was CLOSED is picked up seconds
    # later, and the sweep itself reads no circuit state anywhere.
    affirmed, offending = _affirm_circuit_closed(service_name)
    if not affirmed:
        bound_logger.warning(
            "dlq.circuit_recovery_stopped_reopened",
            service_name=service_name,
            offending_circuit=offending,
        )
        service.emit_circuit_close_chain_stopped(
            service_name=service_name,
            block_reason=REASON_CIRCUIT_REOPENED,
            lane_cursors=carried_cursors,
            offending_circuit=offending,
        )
        return {
            "success": False,
            "service_name": service_name,
            "error": REASON_CIRCUIT_REOPENED,
            "block_reason": REASON_CIRCUIT_REOPENED,
            "total": 0,
        }

    try:
        result = service.replay_on_circuit_close(
            service_name=service_name,
            max_items=max_items,
            deadline=_circuit_close_pass_deadline(self),
            lane_cursors=carried_cursors,
            continuation=continuation,
        )

        # Check governance blocking before success
        if result.governance_blocked:
            bound_logger.warning(
                "dlq.circuit_recovery_blocked",
                service_name=service_name,
                reason=result.governance_block_reason,
            )
            return {
                "success": False,
                "service_name": service_name,
                "error": "governance_blocked",
                "block_reason": result.governance_block_reason,
                "total": 0,
            }

        bound_logger.info(
            "dlq.circuit_recovery_completed",
            service_name=service_name,
            dlq_total=result.total,
            success_count=result.success_count,
            failed_count=result.failed_count,
            capped=result.capped,
            continuation=continuation,
        )

        continued = _dispatch_circuit_close_continuation(
            service=service,
            bound_logger=bound_logger,
            result=result,
            service_name=service_name,
            max_items=max_items,
            max_continuations=max_continuations,
            continuation=continuation,
            carried_cursors=carried_cursors,
        )

        return {
            "success": True,
            "service_name": service_name,
            "total": result.total,
            "success_count": result.success_count,
            "failed_count": result.failed_count,
            "capped": result.capped,
            "continued": continued,
        }

    except Exception as e:
        bound_logger.exception(
            "dlq.circuit_recovery_failed",
            service_name=service_name,
            error=str(e),
        )
        # The chain had reachable work by construction — it was still running —
        # and an ERROR log reaches no event, metric or audit consumer, so a
        # chain that dies here would be indistinguishable from one that
        # finished.
        try:
            service.emit_circuit_close_chain_stopped(
                service_name=service_name,
                block_reason=REASON_PASS_ERRORED,
                lane_cursors=carried_cursors,
            )
        except Exception:
            bound_logger.exception("dlq.circuit_recovery_stop_signal_failed")
        return {
            "success": False,
            "service_name": service_name,
            "error": str(e),
        }


def _dispatch_circuit_close_continuation(
    *,
    service: Any,
    bound_logger: Any,
    result: Any,
    service_name: str,
    max_items: int,
    max_continuations: int,
    continuation: int,
    carried_cursors: dict,
) -> bool:
    """Queue the next pass of this chain, or announce why the chain stopped.

    One extra pass is run with the cursors cleared before a chain concludes
    "nothing left": the stale-replay release returns abandoned entries to
    PENDING at their ORIGINAL created_at — behind every cursor a running chain
    holds — so a chain that has walked past them would otherwise finish over a
    queue that is not empty. Handing the successor no cursors is also what
    bounds the retry: its own carried set is empty, so it cannot ask again.
    """
    from baldur.adapters.celery.tasks import conditional_replay_on_circuit_close
    from baldur.services.replay_service.service import (
        REASON_CONTINUATION_BOUND_REACHED,
        REASON_PASS_MADE_NO_PROGRESS,
    )

    if result.inflight_skipped:
        return False

    lane_cursors = dict(result.lane_cursors)
    reachable, advanced = _chain_progress(result, carried_cursors)
    if reachable and not advanced:
        # A pass that spent its whole deadline selecting replays nothing and
        # advances no cursor, so the chain has to stop — a continuation would
        # re-run the identical page. It must not stop QUIETLY: `total == 0`
        # makes the completion event return early, so without this the drain
        # ends over a queue it never touched and the only trace is a debug
        # line. This is a stop with work reachable, which is exactly what the
        # blocked-family channel is for.
        bound_logger.warning(
            "dlq.circuit_recovery_stopped_without_progress",
            service_name=service_name,
            continuation=continuation,
            capped=result.capped,
        )
        service.emit_circuit_close_chain_stopped(
            service_name=service_name,
            block_reason=REASON_PASS_MADE_NO_PROGRESS,
            scan_exhausted_lanes=result.scan_exhausted_lanes,
            lane_cursors=lane_cursors,
        )
        return False

    if reachable and advanced:
        if continuation + 1 >= max_continuations:
            bound_logger.warning(
                "dlq.circuit_recovery_bound_reached",
                service_name=service_name,
                continuation=continuation,
            )
            service.emit_circuit_close_chain_stopped(
                service_name=service_name,
                block_reason=REASON_CONTINUATION_BOUND_REACHED,
                scan_exhausted_lanes=result.scan_exhausted_lanes,
                lane_cursors=lane_cursors,
            )
            return False
        conditional_replay_on_circuit_close.delay(
            service_name=service_name,
            max_items=max_items,
            max_continuations=max_continuations,
            continuation=continuation + 1,
            cursors=lane_cursors,
        )
        return True

    if carried_cursors and continuation + 1 < max_continuations:
        conditional_replay_on_circuit_close.delay(
            service_name=service_name,
            max_items=max_items,
            max_continuations=max_continuations,
            continuation=continuation + 1,
            cursors=None,
        )
        return True

    return False


@shared_task(
    bind=True,
    name="baldur.celery_tasks.replay_single_dlq_entry",
    queue="dlq_processing",
    max_retries=0,
    time_limit=120,
    soft_time_limit=110,
    acks_late=True,
)
def replay_single_dlq_entry(self, dlq_id: str, trigger: str = "manual_replay") -> dict:
    """
    Replay a single DLQ entry.

    This task is triggered by operators via admin UI or API.

    Args:
        dlq_id: ID of the FailedOperation to replay
        trigger: Provenance trigger stamped into resolution_type (default:
            manual_replay). Operator/manual scheduled batch replay passes
            "scheduled_batch" (there is no automatic scheduled producer).

    Returns:
        Dictionary with replay result
    """
    from baldur.services import get_replay_service

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    bound_logger.info(
        "dlq.replay_starting_replay",
        dlq_id=dlq_id,
    )

    try:
        service = get_replay_service()
        result = service.replay_single(dlq_id, trigger=trigger)

        if result.success:
            bound_logger.info(
                "dlq.replay_successfully_replayed",
                dlq_id=dlq_id,
            )
            return {
                "success": True,
                "dlq_id": dlq_id,
                "message": result.message,
                "data": result.data,
            }
        bound_logger.warning(
            "dlq.replay_failed_replay",
            dlq_id=dlq_id,
            result_error=result.error,
        )
        return {
            "success": False,
            "dlq_id": dlq_id,
            "error": result.error,
        }

    except Exception as e:
        bound_logger.exception(
            "dlq.replay_unexpected_error",
            dlq_id=dlq_id,
            error=e,
        )
        return {
            "success": False,
            "dlq_id": dlq_id,
            "error": str(e),
        }


@shared_task(
    bind=True,
    name="baldur.celery_tasks.replay_batch_by_failure_type",
    queue="dlq_processing",
    max_retries=0,
    time_limit=600,
    soft_time_limit=580,
    acks_late=True,
)
def replay_batch_by_failure_type(
    self,
    failure_type: str,
    max_items: int = 100,
    trigger: str = "manual_replay",
) -> dict:
    """
    Replay all pending DLQ entries of a specific failure type.

    This task is used for batch recovery after system issues are resolved.

    Args:
        failure_type: The failure type to filter by
        max_items: Maximum number of items to replay
        trigger: Provenance trigger stamped into resolution_type (default:
            manual_replay). Operator/manual scheduled batch replay passes
            "scheduled_batch" (there is no automatic scheduled producer).

    Returns:
        Dictionary with batch replay summary
    """
    from baldur.services import get_replay_service

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    bound_logger.info(
        "dlq.batch_replay_starting",
        failure_type=failure_type,
        max_items=max_items,
    )

    try:
        service = get_replay_service()
        result = service.replay_batch(
            failure_type=failure_type,
            max_items=max_items,
            trigger=trigger,
        )

        bound_logger.info(
            "dlq.batch_replay_completed",
            dlq_total=result.total,
            success_count=result.success_count,
            failed_count=result.failed_count,
        )

        return {
            "success": True,
            "total": result.total,
            "success_count": result.success_count,
            "failed_count": result.failed_count,
            "skipped_count": result.skipped_count,
        }

    except Exception as e:
        bound_logger.exception(
            "dlq.batch_replay_unexpected",
            error=e,
        )
        return {
            "success": False,
            "error": str(e),
        }


@shared_task(
    bind=True,
    name="baldur.celery_tasks.replay_batch_by_domain",
    queue="dlq_processing",
    max_retries=0,
    time_limit=600,
    soft_time_limit=580,
    acks_late=True,
)
def replay_batch_by_domain(
    self,
    domain: str,
    max_items: int = 100,
    trigger: str = "manual_replay",
) -> dict:
    """
    Replay all pending DLQ entries for a specific domain.

    This task is used for domain-wide recovery operations.

    Args:
        domain: The domain to filter by (payment, point, inventory, webhook, notification)
        max_items: Maximum number of items to replay
        trigger: Provenance trigger stamped into resolution_type (default:
            manual_replay). Operator/manual scheduled batch replay passes
            "scheduled_batch" (there is no automatic scheduled producer).

    Returns:
        Dictionary with batch replay summary
    """
    from baldur.services import get_replay_service

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    bound_logger.info(
        "dlq.batch_replay_starting",
        healing_domain=domain,
        max_items=max_items,
    )

    try:
        service = get_replay_service()
        result = service.replay_batch(
            domain=domain,
            max_items=max_items,
            trigger=trigger,
        )

        bound_logger.info(
            "dlq.batch_replay_completed",
            dlq_total=result.total,
            success_count=result.success_count,
            failed_count=result.failed_count,
        )

        return {
            "success": True,
            "domain": domain,
            "total": result.total,
            "success_count": result.success_count,
            "failed_count": result.failed_count,
        }

    except Exception as e:
        bound_logger.exception(
            "dlq.batch_replay_unexpected",
            error=e,
        )
        return {
            "success": False,
            "error": str(e),
        }


@shared_task(
    bind=True,
    name="baldur.celery_tasks.evict_overflow_dlq_entries",
    queue="maintenance",
    max_retries=0,
    time_limit=120,
    soft_time_limit=110,
)
def evict_overflow_dlq_entries(self) -> dict:
    """
    Background DLQ overflow eviction with distributed lock.

    Celery Beat: 10s interval recommended.
    3-tier water level based eviction intensity.

    Distributed lock prevents concurrent compression across workers
    when compress_oldest strategy is active.
    """
    from baldur_pro.services.dlq.overflow import run_background_eviction

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    # Distributed lock: prevent concurrent compression across workers
    lock = None
    lock_acquired = False
    lock_namespace = "dlq-compression"
    session_id = f"celery-{task_id}"

    try:
        from datetime import timedelta

        from baldur_pro.services.coordination.distributed_recovery_lock import (
            DistributedRecoveryLock,
        )

        lock = DistributedRecoveryLock(lock_timeout=timedelta(minutes=2))
        lock_acquired = lock.acquire(
            namespace=lock_namespace,
            session_id=session_id,
            blocking=False,  # Non-blocking: skip if another worker is processing
        )
    except ImportError:
        # Coordination module not available (e.g., Redis not configured)
        lock_acquired = True  # Fail-open: proceed without lock
    except Exception:
        bound_logger.warning("dlq.compress_lock_acquisition_failed")
        lock_acquired = True  # Fail-open: system stability over strict locking

    if not lock_acquired:
        bound_logger.info(
            "dlq.compress_lock_skipped",
            reason="another_worker_compressing",
        )
        return {"status": "skipped", "reason": "lock_not_acquired"}

    bound_logger.debug("dlq.compress_lock_acquired", session_id=session_id)

    try:
        return run_background_eviction()
    except Exception as e:
        bound_logger.exception("dlq.overflow_eviction_error", error=e)
        return {"success": False, "error": str(e)}
    finally:
        if lock is not None and lock_acquired:
            try:
                lock.release(namespace=lock_namespace, session_id=session_id)
            except Exception:
                bound_logger.warning("dlq.compress_lock_release_failed")


@shared_task(
    bind=True,
    name="baldur.celery_tasks.cleanup_resolved_dlq_entries",
    queue="maintenance",
    max_retries=1,
    time_limit=300,
    soft_time_limit=290,
)
def cleanup_resolved_dlq_entries(self, days_old: int = 30) -> dict:
    """
    Archive old resolved DLQ entries (soft-delete, NOT hard delete).

    This task runs periodically to archive old DLQ entries.
    Entries are marked as ARCHIVED instead of deleted for audit trail.

    Retention Policy:
    - Expired entries: mark as EXPIRED
    - Old resolved/rejected: mark as ARCHIVED (soft-delete)
    - Never hard delete for compliance (payment/point records)

    Args:
        days_old: Archive entries older than this many days

    Returns:
        Dictionary with cleanup summary
    """

    from baldur.factory.registry import ProviderRegistry
    from baldur.services.daily_report import record_cleanup_result

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    bound_logger.info(
        "dlq.cleanup_starting_cleanup",
        days_old=days_old,
    )

    try:
        dlq_service = ProviderRegistry.dlq_service.safe_get()
        if dlq_service is None:
            raise RuntimeError("baldur_pro DLQService not registered")
        result = dlq_service.cleanup_old_entries(days_old=days_old)

        bound_logger.info(
            "dlq.cleanup_completed",
            expired_count=result.get("expired_count", 0),
            archived_count=result.get("archived_count", 0),
        )

        cleanup_summary = {
            "success": True,
            **result,
        }
        record_cleanup_result(
            "baldur.celery_tasks.cleanup_resolved_dlq_entries", cleanup_summary
        )
        return cleanup_summary

    except Exception as e:
        bound_logger.exception(
            "dlq.cleanup_unexpected_error",
            error=e,
        )
        return {
            "success": False,
            "error": str(e),
        }


@shared_task(
    bind=True,
    name="baldur.celery_tasks.cleanup_compressed_dlq_entries",
    queue="maintenance",
    max_retries=1,
    time_limit=300,
    soft_time_limit=290,
)
def cleanup_compressed_dlq_entries(self) -> dict:
    """
    Transition compressed entry lifecycle statuses.

    Celery Beat daily schedule:
    - ACTIVE entries older than compress_stale_after_days -> STALE
    - STALE entries older than compress_archive_after_days -> ARCHIVED
    - Never hard delete.

    Runs under a distributed lock. Once the drain walks a per-status index,
    a transition removes its entry from the key being walked, so a concurrent
    run shifts the positions the other run is paging through and entries get
    stepped over. Overlap was harmless while the walked index never shrank;
    it is not any more.

    Step 0 of every run reconciles the per-status index family with the stored
    entries, which is what lets the drains use it. It is fail-open: a step-0
    failure leaves the drains to run against whichever index the repository
    considers trustworthy.
    """
    from baldur.dlq.helpers import compressed_lifecycle_lock
    from baldur.factory.registry import ProviderRegistry
    from baldur.settings.dlq import get_dlq_settings

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    settings = get_dlq_settings()
    repository = ProviderRegistry.dlq_repository.safe_get()
    if repository is None:
        raise RuntimeError("baldur_pro DLQRepository not registered")

    with compressed_lifecycle_lock(f"celery-{task_id}") as lock_acquired:
        if not lock_acquired:
            bound_logger.info(
                "dlq.compressed_cleanup_lock_skipped",
                reason="another_worker_sweeping",
            )
            return {"status": "skipped", "reason": "lock_not_acquired"}
        return _run_compressed_cleanup(repository, settings, bound_logger)


def _run_compressed_cleanup(repository, settings, bound_logger) -> dict:
    """Backfill step 0, then drain both lifecycle lanes."""
    from datetime import timedelta

    try:
        repository.backfill_compressed_status_index()
    except Exception as e:
        bound_logger.warning("dlq.compressed_backfill_step_failed", error=str(e))

    now = utc_now()
    stale_cutoff = now - timedelta(days=settings.compress_stale_after_days)
    archive_cutoff = now - timedelta(days=settings.compress_archive_after_days)

    from baldur.interfaces.repositories import DLQCompressedStatus

    def _drain(from_status: str, to_status: str, cutoff, is_eligible) -> int:
        """Transition entries older than ``cutoff``, oldest first, in pages.

        The cursor is the last ``compressed_at`` handled, carried forward as
        the next page's lower bound so the window shrinks instead of
        restarting at the oldest entry. That matters on Redis, where status is
        not part of the index: a scan from the head would re-read every entry
        the drain had already transitioned, once per page.

        A transition moves an entry out of ``from_status``, so it drops out of
        the query on its own. Entries that matched but failed ``is_eligible``
        stay, and those below the cursor fall outside the next window — only
        the ones sitting exactly on the cursor would come back, so those are
        counted into ``offset``.
        """
        transitioned = 0
        after = None
        offset = 0

        for _ in range(_COMPRESSED_DRAIN_MAX_ITERATIONS):
            page = repository.get_compressed_entries_before(
                status=from_status,
                before=cutoff,
                limit=_COMPRESSED_DRAIN_PAGE_SIZE,
                offset=offset,
                after=after,
            )
            if not page:
                break

            left_behind = []
            for entry in page:
                if not is_eligible(entry):
                    left_behind.append(entry)
                    continue
                repository.update_compressed_status(entry.id, to_status)
                transitioned += 1

            cursor = page[-1].compressed_at
            stuck = sum(1 for e in left_behind if e.compressed_at == cursor)
            # The cursor only fails to advance when a whole page shares one
            # timestamp; then the skips accumulate rather than reset.
            offset = offset + stuck if after == cursor else stuck
            after = cursor

        return transitioned

    # ACTIVE -> STALE. The cutoff query already encodes eligibility.
    stale_count = _drain(
        DLQCompressedStatus.ACTIVE.value,
        DLQCompressedStatus.STALE.value,
        stale_cutoff,
        lambda entry: True,
    )

    # STALE -> ARCHIVED. Archiving is driven off stale_at, but compressed_at is
    # always earlier than stale_at, so the compressed_at cutoff yields a
    # superset of the eligible entries — the per-entry check narrows it.
    archived_count = _drain(
        DLQCompressedStatus.STALE.value,
        DLQCompressedStatus.ARCHIVED.value,
        archive_cutoff,
        lambda entry: entry.stale_at is not None and entry.stale_at < archive_cutoff,
    )

    bound_logger.info(
        "dlq.compressed_cleanup_completed",
        stale_count=stale_count,
        archived_count=archived_count,
    )

    return {
        "success": True,
        "stale_count": stale_count,
        "archived_count": archived_count,
    }


@shared_task(
    bind=True,
    name="baldur.celery_tasks.release_stale_replaying",
    queue="maintenance",
    max_retries=1,
    time_limit=120,
    soft_time_limit=110,
)
def release_stale_replaying(self) -> dict:
    """
    Release DLQ entries stuck in REPLAYING state back to PENDING.

    Entries get stuck if the replay worker crashes after acquiring but
    before completing. Runs every 15 minutes via Celery Beat.

    Runs on every tier: the backing resolves through the canonical DLQ
    resolution chain, whose repository implements the release on all three
    OSS adapters (memory / SQL / Redis) as well as under PRO.

    Returns:
        Dictionary with released count
    """
    from baldur.services.dlq_capture.service import resolve_dlq_backing
    from baldur.settings.dlq import get_dlq_settings

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    settings = get_dlq_settings()

    try:
        repository = resolve_dlq_backing().repository
        released = repository.release_stale_replaying(
            older_than_minutes=settings.stale_replaying_timeout_minutes,
        )

        if released > 0:
            bound_logger.warning(
                "dlq.stale_replaying_released",
                released_count=released,
                timeout_minutes=settings.stale_replaying_timeout_minutes,
            )
        else:
            bound_logger.debug(
                "dlq.stale_replaying_none_found",
                timeout_minutes=settings.stale_replaying_timeout_minutes,
            )

        return {
            "success": True,
            "released_count": released,
        }

    except Exception as e:
        bound_logger.exception(
            "dlq.release_stale_replaying_error",
            error=e,
        )
        return {
            "success": False,
            "error": str(e),
        }


def get_dlq_maintenance_beat_schedule():
    """Beat schedule for DLQ maintenance tasks (eviction + cleanup + stale release).

    Tier-resolved per entry. Stale-REPLAYING release is an OSS capability and is
    always scheduled; overflow eviction, resolved-entry cleanup and compressed-entry
    lifecycle are PRO capabilities (OSS enforces overflow synchronously at store
    time and demotes compression to drop), so they are scheduled only when the PRO
    distribution is installed. Without the gate an OSS-only install would run tasks
    that can only fail on cadence.
    """
    from celery.schedules import crontab

    from baldur.utils.tier import is_pro_installed

    schedule: dict[str, Any] = {
        "release-stale-replaying-entries": {
            "task": "baldur.celery_tasks.release_stale_replaying",
            "schedule": crontab(minute="*/15"),
            "options": {"queue": "maintenance"},
        },
    }

    if not is_pro_installed():
        logger.debug("dlq.maintenance_pro_entries_skipped")
        return schedule

    schedule.update(
        {
            "evict-overflow-dlq-entries": {
                "task": "baldur.celery_tasks.evict_overflow_dlq_entries",
                "schedule": 60.0,
                "options": {"queue": "maintenance"},
            },
            "cleanup-resolved-dlq-entries": {
                "task": "baldur.celery_tasks.cleanup_resolved_dlq_entries",
                "schedule": crontab(hour="*/6"),
                "options": {"queue": "maintenance"},
                "kwargs": {"days_old": 30},
            },
            "cleanup-compressed-dlq-entries": {
                "task": "baldur.celery_tasks.cleanup_compressed_dlq_entries",
                "schedule": crontab(hour=4, minute=30),
                "options": {"queue": "maintenance"},
            },
        }
    )
    return schedule
