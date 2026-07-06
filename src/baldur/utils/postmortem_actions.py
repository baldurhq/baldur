"""
Postmortem 동적 Action Items 생성 유틸리티.

타임라인 이벤트를 기반으로 동적 actions 및 recommendations를 생성하는 순수 함수.
Django 의존성 없이 사용 가능.
"""

from __future__ import annotations

# 이벤트 타입별 Action 메시지 매핑
EVENT_ACTION_MAP = {
    "circuit_breaker_opened": "Circuit Breaker transitioned to OPEN",
    "circuit_breaker_half_opened": "Circuit Breaker recovery attempt (HALF_OPEN)",
    "circuit_breaker_closed": "Circuit Breaker recovered (CLOSED)",
    "error_budget_critical": "Error budget critical threshold warning",
    "error_budget_warning": "Error budget warning level reached",
    "emergency_activated": "Emergency mode activated",
    "kill_switch_activated": "Kill switch activated",
    "dlq_item_added": "Item added to DLQ",
    "dlq_replay_blocked": "DLQ replay blocked",
}


def _extract_auto_actions(
    timeline: list,
    seen_actions: set[tuple[str, str]],
) -> list[dict]:
    """타임라인에서 자동 수행된 조치 추출."""
    auto_actions = []

    for event in timeline:
        event_type = event.get("event_type", "").lower()
        service = event.get("details", {}).get("service_name", "")
        timestamp = event.get("timestamp", "")

        for key, action_text in EVENT_ACTION_MAP.items():
            if key in event_type and (key, service) not in seen_actions:
                seen_actions.add((key, service))
                auto_actions.append(
                    {
                        "action": action_text,
                        "status": "completed",
                        "timestamp": timestamp,
                        "service": service,
                    }
                )
                break

    return auto_actions


def _generate_recommendations(
    duration_seconds: float | None,
    affected_services: list,
    seen_actions: set[tuple[str, str]],
) -> list[str]:
    """분석 결과 기반 권장 사항 생성."""
    recommendations = []

    # 복구 시간 기준
    if duration_seconds is not None:
        if duration_seconds > 120:
            recommendations.append(
                f"Recovery time {duration_seconds:.0f}s exceeded 2 minutes - SLA review required"
            )
        elif duration_seconds > 60:
            recommendations.append(
                f"Recovery time {duration_seconds:.0f}s exceeded target (60s) - improvement review recommended"
            )

    # 다중 서비스 장애 기준
    if len(affected_services) > 3:
        recommendations.append(
            f"Multi-service failure ({len(affected_services)} services) - common cause analysis required"
        )

    # Fast Fail 미발생 검사
    has_cb_open = any("circuit_breaker_opened" in (key, "") for key, _ in seen_actions)
    has_cb_recovery = any(
        key in ("circuit_breaker_half_opened", "circuit_breaker_closed")
        for key, _ in seen_actions
    )
    if has_cb_open and not has_cb_recovery:
        recommendations.append(
            "Fast fail not triggered - circuit breaker configuration review required"
        )

    # 기본 recommendation
    if not recommendations:
        recommendations.append(
            "Root cause analysis and recurrence prevention review recommended"
        )

    return recommendations


def generate_dynamic_actions(
    timeline: list,
    affected_services: list,
    duration_seconds: float | None,
    current_timestamp: str | None = None,
) -> tuple[list, list]:
    """
    타임라인과 분석 결과를 기반으로 동적 action items 및 recommendations 생성.

    Args:
        timeline: 이벤트 타임라인 리스트
        affected_services: 영향받은 서비스 리스트
        duration_seconds: 인시던트 지속 시간 (초)
        current_timestamp: 현재 시각 (ISO 형식), 기본 메시지용

    Returns:
        tuple: (auto_actions, recommendations)
            - auto_actions: 자동 수행된 조치 리스트 (Google SRE 표준 구조)
            - recommendations: 권장 사항 문자열 리스트
    """
    # 중복 방지를 위한 이벤트 추적 (이벤트 키, 서비스명)
    seen_actions: set[tuple[str, str]] = set()

    # 자동 조치 추출
    auto_actions = _extract_auto_actions(timeline, seen_actions)

    # 액션이 없으면 기본 메시지
    if not auto_actions:
        auto_actions.append(
            {
                "action": "Incident recorded",
                "status": "completed",
                "timestamp": current_timestamp or "",
                "service": None,
            }
        )

    # 권장 사항 생성
    recommendations = _generate_recommendations(
        duration_seconds, affected_services, seen_actions
    )

    return auto_actions, recommendations
