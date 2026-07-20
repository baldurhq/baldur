"""
Cascade Auditor - event recording module.

Responsible for Cascade Event creation and storage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.cascade_event import (
    CascadeEffect,
    CascadeEvent,
    CascadeTrigger,
    ExternalTraceContext,
    ManualInterventionEffect,
    generate_cascade_id,
    generate_event_id,
    get_current_timestamp,
)
from baldur.core.test_mode_context import TestModeContext

if TYPE_CHECKING:
    import threading

logger = structlog.get_logger()


class RecordingMixin:
    """Cascade Event recording methods."""

    if TYPE_CHECKING:
        # Host contract — attributes/methods provided by CascadeEventAuditor
        # and sibling mixins (WALRecoveryMixin for _save_to_local_fallback).
        _lock: threading.RLock

        def _get_last_hash(self, namespace: str) -> str | None: ...
        def _save_cascade_event(self, event: Any) -> None: ...
        def _update_last_hash(self, namespace: str, hash_value: str) -> None: ...
        def _add_to_index(self, namespace: str, cascade_id: str) -> None: ...
        def _save_to_local_fallback(self, event: Any) -> None: ...

    def record(
        self,
        trigger_type: str,
        trigger_details: dict[str, Any],
        effects: list[dict[str, Any]],
        namespace: str,
        triggered_by: str | None = None,
        external_trace: ExternalTraceContext | None = None,
    ) -> CascadeEvent:
        """
        Record a Cascade Event.

        Args:
            trigger_type: Trigger type (EMERGENCY_LEVEL_CHANGED,
                MANUAL_ACTIVATION, etc.)
            trigger_details: Trigger details
            effects: List of cascading effects (each item carries action_type,
                success, etc.)
            namespace: Namespace
            triggered_by: Triggering actor (user, system)
            external_trace: External distributed trace context (optional)

        Returns:
            The created CascadeEvent

        Note:
            Phase 5 Fail-Soft: falls back to local storage on Redis failure
        """
        with self._lock:
            # 1. Generate IDs
            cascade_id = generate_cascade_id()
            trigger_event_id = generate_event_id()
            now = get_current_timestamp()

            # 2. Build the trigger
            trigger = CascadeTrigger(
                trigger_type=trigger_type,
                event_id=trigger_event_id,
                details=trigger_details,
                triggered_by=triggered_by,
            )

            # 3. Build the effects
            cascade_effects = _create_effects(effects, trigger_event_id, now)

            # 4. Look up the previous hash
            previous_hash = self._get_last_hash(namespace)

            # 5. Build the Cascade Event
            cascade_event = CascadeEvent(
                id=cascade_id,
                trigger=trigger,
                effects=cascade_effects,
                namespace=namespace,
                timestamp=now,
                previous_hash=previous_hash,
                external_trace=external_trace,
                is_test=TestModeContext.is_synthetic(),
            )

            # 6. Compute and set the hash
            cascade_event.current_hash = cascade_event.calculate_hash()

            # 7. Store (Fail-Soft: local fallback on Redis failure)
            try:
                self._save_cascade_event(cascade_event)
                self._update_last_hash(namespace, cascade_event.current_hash)
                self._add_to_index(namespace, cascade_id)
            except Exception as e:
                logger.warning(
                    "cascade_audit.redis_save_failed_using",
                    error=e,
                )
                self._save_to_local_fallback(cascade_event)

            logger.info(
                "cascade_audit.recorded",
                cascade_id=cascade_id,
                trigger_type=trigger_type,
                cascade_effects_count=len(cascade_effects),
                namespace=namespace,
            )

            return cascade_event

    def record_with_external_trace(
        self,
        trigger_type: str,
        trigger_details: dict[str, Any],
        effects: list[dict[str, Any]],
        namespace: str,
        request: Any | None = None,
        triggered_by: str | None = None,
    ) -> CascadeEvent:
        """
        Record a Cascade Event including the external Trace Context.

        Extracts the W3C Trace Context from a Django HttpRequest.
        """
        external_trace = None
        if request:
            headers = {}
            meta = getattr(request, "META", {})

            # Strip the HTTP_ prefix and lowercase
            header_mappings = {
                "HTTP_TRACEPARENT": "traceparent",
                "HTTP_TRACESTATE": "tracestate",
                "HTTP_BAGGAGE": "baggage",
                "HTTP_X_AMZN_TRACE_ID": "x-amzn-trace-id",
                "HTTP_X_REQUEST_ID": "x-request-id",
                "HTTP_X_CORRELATION_ID": "x-correlation-id",
            }

            for meta_key, header_key in header_mappings.items():
                if meta_key in meta:
                    headers[header_key] = meta[meta_key]

            if headers:
                external_trace = ExternalTraceContext.from_headers(headers)

        return self.record(
            trigger_type=trigger_type,
            trigger_details=trigger_details,
            effects=effects,
            namespace=namespace,
            triggered_by=triggered_by,
            external_trace=external_trace,
        )


def _create_effects(
    effects_data: list[dict[str, Any]],
    trigger_event_id: str,
    timestamp: str,
) -> list[CascadeEffect]:
    """
    Build the effect list.

    If an effect's caused_by is not specified, the previous event ID is used.
    """
    cascade_effects: list[CascadeEffect] = []
    previous_event_id = trigger_event_id

    for effect_data in effects_data:
        effect_event_id = generate_event_id()

        # Check whether this is a ManualInterventionEffect
        intervention_type = effect_data.get("intervention_type")
        effect: CascadeEffect
        if intervention_type:
            effect = ManualInterventionEffect(
                event_id=effect_event_id,
                action_type=effect_data.get("action_type", "UNKNOWN"),
                caused_by=effect_data.get("caused_by", previous_event_id),
                success=effect_data.get("success", True),
                target=effect_data.get("target"),
                details=effect_data.get("details", {}),
                error_message=effect_data.get("error_message"),
                executed_at=timestamp,
                intervention_type=intervention_type,
                overridden_decision=effect_data.get("overridden_decision"),
                justification=effect_data.get("justification"),
                approved_by=effect_data.get("approved_by"),
                related_cascade_id=effect_data.get("related_cascade_id"),
            )
        else:
            effect = CascadeEffect(
                event_id=effect_event_id,
                action_type=effect_data.get("action_type", "UNKNOWN"),
                caused_by=effect_data.get("caused_by", previous_event_id),
                success=effect_data.get("success", True),
                target=effect_data.get("target"),
                details=effect_data.get("details", {}),
                error_message=effect_data.get("error_message"),
                executed_at=timestamp,
            )

        cascade_effects.append(effect)
        previous_event_id = effect_event_id

    return cascade_effects
