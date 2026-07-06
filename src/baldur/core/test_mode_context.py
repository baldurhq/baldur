"""
Test Mode Context - Synthetic Request Tracking.

X-Test-Mode 및 Chaos 실험에서 생성되는 합성 요청을 추적하기 위한
글로벌 컨텍스트 시스템입니다.

운영 메트릭/에러 버짓 오염 방지:
- 모든 메트릭에 is_synthetic 레이블 자동 추가
- 에러 버짓 계산 시 합성 에러 자동 제외
- Redis 키 Namespace 자동 분리 (xtest: prefix)

Usage:
    # Context Manager 방식 (권장)
    with TestModeContext.start(session_id="test-123"):
        # 이 블록 내의 모든 작업은 합성으로 태깅됨
        record_dlq_item_created(domain="payment", failure_type="timeout")

    # 수동 제어 방식
    TestModeContext.enter_synthetic_mode(session_id="test-123")
    try:
        # 합성 요청 처리
        pass
    finally:
        TestModeContext.exit_synthetic_mode()

    # 현재 상태 조회
    if TestModeContext.is_synthetic():
        print(f"Synthetic session: {TestModeContext.get_session_id()}")
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token

import structlog

logger = structlog.get_logger()

# ContextVar 정의: 현재 요청이 합성(테스트) 요청인지 여부
_is_synthetic_request: ContextVar[bool] = ContextVar(
    "is_synthetic_request",
    default=False,
)

# ContextVar 정의: 테스트 세션 식별자
_synthetic_session_id: ContextVar[str | None] = ContextVar(
    "synthetic_session_id",
    default=None,
)


class TestModeContext:
    """
    합성 요청 컨텍스트 관리자.

    X-Test-Mode 및 Chaos 실험에서 발생하는 합성 요청을 추적합니다.

    Thread-safe: ContextVar를 사용하여 각 스레드/코루틴마다 독립적인 상태 유지.

    Attributes:
        _token_synthetic: is_synthetic ContextVar 복원 토큰
        _token_session: session_id ContextVar 복원 토큰
    """

    _token_synthetic: Token[bool] | None = None
    _token_session: Token[str | None] | None = None

    @classmethod
    @contextmanager
    def start(
        cls,
        session_id: str | None = None,
    ) -> Generator[TestModeContext, None, None]:
        """
        합성 모드 컨텍스트 시작 (권장 방식).

        Context Manager로 사용하면 블록 종료 시 자동 복원됩니다.

        Args:
            session_id: 테스트 세션 식별자 (X-Test-Session 헤더 등)

        Yields:
            TestModeContext 인스턴스

        Example:
            with TestModeContext.start(session_id="xtest-abc123"):
                # 합성 요청으로 처리됨
                pass
        """
        token_synthetic = _is_synthetic_request.set(True)
        token_session = _synthetic_session_id.set(session_id)

        logger.debug(
            "test_mode_context.entered_synthetic_mode",
            session_id=session_id,
        )

        try:
            yield cls()
        finally:
            _is_synthetic_request.reset(token_synthetic)
            _synthetic_session_id.reset(token_session)

            logger.debug(
                "test_mode_context.exited_synthetic_mode",
                session_id=session_id,
            )

    @classmethod
    def enter_synthetic_mode(
        cls,
        session_id: str | None = None,
    ) -> None:
        """
        합성 모드 수동 진입.

        exit_synthetic_mode()를 반드시 호출해야 합니다.
        Context Manager(start()) 사용을 권장합니다.

        Args:
            session_id: 테스트 세션 식별자
        """
        cls._token_synthetic = _is_synthetic_request.set(True)
        cls._token_session = _synthetic_session_id.set(session_id)

        logger.debug(
            "test_mode_context.manual_enter_synthetic_mode",
            session_id=session_id,
        )

    @classmethod
    def exit_synthetic_mode(cls) -> None:
        """
        합성 모드 수동 종료.

        enter_synthetic_mode()로 진입한 경우에만 호출하세요.
        """
        if cls._token_synthetic is not None:
            _is_synthetic_request.reset(cls._token_synthetic)
            cls._token_synthetic = None

        if cls._token_session is not None:
            _synthetic_session_id.reset(cls._token_session)
            cls._token_session = None

        logger.debug("test_mode_context.manual_exit_synthetic_mode")

    @classmethod
    def is_synthetic(cls) -> bool:
        """
        현재 컨텍스트가 합성 요청인지 확인.

        Returns:
            True if 현재 합성/테스트 요청 컨텍스트 내에 있음
        """
        return _is_synthetic_request.get()

    @classmethod
    def get_session_id(cls) -> str | None:
        """
        현재 합성 세션 ID 반환.

        Returns:
            세션 ID 또는 None (합성 모드가 아니거나 ID가 없는 경우)
        """
        return _synthetic_session_id.get()

    @classmethod
    def get_synthetic_label_value(cls) -> str:
        """
        메트릭 레이블용 합성 여부 문자열 반환.

        Prometheus 레이블에서 사용하기 위한 문자열 값.

        Returns:
            "true" if 합성 요청, "false" otherwise
        """
        return "true" if cls.is_synthetic() else "false"


# 편의 함수들 (모듈 레벨)


def is_synthetic_context() -> bool:
    """현재 합성 요청 컨텍스트인지 확인 (편의 함수)."""
    return TestModeContext.is_synthetic()


def get_synthetic_session_id() -> str | None:
    """현재 합성 세션 ID 반환 (편의 함수)."""
    return TestModeContext.get_session_id()


@contextmanager
def synthetic_context(
    session_id: str | None = None,
) -> Generator[TestModeContext, None, None]:
    """
    합성 모드 컨텍스트 (편의 함수).

    TestModeContext.start()의 alias입니다.

    Args:
        session_id: 테스트 세션 식별자

    Returns:
        Context manager yielding TestModeContext
    """
    with TestModeContext.start(session_id=session_id) as ctx:
        yield ctx
