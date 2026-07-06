"""
Loki/ELK 표준 라벨 정의.

로그 시스템 친화적 라벨링을 위한 헬퍼 함수.

Loki Label Best Practice:
- 낮은 카디널리티 (Low Cardinality) 권장
- 고정 값 선호 (cluster, env, component)
- 높은 카디널리티는 라벨 대신 로그 내용으로
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

# CASCADE_EVENT 대상 이벤트 집합 (throttle/audit.py에서 import)
CASCADE_EVENT_ACTIONS: set[str] = {
    "throttle_emergency_sync",
    "throttle_cb_sync",
    "throttle_sla_critical",
    "throttle_full_stop_activated",
    "throttle_full_stop_deactivated",
}


def get_standard_labels(audit_data: dict[str, Any]) -> dict[str, str]:
    """
    감사 데이터에서 표준 라벨 추출.

    Loki Label Best Practice:
    - 낮은 카디널리티 (Low Cardinality)
    - 고정 값 선호 (cluster, env, component)
    - 높은 카디널리티는 라벨 대신 로그 내용으로

    Args:
        audit_data: 감사 데이터 딕셔너리

    Returns:
        Loki/ELK 라벨용 딕셔너리
    """
    cluster_info = audit_data.get("cluster", {})
    action = audit_data.get("action", "unknown")

    return {
        # 낮은 카디널리티 라벨
        "job": "baldur-audit",
        "component": "throttle",
        "env": cluster_info.get("environment", "production"),
        "region": cluster_info.get("region", "unknown"),
        "cluster": cluster_info.get("cluster_id", "unknown"),
        # 이벤트 분류
        "audit_action": action,
        "severity": audit_data.get("severity", "info"),
        # CASCADE_EVENT 여부
        "is_cascade": str(action in CASCADE_EVENT_ACTIONS).lower(),
    }


def get_throttle_labels(
    action: str,
    severity: str = "info",
    region: str = "unknown",
    cluster_id: str = "unknown",
    environment: str = "production",
) -> dict[str, str]:
    """
    Throttle 감사 이벤트용 표준 라벨 생성.

    Args:
        action: 감사 이벤트 타입
        severity: 심각도 (debug, info, warning, critical)
        region: 리전 정보
        cluster_id: 클러스터 ID
        environment: 환경 (production, staging, development)

    Returns:
        Loki/ELK 라벨 딕셔너리
    """
    return {
        "job": "baldur-audit",
        "component": "throttle",
        "env": environment,
        "region": region,
        "cluster": cluster_id,
        "audit_action": action,
        "severity": severity,
        "is_cascade": str(action in CASCADE_EVENT_ACTIONS).lower(),
    }


def merge_labels(
    base_labels: dict[str, str],
    custom_labels: dict[str, str] | None = None,
) -> dict[str, str]:
    """
    기본 라벨과 사용자 정의 라벨 병합.

    사용자 정의 라벨이 기본 라벨을 오버라이드함.

    Args:
        base_labels: 기본 라벨 딕셔너리
        custom_labels: 사용자 정의 라벨 (선택)

    Returns:
        병합된 라벨 딕셔너리
    """
    if custom_labels is None:
        return base_labels.copy()

    result = base_labels.copy()
    result.update(custom_labels)
    return result


def validate_labels(labels: dict[str, str]) -> tuple[bool, list[str]]:
    """
    라벨 유효성 검증.

    Loki 라벨 규칙:
    - 라벨 이름: 영문자, 숫자, _ 만 허용
    - 라벨 값: 빈 문자열 허용 안함

    Args:
        labels: 검증할 라벨 딕셔너리

    Returns:
        (유효 여부, 오류 목록) 튜플
    """
    import re

    errors = []
    label_name_pattern = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

    for name, value in labels.items():
        # 라벨 이름 검증
        if not label_name_pattern.match(name):
            errors.append(f"Invalid label name: {name}")

        # 라벨 값 검증
        if not value or not isinstance(value, str):
            errors.append(f"Invalid label value for {name}: {value}")

    return len(errors) == 0, errors


def sanitize_label_value(value: str, max_length: int = 128) -> str:
    """
    라벨 값 정제.

    특수문자 제거, 길이 제한 적용.

    Args:
        value: 원본 라벨 값
        max_length: 최대 길이 (기본 128)

    Returns:
        정제된 라벨 값
    """
    if not value:
        return "unknown"

    # 특수문자를 underscore로 대체
    import re

    sanitized = re.sub(r"[^a-zA-Z0-9_\-]", "_", value)

    # 길이 제한
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length]

    return sanitized
