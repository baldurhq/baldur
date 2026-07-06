"""
Cascade Auditor - 이벤트 기록 모듈.

Cascade Event 생성/저장 책임을 담당합니다.
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
    """Cascade Event 기록 관련 메서드."""

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
        Cascade Event 기록.

        Args:
            trigger_type: 트리거 유형 (EMERGENCY_LEVEL_CHANGED, MANUAL_ACTIVATION 등)
            trigger_details: 트리거 상세 정보
            effects: 연쇄 효과 목록 (각 항목은 action_type, success 등 포함)
            namespace: 네임스페이스
            triggered_by: 트리거 주체 (user, system)
            external_trace: 외부 분산 추적 컨텍스트 (선택)

        Returns:
            생성된 CascadeEvent

        Note:
            Phase 5 Fail-Soft: Redis 장애 시 로컬 폴백으로 저장
        """
        with self._lock:
            # 1. ID 생성
            cascade_id = generate_cascade_id()
            trigger_event_id = generate_event_id()
            now = get_current_timestamp()

            # 2. 트리거 생성
            trigger = CascadeTrigger(
                trigger_type=trigger_type,
                event_id=trigger_event_id,
                details=trigger_details,
                triggered_by=triggered_by,
            )

            # 3. 효과 생성
            cascade_effects = _create_effects(effects, trigger_event_id, now)

            # 4. 이전 해시 조회
            previous_hash = self._get_last_hash(namespace)

            # 5. Cascade Event 생성
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

            # 6. 해시 계산 및 설정
            cascade_event.current_hash = cascade_event.calculate_hash()

            # 7. 저장 (Fail-Soft: Redis 실패 시 로컬 폴백)
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
        외부 Trace Context를 포함하여 Cascade Event 기록.

        Django HttpRequest에서 W3C Trace Context를 추출합니다.
        """
        external_trace = None
        if request:
            headers = {}
            meta = getattr(request, "META", {})

            # HTTP_ 접두사를 제거하고 소문자로 변환
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
    효과 목록 생성.

    각 효과의 caused_by가 명시되지 않으면 이전 이벤트 ID를 사용합니다.
    """
    cascade_effects: list[CascadeEffect] = []
    previous_event_id = trigger_event_id

    for effect_data in effects_data:
        effect_event_id = generate_event_id()

        # ManualInterventionEffect 여부 확인
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
