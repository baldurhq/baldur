"""
XTC- Causation ID Prefix 단위 테스트.

X-Test-Mode에서 causation ID에 XTC- 프리픽스가 정상 적용되는지 검증합니다.

테스트 항목:
- X-Test 시 cascade_id에 XTC- 포함
- X-Test 시 event_id에 XTC- 포함
- 일반 요청 시 프리픽스 없음
- normalize_causation_id() 함수 동작
- is_xtest_id() 함수 동작
- start_system_cascade()에서 프리픽스 적용
"""

from baldur.context.causation_context import (
    XTEST_CAUSATION_PREFIX,
    CausationContext,
    _get_xtest_id_prefix,
    is_xtest_id,
    normalize_causation_id,
)
from baldur.core.test_mode_context import TestModeContext


class TestXtestCausationIdPrefix:
    """XTC- Causation ID 프리픽스 테스트."""

    def test_xtest_cascade_id_has_prefix(self):
        """X-Test 모드에서 cascade_id가 XTC- 프리픽스를 포함해야 함."""
        with TestModeContext.start(session_id="test-session-1"):
            with CausationContext.start_cascade(namespace="test") as ctx:
                assert ctx.cascade_id.startswith(XTEST_CAUSATION_PREFIX)
                assert ctx.cascade_id.startswith("XTC-cascade-")

    def test_xtest_event_id_has_prefix(self):
        """X-Test 모드에서 자동 생성된 event_id가 XTC- 프리픽스를 포함해야 함."""
        with TestModeContext.start(session_id="test-session-2"):
            with CausationContext.start_cascade(namespace="test") as ctx:
                # trigger_event_id를 지정하지 않으면 자동 생성됨
                assert ctx.parent_event_id.startswith(XTEST_CAUSATION_PREFIX)
                assert ctx.parent_event_id.startswith("XTC-evt-")

    def test_normal_cascade_id_no_prefix(self):
        """일반 모드에서 cascade_id에 XTC- 프리픽스가 없어야 함."""
        # TestModeContext 외부에서 실행
        with CausationContext.start_cascade(namespace="production") as ctx:
            assert not ctx.cascade_id.startswith(XTEST_CAUSATION_PREFIX)
            assert ctx.cascade_id.startswith("cascade-")

    def test_normal_event_id_no_prefix(self):
        """일반 모드에서 event_id에 XTC- 프리픽스가 없어야 함."""
        with CausationContext.start_cascade(namespace="production") as ctx:
            assert not ctx.parent_event_id.startswith(XTEST_CAUSATION_PREFIX)
            assert ctx.parent_event_id.startswith("evt-")

    def test_xtest_system_cascade_has_prefix(self):
        """X-Test 모드에서 시스템 cascade의 event_id도 XTC- 프리픽스를 포함해야 함."""
        with TestModeContext.start(session_id="test-session-3"):
            with CausationContext.start_system_cascade(
                source="celery_beat",
                namespace="test",
            ) as ctx:
                # cascade_id에 프리픽스
                assert ctx.cascade_id.startswith(XTEST_CAUSATION_PREFIX)
                # SYSTEM_ROOT event_id에도 프리픽스
                assert ctx.parent_event_id.startswith(XTEST_CAUSATION_PREFIX)
                assert "SYSTEM_ROOT_celery_beat_" in ctx.parent_event_id

    def test_normal_system_cascade_no_prefix(self):
        """일반 모드에서 시스템 cascade에 XTC- 프리픽스가 없어야 함."""
        with CausationContext.start_system_cascade(
            source="scheduler",
            namespace="production",
        ) as ctx:
            assert not ctx.cascade_id.startswith(XTEST_CAUSATION_PREFIX)
            assert not ctx.parent_event_id.startswith(XTEST_CAUSATION_PREFIX)
            assert ctx.parent_event_id.startswith("SYSTEM_ROOT_scheduler_")

    def test_custom_trigger_event_id_not_prefixed(self):
        """사용자가 직접 지정한 trigger_event_id는 프리픽스가 추가되지 않음."""
        custom_event_id = "custom-trigger-12345"
        with TestModeContext.start(session_id="test-session-4"):
            with CausationContext.start_cascade(
                namespace="test",
                trigger_event_id=custom_event_id,
            ) as ctx:
                # cascade_id에는 프리픽스 적용
                assert ctx.cascade_id.startswith(XTEST_CAUSATION_PREFIX)
                # 사용자 지정 trigger_event_id는 그대로 유지
                assert ctx.parent_event_id == custom_event_id


class TestIsXtestId:
    """is_xtest_id() 함수 테스트."""

    def test_xtest_cascade_id_detected(self):
        """XTC- 프리픽스가 있는 cascade_id 감지."""
        assert is_xtest_id("XTC-cascade-a1b2c3d4e5f6") is True

    def test_xtest_event_id_detected(self):
        """XTC- 프리픽스가 있는 event_id 감지."""
        assert is_xtest_id("XTC-evt-a1b2c3d4") is True

    def test_xtest_system_root_detected(self):
        """XTC- 프리픽스가 있는 SYSTEM_ROOT ID 감지."""
        assert is_xtest_id("XTC-SYSTEM_ROOT_beat_a1b2c3d4") is True

    def test_normal_cascade_id_not_detected(self):
        """일반 cascade_id는 감지되지 않음."""
        assert is_xtest_id("cascade-a1b2c3d4e5f6") is False

    def test_normal_event_id_not_detected(self):
        """일반 event_id는 감지되지 않음."""
        assert is_xtest_id("evt-a1b2c3d4") is False

    def test_empty_string(self):
        """빈 문자열 처리."""
        assert is_xtest_id("") is False


class TestNormalizeCausationId:
    """normalize_causation_id() 함수 테스트."""

    def test_remove_prefix_from_cascade_id(self):
        """XTC- 프리픽스가 있는 cascade_id에서 프리픽스 제거."""
        normalized = normalize_causation_id("XTC-cascade-a1b2c3d4e5f6")
        assert normalized == "cascade-a1b2c3d4e5f6"

    def test_remove_prefix_from_event_id(self):
        """XTC- 프리픽스가 있는 event_id에서 프리픽스 제거."""
        normalized = normalize_causation_id("XTC-evt-a1b2c3d4")
        assert normalized == "evt-a1b2c3d4"

    def test_remove_prefix_from_system_root(self):
        """XTC- 프리픽스가 있는 SYSTEM_ROOT ID에서 프리픽스 제거."""
        normalized = normalize_causation_id("XTC-SYSTEM_ROOT_beat_a1b2c3d4")
        assert normalized == "SYSTEM_ROOT_beat_a1b2c3d4"

    def test_normal_cascade_id_unchanged(self):
        """프리픽스 없는 cascade_id는 변경 없음."""
        original = "cascade-a1b2c3d4e5f6"
        normalized = normalize_causation_id(original)
        assert normalized == original

    def test_normal_event_id_unchanged(self):
        """프리픽스 없는 event_id는 변경 없음."""
        original = "evt-a1b2c3d4"
        normalized = normalize_causation_id(original)
        assert normalized == original

    def test_empty_string_unchanged(self):
        """빈 문자열 처리."""
        assert normalize_causation_id("") == ""


class TestGetXtestIdPrefix:
    """_get_xtest_id_prefix() 함수 테스트."""

    def test_returns_prefix_in_xtest_mode(self):
        """X-Test 모드에서 XTC- 프리픽스 반환."""
        with TestModeContext.start(session_id="test-prefix"):
            prefix = _get_xtest_id_prefix()
            assert prefix == XTEST_CAUSATION_PREFIX
            assert prefix == "XTC-"

    def test_returns_empty_in_normal_mode(self):
        """일반 모드에서 빈 문자열 반환."""
        # TestModeContext 외부
        prefix = _get_xtest_id_prefix()
        assert prefix == ""


class TestContextExitReset:
    """컨텍스트 종료 시 상태 리셋 테스트."""

    def test_prefix_reset_after_xtest_context_exit(self):
        """X-Test 컨텍스트 종료 후 프리픽스가 리셋됨."""
        # 먼저 X-Test 모드에서 ID 생성
        with TestModeContext.start(session_id="test-reset"):
            xtest_prefix = _get_xtest_id_prefix()
            assert xtest_prefix == "XTC-"

        # 컨텍스트 종료 후 일반 모드로 복귀
        normal_prefix = _get_xtest_id_prefix()
        assert normal_prefix == ""

    def test_nested_xtest_context(self):
        """중첩된 X-Test 컨텍스트에서 프리픽스 유지."""
        with TestModeContext.start(session_id="outer"):
            outer_prefix = _get_xtest_id_prefix()
            assert outer_prefix == "XTC-"

            with CausationContext.start_cascade(namespace="nested") as ctx:
                inner_prefix = _get_xtest_id_prefix()
                assert inner_prefix == "XTC-"
                assert ctx.cascade_id.startswith("XTC-")

            # 내부 CausationContext 종료 후에도 X-Test 모드 유지
            still_xtest = _get_xtest_id_prefix()
            assert still_xtest == "XTC-"

        # 외부 TestModeContext 종료 후 일반 모드
        final_prefix = _get_xtest_id_prefix()
        assert final_prefix == ""


class TestLogFilteringFormats:
    """로그 필터링에 사용되는 ID 형식 테스트."""

    def test_xtest_cascade_id_grepable(self):
        """X-Test cascade_id가 grep 'XTC-'로 검색 가능해야 함."""
        with TestModeContext.start(session_id="grep-test"):
            with CausationContext.start_cascade(namespace="test") as ctx:
                # grep "XTC-" 로 필터링 가능
                assert "XTC-" in ctx.cascade_id
                # grep "XTC-cascade-" 로 더 정밀 필터링 가능
                assert "XTC-cascade-" in ctx.cascade_id

    def test_xtest_event_id_grepable(self):
        """X-Test event_id가 grep 'XTC-evt-'로 검색 가능해야 함."""
        with TestModeContext.start(session_id="grep-test"):
            with CausationContext.start_cascade(namespace="test") as ctx:
                # grep "XTC-evt-" 로 필터링 가능
                assert "XTC-evt-" in ctx.parent_event_id

    def test_production_ids_excluded_by_grep_v(self):
        """운영 ID가 grep -v 'XTC-'로 필터링 가능해야 함."""
        with CausationContext.start_cascade(namespace="production") as ctx:
            # 운영 ID에는 XTC- 없음
            assert "XTC-" not in ctx.cascade_id
            assert "XTC-" not in ctx.parent_event_id
