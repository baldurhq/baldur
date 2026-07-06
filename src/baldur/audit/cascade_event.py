"""
Cascade Event 모델 - 연계 이벤트 감사 추적.

하나의 트리거로 인해 발생한 모든 연계 액션을 묶어서 기록합니다.

Features:
- 인과관계 추적 (causation chain)
- 위변조 방지 (hash chain)
- 전체 흐름 시각화
- 외부 분산 추적 컨텍스트 (W3C/OpenTelemetry 호환)
- 수동 개입 기록

Usage:
    from baldur.audit.cascade_event import CascadeEvent, CascadeEffect, CascadeTrigger

    trigger = CascadeTrigger(
        trigger_type="EMERGENCY_LEVEL_CHANGED",
        event_id="evt-001",
        details={"old_level": "NORMAL", "new_level": "LEVEL_3"},
    )

    effects = [
        CascadeEffect(
            event_id="evt-002",
            action_type="GOVERNANCE_STRICT",
            caused_by="evt-001",
            success=True,
        ),
    ]

    event = CascadeEvent(
        id="cascade-abc123",
        trigger=trigger,
        effects=effects,
        namespace="seoul",
        timestamp="2026-01-21T15:30:00Z",
    )

Reference:
    docs/baldur/middleware_system/76_CASCADE_EVENT_AUDIT.md
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now

# =============================================================================
# CascadeEventPriority (Phase 5: Load Shedding)
# =============================================================================


class CascadeEventPriority(IntEnum):
    """
    Cascade Event 우선순위.

    Load Shedding 시 우선순위가 낮은 이벤트부터 드롭됩니다.

    Priority Order (높을수록 중요):
        CRITICAL (3): 절대 드롭 불가 - Emergency Level 변경, 수동 개입
        HIGH (2): 가능한 유지 - Canary 롤백, Circuit Breaker 상태 변경
        MEDIUM (1): 버퍼 임계치 초과 시 드롭 - 일반 자동화 액션
        LOW (0): 버퍼 경고 임계치 초과 시 드롭 - 정보성 이벤트

    Code reference:
        services/circuit_breaker/load_shedding.py (priority 패턴)
    """

    LOW = 0
    """정보성 이벤트 - 버퍼 경고 시 드롭."""

    MEDIUM = 1
    """일반 자동화 액션 - 버퍼 임계치 초과 시 드롭."""

    HIGH = 2
    """중요 액션 (Canary 롤백 등) - 가능한 유지."""

    CRITICAL = 3
    """Emergency 상태 변경, 수동 개입 - 절대 드롭 불가."""


# Trigger Type별 기본 우선순위 매핑
TRIGGER_TYPE_PRIORITY: dict[str, CascadeEventPriority] = {
    # CRITICAL: 절대 드롭 불가
    "EMERGENCY_LEVEL_CHANGED": CascadeEventPriority.CRITICAL,
    "MANUAL_INTERVENTION": CascadeEventPriority.CRITICAL,
    "MANUAL_ACTIVATION": CascadeEventPriority.CRITICAL,
    "CIRCUIT_BREAKER_OPENED": CascadeEventPriority.CRITICAL,
    # HIGH: 가능한 유지
    "CANARY_ROLLBACK": CascadeEventPriority.HIGH,
    "GOVERNANCE_MODE_CHANGED": CascadeEventPriority.HIGH,
    "ERROR_BUDGET_EXHAUSTED": CascadeEventPriority.HIGH,
    # MEDIUM: 임계치 초과 시 드롭 가능
    "BUDGET_MULTIPLIER_APPLIED": CascadeEventPriority.MEDIUM,
    "CIRCUIT_BREAKER_HALF_OPENED": CascadeEventPriority.MEDIUM,
    "CIRCUIT_BREAKER_CLOSED": CascadeEventPriority.MEDIUM,
    # LOW: 경고 시 드롭 가능
    "METRICS_UPDATED": CascadeEventPriority.LOW,
    "HEALTH_CHECK": CascadeEventPriority.LOW,
}


def get_priority_for_trigger(trigger_type: str) -> CascadeEventPriority:
    """
    트리거 타입에 대한 우선순위 반환.

    Args:
        trigger_type: 트리거 타입

    Returns:
        우선순위 (매핑되지 않은 경우 MEDIUM)
    """
    return TRIGGER_TYPE_PRIORITY.get(trigger_type, CascadeEventPriority.MEDIUM)


# =============================================================================
# External Trace Context (W3C/OpenTelemetry 호환)
# =============================================================================


@dataclass
class ExternalTraceContext(SerializableMixin):
    """
    외부 분산 추적 컨텍스트.

    W3C Trace Context 및 OpenTelemetry 표준과 호환됩니다.

    네이밍 선택 이유:
    - `external_trace_id`: 기존 tracing.py의 `trace_id` 패턴과 일관성 유지
    - `external_` 접두사: 내부 cascade_id와 명확히 구분
    - 프로젝트 내 TracingConfig.captured_headers와 정렬

    Reference:
    - services/circuit_breaker/tracing.py#L35-52 (captured_headers 패턴)
    - W3C Trace Context: https://www.w3.org/TR/trace-context/
    """

    trace_id: str | None = None
    """W3C traceparent의 trace-id (32 hex characters)."""

    span_id: str | None = None
    """W3C traceparent의 parent-id (16 hex characters)."""

    trace_flags: str | None = None
    """W3C traceparent의 trace-flags (예: "01" = sampled)."""

    baggage: dict[str, str] = field(default_factory=dict)
    """W3C Baggage 헤더 값들."""

    # 벤더별 추가 ID
    aws_xray_trace_id: str | None = None
    """AWS X-Ray trace ID (X-Amzn-Trace-Id)."""

    request_id: str | None = None
    """X-Request-ID 헤더 값."""

    correlation_id: str | None = None
    """X-Correlation-ID 헤더 값."""

    # 표시용 축약 trace_id (UI 표시용)
    trace_id_short: str | None = None
    """축약 trace_id (req-xxx 형식, UI 표시용)."""

    @classmethod
    def from_headers(cls, headers: dict[str, str]) -> ExternalTraceContext:
        """HTTP 헤더에서 추출."""
        ctx = cls()

        # W3C traceparent: 00-{trace_id}-{span_id}-{flags}
        traceparent = headers.get("traceparent", "")
        if traceparent:
            parts = traceparent.split("-")
            if len(parts) >= 4:
                ctx.trace_id = parts[1]
                ctx.span_id = parts[2]
                ctx.trace_flags = parts[3]
                # 축약 trace_id 생성
                ctx.trace_id_short = f"req-{parts[1][:8]}"

        # 기타 헤더
        ctx.aws_xray_trace_id = headers.get("x-amzn-trace-id")
        ctx.request_id = headers.get("x-request-id")
        ctx.correlation_id = headers.get("x-correlation-id")

        # Baggage 처리
        baggage_header = headers.get("baggage", "")
        if baggage_header:
            for item in baggage_header.split(","):
                if "=" in item:
                    key, value = item.strip().split("=", 1)
                    ctx.baggage[key] = value

        return ctx

    @classmethod
    def from_current_otel_context(cls) -> ExternalTraceContext:
        """
        현재 OpenTelemetry span 컨텍스트에서 ExternalTraceContext 생성.

        OTEL이 활성화된 경우 현재 span의 trace_id, span_id를 추출합니다.
        OTEL이 비활성화된 경우 빈 컨텍스트를 반환합니다.
        """
        ctx = cls()

        try:
            from baldur.observability import (
                get_current_span,
                get_current_span_id_from_otel,
                get_current_trace_id_from_otel,
                is_otel_enabled,
            )

            if not is_otel_enabled():
                return ctx

            trace_id = get_current_trace_id_from_otel()
            span_id = get_current_span_id_from_otel()

            if trace_id:
                ctx.trace_id = trace_id
                ctx.trace_id_short = f"req-{trace_id[:8]}"

            if span_id:
                ctx.span_id = span_id

            # trace_flags 추출
            span = get_current_span()
            if span is not None:
                try:
                    span_context = span.get_span_context()
                    if span_context and span_context.is_valid:
                        ctx.trace_flags = format(span_context.trace_flags, "02x")
                except Exception:
                    pass

        except ImportError:
            pass
        except Exception:
            pass

        return ctx


# =============================================================================
# Cascade Effect
# =============================================================================


@dataclass
class CascadeEffect(SerializableMixin):
    """
    연쇄 효과 (Cascade Event 내 개별 액션).

    Cascade Event의 트리거로 인해 발생한 각각의 액션을 나타냅니다.
    """

    event_id: str
    """이벤트 고유 ID."""

    action_type: str
    """액션 유형 (GOVERNANCE_STRICT, CANARY_ROLLBACK, BUDGET_MULTIPLIER 등)."""

    caused_by: str
    """원인 이벤트 ID (인과관계 추적)."""

    success: bool
    """성공 여부."""

    target: str | None = None
    """대상 (롤아웃 ID, 서비스 이름 등)."""

    details: dict[str, Any] = field(default_factory=dict)
    """상세 정보."""

    error_message: str | None = None
    """실패 시 에러 메시지."""

    executed_at: str | None = None
    """실행 시각 (ISO format)."""


# =============================================================================
# Manual Intervention Effect (수동 개입)
# =============================================================================


class InterventionType:
    """수동 개입 유형 상수."""

    OVERRIDE = "OVERRIDE"  # 자동화 결정 덮어쓰기
    CANCEL = "CANCEL"  # 진행 중인 자동화 취소
    APPROVE = "APPROVE"  # 대기 중인 자동화 승인
    REJECT = "REJECT"  # 대기 중인 자동화 거부
    ESCALATE = "ESCALATE"  # 수동 격상
    DEESCALATE = "DEESCALATE"  # 수동 해제


@dataclass
class ManualInterventionEffect(CascadeEffect):
    """
    수동 개입으로 인한 효과.

    시스템의 자동화 결정을 사람이 오버라이드했을 때 기록합니다.

    Code reference:
        services/namespace_emergency/atomic_query.py#L34 (precedence 패턴)
    """

    intervention_type: str = InterventionType.OVERRIDE
    """개입 유형: OVERRIDE, CANCEL, APPROVE, REJECT."""

    overridden_decision: dict[str, Any] | None = None
    """오버라이드된 자동화 결정 정보."""

    justification: str | None = None
    """개입 사유."""

    approved_by: str | None = None
    """승인자 (2인 승인 시)."""

    related_cascade_id: str | None = None
    """관련 Cascade ID (기존 자동화 흐름 참조)."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ManualInterventionEffect:
        """딕셔너리에서 생성."""
        return cls(
            event_id=data["event_id"],
            action_type=data["action_type"],
            caused_by=data["caused_by"],
            success=data["success"],
            target=data.get("target"),
            details=data.get("details", {}),
            error_message=data.get("error_message"),
            executed_at=data.get("executed_at"),
            intervention_type=data.get("intervention_type", InterventionType.OVERRIDE),
            overridden_decision=data.get("overridden_decision"),
            justification=data.get("justification"),
            approved_by=data.get("approved_by"),
            related_cascade_id=data.get("related_cascade_id"),
        )


# =============================================================================
# Cascade Trigger
# =============================================================================


@dataclass
class CascadeTrigger(SerializableMixin):
    """
    연쇄 트리거 (Cascade Event의 시작점).

    Cascade Event를 발생시킨 최초 이벤트 정보를 담습니다.
    """

    trigger_type: str
    """트리거 유형 (EMERGENCY_LEVEL_CHANGED, MANUAL_ACTIVATION 등)."""

    event_id: str
    """트리거 이벤트 ID."""

    details: dict[str, Any] = field(default_factory=dict)
    """트리거 상세 정보."""

    triggered_by: str | None = None
    """트리거한 주체 (user, system)."""


# =============================================================================
# Cascade Event
# =============================================================================


@dataclass
class CascadeEvent(SerializableMixin):
    """
    연쇄 이벤트.

    하나의 트리거로 인해 발생한 모든 연계 액션을 묶어서 기록합니다.

    Features:
    - 인과관계 추적 (causation chain)
    - 위변조 방지 (hash chain)
    - 전체 흐름 시각화

    Example:
        >>> trigger = CascadeTrigger(
        ...     trigger_type="EMERGENCY_LEVEL_CHANGED",
        ...     event_id="evt-001",
        ...     details={"old_level": "NORMAL", "new_level": "LEVEL_3"},
        ... )
        >>> effects = [
        ...     CascadeEffect(
        ...         event_id="evt-002",
        ...         action_type="GOVERNANCE_STRICT",
        ...         caused_by="evt-001",
        ...         success=True,
        ...     ),
        ... ]
        >>> event = CascadeEvent(
        ...     id="cascade-abc123",
        ...     trigger=trigger,
        ...     effects=effects,
        ...     namespace="seoul",
        ...     timestamp="2026-01-21T15:30:00Z",
        ... )
    """

    id: str
    """Cascade Event 고유 ID."""

    trigger: CascadeTrigger
    """트리거 정보."""

    effects: list[CascadeEffect]
    """연쇄 효과 목록."""

    namespace: str
    """네임스페이스."""

    timestamp: str
    """생성 시각 (ISO format)."""

    # Hash Chain
    previous_hash: str | None = None
    """이전 CascadeEvent의 해시."""

    current_hash: str | None = None
    """현재 CascadeEvent의 해시."""

    # 외부 분산 추적 컨텍스트 (W3C/OpenTelemetry 호환)
    external_trace: ExternalTraceContext | None = None
    """외부 시스템 Trace Context."""

    # 메타데이터
    version: str = "1.0"
    """스키마 버전."""

    is_test: bool = False
    """테스트 환경 이벤트 여부 (X-Test-Mode에서 생성 시 True)."""

    total_effects: int = field(default=0, init=False)
    """총 효과 수."""

    success_count: int = field(default=0, init=False)
    """성공한 효과 수."""

    failure_count: int = field(default=0, init=False)
    """실패한 효과 수."""

    def __post_init__(self) -> None:
        """초기화 후처리."""
        self.total_effects = len(self.effects)
        self.success_count = sum(1 for e in self.effects if e.success)
        self.failure_count = self.total_effects - self.success_count

    def get_causation_chain(self) -> list[str]:
        """인과관계 체인 반환."""
        chain = [self.trigger.event_id]
        for effect in self.effects:
            if effect.event_id not in chain:
                chain.append(effect.event_id)
        return chain

    def calculate_hash(self) -> str:
        """
        현재 이벤트의 해시 계산.

        위변조 방지를 위한 SHA-256 해시를 생성합니다.
        """
        from baldur.utils.serialization import fast_canonical_dumps

        content = {
            "id": self.id,
            "trigger": self.trigger.to_dict(),
            "effects": [e.to_dict() for e in self.effects],
            "namespace": self.namespace,
            "timestamp": self.timestamp,
            "previous_hash": self.previous_hash,
        }
        return hashlib.sha256(fast_canonical_dumps(content)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리 변환."""
        result = {
            "id": self.id,
            "trigger": self.trigger.to_dict(),
            "effects": [e.to_dict() for e in self.effects],
            "causation_chain": self.get_causation_chain(),
            "namespace": self.namespace,
            "timestamp": self.timestamp,
            "previous_hash": self.previous_hash,
            "current_hash": self.current_hash,
            "version": self.version,
            "is_test": self.is_test,
            "total_effects": self.total_effects,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
        }

        if self.external_trace:
            result["external_trace"] = self.external_trace.to_dict()

        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CascadeEvent:
        """딕셔너리에서 생성."""
        trigger = CascadeTrigger.from_dict(data["trigger"])

        effects: list[CascadeEffect] = []
        for e in data.get("effects", []):
            # ManualInterventionEffect 여부 확인
            if "intervention_type" in e:
                effects.append(ManualInterventionEffect.from_dict(e))
            else:
                effects.append(CascadeEffect.from_dict(e))

        external_trace = None
        if "external_trace" in data and data["external_trace"]:
            external_trace = ExternalTraceContext.from_dict(data["external_trace"])

        return cls(
            id=data["id"],
            trigger=trigger,
            effects=effects,
            namespace=data["namespace"],
            timestamp=data["timestamp"],
            previous_hash=data.get("previous_hash"),
            current_hash=data.get("current_hash"),
            external_trace=external_trace,
            version=data.get("version", "1.0"),
            is_test=data.get("is_test", False),
        )


# =============================================================================
# Helper Functions
# =============================================================================


def generate_cascade_id() -> str:
    """Cascade Event ID 생성."""
    return f"cascade-{uuid.uuid4().hex[:12]}"


def generate_event_id() -> str:
    """이벤트 ID 생성."""
    return f"evt-{uuid.uuid4().hex[:8]}"


def get_current_timestamp() -> str:
    """현재 시각 ISO 형식 반환."""
    return utc_now().isoformat()
