"""Forensic context audit recorder.

Routes captured forensic context dicts to the audit log so post-incident
investigations can recover full task/order/payment/user state at the point
of failure.

Replaces the historical ``services/forensic_audit_bridge.py`` module which
targeted a non-canonical ``audit_adapter.log_event(...)`` API that does not
exist on either ``AuditLogAdapter`` or ``ContinuousAuditRecorder``. This
module instead constructs an ``AuditEntry`` and calls
``AuditLogAdapter.log()`` — the canonical contract.

Usage (called by ``adapters/celery/integrations/forensic_capture.py``):

    from baldur.audit.forensic_recorder import record_forensic_capture

    record_forensic_capture(
        exception=exc,
        stack_trace=einfo.traceback,
        context=captured_dict,
        target_type="celery_task",
        target_id=task_id,
    )

Compliance notes:
    - PCI-DSS 10.2.4 and ISO 27001 A.12.4.1 require traceable evidence of
      what the system was doing at the time of an exceptional event. The
      recorder masks sensitive fields via the canonical
      ``baldur.audit.masking.mask_sensitive_fields`` before writing.
    - The recorder is fail-open: an audit write failure logs a warning but
      never propagates back to the caller, so forensic capture can never
      destabilise the failure path it is observing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from baldur.interfaces.audit_adapter import AuditLogAdapter

logger = structlog.get_logger()

__all__ = ["record_forensic_capture"]


def record_forensic_capture(
    *,
    exception: BaseException,
    stack_trace: str,
    context: dict[str, Any] | None,
    target_type: str,
    target_id: str,
    audit_adapter: AuditLogAdapter | None = None,
) -> bool:
    """Persist a forensic context capture to the audit log.

    Args:
        exception: The captured exception.
        stack_trace: Formatted stack trace string (used for depth metric).
        context: Captured forensic context dict from
            ``capture_forensic_context()``. May be ``None`` if collection
            failed; in that case an entry is still emitted with empty
            context so the failure event itself remains traceable.
        target_type: Audit target classifier, e.g. ``"celery_task"``,
            ``"django_request"``.
        target_id: Identifier of the target (e.g. Celery task id).
        audit_adapter: Optional explicit adapter; if omitted, resolved via
            the canonical ``get_audit_adapter()`` singleton.

    Returns:
        ``True`` on successful audit emission. ``False`` when the recorder
        is disabled via ``ForensicSettings.audit_enabled``, when no
        adapter is available, or when the audit write failed (fail-open).
    """
    try:
        from baldur.settings.forensic import ForensicSettings
        from baldur.settings.layered_provider import get_layered_settings

        # Layered read so an operator's console edit of the forensic domain
        # (audit_enabled / error_message_max_length) takes effect (686 D1/D5);
        # get_layered_settings falls back to the env base when no manager is
        # registered, so the OSS-absent path is identical to the prior read.
        settings = get_layered_settings(ForensicSettings, "forensic")
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("forensic_recorder.settings_unavailable", error=str(exc))
        return False

    if not settings.audit_enabled:
        return False

    adapter = audit_adapter if audit_adapter is not None else _resolve_audit_adapter()
    if adapter is None:
        logger.debug("forensic_recorder.no_audit_adapter")
        return False

    try:
        from baldur.audit.masking import mask_sensitive_fields
        from baldur.interfaces.audit_adapter import AuditAction, AuditEntry
    except ImportError as exc:  # pragma: no cover — defensive
        logger.debug("forensic_recorder.audit_module_unavailable", error=str(exc))
        return False

    masked_context = mask_sensitive_fields(
        context or {}, list(settings.sensitive_field_patterns)
    )
    error_message = str(exception)[: settings.error_message_max_length]
    stack_depth = len(stack_trace.split("\n")) if stack_trace else 0

    entry = AuditEntry(
        action=AuditAction.FORENSIC_CAPTURE_COMPLETED,
        target_type=target_type,
        target_id=target_id,
        success=False,
        error_message=error_message,
        details={
            "exception_type": type(exception).__name__,
            "stack_depth": stack_depth,
            "context": masked_context,
        },
    )

    try:
        adapter.log(entry)
        return True
    except Exception as exc:
        from baldur.metrics.audit_emit_metrics import record_audit_emit_dropped

        record_audit_emit_dropped("forensic_recorder")
        logger.warning(
            "forensic_recorder.audit_write_failed",
            error=str(exc),
            target_type=target_type,
            target_id=target_id,
        )
        return False


def _resolve_audit_adapter() -> AuditLogAdapter | None:
    """Lazy-resolve the canonical audit adapter singleton."""
    try:
        from baldur.adapters.audit.singleton import get_audit_adapter

        return get_audit_adapter()
    except (ImportError, AttributeError) as exc:
        logger.debug("forensic_recorder.adapter_resolution_failed", error=str(exc))
        return None
