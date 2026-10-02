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

import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.helpers import log_dlq_replay_audit, log_dlq_replay_blocked_audit
from baldur.audit.trace import extract_origin_trace
from baldur.core.process_utils import fork_safe_lock
from baldur.interfaces.repositories import ResolutionTrigger, encode_replay_cursor
from baldur.models.dlq import OPEN_CIRCUIT_FAILURE_TYPE, POLICY_CHAIN_CAPTURE_SOURCE
from baldur.services.event_bus.emitter import EventEmitterMixin
from baldur.settings import get_config
from baldur.settings.replay_automation import get_replay_automation_settings
from baldur.utils.domain_validation import FALLBACK_DOMAIN, resolve_stored_domain
from baldur.utils.time import ensure_aware, utc_now

from .handlers import _truncate_gate, get_replay_handler, has_replay_handler
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


# Block reasons a recovery reports when work is parked under the recovered
# name and no lane can replay it. A name gets a lane from the operator map,
# from its own replay handler (the open-circuit lane and the types the handler
# declares), or not at all — and with no map entry, the handler lanes are
# missing for exactly one of two causes: the name has no domain of its own, or
# no replay handler is registered for its domain in this process. The
# open-circuit lane logs the same two strings when it declines a name.
REASON_NO_REPLAY_HANDLER = "no_replay_handler_registered"
REASON_DOMAIN_NOT_ADDRESSABLE = "domain_not_addressable"

# What the operator does about each. Entries at their replay cap count as
# parked but no lane selects them, so the console is named for those too.
_REMEDIATION_NO_REPLAY_HANDLER = (
    "Import, in the worker that runs the recovery sweep, the module that "
    "registers this domain's replay handler (for a @protected(replay=True) "
    "job, the module that defines it); until then, and for entries already at "
    "their replay cap, the console replays the parked entries."
)
_REMEDIATION_DOMAIN_NOT_ADDRESSABLE = (
    "Name the breaker with a valid domain (lowercase letters, digits and "
    "underscores, starting with a letter, dot-separated, at most 64 "
    "characters) so its parked calls are stored under it; until then the "
    "console replays them."
)


# Block reasons a *chain* of on-recovery passes reports when it stops with work
# still reachable. All are task-level: the service already emits its own
# blocked-family signal for the governance stop, so re-emitting it from the
# chain would double-count the metric.
REASON_CONTINUATION_BOUND_REACHED = "continuation_bound_reached"
REASON_CIRCUIT_REOPENED = "circuit_reopened"
REASON_PASS_ERRORED = "pass_errored"
REASON_PASS_MADE_NO_PROGRESS = "pass_made_no_progress"
REASON_INTEGRITY_BLOCKED = "integrity_blocked"

# A chain stopped because an operator holds a breaker projecting onto its
# domain. Not a fault and not a blocked replay: the operator's own action was
# audited where the pin was set, so this stop logs at INFO and nothing else.
REASON_OPERATOR_HOLD = "operator_hold"

# Outcomes of taking one domain's recovery inflight lock without blocking.
RECOVERY_LOCK_ACQUIRED = "acquired"
RECOVERY_LOCK_HELD = "held"
RECOVERY_LOCK_UNAVAILABLE = "unavailable"

# The ``trigger`` field of a recovery sweep's events, by the provenance the
# sweep stamps on what it replays.
_SWEEP_EVENT_TRIGGERS: dict[str, str] = {
    ResolutionTrigger.AUTO_REPLAY_CIRCUIT_CLOSE.value: "circuit_close",
    ResolutionTrigger.AUTO_REPLAY_RECOVERY.value: "recovery_trial",
}


# One selection lane: ``(failure type, domain, capture source)``. Every
# automatic lane is scoped to one stored domain; the open-circuit lane is
# additionally scoped to policy-chain captures.
_Lane = tuple[str, str | None, str | None]


def _lane_key(failure_type: str, domain: str | None) -> str:
    """Stable key for one selection lane, safe to put on a broker message."""
    return f"{failure_type}|{domain or ''}"


@dataclass
class _LaneSelection:
    """What one pass's fill produced, and where each lane stopped."""

    selected: list[tuple[str, FailedOperationData]] = field(default_factory=list)
    cursors: dict[str, str] = field(default_factory=dict)
    scan_exhausted_lanes: list[str] = field(default_factory=list)
    capped: bool = False


def recovery_lanes(domain: str, failure_type_map: dict[str, list[str]]) -> list[_Lane]:
    """Every lane an automatic recovery of one stored domain selects through.

    The one place an automatic lane set is built, so the sweep, its idle check
    and the recovery trial select one set. Every lane is scoped to ``domain``:

    - the failure types an operator mapped to a service name whose stored
      domain is ``domain`` (a mapped type no longer reaches other domains);
    - the types the domain's replay handler declares and the map does not;
    - the open-circuit lane (policy-chain captures only), unless the
      open-circuit type is mapped — that mapped lane then covers it.

    Empty when ``domain`` is the unclassifiable bucket or has no registered
    replay handler: every automatic lane needs a handler that can run, and a
    default handler always fails.

    Args:
        domain: A stored domain (``resolve_stored_domain`` output).
        failure_type_map: Service name → failure types an operator mapped.
    """
    if domain == FALLBACK_DOMAIN or not has_replay_handler(domain):
        return []

    # Order-preserving dedup at the operator-controlled boundary: a map may
    # name one type twice, which would dilute the quota split with repeated
    # queries against the same pool.
    mapped: list[str] = []
    for service_name, failure_types in (failure_type_map or {}).items():
        if not isinstance(failure_types, (list, tuple)):
            continue
        if resolve_stored_domain(str(service_name)) != domain:
            continue
        for failure_type in failure_types:
            if isinstance(failure_type, str) and failure_type not in mapped:
                mapped.append(failure_type)
    lanes: list[_Lane] = [(failure_type, domain, None) for failure_type in mapped]

    # A handler that knows how to re-run its domain's work names which parked
    # failures replay automatically; no map entry is needed.
    try:
        declared = tuple(get_replay_handler(domain).auto_replay_failure_types)
    except Exception as exc:
        logger.warning(
            "replay_service.declared_failure_types_unreadable",
            healing_domain=domain,
            error=str(exc),
        )
        declared = ()
    lanes.extend(
        (failure_type, domain, None)
        for failure_type in dict.fromkeys(declared)
        if isinstance(failure_type, str)
        and failure_type not in mapped
        and failure_type != OPEN_CIRCUIT_FAILURE_TYPE
    )

    # Open-circuit captures need no map entry: the circuit that closed is the
    # one that rejected them. Only a policy-chain capture's domain names the
    # rejecting breaker, so the lane is restricted to that source.
    if OPEN_CIRCUIT_FAILURE_TYPE not in mapped:
        lanes.append((OPEN_CIRCUIT_FAILURE_TYPE, domain, POLICY_CHAIN_CAPTURE_SOURCE))
    return lanes


# Skip reasons of a replay that never ran its job. The handler refused the
# entry before it was taken; or the handler ran but the job's body never began
# because its own breaker refused the call, or for another reason (its own
# idempotency key held or unverifiable). The last two give their attempt back.
REASON_HANDLER_REFUSED = "handler_refused"
REASON_BREAKER_REFUSED = "breaker_refused"
REASON_JOB_NOT_STARTED = "job_not_started"

# Entry metadata key counting the recovery trials that ran the job and failed.
RECOVERY_TRIALS_METADATA_KEY = "recovery_trials"


def _handler_refusal(handler: Any, entry: FailedOperationData) -> str | None:
    """Why the handler refuses this entry, or None when it may be replayed.

    ``can_replay`` is customer code: a raise is a refusal. It returns a
    ``(bool, reason)`` tuple, so the verdict is unpacked, never truth-tested.
    """
    try:
        allowed, reason = handler.can_replay(entry)
    except Exception as exc:
        return f"can_replay raised {type(exc).__name__}: {str(exc)[:200]}"
    if allowed:
        return None
    return str(reason or "refused")


def _job_start_flags(result: ReplayResult) -> tuple[bool, bool]:
    """``(the job's body began, its own breaker refused the call)`` from a result.

    Read off the flags a handler reports in its result's ``data``
    (``job_started``, ``rejected_by_breaker``). A handler that reports neither
    counts as having begun — a failure there is charged as before.
    """
    data = result.data if isinstance(result.data, dict) else {}
    refused = data.get("rejected_by_breaker") is True
    began = data.get("job_started")
    if not isinstance(began, bool):
        began = not refused
    return began, refused and not began


def _skip_reason(result: ReplayResult) -> str | None:
    """The reason a skipped replay result carries, or None for any other result."""
    if not result.skipped or not isinstance(result.data, dict):
        return None
    reason = result.data.get("skip_reason")
    return reason if isinstance(reason, str) else None


def _recovery_trial_count(entry: FailedOperationData) -> dict[str, int]:
    """Metadata merge counting one more recovery trial that ran and failed."""
    metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
    previous = metadata.get(RECOVERY_TRIALS_METADATA_KEY, 0)
    if not isinstance(previous, int) or isinstance(previous, bool):
        previous = 0
    return {RECOVERY_TRIALS_METADATA_KEY: previous + 1}


def _resolution_type_for(trigger: ResolutionTrigger | str) -> str:
    """Normalize a trigger (enum or raw string) to its stamped value.

    ``ResolutionTrigger`` is a ``(str, Enum)`` so members already ARE strings,
    but ``str(member)`` yields the ``ResolutionTrigger.X`` repr on some Python
    versions — take ``.value`` for the enum, pass a raw string through.
    """
    return trigger.value if isinstance(trigger, ResolutionTrigger) else str(trigger)


def _resolution_wall_time(failed_op_data: FailedOperationData) -> float | None:
    """Seconds from the original failure to now, or None when uncomputable.

    This is the recovery-duration histogram's documented semantic. Degrading
    to None keeps the resolution itself recorded when a repository supplies no
    usable ``created_at`` — the duration is a bonus signal, the pending-gauge
    decrement is not.
    """
    try:
        created_at = getattr(failed_op_data, "created_at", None)
        if created_at is None:
            return None
        return max(0.0, (utc_now() - ensure_aware(created_at)).total_seconds())
    except Exception:
        return None


def _record_item_resolved(
    failed_op_data: FailedOperationData, resolution_type: str
) -> None:
    """Feed a replay-path resolution into the DLQ resolution metrics.

    Every other resolution path reaches ``on_item_resolved`` through
    ``resolve_entry``; the replay pipeline finalizes entries via
    ``complete_replay`` instead, and so would otherwise skip the pending-gauge
    decrement, the recovery-duration histogram, and the daily report's
    resolved count.

    Fail-open: recording must never fail a replay that already succeeded.
    """
    try:
        from baldur.metrics.event_handlers import DLQMetricEventHandler

        DLQMetricEventHandler.on_item_resolved(
            domain=failed_op_data.domain,
            resolution_type=resolution_type,
            duration_seconds=_resolution_wall_time(failed_op_data),
        )
    except Exception as e:
        logger.warning(
            "replay_service.resolution_metrics_failed",
            resolution_type=resolution_type,
            error=e,
        )


def _replay_inflight_lock_name(subject: str) -> str:
    """Build the inflight lock name for one recovery of one domain.

    The name shape is fixed (`replay:inflight:circuit_close:<subject>`) so that
    any worker / pod sharing the same cache backend resolves to the same
    `DistributedLock` for the same recovery — owner-fenced acquire/release
    in `cache.get_lock()` makes the suppression cross-process safe.
    """
    return f"replay:inflight:circuit_close:{subject}"


def recovery_lock_subject(service_name: str) -> str:
    """What a recovery of this breaker name is locked by: its stored domain.

    Raw names projecting onto one stored domain select one lane set, so they
    share one lock. A name without a domain of its own keeps its raw name: the
    unclassifiable bucket pools unrelated names.
    """
    domain = resolve_stored_domain(service_name)
    return service_name if domain == FALLBACK_DOMAIN else domain


def sweep_event_trigger(trigger: ResolutionTrigger | str) -> str:
    """The ``trigger`` field a recovery sweep's events carry for this provenance."""
    return _SWEEP_EVENT_TRIGGERS.get(_resolution_type_for(trigger), "circuit_close")


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
        result = service.replay_single(dlq_id="web-1:112:a1b2c3d4e5f60708:5")

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
        *,
        entry: FailedOperationData | None = None,
        trial: bool = False,
    ) -> ReplayResult:
        """Core replay logic: gates -> acquire -> handler -> complete -> audit -> event.

        Handlers MUST ensure idempotency. Partial failure rollback is the
        handler's responsibility; for multi-step compensation, consider a
        dedicated compensation flow instead.

        The truncate gate and the handler's own ``can_replay`` are asked
        before the entry is taken: a refused entry stays PENDING with no
        attempt spent. A replay whose job body never began (its own breaker
        refused the call, or its own idempotency guard did not let it start)
        gives its attempt back and returns a skipped result. A trial
        (``trial=True``, the recovery trial) also gives back the attempt of a
        replay whose job ran and failed. A failed replay whose handler's work
        may still be running is left REPLAYING for the stale release.

        Args:
            dlq_id: DLQ entry to replay.
            replay_type: "single"/"conditional"/"batch" — feeds the replay
                metric label; NOT the provenance stamp.
            trigger: Provenance trigger stamped into ``resolution_type`` on
                success (distinct from ``replay_type``).
            actor_id: Acting principal for the audit trail. When omitted,
                falls back to the ambient ``ActorContext`` (``system`` for
                background/Celery paths).
            entry: The entry as the selecting lane read it; read here when
                omitted. Only the gates read it — the replay runs on the entry
                the acquisition returns.
            trial: Run as a recovery trial: a replay whose job ran and failed
                costs the entry none of its replay attempts.
        """

        from baldur.metrics.event_handlers import ReplayEventHandler
        from baldur.metrics.registry import register_domain
        from baldur.services.event_bus import EventType

        snapshot = entry if entry is not None else self.repository.get_by_id(dlq_id)
        if snapshot is None:
            logger.debug(
                "replay_service.acquisition_skipped",
                dlq_id=dlq_id,
                reason="not_found",
            )
            return ReplayResult.failed(dlq_id, "DLQ entry not found")

        # #502 D7: replay safety gate — block when request_data was
        # truncated by the write-side forensic size cap. Asked before the
        # entry is taken, so a truncated entry stays PENDING with no attempt
        # spent instead of sitting in REPLAYING until the stale release.
        gate_allowed, gate_reason = _truncate_gate(snapshot)
        if not gate_allowed:
            # DEBUG level on the truncate gate is intentional (per #502 D7)
            # and unchanged. No explicit audit on this gate.
            self._emit_replay_blocked(
                log_event="dlq.replay_blocked_truncated",
                log_fields={
                    "dlq_id": dlq_id,
                    "domain": snapshot.domain,
                    "reason": gate_reason,
                },
                event_data={
                    "dlq_id": dlq_id,
                    "domain": snapshot.domain,
                    "block_reason": gate_reason,
                },
                metric_subject=snapshot.domain,
                metric_reason=gate_reason,
                log_level="debug",
            )
            return ReplayResult.skipped_result(dlq_id, reason=gate_reason)

        handler = get_replay_handler(snapshot.domain)
        refusal = _handler_refusal(handler, snapshot)
        if refusal is not None:
            # Nothing was attempted: no event, metric or audit of a replay.
            logger.debug(
                "replay_service.replay_refused_by_handler",
                dlq_id=dlq_id,
                healing_domain=snapshot.domain,
                reason=refusal,
            )
            return ReplayResult.skipped_result(dlq_id, reason=REASON_HANDLER_REFUSED)

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

        acquired_count = failed_op_data.retry_count

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
                finalized = self.repository.complete_replay(
                    dlq_id, success=True, resolution_type="duplicate_skip"
                )
                # This exit finalizes a pending entry, so it must count like
                # any other resolution or the pending gauge stays stale. It
                # cannot double-count: an earlier attempt that already
                # recorded left the entry non-pending and unacquirable, and a
                # crashed one never recorded. Gated on the write landing —
                # complete_replay reports False when the entry is gone, which
                # resolved nothing.
                if finalized:
                    _record_item_resolved(failed_op_data, "duplicate_skip")
                return ReplayResult.skipped_result(dlq_id, reason="duplicate")
            if gate_result.decision == IdempotencyDecision.ABORT:
                logger.info(
                    "replay_service.replay_in_progress_elsewhere",
                    dlq_id=dlq_id,
                    idempotency_key=idem_key.cache_key,
                )
                if trial:
                    # The job never ran: a trial costs nothing. The entry stays
                    # REPLAYING, as this exit leaves it on every lane.
                    self._give_back_attempt(dlq_id, acquired_count)
                return ReplayResult.skipped_result(dlq_id, reason="in_progress")
        except Exception:
            pass  # Fail-open: gate failure → proceed with replay

        # 679 D4/D5: read the origin trace captured at DLQ store time. Linkage
        # is additive — the ambient trigger trace is untouched. Missing-origin
        # entries (pre-679, no-trace capture, non-dict / marker-without-keys
        # metadata, or an entry without a metadata attribute at all) yield
        # all-None and skip linkage silently.
        from baldur.core.abandoned_work import close_work_scope, open_work_scope
        from baldur.observability import span_with_link

        origin = extract_origin_trace(getattr(failed_op_data, "metadata", None))
        origin_trace_id = origin["origin_trace_id"]
        origin_log_fields: dict[str, Any] = (
            {"origin_trace_id": origin_trace_id} if origin_trace_id else {}
        )

        # Declaration site, read-side twin of ``store_failure``: the stored
        # domain was declared when the entry was captured, but the registry is
        # per process, and a replay that runs before this process has made a
        # single protected call in that domain (a cron job replaying first,
        # then sweeping) would otherwise see its first ``replay.started``
        # collapse to the fallback label. Bare call: the registry never raises.
        register_domain(failed_op_data.domain)

        ReplayEventHandler.on_replay_started(failed_op_data.domain, replay_type)
        start_time = time.monotonic()

        # The handler runs inside a work scope: a timeout site whose cancel
        # failed records the still-running work into it, and a scope still
        # holding at close means the job may still be running.
        result: ReplayResult | None = None
        raised: Exception | None = None
        scope, token = open_work_scope(None)
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
            raised = e
        finally:
            work_may_continue = close_work_scope(scope, token) is None

        duration = time.monotonic() - start_time

        if raised is not None:
            return self._finish_crashed_replay(
                failed_op_data,
                raised,
                duration=duration,
                idem_key=idem_key,
                gate_retry_count=gate_retry_count,
                acquired_count=acquired_count,
                trial=trial,
                work_may_continue=work_may_continue,
                origin_trace_id=origin_trace_id,
            )

        assert result is not None  # set whenever the handler did not raise
        ReplayEventHandler.on_replay_completed(
            failed_op_data.domain, result.success, duration
        )

        body_began, refused_by_own_breaker = _job_start_flags(result)
        if result.success:
            finalized = self.repository.complete_replay(
                id=dlq_id,
                success=True,
                resolution_type=_resolution_type_for(trigger),
                note=result.message,
            )

            # Gated on the write landing: complete_replay reports False when
            # the entry vanished between acquisition and completion (TTL
            # expiry, a concurrent purge, eviction), which transitioned nothing
            # and so must not decrement the pending gauge or count as a
            # resolution.
            if finalized:
                _record_item_resolved(failed_op_data, _resolution_type_for(trigger))
            self._mark_replay_key(idem_key, gate_retry_count, dlq_id, result)
        else:
            # Key, give-back, completion — in that order: the attempt number
            # becomes reusable only once its key reads failed.
            self._mark_replay_key(idem_key, gate_retry_count, dlq_id, result)
            still_ours = True
            if trial or not body_began:
                still_ours = self._give_back_attempt(dlq_id, acquired_count)
            if still_ours and not work_may_continue:
                self.repository.complete_replay(
                    id=dlq_id,
                    success=False,
                    note=result.error or "Replay failed",
                    error_details=(
                        _recovery_trial_count(failed_op_data)
                        if trial and body_began
                        else None
                    ),
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

        if not result.success and not body_began:
            # The job never began: not a failed replay but a skipped one, its
            # attempt given back above.
            skipped = ReplayResult.skipped_result(
                dlq_id,
                reason=(
                    REASON_BREAKER_REFUSED
                    if refused_by_own_breaker
                    else REASON_JOB_NOT_STARTED
                ),
            )
            skipped.error = result.error
            skipped.handler_ran = True
            return skipped

        result.handler_ran = True
        result.work_may_continue = work_may_continue and not result.success
        return result

    def _finish_crashed_replay(
        self,
        failed_op_data: FailedOperationData,
        raised: Exception,
        *,
        duration: float,
        idem_key: Any,
        gate_retry_count: int,
        acquired_count: int,
        trial: bool,
        work_may_continue: bool,
        origin_trace_id: str | None,
    ) -> ReplayResult:
        """Settle a replay whose handler raised: key, give-back, completion, event.

        A handler that raised reports no start flags, so its job counts as
        begun: a non-trial replay is charged as before.
        """
        from baldur.metrics.event_handlers import ReplayEventHandler
        from baldur.services.event_bus import EventType

        dlq_id = failed_op_data.id
        ReplayEventHandler.on_replay_completed(failed_op_data.domain, False, duration)

        logger.exception(
            "replay_service.handler_exception_dlq",
            dlq_id=dlq_id,
            error=raised,
            **({"origin_trace_id": origin_trace_id} if origin_trace_id else {}),
        )

        # Key, give-back, completion — in that order: the attempt number
        # becomes reusable only once its key reads failed.
        if idem_key:
            try:
                from baldur.core.idempotency_gate import get_idempotency_gate

                get_idempotency_gate().mark_failed(
                    idem_key.cache_key,
                    error=str(raised),
                    retry_count=gate_retry_count,
                )
            except Exception:
                pass  # Fail-open

        still_ours = True
        if trial:
            still_ours = self._give_back_attempt(dlq_id, acquired_count)
        if still_ours and not work_may_continue:
            error_details: dict[str, Any] = {
                "type": type(raised).__name__,
                "message": str(raised)[:500],
                "occurred_at": utc_now().isoformat(),
                "escalated_to": "requires_review",
            }
            if trial:
                error_details.update(_recovery_trial_count(failed_op_data))
            self.repository.complete_replay(
                id=dlq_id,
                success=False,
                note=f"Handler crash: {type(raised).__name__}: {str(raised)[:200]}",
                error_details=error_details,
            )

        # Event: DLQ_REPLAY_FAILED (handler crash — distinct from COMPLETED)
        failed_event_data: dict[str, Any] = {
            "dlq_id": dlq_id,
            "domain": failed_op_data.domain,
            "replay_attempt": failed_op_data.retry_count,
            "error_type": type(raised).__name__,
            "error_message": str(raised)[:200],
        }
        if origin_trace_id:
            failed_event_data["origin_trace_id"] = origin_trace_id
        self._emit_event(
            EventType.DLQ_REPLAY_FAILED,
            data=failed_event_data,
        )

        crashed = ReplayResult.failed(
            dlq_id, f"internal_error: {type(raised).__name__}"
        )
        crashed.handler_ran = True
        crashed.work_may_continue = work_may_continue
        return crashed

    def _mark_replay_key(
        self,
        idem_key: Any,
        gate_retry_count: int,
        dlq_id: str,
        result: ReplayResult,
    ) -> None:
        """Mark this attempt number's replay key completed or failed (fail-open)."""
        if not idem_key:
            return
        try:
            from baldur.core.idempotency_gate import get_idempotency_gate

            gate = get_idempotency_gate()
            if result.success:
                gate.mark_completed(idem_key.cache_key, retry_count=gate_retry_count)
            else:
                gate.mark_failed(
                    idem_key.cache_key,
                    error=result.error or "replay_failed",
                    retry_count=gate_retry_count,
                )
        except Exception:
            logger.warning("replay_service.gate_mark_completed_failed", dlq_id=dlq_id)

    def _give_back_attempt(self, dlq_id: str, acquired_count: int) -> bool:
        """Give back the attempt this replay's acquisition took.

        Returns whether this replay still holds the entry, so the caller knows
        whether to complete it. A repository that cannot give an attempt back,
        or a store fault, leaves the attempt spent and the completion
        proceeding — a store fault costs an attempt, never an entry. A
        give-back the store refuses means another replay holds the entry (the
        stale release returned it, or another acquisition took it), so this
        one must not complete it.
        """
        try:
            returned = self.repository.return_replay_attempt(dlq_id, acquired_count)
        except NotImplementedError:
            logger.debug(
                "replay_service.replay_attempt_return_unsupported",
                dlq_id=dlq_id,
                repository=type(self.repository).__name__,
            )
            return True
        except Exception as exc:
            logger.warning(
                "replay_service.replay_attempt_return_failed",
                dlq_id=dlq_id,
                error=str(exc),
            )
            return True
        if not returned:
            logger.debug(
                "replay_service.replay_attempt_return_skipped",
                dlq_id=dlq_id,
                acquired_retry_count=acquired_count,
            )
        return bool(returned)

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
            # Separates "this sweep drained everything there was" from "this
            # sweep filled its quota and stopped" — the same event otherwise.
            # Both emitting lanes compute it, so the field has one meaning
            # wherever it is read.
            "capped": batch_result.capped,
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
            # Same meaning the circuit-close sweep gives the flag: the
            # selection filled its allotment exactly, so eligible entries may
            # remain. Computed here too, because the completion event both
            # lanes share must not carry a field that is a constant lie on one
            # of them.
            capped=len(entries) == effective_max_items,
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
                entry=entry,
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

        One of the three lane sources replay_on_circuit_close() selects
        through: a mapped type selects its entries under the mapped service's
        own stored domain. The other two need no map entry — the open-circuit lane and the types the
        domain's own replay handler declares — so the map is needed only for
        failure types the recovered domain's handler does not declare.
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
        *,
        deadline: float | None = None,
        lane_cursors: dict[str, str] | None = None,
        continuation: int = 0,
        trigger: ResolutionTrigger | str = ResolutionTrigger.AUTO_REPLAY_CIRCUIT_CLOSE,
    ) -> BatchReplayResult:
        """
        Replay entries when a service recovers.

        This is triggered when an external service recovers — by its breaker
        closing, or by a recovery trial that found it answering again. Only
        replays entries stored under the recovered service's own domain.

        IMPORTANT: With ``escalate_failures`` (the CLOSED-transition lane), a
        replay whose job ran and failed is escalated to REQUIRES_REVIEW: a
        breaker reaching CLOSED is strong evidence of recovery, so a failure
        there needs explicit attention. A replay whose job never began is
        never escalated.

        One call is one pass. A caller that means to clear a whole backlog runs
        passes in sequence, handing each one the previous result's
        ``lane_cursors``; the keyword arguments all default to a single pass.

        Args:
            service_name: Name of the service that recovered
            max_items: Maximum number of items to replay in THIS pass
            escalate_failures: If True, mark replays whose job ran and failed
                as REQUIRES_REVIEW
            service_failure_type_map: Custom mapping of service names to failure types.
                                      If None, uses RuntimeConfig fallback.
                                      Example: {"my_service": ["TIMEOUT", "CONNECTION_ERROR"]}
            deadline: ``time.monotonic()`` value past which the pass stops
                selecting and replaying and returns what it has, ``capped``.
                The caller derives it from whatever wall clock would otherwise
                kill the pass mid-flight.
            lane_cursors: Per-lane positions returned by the previous pass.
            continuation: How many passes preceded this one. Rotates which lane
                leads the fill, so a deadline landing mid-list cannot starve
                the same tail on every pass.
            trigger: Provenance stamped on what the pass replays — the breaker
                closing, or a recovery trial's success.

        Returns:
            BatchReplayResult with summary. `inflight_skipped=True` indicates
            another recovery of the same domain already holds the inflight
            lock, and this call joined it.
        """
        # Cross-process inflight lock via CacheProviderInterface.get_lock(),
        # named by the stored domain: one recovery per domain at a time, so
        # duplicate dispatches (broker redelivery, multi-pod fan-out, a trial's
        # sweep beside a CLOSED event's) join instead of replaying side by
        # side. Fails open if the cache is unavailable or `get_lock` is not
        # supported, so a degraded cache does not block legitimate recovery.
        # Owner-fenced release prevents a slow holder from clobbering a
        # successor's freshly-acquired lock after TTL expiry.
        subject = recovery_lock_subject(service_name)
        lock, lock_state, lock_error = self.try_acquire_recovery_lock(subject)

        if lock_state == RECOVERY_LOCK_HELD:
            # A second dispatch for a recovery already running is not a
            # blocked replay: the running one drains the same lanes.
            logger.info(
                "replay_service.circuit_close_inflight_joined",
                service_name=service_name,
                healing_domain=subject,
            )
            return BatchReplayResult(inflight_skipped=True)

        if lock_state == RECOVERY_LOCK_UNAVAILABLE and self.cache is not None:
            logger.warning(
                "replay_service.inflight_cache_unavailable",
                reason="lock_unavailable",
                error=lock_error,
                service_name=service_name,
            )

        try:
            return self._replay_on_circuit_close_locked(
                service_name=service_name,
                max_items=max_items,
                escalate_failures=escalate_failures,
                service_failure_type_map=service_failure_type_map,
                deadline=deadline,
                lane_cursors=lane_cursors,
                continuation=continuation,
                trigger=trigger,
            )
        finally:
            self.release_recovery_lock(lock, service_name)

    def try_acquire_recovery_lock(self, subject: str) -> tuple[Any, str, str | None]:
        """Take the inflight lock of one domain's recovery without blocking.

        Returns ``(lock, state, error)``: state is ``RECOVERY_LOCK_ACQUIRED``
        (pass the lock to :meth:`release_recovery_lock`),
        ``RECOVERY_LOCK_HELD`` (another recovery of the domain holds it), or
        ``RECOVERY_LOCK_UNAVAILABLE`` (no cache, or the cache failed to build
        or acquire the lock — the lock is None, ``error`` says why). Logs
        nothing: the sweep proceeds unguarded on an unavailable lock and the
        recovery trial waits a tick, and each says so its own way.
        """
        cache = self.cache
        if cache is None:
            return None, RECOVERY_LOCK_UNAVAILABLE, "no_cache_provider"
        ttl_seconds = get_config().services_group.dlq.circuit_close_inflight_ttl_seconds
        try:
            lock = cache.get_lock(
                name=_replay_inflight_lock_name(subject),
                timeout=timedelta(seconds=ttl_seconds),
            )
            acquired = lock.acquire(blocking=False)
        except Exception as exc:
            return None, RECOVERY_LOCK_UNAVAILABLE, str(exc)
        if not acquired:
            return None, RECOVERY_LOCK_HELD, None
        return lock, RECOVERY_LOCK_ACQUIRED, None

    @staticmethod
    def release_recovery_lock(lock: Any, service_name: str) -> None:
        """Release a lock :meth:`try_acquire_recovery_lock` returned (None: no-op)."""
        if lock is None:
            return
        try:
            lock.release()
        except Exception as exc:
            logger.warning(
                "replay_service.inflight_release_failed",
                service_name=service_name,
                error=str(exc),
            )

    def recovery_is_idle(self, service_name: str) -> bool:
        """Is a recovery pass for this name certain to replay nothing?

        True only when the name's stored domain gets no lane — it has no
        domain of its own, or no replay handler is registered for it — and
        the store answers that nothing is parked under its name. A pass with
        no lane cannot replay anything whatever is stored, so ending it early
        changes no replay; the count decides only whether there is work that
        pass would leave behind, which the operator must hear about.

        A name with a lane is never idle, even with nothing parked: entries
        that become pending between a count and the selection would be missed.
        The lane check runs first, so such a name pays no store read.

        Never raises: a failure while deciding answers False, so the pass runs
        as it would have and reports that failure where it always did.
        """
        try:
            failure_type_map = self._load_failure_type_map()
            if recovery_lanes(resolve_stored_domain(service_name), failure_type_map):
                return False
            return self.parked_count_for_recovery(service_name) == 0
        except Exception as exc:
            logger.debug(
                "replay_service.recovery_idle_unavailable",
                service_name=service_name,
                error=str(exc),
            )
            return False

    def parked_count_for_recovery(self, service_name: str) -> int | None:
        """Pending entries a recovery of this name could concern, or None.

        The count of pending entries stored under the name's own domain, read
        from the shared store. Every lane a recovery selects through is scoped
        to that domain, so the count bounds what it could replay. None means
        the question cannot be answered, and every caller treats None as
        "work may be parked":

        - the name has no domain of its own — the bucket it shares with every
          other rejected name cannot be attributed to it;
        - the store cannot answer from its shared view;
        - the store returned something that is not a count.

        Args:
            service_name: The breaker name that recovered.
        """
        domain = resolve_stored_domain(service_name)
        if domain == FALLBACK_DOMAIN:
            return None

        try:
            count = self.repository.get_cluster_pending_count_by_domain(domain)
        except Exception as exc:
            logger.debug(
                "replay_service.parked_count_unavailable",
                service_name=service_name,
                healing_domain=domain,
                error=str(exc),
            )
            return None
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return None
        return count

    def emit_circuit_close_chain_stopped(
        self,
        *,
        service_name: str,
        block_reason: str,
        scan_exhausted_lanes: list[str] | None = None,
        lane_cursors: dict[str, str] | None = None,
        offending_circuit: str | None = None,
    ) -> None:
        """Announce that a chain of on-recovery passes stopped with work reachable.

        `capped` on the completion event says a *pass* filled its quota; it
        cannot say whether anything will come back for the rest, because the
        pass that continues and the pass that gave up emit it identically. This
        is the signal that can: WARNING log, ``DLQ_REPLAY_BLOCKED`` event,
        metric and audit — the channel an operator already watches for "the
        lane stopped and you should know".

        The lane cursors ride along because they are already in the caller's
        hand and cost no extra query. They are diagnostic: nothing accepts a
        cursor back today, so they record how far a chain got rather than
        offering a resume.

        Called by the task that owns the chain. Governance and inflight stops
        are NOT routed here — the service emits those itself, and a second
        emission would double-count the metric.
        """
        details: dict[str, Any] = {
            "scan_exhausted_lanes": list(scan_exhausted_lanes or []),
            "lane_cursors": dict(lane_cursors or {}),
        }
        if offending_circuit is not None:
            details["offending_circuit"] = offending_circuit
        self._emit_replay_blocked(
            log_event="replay_service.circuit_close_chain_stopped",
            log_fields={
                "service_name": service_name,
                "block_reason": block_reason,
                **details,
            },
            event_data={
                "trigger": "circuit_close",
                "service_name": service_name,
                "block_reason": block_reason,
                **details,
            },
            metric_subject=service_name,
            metric_reason=block_reason,
            audit={
                "domain": "dlq",
                "reason": block_reason,
                "service_name": service_name,
                "trigger": "circuit_close",
                "details": details,
            },
        )

    def _fill_circuit_close_lanes(
        self,
        *,
        service_name: str,
        ordered_lanes: list[_Lane],
        max_items: int,
        max_replays: int,
        lane_cursors: dict[str, str],
        deadline: float | None,
    ) -> _LaneSelection:
        """Select this pass's entries, lane by lane, from the carried cursors.

        Two fill rounds. The first hands every lane its `divmod` share, which
        is the per-type fairness rule: a lane with a deep backlog must not
        crowd out the others. The second offers the share the under-filled
        lanes did not use to the lanes that filled theirs exactly — fairness is
        about contention, and a lane whose pool is empty is not contending, so
        leaving the remainder unspent would just park work. With it, a pass
        moves ``min(max_items, reachable)`` entries whatever the lane count.

        The deadline is checked between lanes as well as between replays: four
        lanes each walking their scan bound over a slow store can spend a whole
        pass in selection, and a pass that reaches its wall clock inside a
        selection call ends by being killed rather than by returning.
        """
        selection = _LaneSelection(cursors=dict(lane_cursors))
        quota_base, extra = divmod(max_items, len(ordered_lanes))
        logger.debug(
            "replay_service.quota_allocated",
            service_name=service_name,
            max_items=max_items,
            n_types=len(ordered_lanes),
            quota_base=quota_base,
            extra=extra,
        )

        filled_exactly: list[_Lane] = []
        used = 0
        for i, lane in enumerate(ordered_lanes):
            if deadline is not None and time.monotonic() >= deadline:
                selection.capped = True
                break
            quota = quota_base + (1 if i < extra else 0)
            if quota <= 0:
                break
            taken = self._fill_one_lane(selection, lane, quota, max_replays)
            used += taken
            if taken == quota:
                filled_exactly.append(lane)

        leftover = max_items - used
        for lane in filled_exactly:
            if leftover <= 0:
                break
            if deadline is not None and time.monotonic() >= deadline:
                selection.capped = True
                break
            leftover -= self._fill_one_lane(selection, lane, leftover, max_replays)

        return selection

    def _fill_one_lane(
        self,
        selection: _LaneSelection,
        lane: _Lane,
        quota: int,
        max_replays: int,
    ) -> int:
        """Take up to ``quota`` entries for one lane, recording where it stopped.

        The lane carries its own source filter. The open-circuit lane needs one:
        a domain+type match does not by itself prove the circuit that closed is
        the circuit that rejected, because a request-boundary layer stores the
        same failure type under a path-inferred domain while the dead
        dependency was something else entirely — only a policy-chain capture
        carries the rejecting breaker's own name as its domain. A lane a replay
        handler declares needs none: among capture layers only the policy
        chain's store writes its ``MAX_RETRIES_`` labels, and a long retry
        history can push the ``source`` stamp out of size-capped metadata, so
        filtering on it would skip exactly the entries that retried longest.
        The restriction is part of the selection, so a quota filled with
        entries this lane may not touch is not possible.
        """
        failure_type, lane_domain, lane_source = lane
        key = _lane_key(failure_type, lane_domain)
        page = self.repository.find_replayable_page(
            max_retries=max_replays,
            domain=lane_domain,
            failure_type=failure_type,
            source=lane_source,
            limit=quota,
            cursor=selection.cursors.get(key),
        )
        if page.next_cursor:
            selection.cursors[key] = page.next_cursor
        if page.scan_exhausted and key not in selection.scan_exhausted_lanes:
            selection.scan_exhausted_lanes.append(key)
        if len(page.entries) == quota:
            # A lane that returned exactly its allotment may have left eligible
            # entries behind it. Derived from the fill itself at zero extra
            # query (no eligible-count scan).
            selection.capped = True
        selection.selected.extend((key, entry) for entry in page.entries)
        logger.debug(
            "replay_service.quota_filled",
            failure_type=failure_type,
            quota=quota,
            actual=len(page.entries),
        )
        return len(page.entries)

    @staticmethod
    def _roll_back_lane_cursors(
        selection: _LaneSelection,
        processed: int,
        carried_cursors: dict[str, str],
    ) -> dict[str, str]:
        """Cursors that resume at the oldest entry this pass selected but skipped.

        A lane's selected entries ascend across both fill rounds, so its last
        *processed* entry is also its highest, and the cursor is exclusive —
        the entry immediately after it is exactly the oldest one left behind.
        A lane that selected entries and processed none of them keeps the
        cursor it came in with, which is what the copy below starts from.

        A lane that selected NOTHING is the third case and keeps the position
        its own walk reached. It has no unprocessed tail to protect, and its
        walk is the expensive one: a lane crossing a long prefix of another
        failure type can spend a whole pass examining members it rejects.
        Rolling that back would make every deadline-stopped pass re-cross the
        same prefix from the same place — the permanent starvation the cursor
        exists to end.
        """
        rolled = dict(carried_cursors)
        selected_lanes = {key for key, _ in selection.selected}
        for key, cursor in selection.cursors.items():
            if key not in selected_lanes:
                rolled[key] = cursor
        for key, entry in selection.selected[:processed]:
            if entry.created_at is not None:
                rolled[key] = encode_replay_cursor(entry.created_at, entry.id)
        return rolled

    def _stop_pass_at(
        self,
        batch_result: BatchReplayResult,
        selection: _LaneSelection,
        processed: int,
        carried_cursors: dict[str, str] | None,
        service_name: str,
        *,
        cut_dlq_id: str | None = None,
        breaker_refused: bool = False,
    ) -> None:
        """End a pass early with the unprocessed tail left selectable.

        Two things end a pass before its selection is replayed: its deadline,
        and a replay its own breaker refused before the job began (the
        breaker re-opened, or its half-open slots are taken — the replays
        behind it would be refused the same way).

        Selection completed before any replay did, and acquisition happens per
        entry inside ``_execute_replay`` — so every entry from ``processed`` on
        is still PENDING at a position BELOW the page cursor this pass would
        otherwise carry forward. Each lane rolls back to its last processed
        entry so the next pass re-selects the tail instead of stepping over it.
        ``total`` follows the same correction: the completion event and the
        daily report must not count entries the pass never touched.

        The entry the stop landed on (``cut_dlq_id``) is part of that tail,
        and it is recorded on the result: a deadline cut used one of its
        replay attempts, and a refusal is a reason to pause — so the chain
        that carries the sweep would otherwise read the pass as one that got
        nowhere and stop before the continuation that replays it.
        """
        batch_result.capped = True
        batch_result.total = processed
        batch_result.lane_cursors = self._roll_back_lane_cursors(
            selection, processed, carried_cursors or {}
        )
        batch_result.deadline_cut_dlq_id = cut_dlq_id
        batch_result.ended_by_breaker_refusal = breaker_refused
        stop_fields: dict[str, Any] = {
            "service_name": service_name,
            "processed": processed,
            "selected": len(selection.selected),
        }
        if cut_dlq_id is not None:
            stop_fields["cut_dlq_id"] = cut_dlq_id
        if breaker_refused:
            logger.info("replay_service.circuit_close_breaker_refused", **stop_fields)
        else:
            logger.info("replay_service.circuit_close_deadline_reached", **stop_fields)

    def _execute_replay_within(
        self,
        dlq_id: str,
        deadline: float | None,
        *,
        trigger: ResolutionTrigger | str = ResolutionTrigger.AUTO_REPLAY_CIRCUIT_CLOSE,
        entry: FailedOperationData | None = None,
        trial: bool = False,
    ) -> tuple[ReplayResult, bool]:
        """One conditional replay, bounded by the pass deadline when there is one.

        Returns the result and whether the deadline cut the replay: it failed,
        and either the deadline has passed or work inside it noted that the
        deadline ended it early — a retry with no room left before the deadline,
        a cooldown longer than the time left, an LLM call never started. That
        early end comes before the deadline itself, so the clock alone would
        read it as an ordinary failure.

        The deadline is set as the request-scoped deadline around the replay,
        which every retry stage inside it already reads: their cooldown waits
        and later attempts stop at it, and a wrapped LLM client hands it to the
        SDK as the call's timeout. Without it, one slow replay ran the whole
        pass into the task's soft time limit, where it was killed before
        queueing the rest of the backlog.

        The scope ends at the pass deadline itself. A request-scoped deadline
        keeps a network-latency buffer short of the time it is given, which is
        handed back here: a replay that ran out of its time would otherwise
        return just before the pass deadline, read as an ordinary failure, and
        be escalated to review instead of staying in the backlog as a cut. The
        pass deadline already keeps its own margin to the task's time limit.
        """
        if deadline is None:
            result = self._execute_replay(
                dlq_id,
                replay_type="conditional",
                trigger=trigger,
                entry=entry,
                trial=trial,
            )
            return result, False
        from baldur.scaling.deadline_context import (
            DEFAULT_NETWORK_LATENCY_BUFFER_MS,
            deadline_scope,
        )

        remaining_ms = max(0.0, deadline - time.monotonic()) * 1000.0
        with deadline_scope(remaining_ms + DEFAULT_NETWORK_LATENCY_BUFFER_MS) as stop:
            result = self._execute_replay(
                dlq_id,
                replay_type="conditional",
                trigger=trigger,
                entry=entry,
                trial=trial,
            )
        cut = not result.success and (stop.stopped or time.monotonic() >= deadline)
        return result, cut

    def _end_recovery_with_no_lane(self, service_name: str) -> BatchReplayResult:
        """End a pass whose name has no lane, loudly only if work is left behind.

        With no lane the pass replays nothing whatever is stored. Nothing
        parked under the name is a finished recovery: a DEBUG line. Otherwise
        the parked work stays unreplayed until the operator acts, so the pass
        raises the blocked surface (WARNING log + DLQ_REPLAY_BLOCKED event +
        metric + audit) naming what is missing — a domain identity, or a replay
        handler for the domain in this worker. Mapping the failure type is not
        the remedy: every automatic lane needs a replay handler.
        """
        domain = resolve_stored_domain(service_name)
        parked = self.parked_count_for_recovery(service_name)
        if parked == 0:
            logger.debug(
                "replay_service.circuit_close_replay_skipped",
                service_name=service_name,
                healing_domain=domain,
                reason="nothing_parked",
            )
            return BatchReplayResult()

        if domain == FALLBACK_DOMAIN:
            block_reason = REASON_DOMAIN_NOT_ADDRESSABLE
            remediation = _REMEDIATION_DOMAIN_NOT_ADDRESSABLE
        else:
            block_reason = REASON_NO_REPLAY_HANDLER
            remediation = _REMEDIATION_NO_REPLAY_HANDLER
        details = {
            "healing_domain": domain,
            "pending": parked,
            "remediation": remediation,
        }
        self._emit_replay_blocked(
            log_event="replay_service.circuit_close_replay_blocked",
            log_fields={
                "service_name": service_name,
                "block_reason": block_reason,
                **details,
            },
            event_data={
                "trigger": "circuit_close",
                "service_name": service_name,
                "block_reason": block_reason,
                **details,
            },
            metric_subject=service_name,
            metric_reason=block_reason,
            audit={
                "domain": "dlq",
                "reason": block_reason,
                "service_name": service_name,
                "trigger": "circuit_close",
                "details": details,
            },
        )
        return BatchReplayResult()

    def _replay_on_circuit_close_locked(  # noqa: C901, PLR0912, PLR0915
        self,
        service_name: str,
        max_items: int = 50,
        escalate_failures: bool = True,
        service_failure_type_map: dict[str, list[str]] | None = None,
        *,
        deadline: float | None = None,
        lane_cursors: dict[str, str] | None = None,
        continuation: int = 0,
        trigger: ResolutionTrigger | str = ResolutionTrigger.AUTO_REPLAY_CIRCUIT_CLOSE,
    ) -> BatchReplayResult:
        """Inner sweep body for `replay_on_circuit_close`.

        Extracted so the outer method can wrap this in the inflight lock via a
        single `try/finally` without indenting the whole sweep. The lock is
        the only thing the guard adds.
        """
        # Explicit mapping takes precedence, RuntimeConfig as fallback
        if service_failure_type_map is not None:
            failure_type_map = service_failure_type_map
        else:
            failure_type_map = self._load_failure_type_map()

        lanes = recovery_lanes(resolve_stored_domain(service_name), failure_type_map)
        if not lanes:
            return self._end_recovery_with_no_lane(service_name)

        event_trigger = sweep_event_trigger(trigger)

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
                    "trigger": event_trigger,
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
        # Lanes are filled into one list and replayed in that order, so a
        # deadline landing mid-list always cuts from the tail — and the
        # open-circuit lane is appended last. Rotating the starting index by
        # the pass counter puts every lane at the head within `len(lanes)`
        # passes, using state the caller already carries. The per-type quota
        # split is untouched; only the order it is handed out in moves.
        rotation = continuation % len(lanes)
        ordered_lanes = lanes[rotation:] + lanes[:rotation]

        selection = self._fill_circuit_close_lanes(
            service_name=service_name,
            ordered_lanes=ordered_lanes,
            max_items=max_items,
            max_replays=max_replays,
            lane_cursors=lane_cursors or {},
            deadline=deadline,
        )
        entries: list[FailedOperationData] = [entry for _, entry in selection.selected]

        batch_result = BatchReplayResult(
            total=len(entries),
            results=[],
            capped=selection.capped,
            lane_cursors=selection.cursors,
            scan_exhausted_lanes=selection.scan_exhausted_lanes,
        )
        batch_start = time.monotonic()

        for processed, (_lane_key, entry) in enumerate(selection.selected):
            if deadline is not None and time.monotonic() >= deadline:
                self._stop_pass_at(
                    batch_result, selection, processed, lane_cursors, service_name
                )
                break
            result, cut = self._execute_replay_within(
                entry.id, deadline, trigger=trigger, entry=entry
            )
            if cut:
                # The deadline cut this replay. It goes back to the backlog, not
                # to review: the entry is already PENDING again (below its
                # replay cap — the attempt it used stays used, so a job longer
                # than a whole pass still reaches review at the cap), and it
                # is left unprocessed so the cursor rolls back to just before
                # it and the continuation re-selects it first in its lane.
                self._stop_pass_at(
                    batch_result,
                    selection,
                    processed,
                    lane_cursors,
                    service_name,
                    cut_dlq_id=entry.id,
                )
                break
            if _skip_reason(result) == REASON_BREAKER_REFUSED:
                # The job's own breaker refused the call before it began: the
                # attempt was given back and the entry is PENDING. The replays
                # behind it would be refused the same way, so the pass ends
                # here, like a deadline cut, and the chain pauses before its
                # next pass instead of spending its budget back to back.
                self._stop_pass_at(
                    batch_result,
                    selection,
                    processed,
                    lane_cursors,
                    service_name,
                    cut_dlq_id=entry.id,
                    breaker_refused=True,
                )
                break
            batch_result.results.append(result)

            if result.skipped:
                batch_result.skipped_count += 1
            elif result.success:
                batch_result.success_count += 1
            else:
                batch_result.failed_count += 1

                # Escalate to REQUIRES_REVIEW only a replay whose job ran: a
                # lost acquisition, a skipped result and a job that never
                # began never reach here or are excluded, and an entry whose
                # work may still run is not pending.
                # TODO: Optimize with bulk_update_status if max_items is increased
                # significantly. Note: bulk_update_status itself currently iterates
                # individually — Redis pipeline optimization needed there too.
                if escalate_failures and result.handler_ran:
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
                "trigger": event_trigger,
                "service_name": service_name,
            },
        )

        self._record_sweep_in_daily_report(service_name, batch_result)

        logger.info(
            "replay_service.circuit_close_replay",
            service_name=service_name,
            trigger=event_trigger,
            batch_result=batch_result.total,
            success_count=batch_result.success_count,
            failed_count=batch_result.failed_count,
            escalated_failures=(batch_result.failed_count if escalate_failures else 0),
        )

        return batch_result

    def _record_sweep_in_daily_report(
        self, service_name: str, batch_result: BatchReplayResult
    ) -> None:
        """Push an on-recovery sweep outcome to the daily report collector.

        Emitted only from the circuit-close sweep, never from operator-initiated
        ``replay_batch`` calls, so what this seam contributes to the digest's
        "Auto-replay" line is automatic recoveries only. That is a property of
        this emission site, not of the rendered line: the PRO batch-replay
        emitter feeds the same ``auto_replay_batch`` name from manual console
        batches, so on a PRO install the line also carries operator work.
        Fail-open: a collector failure never breaks the sweep, matching the
        module's observability posture.

        Args:
            service_name: The recovered service whose backlog was swept.
            batch_result: Outcome of the sweep; nothing is recorded when the
                sweep processed no entries.
        """
        if batch_result.total <= 0:
            return

        try:
            from baldur.services.daily_report import get_daily_report_collector

            get_daily_report_collector().add_result(
                task_name="auto_replay_batch",
                result={
                    "recovered_count": batch_result.success_count,
                    "failed_count": batch_result.failed_count,
                    "processed_count": batch_result.total,
                    "success_rate": round(
                        batch_result.success_count / batch_result.total, 4
                    ),
                    "service_name": service_name,
                },
            )
        except Exception as exc:
            logger.warning(
                "replay_service.daily_report_record_failed",
                service_name=service_name,
                error=str(exc),
            )


# =============================================================================
# Module-level convenience functions
# =============================================================================


_replay_service: ReplayService | None = None
_replay_service_lock = fork_safe_lock()


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
