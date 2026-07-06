"""
Celery Causation Propagation 테스트.

Celery Task 호출 시 causation_id 자동 전파 기능을 테스트합니다.

테스트 항목:
- before_task_publish 시그널 핸들러 등록
- CausationContext 설정 시 헤더 자동 주입
- CausationContext 미설정 시 헤더 주입 생략
- 이미 헤더가 있으면 덮어쓰지 않음
- task_prerun에서 causation 자동 복원
- task_postrun에서 causation 정리
- 시스템 시작 태스크 (Celery Beat) causation 자동 생성
"""

from unittest.mock import MagicMock

from baldur.context.causation_context import (
    CELERY_HEADER_CASCADE_ID,
    CELERY_HEADER_CHAIN_DEPTH,
    CELERY_HEADER_NAMESPACE,
    CELERY_HEADER_PARENT_EVENT,
    CausationContext,
    CausationInfo,
    get_causation_for_celery,
    restore_causation_from_celery,
)


class TestCausationContextBasic:
    """CausationContext 기본 기능 테스트."""

    def test_start_cascade_creates_causation_info(self):
        """start_cascade()가 CausationInfo 생성."""
        with CausationContext.start_cascade(namespace="test-ns") as ctx:
            assert ctx is not None
            assert ctx.cascade_id.startswith("cascade-")
            assert ctx.parent_event_id.startswith("evt-")
            assert ctx.chain_depth == 0
            assert ctx.namespace == "test-ns"

    def test_is_set_returns_true_inside_context(self):
        """컨텍스트 내부에서 is_set() True."""
        assert CausationContext.is_set() is False

        with CausationContext.start_cascade():
            assert CausationContext.is_set() is True

        assert CausationContext.is_set() is False

    def test_get_current_returns_info(self):
        """get_current()가 현재 CausationInfo 반환."""
        with CausationContext.start_cascade(namespace="ns1") as ctx:
            current = CausationContext.get_current()
            assert current is ctx
            assert current.namespace == "ns1"

    def test_get_current_returns_none_outside_context(self):
        """컨텍스트 외부에서 get_current() None 반환."""
        result = CausationContext.get_current()
        assert result is None


class TestStartSystemCascade:
    """start_system_cascade() 테스트 (시스템 트리거용)."""

    def test_start_system_cascade_creates_system_root_event_id(self):
        """start_system_cascade()가 SYSTEM_ROOT_{source} 형식 이벤트 ID 생성."""
        with CausationContext.start_system_cascade(source="celery_beat") as ctx:
            assert ctx.parent_event_id.startswith("SYSTEM_ROOT_celery_beat_")
            assert ctx.cascade_id.startswith("cascade-")
            assert ctx.metadata.get("system_source") == "celery_beat"

    def test_start_system_cascade_different_sources(self):
        """다양한 source 값에 대한 이벤트 ID 생성."""
        sources = ["celery_beat", "management_cmd", "cron", "scheduler"]

        for source in sources:
            with CausationContext.start_system_cascade(source=source) as ctx:
                assert f"SYSTEM_ROOT_{source}_" in ctx.parent_event_id
                assert ctx.metadata.get("system_source") == source


class TestGetCausationForCelery:
    """get_causation_for_celery() 테스트."""

    def test_get_causation_for_celery_inside_context(self):
        """컨텍스트 내에서 Celery 헤더 딕셔너리 반환."""
        with CausationContext.start_cascade(namespace="test") as ctx:
            headers = get_causation_for_celery()

            assert headers[CELERY_HEADER_CASCADE_ID] == ctx.cascade_id
            assert headers[CELERY_HEADER_PARENT_EVENT] == ctx.parent_event_id
            assert headers[CELERY_HEADER_CHAIN_DEPTH] == str(ctx.chain_depth)
            assert headers[CELERY_HEADER_NAMESPACE] == ctx.namespace

    def test_get_causation_for_celery_outside_context(self):
        """컨텍스트 외부에서 빈 딕셔너리 반환."""
        headers = get_causation_for_celery()
        assert headers == {}


class TestRestoreCausationFromCelery:
    """restore_causation_from_celery() 테스트."""

    def test_restore_causation_from_celery(self):
        """Celery 헤더에서 causation 복원."""
        headers = {
            CELERY_HEADER_CASCADE_ID: "cascade-test123",
            CELERY_HEADER_PARENT_EVENT: "evt-parent456",
            CELERY_HEADER_CHAIN_DEPTH: "2",
            CELERY_HEADER_NAMESPACE: "seoul",
        }

        with restore_causation_from_celery(headers) as ctx:
            assert ctx is not None
            assert ctx.cascade_id == "cascade-test123"
            assert ctx.parent_event_id == "evt-parent456"
            # chain_depth는 continue_cascade에서 +1 되므로 3
            assert ctx.chain_depth == 3
            assert ctx.namespace == "seoul"
            assert CausationContext.is_set() is True

        # 컨텍스트 종료 후 정리
        assert CausationContext.is_set() is False

    def test_restore_causation_from_celery_empty_headers(self):
        """빈 헤더일 때 None 반환."""
        with restore_causation_from_celery({}) as ctx:
            assert ctx is None
            assert CausationContext.is_set() is False

    def test_restore_causation_from_celery_no_cascade_id(self):
        """cascade_id 없으면 None 반환."""
        headers = {
            CELERY_HEADER_PARENT_EVENT: "evt-123",
        }

        with restore_causation_from_celery(headers) as ctx:
            assert ctx is None


class TestContinueCascade:
    """continue_cascade() 테스트."""

    def test_continue_cascade_increments_depth(self):
        """continue_cascade()가 chain_depth 증가."""
        original_info = CausationInfo(
            cascade_id="cascade-orig",
            parent_event_id="evt-orig",
            chain_depth=5,
            namespace="ns",
        )

        with CausationContext.continue_cascade(original_info) as ctx:
            assert ctx.cascade_id == "cascade-orig"
            assert ctx.chain_depth == 6  # 5 + 1

    def test_continue_cascade_without_increment(self):
        """increment_depth=False면 깊이 유지."""
        original_info = CausationInfo(
            cascade_id="cascade-orig",
            parent_event_id="evt-orig",
            chain_depth=5,
            namespace="ns",
        )

        with CausationContext.continue_cascade(
            original_info, increment_depth=False
        ) as ctx:
            assert ctx.chain_depth == 5


class TestBeforeTaskPublishHandler:
    """CausationHandler.handle 시그널 핸들러 테스트."""

    def _make_handler(self, enabled=True):
        from baldur.adapters.celery.handlers.causation_handler import (
            CausationHandler,
        )
        from baldur.adapters.celery.signal_config import SignalHooksSettings

        config = SignalHooksSettings(enabled=enabled)
        return CausationHandler(config)

    def test_handler_injects_causation_headers(self):
        """CausationContext 설정 시 헤더 자동 주입."""
        handler = self._make_handler()
        headers = {}

        with CausationContext.start_cascade(namespace="inject-test") as ctx:
            handler.handle(
                sender="test_task",
                headers=headers,
                body=None,
            )

            # 헤더가 주입되었는지 확인
            assert headers.get(CELERY_HEADER_CASCADE_ID) == ctx.cascade_id
            assert headers.get(CELERY_HEADER_PARENT_EVENT) == ctx.parent_event_id
            assert headers.get(CELERY_HEADER_CHAIN_DEPTH) == str(ctx.chain_depth)
            assert headers.get(CELERY_HEADER_NAMESPACE) == ctx.namespace

    def test_handler_skips_when_no_context(self):
        """CausationContext 미설정 시 헤더 주입 생략."""
        handler = self._make_handler()
        headers = {}

        handler.handle(
            sender="test_task",
            headers=headers,
            body=None,
        )

        # 헤더 비어 있음
        assert CELERY_HEADER_CASCADE_ID not in headers

    def test_handler_does_not_overwrite_existing_headers(self):
        """이미 causation 헤더가 있으면 덮어쓰지 않음."""
        handler = self._make_handler()
        original_cascade_id = "cascade-original"
        headers = {
            CELERY_HEADER_CASCADE_ID: original_cascade_id,
        }

        with CausationContext.start_cascade():
            handler.handle(
                sender="test_task",
                headers=headers,
                body=None,
            )

            # 원래 값 유지
            assert headers[CELERY_HEADER_CASCADE_ID] == original_cascade_id

    def test_handler_handles_none_headers(self):
        """headers=None일 때 예외 발생하지 않음."""
        handler = self._make_handler()

        with CausationContext.start_cascade():
            # 예외 없이 실행되어야 함
            handler.handle(
                sender="test_task",
                headers=None,
                body=None,
            )

    def test_handler_disabled_when_config_disabled(self):
        """config.enabled=False면 동작 안 함."""
        handler = self._make_handler(enabled=False)
        headers = {}

        with CausationContext.start_cascade():
            handler.handle(
                sender="test_task",
                headers=headers,
                body=None,
            )

        # disabled면 헤더 주입 안 됨
        assert CELERY_HEADER_CASCADE_ID not in headers


class TestSetupCausationContext:
    """_setup_causation_context 함수 테스트."""

    def test_setup_from_headers(self):
        """헤더에서 causation 복원."""
        from baldur.context.celery_context_utils import _setup_causation_context

        # Mock sender with request containing headers
        mock_request = MagicMock()
        mock_request.headers = {
            CELERY_HEADER_CASCADE_ID: "cascade-fromheader",
            CELERY_HEADER_PARENT_EVENT: "evt-parent",
            CELERY_HEADER_CHAIN_DEPTH: "3",
            CELERY_HEADER_NAMESPACE: "header-ns",
        }

        mock_sender = MagicMock()
        mock_sender.request = mock_request

        _setup_causation_context(mock_sender, "task-123", "test.task")

        # 컨텍스트 설정 확인
        ctx = CausationContext.get_current()
        assert ctx is not None
        assert ctx.cascade_id == "cascade-fromheader"
        assert ctx.chain_depth == 4  # 3 + 1

        # 정리
        from baldur.context.causation_context import _current_causation

        _current_causation.set(None)

    def test_setup_creates_system_cascade_when_no_headers(self):
        """헤더 없으면 시스템 Cascade 자동 생성."""
        from baldur.context.celery_context_utils import _setup_causation_context

        mock_request = MagicMock()
        mock_request.headers = {}

        mock_sender = MagicMock()
        mock_sender.request = mock_request

        _setup_causation_context(mock_sender, "task-456", "celery.beat.check")

        ctx = CausationContext.get_current()
        assert ctx is not None
        assert ctx.cascade_id.startswith("cascade-")
        assert "SYSTEM_ROOT_" in ctx.parent_event_id
        assert ctx.metadata.get("auto_generated") is True

        # 정리
        from baldur.context.causation_context import _current_causation

        _current_causation.set(None)


class TestDetectCausationSource:
    """_detect_causation_source 함수 테스트."""

    def test_detect_celery_beat(self):
        """beat 관련 태스크명 감지."""
        from baldur.context.celery_context_utils import _detect_causation_source

        assert _detect_causation_source("celery.beat.check") == "celery_beat"
        assert _detect_causation_source("schedule_daily_task") == "celery_beat"
        assert _detect_causation_source("periodic_cleanup") == "celery_beat"

    def test_detect_management_cmd(self):
        """management 관련 태스크명 감지."""
        from baldur.context.celery_context_utils import _detect_causation_source

        assert _detect_causation_source("manage_users") == "management_cmd"
        assert _detect_causation_source("run_command_task") == "management_cmd"
        assert _detect_causation_source("admin_bulk_update") == "management_cmd"

    def test_detect_scheduler(self):
        """스케줄러 관련 태스크명 감지."""
        from baldur.context.celery_context_utils import _detect_causation_source

        assert _detect_causation_source("cron_daily_report") == "scheduler"
        assert _detect_causation_source("cleanup_old_data") == "scheduler"
        assert _detect_causation_source("expire_sessions") == "scheduler"

    def test_detect_worker_default(self):
        """기본값은 worker."""
        from baldur.context.celery_context_utils import _detect_causation_source

        assert _detect_causation_source("process_order") == "worker"
        assert _detect_causation_source("send_email") == "worker"
