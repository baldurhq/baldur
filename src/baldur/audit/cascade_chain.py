"""
Cascade Chain 검증 로직.

체인 깊이 검사 및 순환 참조 감지 기능을 제공합니다.

Features:
- check_chain_depth: 체인 깊이 검사
- detect_cycle: 순환 참조 감지

Reference:
    docs/baldur/middleware_system/76_CASCADE_EVENT_AUDIT.md
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from baldur.audit.cascade_config import (
    DEFAULT_CASCADE_CHAIN_CONFIG,
    CascadeChainConfig,
)
from baldur.audit.cascade_exceptions import (
    CascadeChainDepthExceeded,
    CascadeCycleDetected,
)

if TYPE_CHECKING:
    from baldur.audit.cascade_event import CascadeEffect

logger = structlog.get_logger()


# =============================================================================
# 체인 깊이 검사
# =============================================================================


def check_chain_depth(
    current_depth: int,
    cascade_id: str,
    namespace: str,
    trigger_type: str,
    config: CascadeChainConfig | None = None,
) -> None:
    """
    체인 깊이 검사.

    현재 체인 깊이가 설정된 임계치를 초과했는지 확인합니다.

    Args:
        current_depth: 현재 체인 깊이
        cascade_id: Cascade ID
        namespace: 네임스페이스
        trigger_type: 트리거 유형
        config: 체인 설정 (None이면 기본값 사용)

    Raises:
        CascadeChainDepthExceeded: 깊이 초과 시 (block_on_exceed=True)
    """
    if config is None:
        config = DEFAULT_CASCADE_CHAIN_CONFIG

    # 경고 임계치 체크
    if current_depth >= config.warn_at_depth:
        logger.warning(
            "cascade_chain.depth_warning",
            current_depth=current_depth,
            warn_at_depth=config.warn_at_depth,
            cascade_id=cascade_id,
            namespace=namespace,
            trigger_type=trigger_type,
        )

    # 최대 깊이 체크
    if current_depth >= config.max_chain_depth:
        logger.error(
            "cascade_chain.depth_exceeded",
            current_depth=current_depth,
            max_chain_depth=config.max_chain_depth,
            cascade_id=cascade_id,
        )

        # 메트릭 기록 (있는 경우)
        _increment_depth_exceeded_metric(namespace, trigger_type)

        if config.block_on_exceed:
            raise CascadeChainDepthExceeded(
                depth=current_depth,
                max_depth=config.max_chain_depth,
                cascade_id=cascade_id,
            )
        logger.error(
            "cascade_chain.depth_exceeded_blocking",
            current_depth=current_depth,
            max_chain_depth=config.max_chain_depth,
        )


# 메트릭 캐시 (중복 등록 방지)
_CASCADE_CHAIN_DEPTH_EXCEEDED = None
_CASCADE_CYCLE_DETECTED = None


def _get_depth_exceeded_counter():
    """체인 깊이 초과 Counter 싱글톤 반환."""
    global _CASCADE_CHAIN_DEPTH_EXCEEDED
    if _CASCADE_CHAIN_DEPTH_EXCEEDED is None:
        try:
            from baldur.metrics.registry import get_or_create_counter

            _CASCADE_CHAIN_DEPTH_EXCEEDED = get_or_create_counter(
                "baldur_cascade_chain_depth_exceeded_total",
                "Number of times cascade chain depth was exceeded",
                ["namespace", "trigger_type"],
            )
        except ImportError:
            pass
    return _CASCADE_CHAIN_DEPTH_EXCEEDED


def _get_cycle_detected_counter():
    """순환 참조 감지 Counter 싱글톤 반환."""
    global _CASCADE_CYCLE_DETECTED
    if _CASCADE_CYCLE_DETECTED is None:
        try:
            from baldur.metrics.registry import get_or_create_counter

            _CASCADE_CYCLE_DETECTED = get_or_create_counter(
                "baldur_cascade_cycle_detected_total",
                "Number of times a cascade cycle was detected",
                ["namespace"],
            )
        except ImportError:
            pass
    return _CASCADE_CYCLE_DETECTED


def _increment_depth_exceeded_metric(namespace: str, trigger_type: str) -> None:
    """체인 깊이 초과 메트릭 증가 (선택적)."""
    counter = _get_depth_exceeded_counter()
    if counter:
        counter.labels(
            namespace=namespace,
            trigger_type=trigger_type,
        ).inc()


# =============================================================================
# 순환 참조 감지
# =============================================================================


def detect_cycle(
    effects: list[CascadeEffect],
    trigger_event_id: str,
) -> list[str] | None:
    """
    순환 참조 감지.

    효과 목록에서 순환 참조(A → B → A)가 있는지 확인합니다.

    Args:
        effects: 효과 목록
        trigger_event_id: 트리거 이벤트 ID

    Returns:
        순환 경로 (이벤트 ID 목록), 없으면 None

    Example:
        >>> effects = [
        ...     CascadeEffect(event_id="A", caused_by="trigger", ...),
        ...     CascadeEffect(event_id="B", caused_by="A", ...),
        ...     CascadeEffect(event_id="C", caused_by="B", ...),
        ...     CascadeEffect(event_id="A", caused_by="C", ...),  # 순환!
        ... ]
        >>> cycle = detect_cycle(effects, "trigger")
        >>> print(cycle)  # ["A", "B", "C", "A"]
    """
    if not effects:
        return None

    # 그래프 구축: event_id -> caused_by
    graph: dict[str, str | None] = {trigger_event_id: None}
    for effect in effects:
        graph[effect.event_id] = effect.caused_by

    # 각 효과를 원인으로 하는 다음 효과들 매핑
    children: dict[str, list[str]] = {}
    for effect in effects:
        caused_by = effect.caused_by
        if caused_by not in children:
            children[caused_by] = []
        children[caused_by].append(effect.event_id)

    # DFS로 순환 감지
    visited: set[str] = set()
    path: list[str] = []

    def dfs(node: str) -> list[str] | None:
        if node in path:
            # 순환 발견
            cycle_start = path.index(node)
            return path[cycle_start:] + [node]

        if node in visited:
            return None

        visited.add(node)
        path.append(node)

        # 이 노드를 원인으로 하는 효과들 탐색
        for child in children.get(node, []):
            cycle = dfs(child)
            if cycle:
                return cycle

        path.pop()
        return None

    return dfs(trigger_event_id)


def check_and_raise_cycle(
    effects: list[CascadeEffect],
    trigger_event_id: str,
    cascade_id: str,
    namespace: str,
    config: CascadeChainConfig | None = None,
) -> None:
    """
    순환 참조 검사 및 예외 발생.

    Args:
        effects: 효과 목록
        trigger_event_id: 트리거 이벤트 ID
        cascade_id: Cascade ID
        namespace: 네임스페이스
        config: 체인 설정 (None이면 기본값 사용)

    Raises:
        CascadeCycleDetected: 순환 참조 감지 시
    """
    if config is None:
        config = DEFAULT_CASCADE_CHAIN_CONFIG

    if not config.detect_cycles:
        return

    cycle_path = detect_cycle(effects, trigger_event_id)

    if cycle_path:
        logger.error(
            "cascade_chain.cycle_detected",
            cycle_path=cycle_path,
            cascade_id=cascade_id,
            namespace=namespace,
        )

        # 메트릭 기록 (있는 경우)
        _increment_cycle_detected_metric(namespace)

        raise CascadeCycleDetected(
            cycle_path=cycle_path,
            cascade_id=cascade_id,
        )


def _increment_cycle_detected_metric(namespace: str) -> None:
    """순환 참조 감지 메트릭 증가 (선택적)."""
    counter = _get_cycle_detected_counter()
    if counter:
        counter.labels(namespace=namespace).inc()


# =============================================================================
# 통합 검증 함수
# =============================================================================


def validate_cascade_chain(
    effects: list[CascadeEffect],
    trigger_event_id: str,
    cascade_id: str,
    namespace: str,
    current_depth: int,
    trigger_type: str,
    config: CascadeChainConfig | None = None,
) -> None:
    """
    Cascade 체인 전체 검증.

    깊이 검사와 순환 참조 감지를 모두 수행합니다.

    Args:
        effects: 효과 목록
        trigger_event_id: 트리거 이벤트 ID
        cascade_id: Cascade ID
        namespace: 네임스페이스
        current_depth: 현재 체인 깊이
        trigger_type: 트리거 유형
        config: 체인 설정 (None이면 기본값 사용)

    Raises:
        CascadeChainDepthExceeded: 깊이 초과 시
        CascadeCycleDetected: 순환 참조 감지 시
    """
    if config is None:
        config = DEFAULT_CASCADE_CHAIN_CONFIG

    # 1. 깊이 검사
    check_chain_depth(
        current_depth=current_depth,
        cascade_id=cascade_id,
        namespace=namespace,
        trigger_type=trigger_type,
        config=config,
    )

    # 2. 순환 참조 감지
    check_and_raise_cycle(
        effects=effects,
        trigger_event_id=trigger_event_id,
        cascade_id=cascade_id,
        namespace=namespace,
        config=config,
    )
