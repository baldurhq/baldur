"""
Causation Context Propagation 테스트.

request_id → causation_id 전파, Celery 헤더 주입/복원,
System-initiated Cascade 생성 테스트.
"""

import pytest

from baldur.context.causation_context import (
    CELERY_HEADER_CASCADE_ID,
    CELERY_HEADER_CHAIN_DEPTH,
    CELERY_HEADER_NAMESPACE,
    CELERY_HEADER_PARENT_EVENT,
    CausationContext,
    CausationInfo,
    _current_causation,
    get_causation_for_celery,
    restore_causation_from_celery,
)


@pytest.fixture(autouse=True)
def reset_causation_context():
    """테스트 간 CausationContext 상태 초기화."""
    # Reset before test
    token = _current_causation.set(None)
    yield
    # Reset after test
    _current_causation.reset(token)


class TestCausationContextBasic:
    """CausationContext 기본 기능 테스트."""

    def test_start_cascade_creates_context(self):
        """start_cascade로 컨텍스트 생성."""
        with CausationContext.start_cascade(namespace="test") as ctx:
            assert ctx.cascade_id.startswith("cascade-")
            assert ctx.namespace == "test"
            assert ctx.chain_depth == 0

            # 컨텍스트 내에서 조회 가능
            current = CausationContext.get_current()
            assert current is not None
            assert current.cascade_id == ctx.cascade_id

        # 컨텍스트 종료 후 None
        assert CausationContext.get_current() is None

    def test_start_cascade_with_trigger_event_id(self):
        """trigger_event_id 지정 시 사용."""
        trigger = "req-abc123"
        with CausationContext.start_cascade(trigger_event_id=trigger) as ctx:
            assert ctx.parent_event_id == trigger

    def test_start_cascade_without_trigger_auto_generates(self):
        """trigger_event_id 미지정 시 자동 생성."""
        with CausationContext.start_cascade() as ctx:
            assert ctx.parent_event_id.startswith("evt-")

    def test_is_set_returns_correct_value(self):
        """is_set() 메서드 정확성."""
        assert CausationContext.is_set() is False

        with CausationContext.start_cascade():
            assert CausationContext.is_set() is True

        assert CausationContext.is_set() is False

    def test_continue_cascade_increases_depth(self):
        """continue_cascade로 깊이 증가."""
        with CausationContext.start_cascade() as ctx1:
            assert ctx1.chain_depth == 0

            with CausationContext.continue_cascade(ctx1) as ctx2:
                assert ctx2.chain_depth == 1
                assert ctx2.cascade_id == ctx1.cascade_id

    def test_get_current_cascade_id(self):
        """get_current_cascade_id() 정확성."""
        assert CausationContext.get_current_cascade_id() is None

        with CausationContext.start_cascade() as ctx:
            cascade_id = CausationContext.get_current_cascade_id()
            assert cascade_id == ctx.cascade_id

        assert CausationContext.get_current_cascade_id() is None


class TestSystemCausationCascade:
    """System-initiated Cascade 테스트 (Celery Beat / Management Command)."""

    def test_start_system_cascade_creates_system_root(self):
        """start_system_cascade가 SYSTEM_ROOT prefix 생성."""
        with CausationContext.start_system_cascade(source="celery_beat") as ctx:
            assert ctx.parent_event_id.startswith("SYSTEM_ROOT_celery_beat_")
            assert ctx.cascade_id.startswith("cascade-")
            assert ctx.chain_depth == 0

    def test_start_system_cascade_with_management_cmd(self):
        """management_cmd source 테스트."""
        with CausationContext.start_system_cascade(source="management_cmd") as ctx:
            assert "SYSTEM_ROOT_management_cmd_" in ctx.parent_event_id

    def test_start_system_cascade_with_scheduler(self):
        """scheduler source 테스트."""
        with CausationContext.start_system_cascade(source="scheduler") as ctx:
            assert "SYSTEM_ROOT_scheduler_" in ctx.parent_event_id

    def test_start_system_cascade_includes_metadata(self):
        """system_source 메타데이터 포함."""
        with CausationContext.start_system_cascade(source="cron") as ctx:
            assert ctx.metadata.get("system_source") == "cron"

    def test_start_system_cascade_with_custom_metadata(self):
        """커스텀 메타데이터 병합."""
        custom_meta = {"custom_key": "custom_value"}
        with CausationContext.start_system_cascade(
            source="worker",
            metadata=custom_meta,
        ) as ctx:
            assert ctx.metadata.get("system_source") == "worker"
            assert ctx.metadata.get("custom_key") == "custom_value"


class TestCeleryPropagation:
    """Celery 헤더 전파 테스트."""

    def test_get_causation_for_celery_returns_headers(self):
        """get_causation_for_celery가 헤더 딕셔너리 반환."""
        with CausationContext.start_cascade(namespace="test") as ctx:
            headers = get_causation_for_celery()

            assert headers[CELERY_HEADER_CASCADE_ID] == ctx.cascade_id
            assert headers[CELERY_HEADER_PARENT_EVENT] == ctx.parent_event_id
            assert headers[CELERY_HEADER_CHAIN_DEPTH] == str(ctx.chain_depth)
            assert headers[CELERY_HEADER_NAMESPACE] == "test"

    def test_get_causation_for_celery_empty_when_not_set(self):
        """컨텍스트 미설정 시 빈 딕셔너리 반환."""
        headers = get_causation_for_celery()
        assert headers == {}

    def test_restore_causation_from_celery(self):
        """restore_causation_from_celery로 복원."""
        # 원본 컨텍스트에서 헤더 생성
        with CausationContext.start_cascade(namespace="original") as original:
            headers = get_causation_for_celery()

        # 컨텍스트 종료 후 복원
        assert CausationContext.is_set() is False

        with restore_causation_from_celery(headers) as restored:
            assert restored is not None
            assert restored.cascade_id == original.cascade_id
            assert restored.chain_depth == original.chain_depth + 1  # 깊이 증가
            assert restored.namespace == "original"

    def test_restore_causation_from_celery_empty_headers(self):
        """빈 헤더로 복원 시 None."""
        with restore_causation_from_celery({}) as restored:
            assert restored is None

    def test_restore_causation_from_celery_missing_cascade_id(self):
        """cascade_id 없는 헤더로 복원 시 None."""
        headers = {
            CELERY_HEADER_PARENT_EVENT: "some-event",
            CELERY_HEADER_CHAIN_DEPTH: "1",
        }
        with restore_causation_from_celery(headers) as restored:
            assert restored is None


class TestCausationInfoSerialization:
    """CausationInfo 직렬화 테스트."""

    def test_to_dict(self):
        """to_dict 직렬화."""
        info = CausationInfo(
            cascade_id="cascade-abc123",
            parent_event_id="evt-123",
            chain_depth=2,
            namespace="test_ns",
            metadata={"key": "value"},
        )

        result = info.to_dict()

        assert result["cascade_id"] == "cascade-abc123"
        assert result["parent_event_id"] == "evt-123"
        assert result["chain_depth"] == 2
        assert result["namespace"] == "test_ns"
        assert result["metadata"]["key"] == "value"

    def test_from_dict(self):
        """from_dict 역직렬화."""
        data = {
            "cascade_id": "cascade-xyz789",
            "parent_event_id": "evt-456",
            "chain_depth": 3,
            "namespace": "restored_ns",
            "metadata": {"restored_key": "restored_value"},
        }

        info = CausationInfo.from_dict(data)

        assert info.cascade_id == "cascade-xyz789"
        assert info.parent_event_id == "evt-456"
        assert info.chain_depth == 3
        assert info.namespace == "restored_ns"
        assert info.metadata["restored_key"] == "restored_value"

    def test_from_dict_with_missing_fields(self):
        """일부 필드 누락 시 기본값 사용."""
        data = {"cascade_id": "cascade-minimal"}

        info = CausationInfo.from_dict(data)

        assert info.cascade_id == "cascade-minimal"
        assert info.parent_event_id == ""
        assert info.chain_depth == 0
        assert info.namespace == "global"
        assert info.metadata == {}


# Note: Django-dependent tests (TestResponseMetaCausationId, TestStandardErrorResponseWithCausation)
# are located in the global tests folder: tests/api/exceptions/test_causation_propagation.py
# These tests require Django configuration and should be run via docker-compose.
