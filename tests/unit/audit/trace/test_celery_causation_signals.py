"""
Celery Causation Context 시그널 단위 테스트.

Phase 6: task_prerun/postrun 시그널에서 Causation Context 자동 복원/정리 테스트.

Tests:
- _setup_causation_context: Celery 헤더에서 컨텍스트 복원
- _cleanup_causation_context: 태스크 종료 시 컨텍스트 정리
- 깊이 증가 확인
- 메타데이터 설정 확인
"""

from __future__ import annotations

from unittest.mock import MagicMock

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

# =============================================================================
# _setup_causation_context Tests
# =============================================================================


@pytest.fixture(autouse=True)
def reset_causation_context():
    """테스트 간 CausationContext 상태 초기화."""
    # Reset before test
    token = _current_causation.set(None)
    yield
    # Reset after test
    _current_causation.reset(token)


class TestSetupCausationContext:
    """_setup_causation_context 단위 테스트."""

    def test_setup_with_valid_headers(self):
        """유효한 헤더로 컨텍스트 설정."""
        from baldur.context.celery_context_utils import (
            _CAUSATION_TOKEN_ATTR,
            _setup_causation_context,
        )

        # Mock sender with request and headers
        mock_sender = MagicMock()
        mock_sender.request = MagicMock()
        mock_sender.request.headers = {
            CELERY_HEADER_CASCADE_ID: "cascade-test123",
            CELERY_HEADER_PARENT_EVENT: "evt-parent",
            CELERY_HEADER_CHAIN_DEPTH: "2",
            CELERY_HEADER_NAMESPACE: "seoul",
        }

        _setup_causation_context(mock_sender, "task-123", "test_task")

        # 컨텍스트 확인
        info = CausationContext.get_current()
        assert info is not None
        assert info.cascade_id == "cascade-test123"
        assert info.parent_event_id == "evt-parent"
        assert info.chain_depth == 3  # 2 + 1
        assert info.namespace == "seoul"

        # token 저장 확인
        assert hasattr(mock_sender.request, _CAUSATION_TOKEN_ATTR)

    def test_setup_without_cascade_header_creates_system_cascade(self):
        """cascade_id 헤더 없으면 SYSTEM_ROOT cascade 자동 생성."""
        from baldur.context.celery_context_utils import _setup_causation_context

        mock_sender = MagicMock()
        mock_sender.request = MagicMock()
        mock_sender.request.headers = {}  # 헤더 없음

        _setup_causation_context(mock_sender, "task-123", "test_task")

        info = CausationContext.get_current()
        # 새 동작: SYSTEM_ROOT cascade가 자동 생성됨
        assert info is not None
        assert info.cascade_id.startswith("cascade-")
        assert info.parent_event_id.startswith("SYSTEM_ROOT_")
        assert info.metadata.get("auto_generated") is True

    def test_setup_without_request(self):
        """request 없으면 예외 없이 처리, 컨텍스트 설정 안함."""
        from baldur.context.celery_context_utils import _setup_causation_context

        mock_sender = MagicMock()
        mock_sender.request = None

        # 예외 발생하지 않아야 함
        _setup_causation_context(mock_sender, "task-123", "test_task")

        info = CausationContext.get_current()
        # request 없으면 cascade 생성 안함
        assert info is None

    def test_metadata_includes_task_info(self):
        """메타데이터에 태스크 정보 포함."""
        from baldur.context.celery_context_utils import _setup_causation_context

        mock_sender = MagicMock()
        mock_sender.request = MagicMock()
        mock_sender.request.headers = {
            CELERY_HEADER_CASCADE_ID: "cascade-meta",
            CELERY_HEADER_CHAIN_DEPTH: "0",
        }

        _setup_causation_context(mock_sender, "task-456", "my_task_name")

        info = CausationContext.get_current()
        assert info is not None
        assert info.metadata.get("task_id") == "task-456"
        assert info.metadata.get("task_name") == "my_task_name"
        assert info.metadata.get("restored_from") == "celery_signal"


# =============================================================================
# _cleanup_causation_context Tests
# =============================================================================


class TestCleanupCausationContext:
    """_cleanup_causation_context 단위 테스트."""

    def setup_method(self):
        """각 테스트 전 컨텍스트 초기화."""
        try:
            _current_causation.set(None)
        except Exception:
            pass

    def teardown_method(self):
        """각 테스트 후 컨텍스트 정리."""
        try:
            _current_causation.set(None)
        except Exception:
            pass

    def test_cleanup_removes_context(self):
        """정리 후 컨텍스트 제거됨."""
        from baldur.context.celery_context_utils import (
            _cleanup_causation_context,
            _setup_causation_context,
        )

        # 먼저 설정
        mock_sender = MagicMock()
        mock_sender.request = MagicMock()
        mock_sender.request.headers = {
            CELERY_HEADER_CASCADE_ID: "cascade-cleanup",
            CELERY_HEADER_CHAIN_DEPTH: "0",
        }

        _setup_causation_context(mock_sender, "task-789", "cleanup_task")

        # 컨텍스트 설정됨 확인
        assert CausationContext.get_current() is not None

        # 정리
        _cleanup_causation_context(mock_sender)

        # 컨텍스트 제거됨 확인
        assert CausationContext.get_current() is None

    def test_cleanup_without_token(self):
        """token 없어도 예외 없이 처리."""
        from baldur.context.celery_context_utils import _cleanup_causation_context

        mock_sender = MagicMock()
        mock_sender.request = MagicMock()
        # token 속성 없음

        # 예외 발생하지 않아야 함
        _cleanup_causation_context(mock_sender)

    def test_cleanup_without_request(self):
        """request 없어도 예외 없이 처리."""
        from baldur.context.celery_context_utils import _cleanup_causation_context

        mock_sender = MagicMock()
        mock_sender.request = None

        # 예외 발생하지 않아야 함
        _cleanup_causation_context(mock_sender)


# =============================================================================
# get_causation_for_celery Tests
# =============================================================================


class TestGetCausationForCelery:
    """get_causation_for_celery 단위 테스트."""

    def setup_method(self):
        try:
            _current_causation.set(None)
        except Exception:
            pass

    def teardown_method(self):
        try:
            _current_causation.set(None)
        except Exception:
            pass

    def test_returns_empty_when_no_context(self):
        """컨텍스트 없으면 빈 딕셔너리 반환."""
        headers = get_causation_for_celery()

        assert headers == {}

    def test_returns_headers_with_context(self):
        """컨텍스트 있으면 헤더 반환."""
        with CausationContext.start_cascade(namespace="test-ns") as ctx:
            headers = get_causation_for_celery()

            assert headers[CELERY_HEADER_CASCADE_ID] == ctx.cascade_id
            assert headers[CELERY_HEADER_PARENT_EVENT] == ctx.parent_event_id
            assert headers[CELERY_HEADER_CHAIN_DEPTH] == "0"
            assert headers[CELERY_HEADER_NAMESPACE] == "test-ns"


# =============================================================================
# restore_causation_from_celery Tests
# =============================================================================


class TestRestoreCausationFromCelery:
    """restore_causation_from_celery 단위 테스트."""

    def setup_method(self):
        try:
            _current_causation.set(None)
        except Exception:
            pass

    def teardown_method(self):
        try:
            _current_causation.set(None)
        except Exception:
            pass

    def test_restore_with_valid_headers(self):
        """유효한 헤더로 컨텍스트 복원."""
        headers = {
            CELERY_HEADER_CASCADE_ID: "cascade-restore",
            CELERY_HEADER_PARENT_EVENT: "evt-parent",
            CELERY_HEADER_CHAIN_DEPTH: "1",
            CELERY_HEADER_NAMESPACE: "busan",
        }

        with restore_causation_from_celery(headers) as ctx:
            assert ctx is not None
            assert ctx.cascade_id == "cascade-restore"
            assert ctx.chain_depth == 2  # 1 + 1 증가
            assert ctx.namespace == "busan"

        # 컨텍스트 매니저 종료 후 정리됨
        assert CausationContext.get_current() is None

    def test_restore_without_cascade_id(self):
        """cascade_id 없으면 None 반환."""
        headers = {
            CELERY_HEADER_PARENT_EVENT: "evt-parent",
        }

        with restore_causation_from_celery(headers) as ctx:
            assert ctx is None

    def test_restore_with_empty_headers(self):
        """빈 헤더면 None 반환."""
        with restore_causation_from_celery({}) as ctx:
            assert ctx is None


# =============================================================================
# Chain Depth Increment Tests
# =============================================================================


class TestChainDepthIncrement:
    """체인 깊이 증가 테스트."""

    def setup_method(self):
        try:
            _current_causation.set(None)
        except Exception:
            pass

    def teardown_method(self):
        try:
            _current_causation.set(None)
        except Exception:
            pass

    def test_depth_increments_on_continue(self):
        """continue_cascade 시 깊이 증가."""
        info = CausationInfo(
            cascade_id="cascade-depth",
            parent_event_id="evt-0",
            chain_depth=5,
            namespace="global",
        )

        with CausationContext.continue_cascade(info) as ctx:
            assert ctx.chain_depth == 6

    def test_depth_increments_on_restore(self):
        """Celery 복원 시 깊이 증가."""
        headers = {
            CELERY_HEADER_CASCADE_ID: "cascade-inc",
            CELERY_HEADER_CHAIN_DEPTH: "3",
        }

        with restore_causation_from_celery(headers) as ctx:
            assert ctx.chain_depth == 4

    def test_depth_starts_at_zero(self):
        """새 cascade는 깊이 0에서 시작."""
        with CausationContext.start_cascade() as ctx:
            assert ctx.chain_depth == 0


# =============================================================================
# Integration Scenario Tests
# =============================================================================


class TestCausationSignalIntegration:
    """시그널 통합 시나리오 테스트."""

    def setup_method(self):
        try:
            _current_causation.set(None)
        except Exception:
            pass

    def teardown_method(self):
        try:
            _current_causation.set(None)
        except Exception:
            pass

    def test_full_celery_task_lifecycle(self):
        """전체 Celery 태스크 라이프사이클 시뮬레이션."""
        from baldur.context.celery_context_utils import (
            _cleanup_causation_context,
            _setup_causation_context,
        )

        # 1. 부모 태스크에서 cascade 시작
        with CausationContext.start_cascade(namespace="prod") as parent_ctx:
            parent_cascade_id = parent_ctx.cascade_id
            parent_depth = parent_ctx.chain_depth

            # 2. 자식 태스크 호출용 헤더 생성
            headers = get_causation_for_celery()

            # 3. 자식 태스크에서 수신 (시그널 핸들러 시뮬레이션)
            mock_child_sender = MagicMock()
            mock_child_sender.request = MagicMock()
            mock_child_sender.request.headers = headers

            _setup_causation_context(mock_child_sender, "child-task-1", "child_task")

            # 4. 자식 태스크 내 컨텍스트 확인
            child_ctx = CausationContext.get_current()
            assert child_ctx is not None
            assert child_ctx.cascade_id == parent_cascade_id  # 동일 cascade
            assert child_ctx.chain_depth == parent_depth + 1  # 깊이 증가

            # 5. 자식 태스크 종료
            _cleanup_causation_context(mock_child_sender)

        # 6. 모든 컨텍스트 정리됨
        assert CausationContext.get_current() is None

    def test_nested_task_chain(self):
        """중첩 태스크 체인 시뮬레이션."""
        # Level 0: HTTP 요청
        with CausationContext.start_cascade() as level0:
            assert level0.chain_depth == 0

            # Level 1: 첫 번째 Celery 태스크
            headers1 = get_causation_for_celery()
            with restore_causation_from_celery(headers1) as level1:
                assert level1.chain_depth == 1

                # Level 2: 두 번째 Celery 태스크
                headers2 = get_causation_for_celery()
                with restore_causation_from_celery(headers2) as level2:
                    assert level2.chain_depth == 2

                    # 모두 동일 cascade
                    assert level2.cascade_id == level0.cascade_id
