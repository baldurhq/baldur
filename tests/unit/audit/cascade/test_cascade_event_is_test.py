"""
CascadeEvent is_test 필드 단위 테스트.

Tests:
- Dataclass is_test 필드 존재 및 기본값
- to_dict()에 is_test 포함
- from_dict()에서 is_test 파싱
- TestModeContext 연동으로 is_test 자동 설정
- Django Model is_test 필드 (Abstract Model)
"""

from unittest.mock import MagicMock

import pytest

from baldur.audit.cascade_event import (
    CascadeEffect,
    CascadeEvent,
    CascadeTrigger,
)
from baldur.core.test_mode_context import TestModeContext

# =============================================================================
# Fixtures
# =============================================================================


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
# CascadeEvent is_test 필드 테스트
# =============================================================================


class TestCascadeEventIsTestField:
    """CascadeEvent.is_test 필드 테스트."""

    def test_dataclass_is_test_field_exists(self, sample_trigger, sample_effects):
        """Dataclass에 is_test 필드가 존재하는지 확인."""
        event = CascadeEvent(
            id="cascade-test-001",
            trigger=sample_trigger,
            effects=sample_effects,
            namespace="seoul",
            timestamp="2026-01-21T15:30:00Z",
        )

        assert hasattr(event, "is_test")

    def test_default_is_test_false(self, sample_trigger, sample_effects):
        """is_test 필드의 기본값이 False인지 확인."""
        event = CascadeEvent(
            id="cascade-test-002",
            trigger=sample_trigger,
            effects=sample_effects,
            namespace="seoul",
            timestamp="2026-01-21T15:30:00Z",
        )

        assert event.is_test is False

    def test_is_test_can_be_set_true(self, sample_trigger, sample_effects):
        """is_test 필드를 True로 설정할 수 있는지 확인."""
        event = CascadeEvent(
            id="cascade-test-003",
            trigger=sample_trigger,
            effects=sample_effects,
            namespace="seoul",
            timestamp="2026-01-21T15:30:00Z",
            is_test=True,
        )

        assert event.is_test is True

    def test_to_dict_includes_is_test(self, sample_trigger, sample_effects):
        """to_dict()에 is_test가 포함되는지 확인."""
        event = CascadeEvent(
            id="cascade-test-004",
            trigger=sample_trigger,
            effects=sample_effects,
            namespace="seoul",
            timestamp="2026-01-21T15:30:00Z",
            is_test=True,
        )

        result = event.to_dict()

        assert "is_test" in result
        assert result["is_test"] is True

    def test_to_dict_is_test_false(self, sample_trigger, sample_effects):
        """to_dict()에서 is_test=False도 정상 포함되는지 확인."""
        event = CascadeEvent(
            id="cascade-test-005",
            trigger=sample_trigger,
            effects=sample_effects,
            namespace="seoul",
            timestamp="2026-01-21T15:30:00Z",
            is_test=False,
        )

        result = event.to_dict()

        assert "is_test" in result
        assert result["is_test"] is False

    def test_from_dict_parses_is_test_true(self, sample_trigger, sample_effects):
        """from_dict()에서 is_test=True를 파싱하는지 확인."""
        data = {
            "id": "cascade-test-006",
            "trigger": sample_trigger.to_dict(),
            "effects": [e.to_dict() for e in sample_effects],
            "namespace": "seoul",
            "timestamp": "2026-01-21T15:30:00Z",
            "is_test": True,
        }

        event = CascadeEvent.from_dict(data)

        assert event.is_test is True

    def test_from_dict_parses_is_test_false(self, sample_trigger, sample_effects):
        """from_dict()에서 is_test=False를 파싱하는지 확인."""
        data = {
            "id": "cascade-test-007",
            "trigger": sample_trigger.to_dict(),
            "effects": [e.to_dict() for e in sample_effects],
            "namespace": "seoul",
            "timestamp": "2026-01-21T15:30:00Z",
            "is_test": False,
        }

        event = CascadeEvent.from_dict(data)

        assert event.is_test is False

    def test_from_dict_defaults_is_test_false_when_missing(
        self, sample_trigger, sample_effects
    ):
        """from_dict()에서 is_test가 없을 때 기본값 False인지 확인."""
        data = {
            "id": "cascade-test-008",
            "trigger": sample_trigger.to_dict(),
            "effects": [e.to_dict() for e in sample_effects],
            "namespace": "seoul",
            "timestamp": "2026-01-21T15:30:00Z",
            # is_test 필드 없음
        }

        event = CascadeEvent.from_dict(data)

        assert event.is_test is False

    def test_roundtrip_is_test_true(self, sample_trigger, sample_effects):
        """is_test=True 직렬화/역직렬화 왕복 테스트."""
        original = CascadeEvent(
            id="cascade-test-009",
            trigger=sample_trigger,
            effects=sample_effects,
            namespace="seoul",
            timestamp="2026-01-21T15:30:00Z",
            is_test=True,
        )

        # 직렬화 → 역직렬화
        data = original.to_dict()
        restored = CascadeEvent.from_dict(data)

        assert restored.is_test is True
        assert restored.is_test == original.is_test

    def test_roundtrip_is_test_false(self, sample_trigger, sample_effects):
        """is_test=False 직렬화/역직렬화 왕복 테스트."""
        original = CascadeEvent(
            id="cascade-test-010",
            trigger=sample_trigger,
            effects=sample_effects,
            namespace="seoul",
            timestamp="2026-01-21T15:30:00Z",
            is_test=False,
        )

        # 직렬화 → 역직렬화
        data = original.to_dict()
        restored = CascadeEvent.from_dict(data)

        assert restored.is_test is False
        assert restored.is_test == original.is_test


# =============================================================================
# TestModeContext 연동 테스트
# =============================================================================


class TestCascadeEventTestModeContextIntegration:
    """CascadeEvent와 TestModeContext 연동 테스트."""

    def test_test_mode_context_is_synthetic_default_false(self):
        """TestModeContext.is_synthetic() 기본값이 False인지 확인."""
        # 컨텍스트 외부에서는 False
        assert TestModeContext.is_synthetic() is False

    def test_test_mode_context_is_synthetic_true_in_context(self):
        """TestModeContext 내에서 is_synthetic()이 True인지 확인."""
        with TestModeContext.start(session_id="test-session-001"):
            assert TestModeContext.is_synthetic() is True

        # 컨텍스트 외부에서는 다시 False
        assert TestModeContext.is_synthetic() is False

    def test_cascade_event_with_test_mode_context(self, sample_trigger, sample_effects):
        """TestModeContext 내에서 CascadeEvent 생성 시 is_test=True 설정 가능."""
        with TestModeContext.start(session_id="test-session-002"):
            is_synthetic = TestModeContext.is_synthetic()

            event = CascadeEvent(
                id="cascade-synthetic-001",
                trigger=sample_trigger,
                effects=sample_effects,
                namespace="seoul",
                timestamp="2026-01-21T15:30:00Z",
                is_test=is_synthetic,
            )

            assert event.is_test is True

    def test_cascade_event_outside_test_mode_context(
        self, sample_trigger, sample_effects
    ):
        """TestModeContext 외부에서 CascadeEvent 생성 시 is_test=False."""
        is_synthetic = TestModeContext.is_synthetic()

        event = CascadeEvent(
            id="cascade-production-001",
            trigger=sample_trigger,
            effects=sample_effects,
            namespace="seoul",
            timestamp="2026-01-21T15:30:00Z",
            is_test=is_synthetic,
        )

        assert event.is_test is False


# =============================================================================
# CascadeEventAuditor is_test 연동 테스트
# =============================================================================


class TestCascadeAuditorIsTestIntegration:
    """CascadeEventAuditor에서 is_test 자동 설정 테스트."""

    @pytest.fixture
    def memory_backend(self):
        """메모리 백엔드 fixture."""
        from baldur.core.state_backend import MemoryStateBackend

        return MemoryStateBackend()

    @pytest.fixture
    def cascade_auditor(self, memory_backend):
        """CascadeEventAuditor fixture with memory backend."""
        from baldur.audit.cascade_auditor import (
            CascadeEventAuditor,
            reset_cascade_auditor,
        )

        reset_cascade_auditor()

        auditor = CascadeEventAuditor()
        auditor._get_backend = MagicMock(return_value=memory_backend)

        return auditor

    def test_auditor_record_sets_is_test_false_by_default(self, cascade_auditor):
        """기본 상황에서 record() 호출 시 is_test=False."""
        event = cascade_auditor.record(
            trigger_type="EMERGENCY_LEVEL_CHANGED",
            trigger_details={"old_level": "NORMAL", "new_level": "LEVEL_3"},
            effects=[{"action_type": "GOVERNANCE_STRICT", "success": True}],
            namespace="seoul",
            triggered_by="system",
        )

        assert event.is_test is False

    def test_auditor_record_sets_is_test_true_in_test_mode(self, cascade_auditor):
        """TestModeContext 내에서 record() 호출 시 is_test=True."""
        with TestModeContext.start(session_id="xtest-session-001"):
            event = cascade_auditor.record(
                trigger_type="EMERGENCY_LEVEL_CHANGED",
                trigger_details={"old_level": "NORMAL", "new_level": "LEVEL_3"},
                effects=[{"action_type": "GOVERNANCE_STRICT", "success": True}],
                namespace="seoul",
                triggered_by="system",
            )

            assert event.is_test is True

    def test_auditor_record_is_test_persists_in_dict(self, cascade_auditor):
        """record()로 생성된 이벤트의 to_dict()에 is_test 포함 확인."""
        with TestModeContext.start(session_id="xtest-session-002"):
            event = cascade_auditor.record(
                trigger_type="MANUAL_INTERVENTION",
                trigger_details={"action": "override"},
                effects=[],
                namespace="tokyo",
                triggered_by="admin",
            )

            event_dict = event.to_dict()

            assert "is_test" in event_dict
            assert event_dict["is_test"] is True
