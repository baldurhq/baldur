"""
Causation Context - 인과관계 추적 컨텍스트.

비동기 경계(Celery/Kafka)에서 인과관계 정보를 안전하게 전파합니다.

Features:
- contextvars 기반 스레드/async 안전한 컨텍스트 관리
- Celery 태스크 간 인과관계 정보 전파
- cascade_id, parent_event_id, chain_depth 추적

Usage:
    # 새 Cascade 시작
    with CausationContext.start_cascade(namespace="seoul") as ctx:
        print(f"Cascade ID: {ctx.cascade_id}")
        do_work()

    # Celery 태스크 호출 시
    my_task.apply_async(
        args=[...],
        headers=get_causation_for_celery(),
    )

    # Celery 태스크 내에서 복원
    @shared_task(bind=True)
    def my_task(self, ...):
        with restore_causation_from_celery(self.request.headers or {}):
            do_work()

Reference:
    docs/baldur/middleware_system/76_CASCADE_EVENT_AUDIT.md
    context/actor_context.py (패턴 참조)
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import structlog

from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# =============================================================================
# X-Test Causation ID 프리픽스 상수 및 헬퍼
# =============================================================================


XTEST_CAUSATION_PREFIX = "XTC-"
"""X-Test-Mode causation ID 프리픽스. 로그에서 테스트 요청 식별용."""


def _get_xtest_id_prefix() -> str:
    """
    X-Test-Mode 여부에 따라 ID 프리픽스 반환.

    TestModeContext.is_synthetic()이 True이면 'XTC-' 반환,
    그렇지 않으면 빈 문자열 반환.

    Returns:
        'XTC-' (X-Test-Mode) 또는 '' (운영 모드)
    """
    try:
        from baldur.core.test_mode_context import TestModeContext

        if TestModeContext.is_synthetic():
            return XTEST_CAUSATION_PREFIX
    except ImportError:
        pass
    return ""


def is_xtest_id(causation_id: str) -> bool:
    """
    주어진 ID가 X-Test causation ID인지 확인.

    Args:
        causation_id: 확인할 causation ID (cascade_id 또는 event_id)

    Returns:
        True if ID가 XTC- 프리픽스로 시작함

    Examples:
        >>> is_xtest_id("XTC-cascade-a1b2c3d4e5f6")
        True
        >>> is_xtest_id("cascade-a1b2c3d4e5f6")
        False
    """
    return causation_id.startswith(XTEST_CAUSATION_PREFIX)


def normalize_causation_id(causation_id: str) -> str:
    """
    Causation ID에서 XTC- 프리픽스 제거.

    역호환성을 위해 기존 ID 파싱 로직에서 사용합니다.
    프리픽스가 없는 ID는 그대로 반환합니다.

    Args:
        causation_id: 정규화할 causation ID

    Returns:
        XTC- 프리픽스가 제거된 순수 ID

    Examples:
        >>> normalize_causation_id("XTC-cascade-a1b2c3d4e5f6")
        'cascade-a1b2c3d4e5f6'
        >>> normalize_causation_id("cascade-a1b2c3d4e5f6")
        'cascade-a1b2c3d4e5f6'
    """
    if is_xtest_id(causation_id):
        return causation_id[len(XTEST_CAUSATION_PREFIX) :]
    return causation_id


# =============================================================================
# Celery 헤더 상수
# =============================================================================


CELERY_HEADER_CASCADE_ID = "x-baldur-cascade-id"
"""Celery 메시지 헤더: Cascade ID."""

CELERY_HEADER_PARENT_EVENT = "x-baldur-parent-event"
"""Celery 메시지 헤더: 부모 이벤트 ID."""

CELERY_HEADER_CHAIN_DEPTH = "x-baldur-chain-depth"
"""Celery 메시지 헤더: 체인 깊이."""

CELERY_HEADER_NAMESPACE = "x-baldur-namespace"
"""Celery 메시지 헤더: 네임스페이스."""


# Kafka 헤더 (동일 구조)
KAFKA_HEADER_PREFIX = "baldur."
"""Kafka 메시지 헤더 접두사."""


# =============================================================================
# CausationInfo
# =============================================================================


@dataclass
class CausationInfo(SerializableMixin):
    """
    인과관계 추적 정보.

    contextvars를 사용하여 스레드/async 안전을 보장합니다.

    Attributes:
        cascade_id: 현재 Cascade Event ID
        parent_event_id: 부모 이벤트 ID (인과관계 체인)
        chain_depth: 현재 체인 깊이 (순환 참조 방지용)
        namespace: 네임스페이스
        metadata: 추가 메타데이터

    Code reference:
        context/actor_context.py#L48 (_current_actor ContextVar 패턴)
    """

    cascade_id: str
    """현재 Cascade Event ID."""

    parent_event_id: str
    """부모 이벤트 ID (인과관계 체인)."""

    chain_depth: int = 0
    """현재 체인 깊이 (순환 참조 방지용)."""

    namespace: str = "global"
    """네임스페이스."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """추가 메타데이터."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CausationInfo:
        """역직렬화 (수신 측 복원용)."""
        return cls(
            cascade_id=data.get("cascade_id", ""),
            parent_event_id=data.get("parent_event_id", ""),
            chain_depth=data.get("chain_depth", 0),
            namespace=data.get("namespace", "global"),
            metadata=data.get("metadata", {}),
        )


# =============================================================================
# ContextVar 선언
# =============================================================================


_current_causation: ContextVar[CausationInfo | None] = ContextVar(
    "current_causation", default=None
)
"""현재 인과관계 컨텍스트 (actor_context.py 패턴 준수)."""


# =============================================================================
# CausationContext
# =============================================================================


class CausationContext:
    """
    인과관계 컨텍스트 관리자.

    Python의 contextvars를 사용하여 스레드 및 async 환경에서
    안전하게 인과관계 정보를 추적합니다.

    Usage:
        # 새 Cascade 시작
        with CausationContext.start_cascade(namespace="seoul") as ctx:
            print(f"Cascade: {ctx.cascade_id}")
            # ctx.cascade_id 사용 가능
            do_work()

        # 기존 Cascade 계속 (비동기 경계 복원)
        with CausationContext.continue_cascade(causation_info):
            do_work()

        # 현재 컨텍스트 조회
        info = CausationContext.get_current()
        if info:
            print(f"Current cascade: {info.cascade_id}")

    Code reference:
        context/actor_context.py (ActorContext 패턴)
    """

    @classmethod
    @contextmanager
    def start_cascade(
        cls,
        namespace: str = "global",
        trigger_event_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Generator[CausationInfo, None, None]:
        """
        새 Cascade 시작.

        X-Test-Mode에서는 모든 ID에 XTC- 프리픽스가 자동 추가됩니다.

        Args:
            namespace: 네임스페이스
            trigger_event_id: 트리거 이벤트 ID (없으면 자동 생성)
            metadata: 추가 메타데이터

        Yields:
            CausationInfo 인스턴스
        """
        prefix = _get_xtest_id_prefix()
        cascade_id = f"{prefix}cascade-{uuid.uuid4().hex[:12]}"
        event_id = trigger_event_id or f"{prefix}evt-{uuid.uuid4().hex[:8]}"

        info = CausationInfo(
            cascade_id=cascade_id,
            parent_event_id=event_id,
            chain_depth=0,
            namespace=namespace,
            metadata=metadata or {},
        )

        token = _current_causation.set(info)
        try:
            logger.debug(
                "causation_context.started_cascade",
                cascade_id=cascade_id,
                namespace=namespace,
            )
            yield info
        finally:
            _current_causation.reset(token)
            logger.debug(
                "causation_context.ended_cascade",
                cascade_id=cascade_id,
            )

    @classmethod
    @contextmanager
    def start_system_cascade(
        cls,
        source: str,
        namespace: str = "global",
        metadata: dict[str, Any] | None = None,
    ) -> Generator[CausationInfo, None, None]:
        """
        시스템 트리거용 Cascade 시작 (Celery Beat / Management Command).

        API 요청이 아닌 시스템 자동화 작업의 인과관계 추적에 사용합니다.
        trigger_event_id를 SYSTEM_ROOT_{source}_{uuid} 형식으로 생성합니다.
        X-Test-Mode에서는 XTC- 프리픽스가 자동 추가됩니다.

        Args:
            source: 트리거 소스 (celery_beat, management_cmd, cron, scheduler)
            namespace: 네임스페이스
            metadata: 추가 메타데이터

        Yields:
            CausationInfo 인스턴스

        Examples:
            with CausationContext.start_system_cascade(source="celery_beat") as ctx:
                process_scheduled_task()
                # ctx.parent_event_id = "SYSTEM_ROOT_celery_beat_{uuid}"
        """
        prefix = _get_xtest_id_prefix()
        system_event_id = f"{prefix}SYSTEM_ROOT_{source}_{uuid.uuid4().hex[:8]}"

        with cls.start_cascade(
            namespace=namespace,
            trigger_event_id=system_event_id,
            metadata={**(metadata or {}), "system_source": source},
        ) as ctx:
            logger.debug(
                "causation_context.started_system_cascade",
                source=source,
                system_event_id=system_event_id,
            )
            yield ctx

    @classmethod
    @contextmanager
    def continue_cascade(
        cls,
        info: CausationInfo,
        increment_depth: bool = True,
    ) -> Generator[CausationInfo, None, None]:
        """
        기존 Cascade 계속 (비동기 경계 복원).

        Args:
            info: 복원할 CausationInfo
            increment_depth: 체인 깊이 증가 여부

        Yields:
            CausationInfo 인스턴스 (깊이 증가됨)
        """
        new_depth = info.chain_depth + 1 if increment_depth else info.chain_depth

        continued_info = CausationInfo(
            cascade_id=info.cascade_id,
            parent_event_id=info.parent_event_id,
            chain_depth=new_depth,
            namespace=info.namespace,
            metadata=dict(info.metadata),  # 복사
        )

        token = _current_causation.set(continued_info)
        try:
            logger.debug(
                "causation_context.continued_cascade",
                cascade_info_id=info.cascade_id,
                new_depth=new_depth,
            )
            yield continued_info
        finally:
            _current_causation.reset(token)

    @classmethod
    def get_current(cls) -> CausationInfo | None:
        """
        현재 컨텍스트 조회.

        Returns:
            현재 CausationInfo 또는 None
        """
        return _current_causation.get()

    @classmethod
    def is_set(cls) -> bool:
        """
        컨텍스트 설정 여부 확인.

        Returns:
            컨텍스트가 설정되어 있으면 True
        """
        return _current_causation.get() is not None

    @classmethod
    def get_current_cascade_id(cls) -> str | None:
        """
        현재 Cascade ID 조회.

        Returns:
            현재 cascade_id 또는 None
        """
        info = cls.get_current()
        return info.cascade_id if info else None

    @classmethod
    def get_current_depth(cls) -> int:
        """
        현재 체인 깊이 조회.

        Returns:
            현재 chain_depth (컨텍스트 없으면 0)
        """
        info = cls.get_current()
        return info.chain_depth if info else 0

    @classmethod
    @contextmanager
    def set_parent_event(
        cls,
        new_event_id: str,
    ) -> Generator[CausationInfo, None, None]:
        """
        부모 이벤트 ID 변경.

        현재 컨텍스트 내에서 새 효과를 기록할 때 사용합니다.

        Args:
            new_event_id: 새 부모 이벤트 ID

        Yields:
            업데이트된 CausationInfo
        """
        current = cls.get_current()
        if not current:
            raise RuntimeError("No causation context set")

        updated_info = CausationInfo(
            cascade_id=current.cascade_id,
            parent_event_id=new_event_id,
            chain_depth=current.chain_depth,
            namespace=current.namespace,
            metadata=dict(current.metadata),
        )

        token = _current_causation.set(updated_info)
        try:
            yield updated_info
        finally:
            _current_causation.reset(token)


# =============================================================================
# Celery 전파 함수
# =============================================================================


def get_causation_for_celery() -> dict[str, str]:
    """
    Celery Task 호출 시 전달할 causation 헤더 생성.

    Usage:
        my_task.apply_async(
            args=[...],
            headers=get_causation_for_celery(),
        )

    Returns:
        Celery 메시지 헤더 딕셔너리

    Code reference:
        context/actor_context.py (get_actor_for_celery 패턴)
    """
    info = CausationContext.get_current()
    if not info:
        return {}

    return {
        CELERY_HEADER_CASCADE_ID: info.cascade_id,
        CELERY_HEADER_PARENT_EVENT: info.parent_event_id,
        CELERY_HEADER_CHAIN_DEPTH: str(info.chain_depth),
        CELERY_HEADER_NAMESPACE: info.namespace,
    }


@contextmanager
def restore_causation_from_celery(
    headers: dict[str, str],
) -> Generator[CausationInfo | None, None, None]:
    """
    Celery Task에서 causation 복원.

    Usage:
        @shared_task(bind=True)
        def my_task(self, ...):
            with restore_causation_from_celery(self.request.headers or {}):
                do_work()

    Args:
        headers: Celery request headers

    Yields:
        복원된 CausationInfo 또는 None

    Code reference:
        context/actor_context.py (restore_actor_from_celery 패턴)
    """
    cascade_id = headers.get(CELERY_HEADER_CASCADE_ID)

    if not cascade_id:
        yield None
        return

    info = CausationInfo(
        cascade_id=cascade_id,
        parent_event_id=headers.get(CELERY_HEADER_PARENT_EVENT, ""),
        chain_depth=int(headers.get(CELERY_HEADER_CHAIN_DEPTH, "0")),
        namespace=headers.get(CELERY_HEADER_NAMESPACE, "global"),
        metadata={
            "restored_from": "celery",
            "restored_at": utc_now().isoformat(),
        },
    )

    with CausationContext.continue_cascade(info) as ctx:
        yield ctx


def get_causation_for_kafka() -> dict[str, bytes]:
    """
    Kafka 메시지 전송 시 전달할 causation 헤더 생성.

    Usage:
        producer.send(
            topic="my-topic",
            value=message,
            headers=list(get_causation_for_kafka().items()),
        )

    Returns:
        Kafka 메시지 헤더 딕셔너리 (bytes 값)
    """
    info = CausationContext.get_current()
    if not info:
        return {}

    return {
        f"{KAFKA_HEADER_PREFIX}cascade_id": info.cascade_id.encode("utf-8"),
        f"{KAFKA_HEADER_PREFIX}parent_event": info.parent_event_id.encode("utf-8"),
        f"{KAFKA_HEADER_PREFIX}chain_depth": str(info.chain_depth).encode("utf-8"),
        f"{KAFKA_HEADER_PREFIX}namespace": info.namespace.encode("utf-8"),
    }


@contextmanager
def restore_causation_from_kafka(
    headers: list | None = None,
) -> Generator[CausationInfo | None, None, None]:
    """
    Kafka Consumer에서 causation 복원.

    Usage:
        for message in consumer:
            with restore_causation_from_kafka(message.headers):
                process_message(message)

    Args:
        headers: Kafka 메시지 헤더 리스트 [(key, value), ...]

    Yields:
        복원된 CausationInfo 또는 None
    """
    if not headers:
        yield None
        return

    # 헤더를 딕셔너리로 변환
    header_dict = {}
    for key, value in headers:
        if key.startswith(KAFKA_HEADER_PREFIX):
            short_key = key[len(KAFKA_HEADER_PREFIX) :]
            header_dict[short_key] = (
                value.decode("utf-8") if isinstance(value, bytes) else value
            )

    cascade_id = header_dict.get("cascade_id")
    if not cascade_id:
        yield None
        return

    info = CausationInfo(
        cascade_id=cascade_id,
        parent_event_id=header_dict.get("parent_event", ""),
        chain_depth=int(header_dict.get("chain_depth", "0")),
        namespace=header_dict.get("namespace", "global"),
        metadata={
            "restored_from": "kafka",
            "restored_at": utc_now().isoformat(),
        },
    )

    with CausationContext.continue_cascade(info) as ctx:
        yield ctx
