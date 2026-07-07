"""
DLQ Replay Service

Provides replay functionality for failed operations in the DLQ.
Supports manual replay, batch replay, and conditional replay on circuit breaker recovery.

Replay Types:
- Manual Replay: Operator selects individual items
- Batch Replay: Operator selects multiple items by filter
- Conditional Replay: Auto-replay when external system recovers

Thin Task, Fat Service Architecture:
    - All governance checks run inside this service
    - Celery Tasks act only as thin delegators
    - Audit logging runs automatically via check_all_governance

Provides DLQ replay functionality.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.helpers import log_dlq_replay_audit, log_dlq_replay_blocked_audit
from baldur.audit.trace import extract_origin_trace
from baldur.core.timezone import now
from baldur.interfaces.repositories import ResolutionTrigger
from baldur.services.event_bus.emitter import EventEmitterMixin
from baldur.settings import get_config
from baldur.settings.replay_automation import get_replay_automation_settings

from .handlers import _truncate_gate, get_replay_handler
from .models import BatchReplayResult, ReplayResult

if TYPE_CHECKING:
    from baldur.interfaces.cache_provider import CacheProviderInterface
    from baldur.interfaces.governance import GovernanceChecker
    from baldur.interfaces.repositories import (
        FailedOperationData,
        FailedOperationRepository,
    )
    from baldur.services.adaptive_replay import (
        AdaptiveReplayConfig,
        AdaptiveReplayManager,
    )

logger = structlog.get_logger()


# Block reason emitted when `service_failure_type_map` has no entry for the
# recovered service (operator misconfig). Mirrors the inline string-literal
# pattern used by the same-function `max_replay_attempts_exceeded` branch.
REASON_NO_FAILURE_TYPE_MAPPING = "service_failure_type_map_unconfigured"

# Operator-facing RuntimeConfig path the operator must populate to resolve
# the misconfig. Surfaced uniformly across log / event / audit channels so
# Kibana/Loki filters, EventBus subscribers, and WAL audit queries all see
# the same key.
CONFIG_PATH_FAILURE_TYPE_MAP = "replay_automation.service_failure_type_map"


# 497 D4: Block reason emitted when the per-service inflight DistributedLock
# rejects a duplicate `replay_on_circuit_close` sweep. Defined inline next to
# REASON_NO_FAILURE_TYPE_MAPPING because both flow through the same 4-channel
# block surface (log / event / metric / audit) inside this function.
REASON_CIRCUIT_CLOSE_INFLIGHT = "circuit_close_inflight"


def _resolution_type_for(trigger: ResolutionTrigger | str) -> str:
    """Normalize a trigger (enum or raw string) to its stamped value.

    ``ResolutionTrigger`` is a ``(str, Enum)`` so members already ARE strings,
    but ``str(member)`` yields the ``ResolutionTrigger.X`` repr on some Python
    versions — take ``.value`` for the enum, pass a raw string through.
    """
    return trigger.value if isinstance(trigger, ResolutionTrigger) else str(trigger)


def _replay_inflight_lock_name(service_name: str) -> str:
    """Build the per-service inflight lock name for circuit-close replay.

    The name shape is fixed (`replay:inflight:circuit_close:<svc>`) so that
    any worker / pod sharing the same cache backend resolves to the same
    `DistributedLock` for the same service — owner-fenced acquire/release
    in `cache.get_lock()` makes the suppression cross-process safe.
    """
    return f"replay:inflight:circuit_close:{service_name}"


# =============================================================================
# Replay Service
# =============================================================================


class ReplayService(EventEmitterMixin):
    """
    DLQ Replay Service.

    Orchestrates replay operations for failed operations.

    Usage:
        service = ReplayService()

        # Single replay
        result = service.replay_single(dlq_id=123)

        # Batch replay
        batch_result = service.replay_batch(
            failure_type="PG_TIMEOUT",
            max_items=50
        )

    For testing with mock repository:
        mock_repo = Mock(spec=FailedOperationRepository)
        service = ReplayService(repository=mock_repo)
    """

    _event_source = "replay_service"

    def __init__(
        self,
        repository: FailedOperationRepository | None = None,
        cache: CacheProviderInterface | None = None,
    ):
        """
        Initialize the replay service.

        Args:
            repository: Optional repository for DI, uses Django adapter if None
            cache: Optional cache provider for the per-service inflight lock
                guarding `replay_on_circuit_close`. If omitted, the
                provider is lazy-resolved via ProviderRegistry on first use.
                If resolution fails or the resolved provider does not support
                `get_lock()`, the guard fails open with a WARNING log.
        """
        self.config = self._load_config()
        self._repository = repository
        self._cache = cache
        self._cache_resolution_attempted = False
        self._governance: GovernanceChecker | None = None
        self._governance_resolved: bool = False
        # D1: the PRO RuntimeConfigManager being absent is the OSS-normal
        # state, logged at DEBUG at most once per instance (not a per-call
        # WARNING). Read via getattr for bypassed-__init__ fixtures.
        self._runtime_config_absent_logged: bool = False

    def _get_governance(self) -> GovernanceChecker:
        """Lazily resolve and cache the GovernanceChecker provider.

        Lazy (not eager in __init__) so test fixtures, REPL sessions, and
        Django auto-discovery that construct ReplayService before
        ``baldur.init()`` registers the PRO provider stay fail-open via
        the OSS NoOp default. Precedent: ``ThrottleGovernanceGuard._get_governance()``.

        Always returns a non-None checker; on resolution failure, falls
        back to a fresh ``NoOpGovernanceChecker`` so callers can invoke
        governance methods unconditionally.
        """
        if self._governance_resolved and self._governance is not None:
            return self._governance
        try:
            from baldur.factory.registry import ProviderRegistry

            self._governance = ProviderRegistry.governance.get()
        except Exception as e:
            from baldur.interfaces.governance import NoOpGovernanceChecker

            logger.warning(
                "replay_service.governance_resolve_failed_fail_open",
                error=str(e),
            )
            self._governance = NoOpGovernanceChecker()
        self._governance_resolved = True
        assert self._governance is not None  # set non-None in both branches above
        return self._governance

    @property
    def repository(self) -> FailedOperationRepository:
        """Get the repository using ProviderRegistry with fallback policy."""
        if self._repository is None:
            from baldur.adapters.memory import (
                InMemoryFailedOperationRepository,
            )
            from baldur.core.di_fallback import resolve_with_fallback
            from baldur.factory import ProviderRegistry

            self._repository = resolve_with_fallback(
                registry_method=lambda: ProviderRegistry.get_failed_operation_repo(),
                fallback_class=InMemoryFailedOperationRepository,
                service_name=self.__class__.__name__,
            )
        return self._repository

    @property
    def cache(self) -> CacheProviderInterface | None:
        """Lazy-resolve the cache provider for the circuit-close inflight lock.

        Returns None if no provider can be resolved — caller falls
        open in that case. The inflight lock uses `cache.get_lock()`
        (owner-fenced `DistributedLock`), so adapter-level lock support is
        validated at acquire-time rather than via a separate setnx gate.

        getattr with defaults handles test fixtures that bypass `__init__`
        via `ReplayService.__new__(...)`. A bypassed-init instance is
        observationally identical to a fresh instance with `cache=None` for
        this fail-open guard.
        """
        cached = getattr(self, "_cache", None)
        attempted = getattr(self, "_cache_resolution_attempted", False)
        if cached is not None or attempted:
            return cached

        self._cache_resolution_attempted = True
        try:
            from baldur.factory import ProviderRegistry

            resolved = ProviderRegistry.get_cache()
        except Exception as exc:
            logger.warning(
                "replay_service.inflight_cache_unavailable",
                reason="provider_resolution_failed",
                error=str(exc),
            )
            return None

        self._cache = resolved
        return self._cache

    def _load_config(self) -> dict[str, Any]:
        """Load replay configuration from config system."""
        config = get_config()
        return {
            "max_replay_attempts": config.services_group.dlq.max_replay_attempts,
        }

    def _emit_replay_blocked(
        self,
        *,
        log_event: str,
        log_fields: dict[str, Any],
        event_data: dict[str, Any],
        metric_subject: str,
        metric_reason: str,
        log_level: str = "warning",
        audit: dict[str, Any] | None = None,
    ) -> None:
        """Emit the multi-channel replay-block surface from one call site.

        Consolidates the block channels that were copy-pasted across the
        replay service's block branches:

        1. structlog log (level dispatched by ``log_level`` — "warning" or
           "debug")
        2. ``DLQ_REPLAY_BLOCKED`` EventBus event (via EventEmitterMixin)
        3. ``ReplayEventHandler.on_replay_blocked`` Prometheus metric
        4. optional explicit audit (``log_dlq_replay_blocked_audit(**audit)``)

        Per-site fidelity is the contract: callers pass their exact event
        name, log fields, event payload, and metric args, so consolidation
        changes no operator-facing log/event/metric output. ``audit`` is None
        for the governance sites (their audit runs inside
        ``check_all_governance(audit_on_block=True)`` — the helper must not
        double-audit) and for the DEBUG truncate-gate site.
        """
        from baldur.metrics.event_handlers import ReplayEventHandler
        from baldur.services.event_bus import EventType

        getattr(logger, log_level)(log_event, **log_fields)
        self._emit_event(EventType.DLQ_REPLAY_BLOCKED, data=event_data)
        ReplayEventHandler.on_replay_blocked(metric_subject, metric_reason)
        if audit is not None:
            log_dlq_replay_blocked_audit(**audit)

    # =========================================================================
    # Core Replay Logic
    # =========================================================================

    def _execute_replay(  # noqa: C901, PLR0912, PLR0915
        self,
        dlq_id: str,
        replay_type: str = "single",
        trigger: ResolutionTrigger | str = ResolutionTrigger.MANUAL_REPLAY,
        actor_id: str | None = None,
    ) -> ReplayResult:
        """Core replay logic: acquire -> handler -> complete -> audit -> event.

        Handlers MUST ensure idempotency. Partial failure rollback is the
        handler's responsibility; for multi-step compensation, consider a
        dedicated compensation flow instead.

        Args:
            dlq_id: DLQ entry to replay.
            replay_type: "single"/"conditional"/"batch" — feeds the replay
                metric label; NOT the provenance stamp.
            trigger: Provenance trigger stamped into ``resolution_type`` on
                success (distinct from ``replay_type``).
            actor_id: Acting principal for the audit trail. When omitted,
                falls back to the ambient ``ActorContext`` (``system`` for
                background/Celery paths).
        """

        from baldur.metrics.event_handlers import ReplayEventHandler
        from baldur.services.event_bus import EventType

        config_max = self.config["max_replay_attempts"]
        failed_op_data = self.repository.try_acquire_for_replay(dlq_id, config_max)

        if failed_op_data is None:
            existing = self.repository.get_by_id(dlq_id)
            if existing is None:
                logger.debug(
                    "replay_service.acquisition_skipped",
                    dlq_id=dlq_id,
                    reason="not_found",
                )
                return ReplayResult.failed(dlq_id, "DLQ entry not found")
            if existing.status != "pending":
                logger.debug(
                    "replay_service.acquisition_skipped",
                    dlq_id=dlq_id,
                    reason="already_processed",
                    current_status=existing.status,
                )
                return ReplayResult.failed(
                    dlq_id, f"Cannot replay: status is '{existing.status}'"
                )
            # Config lowered or race condition — entry is PENDING but
            # retry_count >= max_replay_attempts.  Emit BLOCKED so
            # operators can diagnose why the queue is not draining.
            # #496: the audit channel was previously missing on this branch —
            # added so a max-attempts block leaves a WAL trail like every
            # other block. Blocked-family audit (not per-item replay audit):
            # a max-attempts block never attempts, so recording it as a failed
            # attempt would pollute attempt statistics.
            self._emit_replay_blocked(
                log_event="replay_service.replay_max_attempts_exceeded",
                log_fields={"dlq_id": dlq_id},
                event_data={
                    "dlq_id": dlq_id,
                    "block_reason": "max_replay_attempts_exceeded",
                },
                metric_subject=existing.domain if existing else "unknown",
                metric_reason="max_replay_attempts_exceeded",
                audit={
                    "domain": existing.domain if existing else "unknown",
                    "reason": "max_replay_attempts_exceeded",
                    "service_name": "ReplayService",
                    "trigger": replay_type,
                    "details": {"dlq_id": dlq_id},
                },
            )
            return ReplayResult.failed(dlq_id, "max_replays_exceeded")

        # Idempotency gate check (fail-open)
        idem_key = None
        gate_retry_count = 0
        try:
            from baldur.core.idempotency_gate import (
                IdempotencyDecision,
                get_idempotency_gate,
            )
            from baldur.services.idempotency.models import IdempotencyKey

            idem_key = IdempotencyKey.for_dlq_replay(
                dlq_id=dlq_id,
                domain=failed_op_data.domain,
                retry_count=failed_op_data.retry_count,
            )
            gate = get_idempotency_gate()
            gate_result = gate.check_and_acquire(idem_key.cache_key)
            gate_retry_count = gate_result.retry_count
            if gate_result.decision == IdempotencyDecision.SKIP:
                logger.info(
                    "replay_service.duplicate_replay_skipped",
                    dlq_id=dlq_id,
                    idempotency_key=idem_key.cache_key,
                )
                self.repository.complete_replay(
                    dlq_id, success=True, resolution_type="duplicate_skip"
                )
                return ReplayResult.skipped_result(dlq_id, reason="duplicate")
            if gate_result.decision == IdempotencyDecision.ABORT:
                logger.info(
                    "replay_service.replay_in_progress_elsewhere",
                    dlq_id=dlq_id,
                    idempotency_key=idem_key.cache_key,
                )
                return ReplayResult.skipped_result(dlq_id, reason="in_progress")
        except Exception:
            pass  # Fail-open: gate failure → proceed with replay

        # #502 D7: replay safety gate — block when request_data was
        # truncated by the write-side forensic size cap. Gate runs before
        # the handler so customer ReplayHandler implementations stay clean.
        gate_allowed, gate_reason = _truncate_gate(failed_op_data)
        if not gate_allowed:
            # DEBUG level on the truncate gate is intentional (per #502 D7)
            # and unchanged. No explicit audit on this gate.
            self._emit_replay_blocked(
                log_event="dlq.replay_blocked_truncated",
                log_fields={
                    "dlq_id": dlq_id,
                    "domain": failed_op_data.domain,
                    "reason": gate_reason,
                },
                event_data={
                    "dlq_id": dlq_id,
                    "domain": failed_op_data.domain,
                    "block_reason": gate_reason,
                },
                metric_subject=failed_op_data.domain,
                metric_reason=gate_reason,
                log_level="debug",
            )
            # Entry stays PENDING — do not enter complete_replay so the
            # acquired retry_count stays in place for operator visibility.
            return ReplayResult.skipped_result(dlq_id, reason=gate_reason)

        # 679 D4/D5: read the origin trace captured at DLQ store time. Linkage
        # is additive — the ambient trigger trace is untouched. Missing-origin
        # entries (pre-679, no-trace capture, non-dict / marker-without-keys
        # metadata, or an entry without a metadata attribute at all) yield
        # all-None and skip linkage silently.
        from baldur.observability import span_with_link

        origin = extract_origin_trace(getattr(failed_op_data, "metadata", None))
        origin_trace_id = origin["origin_trace_id"]
        origin_log_fields: dict[str, Any] = (
            {"origin_trace_id": origin_trace_id} if origin_trace_id else {}
        )

        handler = get_replay_handler(failed_op_data.domain)

        ReplayEventHandler.on_replay_started(failed_op_data.domain, replay_type)
        start_time = time.monotonic()

        try:
            # Wrap the handler execution in a `dlq.replay` span linked to the
            # origin SpanContext (no-op when OTEL is off or the full ids are
            # absent). The link makes "original failure → replay" one trace.
            with span_with_link(
                "dlq.replay",
                origin["origin_trace_id_full"],
                origin["origin_span_id"],
                attributes={
                    "baldur.dlq.id": str(dlq_id),
                    "baldur.dlq.origin_trace_id": origin_trace_id or "",
                },
            ):
                result = handler.replay(failed_op_data)
        except Exception as e:
            duration = time.monotonic() - start_time
            ReplayEventHandler.on_replay_completed(
                failed_op_data.domain, False, duration
            )

            logger.exception(
                "replay_service.handler_exception_dlq",
                dlq_id=dlq_id,
                error=e,
                **origin_log_fields,
            )
            self.repository.complete_replay(
                id=dlq_id,
                success=False,
                note=f"Handler crash: {type(e).__name__}: {str(e)[:200]}",
                error_details={
                    "type": type(e).__name__,
                    "message": str(e)[:500],
                    "occurred_at": now().isoformat(),
                    "escalated_to": "requires_review",
                },
            )

            # Event: DLQ_REPLAY_FAILED (handler crash — distinct from COMPLETED)
            failed_event_data: dict[str, Any] = {
                "dlq_id": dlq_id,
                "domain": failed_op_data.domain,
                "replay_attempt": failed_op_data.retry_count,
                "error_type": type(e).__name__,
                "error_message": str(e)[:200],
            }
            if origin_trace_id:
                failed_event_data["origin_trace_id"] = origin_trace_id
            self._emit_event(
                EventType.DLQ_REPLAY_FAILED,
                data=failed_event_data,
            )

            # Mark idempotency gate as failed
            if idem_key:
                try:
                    from baldur.core.idempotency_gate import get_idempotency_gate

                    get_idempotency_gate().mark_failed(
                        idem_key.cache_key,
                        error=str(e),
                        retry_count=gate_retry_count,
                    )
                except Exception:
                    pass  # Fail-open

            return ReplayResult.failed(dlq_id, f"internal_error: {type(e).__name__}")

        duration = time.monotonic() - start_time
        ReplayEventHandler.on_replay_completed(
            failed_op_data.domain, result.success, duration
        )

        self.repository.complete_replay(
            id=dlq_id,
            success=result.success,
            resolution_type=_resolution_type_for(trigger) if result.success else "",
            note=result.message
            if result.success
            else (result.error or "Replay failed"),
        )

        # Mark idempotency gate completion
        if idem_key:
            try:
                from baldur.core.idempotency_gate import get_idempotency_gate

                gate = get_idempotency_gate()
                if result.success:
                    gate.mark_completed(
                        idem_key.cache_key, retry_count=gate_retry_count
                    )
                else:
                    gate.mark_failed(
                        idem_key.cache_key,
                        error=result.error or "replay_failed",
                        retry_count=gate_retry_count,
                    )
            except Exception:
                logger.warning(
                    "replay_service.gate_mark_completed_failed", dlq_id=dlq_id
                )

        if result.success:
            logger.info(
                "replay_service.dlq_entry_replayed_successfully",
                dlq_id=dlq_id,
                **origin_log_fields,
            )
        else:
            logger.warning(
                "replay_service.dlq_entry_replay_failed",
                dlq_id=dlq_id,
                result=result.error,
                **origin_log_fields,
            )

        # Audit — record the acting principal. When no explicit actor is
        # threaded (Celery task bodies / system-triggered paths), fall back to
        # the ambient ActorContext, which resolves to "system" for background
        # jobs.
        if actor_id:
            resolved_actor = actor_id
        else:
            from baldur.context.actor_context import ActorContext

            resolved_actor = ActorContext.get_current().actor_id
        log_dlq_replay_audit(
            dlq_id=dlq_id,
            domain=failed_op_data.domain,
            success=result.success,
            actor_id=resolved_actor,
            error_message=result.error,
            origin_trace_id=origin_trace_id,
        )

        # Event: DLQ_REPLAY_COMPLETED (per-item)
        # replay_attempt = current attempt number (1-indexed).
        # Both Redis and Memory adapters return post-incremented retry_count
        # from try_acquire_for_replay().
        completed_event_data: dict[str, Any] = {
            "dlq_id": dlq_id,
            "domain": failed_op_data.domain,
            "success": result.success,
            "replay_attempt": failed_op_data.retry_count,
        }
        if origin_trace_id:
            completed_event_data["origin_trace_id"] = origin_trace_id
        self._emit_event(
            EventType.DLQ_REPLAY_COMPLETED,
            data=completed_event_data,
        )

        return result

    def _record_batch_completion(
        self,
        domain: str,
        batch_result: BatchReplayResult,
        duration: float,
        *,
        extra_event_data: dict[str, Any] | None = None,
    ) -> None:
        """Record batch completion via EventBus event + Prometheus metrics (DD-8)."""
        if batch_result.total == 0:
            return
        from baldur.metrics.event_handlers import ReplayEventHandler
        from baldur.services.event_bus import EventType

        data: dict[str, Any] = {
            "domain": domain,
            "total": batch_result.total,
            "success_count": batch_result.success_count,
            "failed_count": batch_result.failed_count,
        }
        if extra_event_data:
            data.update(extra_event_data)
        self._emit_event(EventType.DLQ_REPLAY_BATCH_COMPLETED, data=data)
        ReplayEventHandler.on_batch_completed(
            domain,
            batch_result.total,
            batch_result.success_count,
            batch_result.failed_count,
            duration,
        )

    # =========================================================================
    # Single Replay
    # =========================================================================

    def replay_single(
        self,
        dlq_id: str,
        trigger: ResolutionTrigger | str = ResolutionTrigger.MANUAL_REPLAY,
        actor_id: str | None = None,
    ) -> ReplayResult:
        """
        Replay a single DLQ entry.

        This method uses atomic acquisition to prevent race conditions when
        multiple workers try to replay the same entry simultaneously.

        Safety Checks (via check_all_governance):
        1. Kill Switch - system-wide deactivation check
        2. Emergency Level - blocked at LEVEL_2+ to protect resources
        3. Error budget - automation blocked when the budget is exhausted

        Audit Logging:
        - Blocks are automatically recorded in the AuditLog

        Args:
            dlq_id: ID of the FailedOperation to replay
            trigger: Provenance trigger stamped into ``resolution_type`` on
                success (default: manual replay)
            actor_id: Acting principal for the audit trail (default: ambient
                ActorContext / system)

        Returns:
            ReplayResult indicating success or failure
        """
        # Governance check (Kill Switch, Emergency Mode, Error Budget)
        # check_all_governance automatically performs Audit logging on a block
        governance = self._get_governance().check_all_governance(
            check_kill_switch=True,
            check_emergency=True,
            emergency_min_level=2,
            check_error_budget=True,
            operation_name="replay_single",
            service_name="ReplayService",
            domain="dlq",
            audit_on_block=True,
        )

        if not governance.allowed:
            # Governance audit already ran inside check_all_governance
            # (audit_on_block=True) — audit=None so the helper does not
            # double-audit.
            self._emit_replay_blocked(
                log_event="replay_service.blocked",
                log_fields={
                    "governance": governance.block_message,
                    "dlq_id": dlq_id,
                },
                event_data={
                    "dlq_id": dlq_id,
                    "block_reason": (
                        governance.block_reason.value
                        if governance.block_reason
                        else None
                    ),
                    "block_message": governance.block_message,
                },
                metric_subject="dlq",
                metric_reason=(
                    governance.block_reason.value
                    if governance.block_reason
                    else "unknown"
                ),
            )
            return ReplayResult.blocked(dlq_id, governance)

        return self._execute_replay(dlq_id, trigger=trigger, actor_id=actor_id)

    # =========================================================================
    # Batch Replay
    # =========================================================================

    def replay_batch(
        self,
        domain: str | None = None,
        failure_type: str | None = None,
        max_items: int = 100,
        use_adaptive: bool | None = None,
        use_priority: bool | None = None,
        trigger: ResolutionTrigger | str = ResolutionTrigger.MANUAL_REPLAY,
        actor_id: str | None = None,
    ) -> BatchReplayResult:
        """
        Replay multiple DLQ entries matching criteria.

        Safety Checks (via check_all_governance):
        1. Kill Switch - system-wide deactivation check
        2. Emergency Level - blocked at LEVEL_2+ to protect resources
        3. Error budget - automation blocked when the budget is exhausted

        Adaptive Mode:
        - When adaptive_enabled=True in RuntimeConfig, batch size is dynamic
        - High failure rate (>=20%) reduces batch size by 20%
        - 3 consecutive perfect batches increases batch size by 5

        Priority Mode:
        - When priority_enabled=True in RuntimeConfig, domains are processed by priority
        - Critical domains are processed first, then normal, then low
        - Respects domain-specific max_retries overrides

        Audit Logging:
        - Blocks are automatically recorded in the AuditLog

        Args:
            domain: Filter by domain (optional, ignored in priority mode)
            failure_type: Filter by failure type (optional)
            max_items: Maximum number of items to replay (ignored in adaptive mode)
            use_adaptive: Override adaptive mode setting (None = use RuntimeConfig)
            use_priority: Override priority mode setting (None = use RuntimeConfig)

        Returns:
            BatchReplayResult with summary and individual results
        """
        # Governance check (Kill Switch, Emergency Mode, Error Budget)
        # check_all_governance automatically performs Audit logging on a block
        governance = self._get_governance().check_all_governance(
            check_kill_switch=True,
            check_emergency=True,
            emergency_min_level=2,
            check_error_budget=True,
            operation_name="replay_batch",
            service_name="ReplayService",
            domain=domain or "dlq",
            audit_on_block=True,
        )

        if not governance.allowed:
            # Governance audit already ran inside check_all_governance
            # (audit_on_block=True) — audit=None so the helper does not
            # double-audit.
            self._emit_replay_blocked(
                log_event="replay_service.blocked",
                log_fields={
                    "governance": governance.block_message,
                    "domain": domain,
                    "failure_type": failure_type,
                },
                event_data={
                    "domain": domain or "all",
                    "block_reason": (
                        governance.block_reason.value
                        if governance.block_reason
                        else None
                    ),
                    "block_message": governance.block_message,
                },
                metric_subject=domain or "all",
                metric_reason=(
                    governance.block_reason.value
                    if governance.block_reason
                    else "unknown"
                ),
            )
            return BatchReplayResult(
                total=0,
                success_count=0,
                failed_count=0,
                skipped_count=0,
                results=[],
                governance_blocked=True,
                governance_block_reason=governance.block_message,
            )

        # Determine effective max_items (Adaptive mode support)
        effective_max_items, adaptive_manager = self._get_effective_max_items(
            max_items=max_items,
            use_adaptive=use_adaptive,
        )

        max_replays = self.config["max_replay_attempts"]

        # Check if priority mode is enabled
        priority_enabled = use_priority
        if priority_enabled is None:
            priority_enabled = self._is_priority_enabled()

        # Get eligible entries using repository
        if priority_enabled and domain is None:
            # Priority mode: get entries sorted by domain priority
            entries, domains_processed = self._get_entries_by_priority(
                failure_type=failure_type,
                max_replays=max_replays,
                limit=effective_max_items,
            )
            priority_used = True
        else:
            # Normal mode: get entries by domain/failure_type filter
            entries = self.repository.find_replayable(
                max_retries=max_replays,
                domain=domain,
                failure_type=failure_type,
                limit=effective_max_items,
            )
            domains_processed = None
            priority_used = False

        batch_result = BatchReplayResult(
            total=len(entries),
            results=[],
            priority_used=priority_used,
            domains_processed=domains_processed,
        )

        batch_start = time.monotonic()

        for entry in entries:
            result = self._execute_replay(
                entry.id,
                replay_type="batch",
                trigger=trigger,
                actor_id=actor_id,
            )
            batch_result.results.append(result)

            if result.skipped:
                batch_result.skipped_count += 1
            elif result.success:
                batch_result.success_count += 1
            else:
                batch_result.failed_count += 1

        # Record batch result for adaptive adjustment
        if adaptive_manager is not None:
            adaptive_manager.record_batch_result(
                total=batch_result.total,
                success=batch_result.success_count,
                failures=batch_result.failed_count,
            )
            logger.debug(
                "replay_service.adaptive_batch_recorded",
                adaptive_manager=adaptive_manager.get_current_max_items(),
            )

        self._record_batch_completion(
            domain or "all", batch_result, time.monotonic() - batch_start
        )

        logger.info(
            "replay_service.batch_replay_completed",
            batch_result=batch_result.total,
            success_count=batch_result.success_count,
            failed_count=batch_result.failed_count,
        )

        return batch_result

    def _get_effective_max_items(
        self,
        max_items: int,
        use_adaptive: bool | None,
    ) -> tuple[int, AdaptiveReplayManager | None]:
        """
        Determine effective max_items based on adaptive mode.

        Args:
            max_items: Caller-provided max_items
            use_adaptive: Override for adaptive mode (None = use RuntimeConfig)

        Returns:
            Tuple of (effective_max_items, adaptive_manager or None)
        """
        from baldur.services.adaptive_replay import (
            get_adaptive_replay_manager,
        )

        # Check RuntimeConfig for adaptive mode
        adaptive_enabled = use_adaptive
        if adaptive_enabled is None:
            adaptive_enabled = self._is_adaptive_enabled()

        if not adaptive_enabled:
            return max_items, None

        # Get adaptive manager and configure it
        manager = get_adaptive_replay_manager()

        # Sync config from RuntimeConfig
        config = self._get_adaptive_config()
        manager.configure(config)

        effective_max_items = manager.get_current_max_items()

        logger.debug(
            "replay_service.adaptive_mode",
            max_items=max_items,
            effective_max_items=effective_max_items,
        )

        return effective_max_items, manager

    def _get_replay_automation_config(self) -> dict[str, Any] | None:
        """Resolve the PRO ``replay_automation`` RuntimeConfig block, or None.

        Returns None in two distinct situations, surfaced at distinct
        severities so an OSS install does not drown in WARNING noise:

        - **Absent** (no PRO ``RuntimeConfigManager`` registered): the
          OSS-normal state — Runtime Config is Deferred even for PRO v1.0.
          Logged at DEBUG ``replay_service.runtime_config_absent`` at most
          once per service instance, then silent.
        - **Read failure** (manager present and ``get_config()`` raises, or
          provider resolution itself raises): genuinely abnormal. Logged at
          WARNING ``replay_service.runtime_config_read_failed`` on every
          occurrence, then falls back to the absent default (None).

        Resolution is per-call (a plain registry lookup) so a late PRO
        registration is picked up; only the absence marker is
        once-per-instance. Uses the public ``manager.get_config()`` accessor,
        never the private internal getter.
        """
        from baldur.factory.registry import ProviderRegistry

        try:
            manager = ProviderRegistry.runtime_config_manager.safe_get()
            if manager is None:
                if not getattr(self, "_runtime_config_absent_logged", False):
                    logger.debug("replay_service.runtime_config_absent")
                    self._runtime_config_absent_logged = True
                return None
            return manager.get_config("replay_automation")
        except Exception as e:
            logger.warning(
                "replay_service.runtime_config_read_failed",
                error=e,
            )
            return None

    def _is_adaptive_enabled(self) -> bool:
        """Check if adaptive mode is enabled: RuntimeConfig → static settings."""
        config = self._get_replay_automation_config() or {}
        return config.get(
            "adaptive_enabled", get_replay_automation_settings().adaptive_enabled
        )

    def _is_priority_enabled(self) -> bool:
        """Check if priority mode is enabled: RuntimeConfig → static settings."""
        config = self._get_replay_automation_config() or {}
        return config.get(
            "priority_enabled", get_replay_automation_settings().priority_enabled
        )

    def _get_domain_priorities(self) -> dict[str, str]:
        """Load domain priorities: RuntimeConfig → static settings."""
        config = self._get_replay_automation_config() or {}
        return config.get(
            "domain_priorities", get_replay_automation_settings().domain_priorities
        )

    def _get_domain_max_retries(self, domain: str) -> int | None:
        """Get domain-specific max_retries override: RuntimeConfig → settings."""
        config = self._get_replay_automation_config() or {}
        domain_max_retries = config.get(
            "domain_max_retries", get_replay_automation_settings().domain_max_retries
        )
        return domain_max_retries.get(domain)

    def _load_failure_type_map(self) -> dict[str, list[str]]:
        """Load service→failure_types mapping: RuntimeConfig → static settings.

        This mapping is required for replay_on_circuit_close() to identify
        which DLQ entries are related to the recovered service. Without it,
        the method surfaces a blocked-with-signal outcome and drains nothing.
        Falling back to the static settings makes
        BALDUR_REPLAY_AUTOMATION_SERVICE_FAILURE_TYPE_MAP effective even when
        the RuntimeConfigManager is absent.

        RuntimeConfig key: replay_automation.service_failure_type_map
        Example value: {"payment_api": ["PG_TIMEOUT", "CONNECTION_ERROR"]}
        """
        config = self._get_replay_automation_config() or {}
        return config.get(
            "service_failure_type_map",
            get_replay_automation_settings().service_failure_type_map,
        )

    def _get_entries_by_priority(  # noqa: C901
        self,
        failure_type: str | None,
        max_replays: int,
        limit: int,
    ) -> tuple[list[FailedOperationData], list[str]]:
        """
        Get DLQ entries sorted by domain priority.

        Priority order: critical (1) > normal (2) > low (3) > unconfigured (4)

        Args:
            failure_type: Filter by failure type (optional)
            max_replays: Maximum retry count for filtering
            limit: Total maximum entries to return

        Returns:
            Tuple of (entries list, domains processed in order)
        """
        domain_priorities = self._get_domain_priorities()

        # Group domains by priority level
        priority_groups: dict[str, list[str]] = {
            "critical": [],
            "normal": [],
            "low": [],
        }

        for domain, priority in domain_priorities.items():
            if priority in priority_groups:
                priority_groups[priority].append(domain)

        all_entries: list[FailedOperationData] = []
        domains_processed: list[str] = []
        remaining = limit

        # Process in priority order: critical -> normal -> low
        for priority in ["critical", "normal", "low"]:
            if remaining <= 0:
                break

            for domain in priority_groups[priority]:
                if remaining <= 0:
                    break

                # Get domain-specific max_retries if configured
                domain_max = self._get_domain_max_retries(domain)
                effective_max_retries = (
                    domain_max if domain_max is not None else max_replays
                )

                entries = self.repository.find_replayable(
                    max_retries=effective_max_retries,
                    domain=domain,
                    failure_type=failure_type,
                    limit=remaining,
                )

                if entries:
                    all_entries.extend(entries)
                    domains_processed.append(domain)
                    remaining -= len(entries)

                    logger.debug(
                        "replay_service.priority_fetch",
                        domain=domain,
                        priority=priority,
                        count=len(entries),
                    )

        # If still have capacity, get entries from unconfigured domains
        if remaining > 0:
            # Get all pending entries without domain filter
            unconfigured_entries = self.repository.find_replayable(
                max_retries=max_replays,
                domain=None,
                failure_type=failure_type,
                limit=remaining + len(all_entries),  # Get extra to filter
            )

            # Filter out already fetched domains
            configured_domains = set(domain_priorities.keys())
            for entry in unconfigured_entries:
                if remaining <= 0:
                    break
                if entry.domain not in configured_domains:
                    all_entries.append(entry)
                    if entry.domain not in domains_processed:
                        domains_processed.append(entry.domain)
                    remaining -= 1

        logger.info(
            "replay_service.priority_based_fetch_complete",
            count=len(all_entries),
            domains_processed=domains_processed,
        )

        return all_entries, domains_processed

    def _get_adaptive_config(self) -> AdaptiveReplayConfig:
        """Load AdaptiveReplayConfig: RuntimeConfig → static settings."""
        from baldur.services.adaptive_replay import AdaptiveReplayConfig

        config = self._get_replay_automation_config() or {}
        settings = get_replay_automation_settings()
        return AdaptiveReplayConfig(
            min_items=config.get("adaptive_min_items", settings.adaptive_min_items),
            max_items=config.get("adaptive_max_items", settings.adaptive_max_items),
            initial_items=config.get(
                "adaptive_initial_items", settings.adaptive_initial_items
            ),
            failure_threshold=config.get(
                "adaptive_failure_threshold", settings.adaptive_failure_threshold
            ),
        )

    # =========================================================================
    # Conditional Replay (Circuit Breaker Recovery)
    # =========================================================================

    def replay_on_circuit_close(
        self,
        service_name: str,
        max_items: int = 50,
        escalate_failures: bool = True,
        service_failure_type_map: dict[str, list[str]] | None = None,
    ) -> BatchReplayResult:
        """
        Replay entries when circuit breaker closes.

        This is triggered when an external service recovers.
        Only replays entries related to the recovered service.

        IMPORTANT: When triggered by force_close with trigger_replay=True,
        any replay failures are escalated to REQUIRES_REVIEW status.
        This is because operator-initiated recovery implies the operator
        intended to resolve these items, so failures need explicit attention.

        Args:
            service_name: Name of the service that recovered
            max_items: Maximum number of items to replay
            escalate_failures: If True, mark failed replays as REQUIRES_REVIEW
            service_failure_type_map: Custom mapping of service names to failure types.
                                      If None, uses RuntimeConfig fallback.
                                      Example: {"my_service": ["TIMEOUT", "CONNECTION_ERROR"]}

        Returns:
            BatchReplayResult with summary. `inflight_skipped=True` indicates
            the per-service inflight lock rejected this call as a duplicate.
        """
        # 497 D4: cross-process inflight lock via CacheProviderInterface.get_lock().
        # Owner-fenced DistributedLock suppresses duplicate sweeps when
        # CIRCUIT_BREAKER_CLOSED is emitted more than once for the same
        # logical recovery (broker redelivery, multi-pod fan-out, or — pre-D1
        # — the now-fixed source-level multi-fire race). Fails open if the
        # cache is unavailable or `get_lock` is not supported so a degraded
        # cache does not block legitimate CB-recovery replay. Owner-fenced
        # release (Lua eval on Redis, owner_id check in-memory) prevents a
        # slow holder from clobbering a successor's freshly-acquired lock
        # after TTL expiry — the failure mode that a raw setnx+delete pattern
        # would have at scale.
        ttl_seconds = get_config().services_group.dlq.circuit_close_inflight_ttl_seconds
        cache = self.cache
        lock = None

        if cache is not None:
            try:
                lock = cache.get_lock(
                    name=_replay_inflight_lock_name(service_name),
                    timeout=timedelta(seconds=ttl_seconds),
                )
                acquired = lock.acquire(blocking=False)
            except Exception as exc:
                logger.warning(
                    "replay_service.inflight_cache_unavailable",
                    reason="lock_unavailable",
                    error=str(exc),
                    service_name=service_name,
                )
                lock = None
                acquired = False

            if lock is not None and not acquired:
                self._emit_replay_blocked(
                    log_event="replay_service.circuit_close_inflight_skipped",
                    log_fields={
                        "service_name": service_name,
                        "block_reason": REASON_CIRCUIT_CLOSE_INFLIGHT,
                    },
                    event_data={
                        "trigger": "circuit_close",
                        "service_name": service_name,
                        "block_reason": REASON_CIRCUIT_CLOSE_INFLIGHT,
                    },
                    metric_subject=service_name,
                    metric_reason=REASON_CIRCUIT_CLOSE_INFLIGHT,
                    audit={
                        "domain": "dlq",
                        "reason": REASON_CIRCUIT_CLOSE_INFLIGHT,
                        "service_name": service_name,
                        "trigger": "circuit_close",
                        "details": {"ttl_seconds": ttl_seconds},
                    },
                )
                return BatchReplayResult(inflight_skipped=True)

            if not acquired:
                # Lock construction/acquire raised — fall through to the
                # unguarded sweep (fail-open). `lock` is None so the finally
                # block below is a no-op.
                lock = None

        try:
            return self._replay_on_circuit_close_locked(
                service_name=service_name,
                max_items=max_items,
                escalate_failures=escalate_failures,
                service_failure_type_map=service_failure_type_map,
            )
        finally:
            if lock is not None:
                try:
                    lock.release()
                except Exception as exc:
                    logger.warning(
                        "replay_service.inflight_release_failed",
                        service_name=service_name,
                        error=str(exc),
                    )

    def _replay_on_circuit_close_locked(  # noqa: C901, PLR0912
        self,
        service_name: str,
        max_items: int = 50,
        escalate_failures: bool = True,
        service_failure_type_map: dict[str, list[str]] | None = None,
    ) -> BatchReplayResult:
        """Inner sweep body for `replay_on_circuit_close`.

        Extracted so the outer method can wrap this in the setnx-based
        inflight guard via a single `try/finally` without indenting the
        whole sweep. Keep this body strictly identical to the earlier
        circuit-close behavior — the lock is the only thing the guard adds.
        """
        # Explicit mapping takes precedence, RuntimeConfig as fallback
        if service_failure_type_map is not None:
            failure_type_map = service_failure_type_map
        else:
            failure_type_map = self._load_failure_type_map()

        # Order-preserving dedup at the operator-controlled boundary (D5):
        # RuntimeConfig may be misconfigured with duplicate failure types
        # (e.g., ["TIMEOUT", "TIMEOUT"]), which would dilute the divmod
        # quota allocation by issuing repeated queries against the same ID pool.
        failure_types = list(dict.fromkeys(failure_type_map.get(service_name, [])))
        if not failure_types:
            # Operator misconfig: `service_failure_type_map` has no entry for
            # this service. Surface through the same channels as governance
            # blocks (WARNING log + DLQ_REPLAY_BLOCKED event + metric + audit)
            # so a missing RuntimeConfig key is not silent.
            self._emit_replay_blocked(
                log_event="replay_service.no_failure_types_mapped",
                log_fields={
                    "service_name": service_name,
                    "block_reason": REASON_NO_FAILURE_TYPE_MAPPING,
                    "config_path": CONFIG_PATH_FAILURE_TYPE_MAP,
                },
                event_data={
                    "trigger": "circuit_close",
                    "service_name": service_name,
                    "block_reason": REASON_NO_FAILURE_TYPE_MAPPING,
                    "config_path": CONFIG_PATH_FAILURE_TYPE_MAP,
                },
                metric_subject=service_name,
                metric_reason=REASON_NO_FAILURE_TYPE_MAPPING,
                audit={
                    "domain": "dlq",
                    "reason": REASON_NO_FAILURE_TYPE_MAPPING,
                    "service_name": service_name,
                    "trigger": "circuit_close",
                    "details": {"config_path": CONFIG_PATH_FAILURE_TYPE_MAP},
                },
            )
            return BatchReplayResult()

        # Batch-level governance check (replaces per-item checks)
        governance = self._get_governance().check_all_governance(
            check_kill_switch=True,
            check_emergency=True,
            emergency_min_level=2,
            check_error_budget=True,
            operation_name="replay_on_circuit_close",
            service_name="ReplayService",
            domain="dlq",
            audit_on_block=True,
        )

        if not governance.allowed:
            # Governance audit already ran inside check_all_governance
            # (audit_on_block=True) — audit=None so the helper does not
            # double-audit.
            self._emit_replay_blocked(
                log_event="replay_service.blocked",
                log_fields={
                    "governance": governance.block_message,
                    "service_name": service_name,
                },
                event_data={
                    "trigger": "circuit_close",
                    "service_name": service_name,
                    "block_reason": (
                        governance.block_reason.value
                        if governance.block_reason
                        else None
                    ),
                    "block_message": governance.block_message,
                },
                metric_subject=service_name,
                metric_reason=(
                    governance.block_reason.value
                    if governance.block_reason
                    else "unknown"
                ),
            )
            return BatchReplayResult(
                governance_blocked=True,
                governance_block_reason=governance.block_message,
            )

        max_replays = self.config["max_replay_attempts"]
        entries: list[FailedOperationData] = []
        # Per-type fairness quota (D1): divmod proportional split prevents
        # the first failure_type's backlog from starving the rest of the
        # recovered service's mapped types on circuit-close replay.
        quota_base, extra = divmod(max_items, len(failure_types))
        logger.debug(
            "replay_service.quota_allocated",
            service_name=service_name,
            max_items=max_items,
            n_types=len(failure_types),
            quota_base=quota_base,
            extra=extra,
        )
        # D12: a per-type fill that returns exactly its allotted quota means the
        # cap may have left eligible entries undrained. Derived from the
        # existing fill loop at zero extra query (no eligible-count scan).
        capped = False
        for i, ft in enumerate(failure_types):
            quota = quota_base + (1 if i < extra else 0)
            if quota <= 0:
                break
            batch = self.repository.find_replayable(
                max_retries=max_replays,
                failure_type=ft,
                limit=quota,
            )
            if len(batch) == quota:
                capped = True
            entries.extend(batch)
            logger.debug(
                "replay_service.quota_filled",
                failure_type=ft,
                quota=quota,
                actual=len(batch),
            )

        batch_result = BatchReplayResult(total=len(entries), results=[], capped=capped)
        batch_start = time.monotonic()

        for entry in entries:
            result = self._execute_replay(
                entry.id,
                replay_type="conditional",
                trigger=ResolutionTrigger.AUTO_REPLAY_CIRCUIT_CLOSE,
            )
            batch_result.results.append(result)

            if result.skipped:
                batch_result.skipped_count += 1
            elif result.success:
                batch_result.success_count += 1
            else:
                batch_result.failed_count += 1

                # Escalate failures to REQUIRES_REVIEW (existing behavior preserved)
                # TODO: Optimize with bulk_update_status if max_items is increased
                # significantly. Note: bulk_update_status itself currently iterates
                # individually — Redis pipeline optimization needed there too.
                if escalate_failures:
                    current_entry = self.repository.get_by_id(entry.id)
                    if current_entry and current_entry.status == "pending":
                        self.repository.update_status(
                            entry.id,
                            status="requires_review",
                            resolution_note=(
                                f"Conditional replay failed after circuit close "
                                f"for {service_name}: {result.error}"
                            ),
                            recommended_action="escalate",
                        )
                        logger.warning(
                            "replay_service.escalated_dlq_after_conditional",
                            entry=entry.id,
                        )

        self._record_batch_completion(
            service_name,
            batch_result,
            time.monotonic() - batch_start,
            extra_event_data={
                "trigger": "circuit_close",
                "service_name": service_name,
            },
        )

        logger.info(
            "replay_service.circuit_close_replay",
            service_name=service_name,
            batch_result=batch_result.total,
            success_count=batch_result.success_count,
            failed_count=batch_result.failed_count,
            escalated_failures=(batch_result.failed_count if escalate_failures else 0),
        )

        return batch_result


# =============================================================================
# Module-level convenience functions
# =============================================================================


_replay_service: ReplayService | None = None
_replay_service_lock = threading.Lock()


def get_replay_service() -> ReplayService:
    """Get the singleton replay service instance."""
    global _replay_service
    if _replay_service is None:
        with _replay_service_lock:
            if _replay_service is None:
                _replay_service = ReplayService()
    return _replay_service


def reset_replay_service() -> None:
    """Reset the singleton replay service instance."""
    global _replay_service
    _replay_service = None


def replay_failed_operation(dlq_id: str) -> ReplayResult:
    """
    Convenience function to replay a single DLQ entry.

    This is a shortcut for get_replay_service().replay_single(dlq_id).
    """
    return get_replay_service().replay_single(dlq_id)


def batch_replay_by_failure_type(
    failure_type: str,
    max_items: int = 100,
) -> BatchReplayResult:
    """
    Convenience function to replay entries by failure type.

    This is a shortcut for get_replay_service().replay_batch(...).
    """
    return get_replay_service().replay_batch(
        failure_type=failure_type,
        max_items=max_items,
    )
