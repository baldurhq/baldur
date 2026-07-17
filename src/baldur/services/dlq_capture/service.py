# verified-by: test_fallback_to_local_on_redis_failure
"""OSS DLQ capture backing.

``DLQCaptureService`` durably captures a failed operation into the DLQ store
and routes it through the async outbox / local disk fallback. This is the
OSS-tier capture core; the PRO ``DLQService`` inherits it and overlays
lazy-eviction overflow, disk-durable outbox, and throttled replay.

Fallback strategy (zero data loss):
    1. Primary: FailedOperationRepository (Redis / in-memory DI fallback)
    2. Fallback: DiskPersistentBuffer (LMDB) → JSONL file → stderr
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog

from baldur.audit.helpers import (
    log_dlq_force_redrive_audit,
    log_dlq_replay_audit,
    log_dlq_store_audit,
)
from baldur.audit.masking import mask_sensitive_fields
from baldur.core.exceptions import DomainValidationError
from baldur.decorators.domain_tag import get_current_domain
from baldur.metrics.event_handlers import DLQMetricEventHandler
from baldur.metrics.prometheus import get_metrics
from baldur.models.dlq import DLQConfig, DLQEntryResult
from baldur.services.dlq_capture.overflow import (
    enforce_overflow_eviction,
    handle_overflow,
)
from baldur.settings.dlq import get_dlq_settings
from baldur.utils.domain_validation import (
    FALLBACK_DOMAIN,
    validate_and_normalize_domain,
)
from baldur.utils.serialization import fast_dumps_str
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.interfaces.repositories import FailedOperationRepository

logger = structlog.get_logger()

__all__ = [
    "DLQCaptureService",
    "get_dlq_capture_service",
    "reset_dlq_capture_service",
    "resolve_dlq_backing",
    "resolve_dlq_backing_tier",
]


# DLQ fallback path (owned by baldur, non-intrusive to the host system)
DLQ_FALLBACK_PATH = Path("/tmp/baldur_dlq_fallback.jsonl")


def _truncate_field_if_oversize(
    name: str,
    value: dict[str, Any] | None,
    max_bytes: int,
) -> dict[str, Any] | None:
    """Cap forensic field size by JSON-encoded byte length.

    Returns the original value when within the cap (or absent). Oversize
    values are replaced with a marker dict that preserves the original
    size for forensic context and a 200-char str preview. The marker
    shape mirrors the dict-fallback path in the request-audit capture so
    downstream code paths that expect a dict keep working.

    Emits ``dlq.field_truncated`` at WARNING level on truncation —
    forensic data loss must be visible to operators, not buried in DEBUG.
    """
    if not value:
        return value

    try:
        encoded = fast_dumps_str(value).encode("utf-8")
    except (TypeError, ValueError):
        encoded = str(value).encode("utf-8")

    original_size = len(encoded)
    if original_size <= max_bytes:
        return value

    logger.warning(
        "dlq.field_truncated",
        field_name=name,
        original_size=original_size,
        max_bytes=max_bytes,
    )

    return {
        "_truncated": True,
        "original_size": original_size,
        "preview": str(value)[:200],
    }


class DLQCaptureService:
    """OSS DLQ capture backing.

    Captures failed operations into the DLQ store with full forensic context
    (PII masking, size caps, origin-trace linkage), dispatches through the
    async outbox when enabled, and falls back to durable local storage when
    the primary store is unavailable.
    """

    def __init__(
        self,
        config: DLQConfig | None = None,
        repository: FailedOperationRepository | None = None,
    ):
        """Initialize the capture service.

        Args:
            config: Optional configuration; loads from settings if None.
            repository: Optional repository for DI; resolved lazily on first
                store (registry → in-memory fallback) when None.
        """
        self.config = config or DLQConfig.from_settings()
        self._repository = repository

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
                registry_method=ProviderRegistry.get_failed_operation_repo,
                fallback_class=InMemoryFailedOperationRepository,
                service_name=self.__class__.__name__,
            )
        return self._repository

    @property
    def is_enabled(self) -> bool:
        """Check if DLQ is enabled."""
        return self.config.enabled

    # =========================================================================
    # Overflow enforcement seam
    # =========================================================================

    def _enforce_overflow(
        self,
        domain: str,
        failure_type: str,
        error_message: str,
    ) -> DLQEntryResult | None:
        """OSS strategy-faithful synchronous overflow enforcement.

        Returns a failed ``DLQEntryResult`` to reject the store, or ``None`` to
        proceed. ``reject`` rejects at the cap; ``drop_oldest``/``compress_oldest``
        synchronously evict the oldest (via :func:`enforce_overflow_eviction`)
        then accept. Fail-open: any error degrades to accept.

        The PRO overlay overrides this to keep lazy (background) eviction.
        """
        try:
            dlq_settings = get_dlq_settings()
            overflow_result = handle_overflow(self.repository, dlq_settings, domain)
            if not overflow_result.accepted:
                logger.warning(
                    "dlq.store_rejected_overflow",
                    domain=domain,
                    failure_type=failure_type,
                    error_message=error_message[:200] if error_message else "",
                    reason=overflow_result.reason,
                )
                try:
                    DLQMetricEventHandler.on_overflow_rejected(domain)
                except Exception:
                    pass
                return DLQEntryResult.failed(overflow_result.reason)
            if overflow_result.overflow_detected:
                enforce_overflow_eviction(
                    self.repository, dlq_settings, domain, overflow_result
                )
        except Exception as e:
            # Overflow enforcement failure must not block store (fail-open)
            logger.debug(
                "dlq.overflow_check_skipped",
                error=str(e),
            )
        return None

    # =========================================================================
    # Store
    # =========================================================================

    def store_failure(  # noqa: C901, PLR0912, PLR0915
        self,
        domain: str,
        failure_type: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        user_id: int | None = None,
        error_code: str = "",
        error_message: str = "",
        snapshot_data: dict[str, Any] | None = None,
        request_data: dict[str, Any] | None = None,
        response_data: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        next_action_hint: str = "",
        recommended_action: str = "",
        request: Any = None,
        mode: Literal["sync", "async"] | None = None,
        _captured_origin: dict[str, str | None] | None = None,
    ) -> DLQEntryResult:
        """
        Store a failed operation in the DLQ.

        Hybrid logic:
        - request present -> buffer into RequestAuditBuffer (AuditMiddleware flushes in batch)
        - request absent -> call the adapter directly (async contexts such as Celery)

        Args:
            domain: Business domain (payment, point, inventory, webhook, notification)
            failure_type: Specific failure type (e.g., PG_TIMEOUT, AMOUNT_MISMATCH)
            entity_type: Type of related entity (e.g., "order", "payment", "product")
            entity_id: ID of related entity
            user_id: Related User ID
            error_code: Error code from external system
            error_message: Human-readable error message
            snapshot_data: State snapshot for recovery
            request_data: Original request payload
            response_data: External system response
            metadata: Additional debug context
            next_action_hint: Guidance for operators
            recommended_action: Suggested action (replay, manual_check, etc.)
            request: Django HttpRequest object (buffered when present)
            mode: Dispatch mode:
                - ``"sync"`` — execute on calling thread, return real ``dlq_id``.
                - ``"async"`` — enqueue into the outbox, return
                  ``DLQEntryResult(success=True, dlq_id=None)``.
                - ``None`` (default) — resolve against
                  ``BALDUR_DLQ_OUTBOX_ENABLED``.
                - ``"async"`` + ``request != None`` raises ``ValueError``
                  (HttpRequest cannot be safely thread-shared — programmer
                  error, fail-fast).
                - ``None`` + ``request != None`` silently coerces to
                  ``"sync"`` (env-default safe path).
            _captured_origin: Internal. The origin trace context peeked on the
                caller thread, threaded through the outbox so the worker-thread
                re-execution (which has no live trace) links back to the failure.
                Set only by the async dispatch path; None on a direct call, where
                the origin is peeked live. Kept OUT of ``metadata`` so it neither
                counts against the field size cap nor is stripped by truncation.

        Returns:
            DLQEntryResult with creation status
        """

        if not self.is_enabled:
            logger.debug("dlq.store_skipped_disabled")
            return DLQEntryResult.failed("DLQ is disabled")

        # === Domain input validation ===
        # Function-entry insertion: the async dispatch branch and the overflow
        # per-domain bucket both consume ``domain`` before metadata injection,
        # so validation must run before either reads the value. Async worker
        # re-execution (``_dispatch_to_outbox`` → ``store_failure`` round-trip)
        # re-validates idempotently — a lowercase domain that already passed is
        # a no-op pass.
        try:
            domain = validate_and_normalize_domain(domain)
        except DomainValidationError as _dom_err:
            try:
                DLQMetricEventHandler.on_domain_rejected(
                    site="store_failure",
                    reason=_dom_err.reason,
                    original_domain=_dom_err.original_domain,
                )
            except Exception:
                logger.warning(
                    "domain.input_rejected",
                    site="store_failure",
                    reason=getattr(_dom_err.reason, "value", _dom_err.reason),
                    original_preview=str(_dom_err.original_domain)[:32],
                )
            domain = FALLBACK_DOMAIN

        # === Forensic field redaction + size cap ===
        # Redaction MUST run before truncation: an oversize dict containing
        # ``{"password": "abc"}`` would otherwise be replaced with a marker
        # whose 200-char ``preview`` embeds the raw secret string, bypassing
        # the dict-walker masker.
        #
        # Both transforms live inside the same try/except: on RecursionError /
        # cycle errors the ORIGINAL dict passes through to the next step
        # (forensic context preserved). Subsequent size-cap truncation bounds
        # residual exposure to the 200-char preview literal.
        try:
            _size_settings = get_dlq_settings()
            try:
                request_data = mask_sensitive_fields(request_data)
                snapshot_data = mask_sensitive_fields(snapshot_data)
                response_data = mask_sensitive_fields(response_data)
                metadata = mask_sensitive_fields(metadata)
                logger.debug("dlq.masking_applied", field_count=4)
            except Exception as mask_err:
                logger.warning(
                    "dlq.masking_failed_passthrough",
                    error=str(mask_err),
                )
            request_data = _truncate_field_if_oversize(
                "request_data", request_data, _size_settings.request_data_max_bytes
            )
            snapshot_data = _truncate_field_if_oversize(
                "snapshot_data", snapshot_data, _size_settings.field_max_bytes
            )
            response_data = _truncate_field_if_oversize(
                "response_data", response_data, _size_settings.field_max_bytes
            )
            metadata = _truncate_field_if_oversize(
                "metadata", metadata, _size_settings.field_max_bytes
            )
        except Exception as e:
            logger.debug("dlq.size_cap_skipped", error=str(e))

        # === Origin-trace peek ===
        # Peek the failing request's trace ONCE on the caller thread — the only
        # thread with the live trace context. It is NOT injected into
        # ``metadata`` here: on the async path the origin rides
        # ``_captured_origin`` into the outbox (threaded OUTSIDE the size-capped
        # ``metadata``, so it never inflates the field toward the cap nor is
        # stripped by the worker's second truncation), and on the sync store
        # path it is injected below, after truncation. On the outbox worker
        # re-execution the caller-thread origin arrives via ``_captured_origin``
        # (the worker has no live trace). Fail-open: any failure degrades to
        # no-link, never blocks the store.
        _origin: dict[str, str | None] | None
        try:
            if _captured_origin is not None:
                _origin = _captured_origin
            else:
                from baldur.audit.trace import peek_trace_context

                _origin = peek_trace_context()
        except Exception as e:
            logger.debug("dlq.origin_trace_capture_skipped", error=str(e))
            _origin = None

        # === Async dispatch ===
        # Resolve mode against env-default. ``request != None`` requires the
        # sync path (RequestAuditBuffer integration + thread-safety).
        if mode == "async" and request is not None:
            raise ValueError(
                "explicit mode='async' with HttpRequest is unsafe; "
                "use mode='sync' or omit request"
            )
        resolved_mode = mode
        if resolved_mode is None:
            try:
                from baldur.settings.dlq_outbox import get_dlq_outbox_settings

                outbox_enabled = get_dlq_outbox_settings().enabled
            except Exception:
                outbox_enabled = False
            if outbox_enabled and request is None:
                resolved_mode = "async"
            else:
                resolved_mode = "sync"
                if request is not None:
                    logger.debug("dlq.async_coerced_to_sync_for_request")

        if resolved_mode == "async":
            return self._dispatch_to_outbox(
                domain=domain,
                failure_type=failure_type,
                entity_type=entity_type,
                entity_id=entity_id,
                user_id=user_id,
                error_code=error_code,
                error_message=error_message,
                snapshot_data=snapshot_data,
                request_data=request_data,
                response_data=response_data,
                metadata=metadata,
                next_action_hint=next_action_hint,
                recommended_action=recommended_action,
                _captured_origin=_origin,
            )

        # === Origin-trace injection (sync store path) ===
        # Inject the peeked origin into ``metadata`` AFTER truncation and the
        # async branch — reached only on the sync store path (a direct
        # ``mode="sync"`` call OR the outbox worker's re-execution). Because it
        # runs after truncation, a just-under-cap user ``metadata`` is stored
        # intact (never inflated past the cap by the origin keys). ``setdefault``
        # keeps same-capture idempotency (the worker re-exec never overwrites).
        try:
            if _origin and _origin.get("trace_id"):
                metadata = metadata or {}
                metadata.setdefault("origin_trace_id", _origin["trace_id"])
                if _origin.get("trace_id_full"):
                    metadata.setdefault(
                        "origin_trace_id_full", _origin["trace_id_full"]
                    )
                if _origin.get("span_id"):
                    metadata.setdefault("origin_span_id", _origin["span_id"])
        except Exception as e:
            logger.debug("dlq.origin_trace_capture_skipped", error=str(e))

        # === Overflow enforcement seam ===
        # OSS enforces synchronously (reject / evict-then-accept); the PRO
        # overlay overrides this to defer to background eviction.
        _overflow_rejection = self._enforce_overflow(
            domain, failure_type, error_message
        )
        if _overflow_rejection is not None:
            return _overflow_rejection

        # Auto-inject domain context (canary, chaos, etc.)
        try:
            current_domain = get_current_domain()
            if current_domain:
                metadata = metadata or {}
                metadata[f"is_{current_domain}"] = True
        except Exception:
            pass

        # Compute expires_at independently (fail-open: None if settings unavailable)
        _expires_at = None
        try:
            _expires_at = utc_now() + timedelta(hours=get_dlq_settings().expiry_hours)
        except Exception as e:
            logger.warning("dlq.expires_at_computation_failed", error=str(e))

        start = time.monotonic()
        _duration_observed = False
        try:
            failed_op = self.repository.create(
                domain=domain,
                failure_type=failure_type,
                entity_type=entity_type,
                entity_id=entity_id,
                user_id=user_id,
                error_code=error_code,
                error_message=error_message,
                snapshot_data=snapshot_data,
                request_data=request_data,
                response_data=response_data,
                metadata=metadata,
                max_retries=self.config.max_replay_attempts,
                next_action_hint=next_action_hint,
                recommended_action=recommended_action,
                expires_at=_expires_at,
            )

            logger.info(
                "dlq.entry_created",
                failed_op=failed_op.id,
                healing_domain=domain,
                failure_type=failure_type,
            )

            # Push event - increment gauge (uses SafeGauge).
            try:
                duration = time.monotonic() - start
                DLQMetricEventHandler.on_item_created(
                    domain, failure_type, duration_seconds=duration
                )
                _duration_observed = True
            except Exception:
                pass  # Metrics not available

            # Audit logging: DLQ store record (buffer pattern supported)
            self._log_dlq_audit(
                action="store",
                dlq_id=failed_op.id,
                domain=domain,
                failure_type=failure_type,
                error_message=error_message,
                success=True,
                request=request,
            )

            return DLQEntryResult.created(failed_op.id)

        except Exception as e:
            logger.exception(
                "dlq.entry_store_failed",
                error=e,
            )

            # Local fallback: zero data loss guarantee
            entry_data = {
                "domain": domain,
                "failure_type": failure_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "user_id": user_id,
                "error_code": error_code,
                "error_message": error_message,
                "snapshot_data": snapshot_data,
                "request_data": request_data,
                "response_data": response_data,
                "metadata": metadata,
                "next_action_hint": next_action_hint,
                "recommended_action": recommended_action,
            }

            fallback_path = self._write_to_local_fallback(entry_data, str(e))
            if fallback_path:
                logger.warning(
                    "dlq.fallback_local_file_saved",
                    fallback_path=fallback_path,
                )
                return DLQEntryResult.fallback(str(e), fallback_path)

            return DLQEntryResult.failed(str(e))
        finally:
            if not _duration_observed:
                # Defensive duplicate observation preserved per cross-service
                # standards (fail-open metrics).
                try:
                    metrics = get_metrics()
                    if metrics and hasattr(metrics, "dlq"):
                        metrics.dlq.record_store_duration(
                            domain, time.monotonic() - start
                        )
                except Exception:
                    pass

    # =========================================================================
    # Async Outbox Dispatch
    # =========================================================================

    def _dispatch_to_outbox(
        self,
        **kwargs: Any,
    ) -> DLQEntryResult:
        """Enqueue ``store_failure`` kwargs into the DLQ outbox.

        Producer hot path — must complete in lock-bounded RingBuffer ``put``
        time (~50-100 ns + a single ``time.monotonic()`` clock read).
        Returns ``DLQEntryResult(success=True, dlq_id=None)``: callers that
        need the real ``dlq_id`` must opt into ``mode="sync"``.

        Fail-open: when the worker thread has died, coerce the call
        to the synchronous path so no entries are lost while operators
        diagnose the dead worker.
        """
        try:
            from baldur.services.dlq_outbox import outbox as _outbox_module
        except ImportError:
            # Outbox unavailable (very early import order) — fall back to sync.
            return self.store_failure(mode="sync", **kwargs)

        if _outbox_module.is_worker_dead():
            _outbox_module.record_worker_dead_coercion()
            return self.store_failure(mode="sync", **kwargs)

        try:
            outbox = _outbox_module.get_outbox()
        except Exception as e:
            logger.warning("dlq.outbox_unavailable", error=e)
            return self.store_failure(mode="sync", **kwargs)

        outbox.put(kwargs)
        return DLQEntryResult(success=True, dlq_id=None)

    # =========================================================================
    # Local Fallback (Zero Data Loss)
    # =========================================================================

    _fallback_lock = threading.Lock()

    def _write_to_local_fallback(
        self,
        entry_data: dict[str, Any],
        original_error: str,
    ) -> str | None:
        """
        Three-tier fallback chain guaranteeing zero DLQ data loss.

        Tier 1: DiskPersistentBuffer (LMDB) - CRC32 integrity, Group Commit, Pod-restart durability
        Tier 2: JSONL file - when DiskPersistentBuffer is unavailable
        Tier 3: stderr output - minimal record when every fallback fails

        Terminal fail-open: a write failure at any tier is caught and logged,
        never re-raised into the protected call — capture is never a new source
        of exceptions in the user's operation.

        Args:
            entry_data: DLQ entry data
            original_error: The originally raised error

        Returns:
            Stored path (on success), None (on failure)
        """
        # Tier 1: DiskPersistentBuffer (LMDB-based). Import from the adapter's
        # home module — the disk_buffer barrel re-exports it through an untyped
        # lazy __getattr__ (cycle-breaker), which erases the class type.
        try:
            from baldur.audit.persistence.disk_buffer_adapter import (
                DiskBufferAdapter,
            )

            buffer = DiskBufferAdapter.get_instance()
            buffer.add(
                {
                    "category": "dlq_fallback",
                    "timestamp": utc_now().isoformat(),
                    "original_error": original_error,
                    "entry_data": entry_data,
                    "pending_reconciliation": True,
                }
            )
            logger.info(
                "dlq.fallback_disk_buffer_saved",
                entry_data=entry_data.get("domain"),
            )
            # Fallback channel metric (fail-open)
            try:
                from baldur.services.metrics.definitions import (
                    throttle_dlq_fallback_total,
                )

                throttle_dlq_fallback_total.labels(
                    channel="disk_persistent_buffer"
                ).inc()
            except Exception:
                pass
            return "disk_persistent_buffer://dlq_fallback"
        except ImportError:
            logger.debug("dlq.disk_buffer_unavailable")
        except Exception as e:
            logger.warning(
                "dlq.disk_buffer_write_failed",
                error=e,
            )

        # Tier 2: JSONL file (legacy path)
        try:
            with self._fallback_lock:
                DLQ_FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)

                fallback_entry = {
                    "timestamp": utc_now().isoformat(),
                    "original_error": original_error,
                    "entry_data": entry_data,
                    "pending_reconciliation": True,
                }

                with open(DLQ_FALLBACK_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(fallback_entry, default=str) + "\n")
                    f.flush()
                    os.fsync(f.fileno())

                logger.info(
                    "dlq.fallback_jsonl_saved",
                    entry_data=entry_data.get("domain"),
                )
                # Fallback channel metric (fail-open)
                try:
                    from baldur.services.metrics.definitions import (
                        throttle_dlq_fallback_total,
                    )

                    throttle_dlq_fallback_total.labels(channel="jsonl").inc()
                except Exception:
                    pass
                return str(DLQ_FALLBACK_PATH)

        except Exception as fallback_error:
            # Tier 3: stderr output (last resort)
            import sys

            print(
                f"[DLQ CRITICAL] All fallbacks failed. "
                f"DB: {original_error}, JSONL: {fallback_error}. "
                f"Data: {json.dumps(entry_data, default=str)[:500]}",
                file=sys.stderr,
            )
            logger.critical(
                "dlq.fallback_exhausted",
                original_error=original_error,
                fallback_error=fallback_error,
            )
            # Fallback channel metric (fail-open)
            try:
                from baldur.services.metrics.definitions import (
                    throttle_dlq_fallback_total,
                )

                throttle_dlq_fallback_total.labels(channel="stderr").inc()
            except Exception:
                pass
            return None

    # =========================================================================
    # Audit
    # =========================================================================

    def _log_dlq_audit(
        self,
        action: str,
        dlq_id: str,
        domain: str,
        failure_type: str = "",
        error_message: str = "",
        success: bool = True,
        actor_id: str | None = None,
        request: Any = None,
        reason: str = "",
        ticket_url: str | None = None,
        previous_total_retries: int | None = None,
        origin_trace_id: str | None = None,
    ) -> None:
        """
        Record a DLQ operation in the audit log.

        Hybrid logic:
        - request present -> buffer into RequestAuditBuffer (AuditMiddleware flushes in batch)
        - request absent -> call the adapter directly (async contexts such as Celery)

        Audit calls route through the OSS delegating wrappers in
        ``baldur.audit.helpers``: PRO-present each resolves the identical PRO
        function; PRO-absent each no-ops (the audit-trail *persistence* stays a
        PRO-tier feature). All three branches (``store`` / ``replay`` /
        ``force_redrive``) fire on the OSS path — ``store`` from the capture
        core and ``replay`` / ``force_redrive`` from the OSS read/single-entry
        service (``DLQReadService``) — so the OSS tier *dispatches* the distinct
        audit event even though its persistence remains PRO-gated.

        Args:
            action: Operation type (store, replay, force_redrive, etc.)
            dlq_id: DLQ entry ID
            domain: Business domain
            failure_type: Failure type (when storing)
            error_message: Error message
            success: Whether the operation succeeded
            actor_id: Operation actor
            request: Django HttpRequest object (buffered when present)
            reason: Operator justification (force_redrive)
            ticket_url: Optional change/incident ticket reference (force_redrive)
            previous_total_retries: Pre-reset retry budget overridden (force_redrive)
            origin_trace_id: Origin trace id of the failure that created the
                entry, linked into the replay / force_redrive audit details.
                None for pre-existing / no-trace-capture entries.
        """
        try:
            if action == "store":
                log_dlq_store_audit(
                    dlq_id=dlq_id,
                    domain=domain,
                    failure_type=failure_type,
                    error_message=error_message,
                    request=request,  # buffer pattern supported
                )
            elif action == "replay":
                log_dlq_replay_audit(
                    dlq_id=dlq_id,
                    domain=domain,
                    success=success,
                    actor_id=actor_id,
                    error_message=error_message if not success else None,
                    request=request,  # buffer pattern supported
                    reason=reason or None,
                    origin_trace_id=origin_trace_id,
                )
            elif action == "force_redrive":
                log_dlq_force_redrive_audit(
                    dlq_id=dlq_id,
                    domain=domain,
                    actor_id=actor_id,
                    reason=reason,
                    ticket_url=ticket_url,
                    previous_total_retries=previous_total_retries,
                    request=request,  # buffer pattern supported
                    origin_trace_id=origin_trace_id,
                )
        except Exception as e:
            # Audit logging should never break the main flow
            logger.debug(
                "dlq.audit_logging_skipped",
                error=e,
            )


# =============================================================================
# Singleton + resolution chain
# =============================================================================


_capture_service: DLQCaptureService | None = None
_capture_service_lock = threading.Lock()


def get_dlq_capture_service() -> DLQCaptureService:
    """Return the process-singleton OSS DLQ capture backing."""
    global _capture_service
    if _capture_service is None:
        with _capture_service_lock:
            if _capture_service is None:
                _capture_service = DLQCaptureService()
    return _capture_service


def reset_dlq_capture_service() -> None:
    """Reset the singleton OSS DLQ capture backing (test-reset hook)."""
    global _capture_service
    _capture_service = None


def resolve_dlq_backing() -> Any:
    """Resolve the active DLQ capture backing.

    Single resolution chain: the PRO ``DLQService`` (registered under ACTIVE
    entitlement) takes precedence; otherwise the OSS capture backing. Always
    resolves on a functional install (OSS construction is I/O-free).
    """
    from baldur.factory.registry import ProviderRegistry

    service = ProviderRegistry.dlq_service.safe_get()
    if service is not None:
        return service
    return get_dlq_capture_service()


def resolve_dlq_backing_tier() -> str:
    """Return the resolved backing tier: ``"pro"`` or ``"oss"``."""
    from baldur.factory.registry import ProviderRegistry

    return "pro" if ProviderRegistry.dlq_service.safe_get() is not None else "oss"
