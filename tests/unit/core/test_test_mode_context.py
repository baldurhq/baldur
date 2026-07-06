"""
Test Mode Context 단위 테스트.

TestModeContext의 ContextVar 전파, 합성 레이블, 동적 프리픽스 기능을 검증합니다.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

from baldur.core.test_mode_context import (
    TestModeContext,
    get_synthetic_session_id,
    is_synthetic_context,
    synthetic_context,
)


class TestTestModeContextBasic:
    """TestModeContext 기본 기능 테스트."""

    def test_default_state_is_not_synthetic(self):
        """기본 상태는 합성 요청이 아님."""
        assert TestModeContext.is_synthetic() is False
        assert is_synthetic_context() is False
        assert TestModeContext.get_session_id() is None

    def test_context_manager_sets_synthetic_flag(self):
        """Context Manager 사용 시 합성 플래그 설정."""
        assert TestModeContext.is_synthetic() is False

        with TestModeContext.start(session_id="test-session"):
            assert TestModeContext.is_synthetic() is True
            assert TestModeContext.get_session_id() == "test-session"

        # 블록 종료 후 복원
        assert TestModeContext.is_synthetic() is False
        assert TestModeContext.get_session_id() is None

    def test_context_manager_without_session_id(self):
        """세션 ID 없이 Context Manager 사용."""
        with TestModeContext.start():
            assert TestModeContext.is_synthetic() is True
            assert TestModeContext.get_session_id() is None

    def test_manual_enter_exit(self):
        """수동 enter/exit 방식 테스트."""
        TestModeContext.enter_synthetic_mode(session_id="manual-test")

        try:
            assert TestModeContext.is_synthetic() is True
            assert TestModeContext.get_session_id() == "manual-test"
        finally:
            TestModeContext.exit_synthetic_mode()

        assert TestModeContext.is_synthetic() is False
        assert TestModeContext.get_session_id() is None

    def test_synthetic_label_value(self):
        """메트릭 레이블용 값 변환 테스트."""
        assert TestModeContext.get_synthetic_label_value() == "false"

        with TestModeContext.start():
            assert TestModeContext.get_synthetic_label_value() == "true"

        assert TestModeContext.get_synthetic_label_value() == "false"


class TestTestModeContextNesting:
    """중첩 컨텍스트 테스트."""

    def test_nested_context_managers(self):
        """중첩 Context Manager 테스트."""
        assert TestModeContext.is_synthetic() is False

        with TestModeContext.start(session_id="outer"):
            assert TestModeContext.is_synthetic() is True
            assert TestModeContext.get_session_id() == "outer"

            with TestModeContext.start(session_id="inner"):
                assert TestModeContext.is_synthetic() is True
                assert TestModeContext.get_session_id() == "inner"

            # 내부 블록 종료 후 외부 세션 복원
            assert TestModeContext.is_synthetic() is True
            assert TestModeContext.get_session_id() == "outer"

        assert TestModeContext.is_synthetic() is False


class TestTestModeContextThreadSafety:
    """스레드 안전성 테스트."""

    def test_thread_isolation(self):
        """각 스레드는 독립적인 컨텍스트를 가짐."""
        results = {}

        def thread_func(thread_id: int, set_synthetic: bool):
            if set_synthetic:
                with TestModeContext.start(session_id=f"thread-{thread_id}"):
                    results[thread_id] = {
                        "is_synthetic": TestModeContext.is_synthetic(),
                        "session_id": TestModeContext.get_session_id(),
                    }
            else:
                results[thread_id] = {
                    "is_synthetic": TestModeContext.is_synthetic(),
                    "session_id": TestModeContext.get_session_id(),
                }

        threads = [
            threading.Thread(target=thread_func, args=(1, True)),
            threading.Thread(target=thread_func, args=(2, False)),
            threading.Thread(target=thread_func, args=(3, True)),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 스레드 1: synthetic=True
        assert results[1]["is_synthetic"] is True
        assert results[1]["session_id"] == "thread-1"

        # 스레드 2: synthetic=False (기본값)
        assert results[2]["is_synthetic"] is False
        assert results[2]["session_id"] is None

        # 스레드 3: synthetic=True
        assert results[3]["is_synthetic"] is True
        assert results[3]["session_id"] == "thread-3"

    def test_concurrent_access(self):
        """동시 접근 시 스레드 안전성."""
        errors = []

        def worker(worker_id: int):
            try:
                for i in range(100):
                    with TestModeContext.start(session_id=f"worker-{worker_id}-{i}"):
                        assert TestModeContext.is_synthetic() is True
                        expected = f"worker-{worker_id}-{i}"
                        actual = TestModeContext.get_session_id()
                        if actual != expected:
                            errors.append(f"Expected {expected}, got {actual}")
            except Exception as e:
                errors.append(str(e))

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(worker, i) for i in range(10)]
            for f in futures:
                f.result()

        assert len(errors) == 0, f"Thread safety errors: {errors}"


class TestSyntheticContextFunction:
    """synthetic_context 편의 함수 테스트."""

    def test_synthetic_context_function(self):
        """synthetic_context 함수 테스트."""
        assert is_synthetic_context() is False

        with synthetic_context(session_id="func-test"):
            assert is_synthetic_context() is True
            assert get_synthetic_session_id() == "func-test"

        assert is_synthetic_context() is False


# =============================================================================
# 문서 137 섹션 5.1 명시 테스트
# =============================================================================


def test_synthetic_context_propagation():
    """
    문서 137 섹션 5.1 명시 테스트: ContextVar 전파 확인.

    TestModeContext가 ContextVar를 통해 합성 요청 상태를
    정확히 전파하고 복원하는지 검증합니다.
    """
    # 초기 상태: 합성 모드 아님
    assert TestModeContext.is_synthetic() is False
    assert is_synthetic_context() is False

    # 컨텍스트 진입: 합성 모드 활성화
    with TestModeContext.start(session_id="propagation-test"):
        assert TestModeContext.is_synthetic() is True
        assert is_synthetic_context() is True
        assert TestModeContext.get_session_id() == "propagation-test"
        assert get_synthetic_session_id() == "propagation-test"

    # 컨텍스트 종료: 원래 상태로 복원
    assert TestModeContext.is_synthetic() is False
    assert is_synthetic_context() is False
    assert TestModeContext.get_session_id() is None
