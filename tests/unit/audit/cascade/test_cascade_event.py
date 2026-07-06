"""
Cascade Event 단위 테스트.

Tests:
- CascadeEffect, CascadeTrigger, CascadeEvent 모델 생성 및 직렬화
- ExternalTraceContext 헤더 파싱
- ManualInterventionEffect 수동 개입 효과
- Hash 계산
- CascadeEventAuditor CRUD 및 무결성 검증
"""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from baldur.audit.cascade_auditor import (
    CascadeEventAuditor,
    get_cascade_event_auditor,
    reset_cascade_auditor,
)
from baldur.audit.cascade_event import (
    CascadeEffect,
    CascadeEvent,
    CascadeTrigger,
    ExternalTraceContext,
    InterventionType,
    ManualInterventionEffect,
    generate_cascade_id,
    generate_event_id,
    get_current_timestamp,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def memory_backend():
    """메모리 백엔드 fixture."""
    from baldur.core.state_backend import MemoryStateBackend

    return MemoryStateBackend()


@pytest.fixture
def cascade_auditor(memory_backend):
    """CascadeEventAuditor fixture with memory backend."""
    reset_cascade_auditor()

    auditor = CascadeEventAuditor()

    # Mock _get_backend to return memory backend
    auditor._get_backend = MagicMock(return_value=memory_backend)

    return auditor


@pytest.fixture
def sample_trigger():
    """샘플 트리거 fixture."""
    return CascadeTrigger(
        trigger_type="EMERGENCY_LEVEL_CHANGED",
        event_id="evt-001",
        details={"old_level": "NORMAL", "new_level": "LEVEL_3"},
        triggered_by="system",
    )


@pytest.fixture
def sample_effects():
    """샘플 효과 목록 fixture."""
    return [
        CascadeEffect(
            event_id="evt-002",
            action_type="GOVERNANCE_STRICT",
            caused_by="evt-001",
            success=True,
            details={"mode": "STRICT"},
        ),
        CascadeEffect(
            event_id="evt-003",
            action_type="CANARY_ROLLBACK",
            caused_by="evt-002",
            success=True,
            target="rollout-123",
            details={"rollouts": ["rollout-123"]},
        ),
        CascadeEffect(
            event_id="evt-004",
            action_type="BUDGET_MULTIPLIER",
            caused_by="evt-001",
            success=True,
            details={"multiplier": 5.0},
        ),
    ]


@pytest.fixture
def sample_cascade_event(sample_trigger, sample_effects):
    """샘플 CascadeEvent fixture."""
    return CascadeEvent(
        id="cascade-abc123",
        trigger=sample_trigger,
        effects=sample_effects,
        namespace="seoul",
        timestamp="2026-01-21T15:30:00Z",
    )


# =============================================================================
# CascadeEffect Tests
# =============================================================================


class TestCascadeEffect:
    """CascadeEffect 모델 테스트."""

    def test_create_effect(self):
        """CascadeEffect 생성 테스트."""
        effect = CascadeEffect(
            event_id="evt-001",
            action_type="GOVERNANCE_STRICT",
            caused_by="evt-trigger",
            success=True,
            target="service-a",
            details={"mode": "STRICT"},
        )

        assert effect.event_id == "evt-001"
        assert effect.action_type == "GOVERNANCE_STRICT"
        assert effect.caused_by == "evt-trigger"
        assert effect.success is True
        assert effect.target == "service-a"
        assert effect.details == {"mode": "STRICT"}

    def test_to_dict(self):
        """CascadeEffect 딕셔너리 변환 테스트."""
        effect = CascadeEffect(
            event_id="evt-001",
            action_type="GOVERNANCE_STRICT",
            caused_by="evt-trigger",
            success=True,
            details={"mode": "STRICT"},
            error_message=None,
            executed_at="2026-01-21T15:30:00Z",
        )

        result = effect.to_dict()

        assert result["event_id"] == "evt-001"
        assert result["action_type"] == "GOVERNANCE_STRICT"
        assert result["caused_by"] == "evt-trigger"
        assert result["success"] is True
        assert result["details"] == {"mode": "STRICT"}
        assert result["executed_at"] == "2026-01-21T15:30:00Z"

    def test_from_dict(self):
        """CascadeEffect 딕셔너리에서 생성 테스트."""
        data = {
            "event_id": "evt-001",
            "action_type": "GOVERNANCE_STRICT",
            "caused_by": "evt-trigger",
            "success": True,
            "details": {"mode": "STRICT"},
        }

        effect = CascadeEffect.from_dict(data)

        assert effect.event_id == "evt-001"
        assert effect.action_type == "GOVERNANCE_STRICT"
        assert effect.success is True

    def test_failed_effect(self):
        """실패한 효과 테스트."""
        effect = CascadeEffect(
            event_id="evt-001",
            action_type="CANARY_ROLLBACK",
            caused_by="evt-trigger",
            success=False,
            error_message="Rollback failed: timeout",
        )

        assert effect.success is False
        assert effect.error_message == "Rollback failed: timeout"


# =============================================================================
# CascadeTrigger Tests
# =============================================================================


class TestCascadeTrigger:
    """CascadeTrigger 모델 테스트."""

    def test_create_trigger(self):
        """CascadeTrigger 생성 테스트."""
        trigger = CascadeTrigger(
            trigger_type="EMERGENCY_LEVEL_CHANGED",
            event_id="evt-trigger",
            details={"old_level": "NORMAL", "new_level": "LEVEL_3"},
            triggered_by="system",
        )

        assert trigger.trigger_type == "EMERGENCY_LEVEL_CHANGED"
        assert trigger.event_id == "evt-trigger"
        assert trigger.details["old_level"] == "NORMAL"
        assert trigger.details["new_level"] == "LEVEL_3"
        assert trigger.triggered_by == "system"

    def test_to_dict(self):
        """CascadeTrigger 딕셔너리 변환 테스트."""
        trigger = CascadeTrigger(
            trigger_type="MANUAL_ACTIVATION",
            event_id="evt-001",
            details={"reason": "Emergency test"},
            triggered_by="admin@example.com",
        )

        result = trigger.to_dict()

        assert result["trigger_type"] == "MANUAL_ACTIVATION"
        assert result["event_id"] == "evt-001"
        assert result["triggered_by"] == "admin@example.com"

    def test_from_dict(self):
        """CascadeTrigger 딕셔너리에서 생성 테스트."""
        data = {
            "trigger_type": "EMERGENCY_LEVEL_CHANGED",
            "event_id": "evt-001",
            "details": {"old_level": "NORMAL", "new_level": "LEVEL_2"},
        }

        trigger = CascadeTrigger.from_dict(data)

        assert trigger.trigger_type == "EMERGENCY_LEVEL_CHANGED"
        assert trigger.event_id == "evt-001"


# =============================================================================
# CascadeEvent Tests
# =============================================================================


class TestCascadeEvent:
    """CascadeEvent 모델 테스트."""

    def test_create_event(self, sample_trigger, sample_effects):
        """CascadeEvent 생성 테스트."""
        event = CascadeEvent(
            id="cascade-abc123",
            trigger=sample_trigger,
            effects=sample_effects,
            namespace="seoul",
            timestamp="2026-01-21T15:30:00Z",
        )

        assert event.id == "cascade-abc123"
        assert event.trigger.trigger_type == "EMERGENCY_LEVEL_CHANGED"
        assert len(event.effects) == 3
        assert event.namespace == "seoul"
        assert event.total_effects == 3
        assert event.success_count == 3
        assert event.failure_count == 0

    def test_post_init_counts(self, sample_trigger):
        """__post_init__ 카운트 계산 테스트."""
        effects = [
            CascadeEffect("evt-1", "ACTION_A", "evt-trigger", success=True),
            CascadeEffect("evt-2", "ACTION_B", "evt-1", success=False),
            CascadeEffect("evt-3", "ACTION_C", "evt-2", success=True),
        ]

        event = CascadeEvent(
            id="cascade-test",
            trigger=sample_trigger,
            effects=effects,
            namespace="test",
            timestamp="2026-01-21T15:30:00Z",
        )

        assert event.total_effects == 3
        assert event.success_count == 2
        assert event.failure_count == 1

    def test_get_causation_chain(self, sample_cascade_event):
        """인과관계 체인 반환 테스트."""
        chain = sample_cascade_event.get_causation_chain()

        assert chain[0] == "evt-001"  # Trigger
        assert "evt-002" in chain
        assert "evt-003" in chain
        assert "evt-004" in chain

    def test_calculate_hash(self, sample_cascade_event):
        """해시 계산 테스트."""
        hash1 = sample_cascade_event.calculate_hash()
        hash2 = sample_cascade_event.calculate_hash()

        # 동일한 입력은 동일한 해시
        assert hash1 == hash2
        assert len(hash1) == 64  # SHA-256

        # 다른 입력은 다른 해시
        sample_cascade_event.namespace = "busan"
        hash3 = sample_cascade_event.calculate_hash()
        assert hash1 != hash3

    def test_to_dict(self, sample_cascade_event):
        """딕셔너리 변환 테스트."""
        result = sample_cascade_event.to_dict()

        assert result["id"] == "cascade-abc123"
        assert result["namespace"] == "seoul"
        assert "trigger" in result
        assert "effects" in result
        assert "causation_chain" in result
        assert len(result["effects"]) == 3

    def test_from_dict(self, sample_cascade_event):
        """딕셔너리에서 생성 테스트."""
        data = sample_cascade_event.to_dict()

        restored = CascadeEvent.from_dict(data)

        assert restored.id == sample_cascade_event.id
        assert restored.namespace == sample_cascade_event.namespace
        assert len(restored.effects) == len(sample_cascade_event.effects)

    def test_hash_chain_connection(self, sample_trigger, sample_effects):
        """해시 체인 연결 테스트."""
        event1 = CascadeEvent(
            id="cascade-001",
            trigger=sample_trigger,
            effects=sample_effects[:1],
            namespace="test",
            timestamp="2026-01-21T15:30:00Z",
            previous_hash=None,
        )
        event1.current_hash = event1.calculate_hash()

        event2 = CascadeEvent(
            id="cascade-002",
            trigger=sample_trigger,
            effects=sample_effects[1:2],
            namespace="test",
            timestamp="2026-01-21T15:31:00Z",
            previous_hash=event1.current_hash,
        )
        event2.current_hash = event2.calculate_hash()

        assert event2.previous_hash == event1.current_hash
        assert event2.current_hash != event1.current_hash


# =============================================================================
# ExternalTraceContext Tests
# =============================================================================


class TestExternalTraceContext:
    """ExternalTraceContext 모델 테스트."""

    def test_from_headers_w3c(self):
        """W3C Trace Context 헤더 파싱 테스트."""
        headers = {
            "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        }

        ctx = ExternalTraceContext.from_headers(headers)

        assert ctx.trace_id == "0af7651916cd43dd8448eb211c80319c"
        assert ctx.span_id == "b7ad6b7169203331"
        assert ctx.trace_flags == "01"

    def test_from_headers_aws_xray(self):
        """AWS X-Ray 헤더 파싱 테스트."""
        headers = {
            "x-amzn-trace-id": "Root=1-5f84c7a7-1234567890;Parent=abcd1234;Sampled=1",
        }

        ctx = ExternalTraceContext.from_headers(headers)

        assert (
            ctx.aws_xray_trace_id
            == "Root=1-5f84c7a7-1234567890;Parent=abcd1234;Sampled=1"
        )

    def test_from_headers_custom(self):
        """커스텀 헤더 파싱 테스트."""
        headers = {
            "x-request-id": "req-12345",
            "x-correlation-id": "corr-67890",
        }

        ctx = ExternalTraceContext.from_headers(headers)

        assert ctx.request_id == "req-12345"
        assert ctx.correlation_id == "corr-67890"

    def test_from_headers_baggage(self):
        """Baggage 헤더 파싱 테스트."""
        headers = {
            "baggage": "userId=alice,serverRegion=seoul",
        }

        ctx = ExternalTraceContext.from_headers(headers)

        assert ctx.baggage.get("userId") == "alice"
        assert ctx.baggage.get("serverRegion") == "seoul"

    def test_to_dict(self):
        """딕셔너리 변환 테스트."""
        ctx = ExternalTraceContext(
            trace_id="abc123",
            span_id="def456",
            request_id="req-001",
        )

        result = ctx.to_dict()

        assert result["trace_id"] == "abc123"
        assert result["span_id"] == "def456"
        assert result["request_id"] == "req-001"


# =============================================================================
# ManualInterventionEffect Tests
# =============================================================================


class TestManualInterventionEffect:
    """ManualInterventionEffect 모델 테스트."""

    def test_create_intervention(self):
        """ManualInterventionEffect 생성 테스트."""
        effect = ManualInterventionEffect(
            event_id="evt-intervention",
            action_type="OVERRIDE",
            caused_by="evt-trigger",
            success=True,
            intervention_type=InterventionType.OVERRIDE,
            overridden_decision={"action": "AUTO_ROLLBACK"},
            justification="False positive detected",
            approved_by="admin@example.com",
        )

        assert effect.intervention_type == InterventionType.OVERRIDE
        assert effect.justification == "False positive detected"
        assert effect.approved_by == "admin@example.com"

    def test_to_dict_includes_intervention_fields(self):
        """딕셔너리에 intervention 필드 포함 테스트."""
        effect = ManualInterventionEffect(
            event_id="evt-001",
            action_type="OVERRIDE",
            caused_by="evt-trigger",
            success=True,
            intervention_type=InterventionType.CANCEL,
            justification="Testing purposes",
        )

        result = effect.to_dict()

        assert "intervention_type" in result
        assert result["intervention_type"] == InterventionType.CANCEL
        assert result["justification"] == "Testing purposes"

    def test_intervention_types(self):
        """InterventionType 상수 테스트."""
        assert InterventionType.OVERRIDE == "OVERRIDE"
        assert InterventionType.CANCEL == "CANCEL"
        assert InterventionType.APPROVE == "APPROVE"
        assert InterventionType.REJECT == "REJECT"
        assert InterventionType.ESCALATE == "ESCALATE"
        assert InterventionType.DEESCALATE == "DEESCALATE"


# =============================================================================
# Helper Functions Tests
# =============================================================================


class TestHelperFunctions:
    """헬퍼 함수 테스트."""

    def test_generate_cascade_id(self):
        """Cascade ID 생성 테스트."""
        id1 = generate_cascade_id()
        id2 = generate_cascade_id()

        assert id1.startswith("cascade-")
        assert id2.startswith("cascade-")
        assert id1 != id2

    def test_generate_event_id(self):
        """Event ID 생성 테스트."""
        id1 = generate_event_id()
        id2 = generate_event_id()

        assert id1.startswith("evt-")
        assert id2.startswith("evt-")
        assert id1 != id2

    def test_get_current_timestamp(self):
        """현재 시각 ISO 형식 테스트."""
        ts = get_current_timestamp()

        # ISO 형식 파싱 가능 확인
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert dt.tzinfo is not None


# =============================================================================
# CascadeEventAuditor Tests
# =============================================================================


class TestCascadeEventAuditor:
    """CascadeEventAuditor 테스트."""

    def test_record_cascade_event(self, cascade_auditor):
        """Cascade Event 기록 테스트."""
        event = cascade_auditor.record(
            trigger_type="EMERGENCY_LEVEL_CHANGED",
            trigger_details={"old_level": "NORMAL", "new_level": "LEVEL_3"},
            effects=[
                {"action_type": "GOVERNANCE_STRICT", "success": True},
                {
                    "action_type": "CANARY_ROLLBACK",
                    "success": True,
                    "target": "rollout-123",
                },
            ],
            namespace="seoul",
            triggered_by="system",
        )

        assert event.id.startswith("cascade-")
        assert event.trigger.trigger_type == "EMERGENCY_LEVEL_CHANGED"
        assert len(event.effects) == 2
        assert event.current_hash is not None

    def test_get_cascade_event(self, cascade_auditor):
        """Cascade Event 조회 테스트."""
        # 기록
        recorded = cascade_auditor.record(
            trigger_type="TEST_TRIGGER",
            trigger_details={},
            effects=[{"action_type": "TEST_ACTION", "success": True}],
            namespace="test",
        )

        # 조회
        retrieved = cascade_auditor.get_cascade_event(recorded.id, "test")

        assert retrieved is not None
        assert retrieved.id == recorded.id
        assert retrieved.trigger.trigger_type == "TEST_TRIGGER"

    def test_get_cascade_event_not_found(self, cascade_auditor):
        """존재하지 않는 Cascade Event 조회 테스트."""
        result = cascade_auditor.get_cascade_event("nonexistent", "test")
        assert result is None

    def test_get_recent_events(self, cascade_auditor):
        """최근 이벤트 목록 조회 테스트."""
        # 여러 이벤트 기록
        for i in range(5):
            cascade_auditor.record(
                trigger_type=f"TEST_TRIGGER_{i}",
                trigger_details={"index": i},
                effects=[{"action_type": "TEST", "success": True}],
                namespace="test",
            )

        events = cascade_auditor.get_recent_events("test", limit=10)

        assert len(events) == 5
        # 최신순 정렬 확인
        assert events[0].trigger.trigger_type == "TEST_TRIGGER_4"

    def test_hash_chain_integrity(self, cascade_auditor):
        """해시 체인 무결성 검증 테스트."""
        # 여러 이벤트 기록 (체인 형성)
        for i in range(3):
            cascade_auditor.record(
                trigger_type=f"TEST_TRIGGER_{i}",
                trigger_details={},
                effects=[{"action_type": "TEST", "success": True}],
                namespace="test",
            )

        # 무결성 검증
        result = cascade_auditor.verify_chain_integrity("test")

        assert result["valid"] is True
        assert result["checked"] == 3
        assert len(result["errors"]) == 0

    def test_causation_trace(self, cascade_auditor):
        """인과관계 추적 테스트."""
        event = cascade_auditor.record(
            trigger_type="ROOT_CAUSE",
            trigger_details={},
            effects=[
                {"action_type": "STEP_1", "success": True},
                {"action_type": "STEP_2", "success": True},
                {"action_type": "STEP_3", "success": True},
            ],
            namespace="test",
        )

        # 마지막 효과의 인과관계 추적
        last_effect_id = event.effects[-1].event_id
        trace = cascade_auditor.get_causation_trace(last_effect_id, "test")

        assert len(trace) >= 2  # 최소 트리거 + 효과
        assert trace[0]["caused_by"] is None  # 트리거
        assert trace[-1]["event_id"] == last_effect_id

    def test_record_with_external_trace(self, cascade_auditor):
        """외부 Trace Context 포함 기록 테스트."""
        external_trace = ExternalTraceContext(
            trace_id="abc123",
            span_id="def456",
            request_id="req-001",
        )

        event = cascade_auditor.record(
            trigger_type="EXTERNAL_TRIGGER",
            trigger_details={},
            effects=[{"action_type": "TEST", "success": True}],
            namespace="test",
            external_trace=external_trace,
        )

        assert event.external_trace is not None
        assert event.external_trace.trace_id == "abc123"

    def test_record_with_manual_intervention(self, cascade_auditor):
        """수동 개입 효과 기록 테스트."""
        event = cascade_auditor.record(
            trigger_type="MANUAL_OVERRIDE",
            trigger_details={},
            effects=[
                {
                    "action_type": "OVERRIDE",
                    "success": True,
                    "intervention_type": InterventionType.OVERRIDE,
                    "justification": "False positive",
                    "approved_by": "admin@example.com",
                },
            ],
            namespace="test",
            triggered_by="admin@example.com",
        )

        assert len(event.effects) == 1
        effect = event.effects[0]
        assert isinstance(effect, ManualInterventionEffect)
        assert effect.intervention_type == InterventionType.OVERRIDE
        assert effect.approved_by == "admin@example.com"

    def test_find_by_trigger_event(self, cascade_auditor):
        """트리거 이벤트 ID로 조회 테스트."""
        event = cascade_auditor.record(
            trigger_type="FIND_TEST",
            trigger_details={},
            effects=[{"action_type": "TEST", "success": True}],
            namespace="test",
        )

        trigger_event_id = event.trigger.event_id
        found = cascade_auditor.find_by_trigger_event(trigger_event_id, "test")

        assert found is not None
        assert found.id == event.id

    def test_index_max_size(self, cascade_auditor):
        """인덱스 최대 크기 유지 테스트."""
        # _max_index_size 인스턴스 변수를 작게 설정
        original_max = cascade_auditor._max_index_size
        cascade_auditor._max_index_size = 5

        try:
            # 10개 이벤트 기록
            for i in range(10):
                cascade_auditor.record(
                    trigger_type=f"TEST_{i}",
                    trigger_details={},
                    effects=[{"action_type": "TEST", "success": True}],
                    namespace="test",
                )

            events = cascade_auditor.get_recent_events("test", limit=100)

            # _max_index_size 만큼만 유지
            assert len(events) == 5
        finally:
            cascade_auditor._max_index_size = original_max


class TestCascadeEventAuditorSingleton:
    """CascadeEventAuditor 싱글톤 테스트."""

    def test_get_cascade_event_auditor_singleton(self):
        """싱글톤 패턴 테스트."""
        reset_cascade_auditor()

        auditor1 = get_cascade_event_auditor()
        auditor2 = get_cascade_event_auditor()

        assert auditor1 is auditor2

    def test_reset_cascade_auditor(self):
        """싱글톤 리셋 테스트."""
        auditor1 = get_cascade_event_auditor()
        reset_cascade_auditor()
        auditor2 = get_cascade_event_auditor()

        assert auditor1 is not auditor2
