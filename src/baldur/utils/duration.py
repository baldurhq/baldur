"""
Postmortem Duration 계산 유틸리티.

인시던트 지속 시간을 타임라인 이벤트에서 계산하는 순수 함수들.
Django 의존성 없이 사용 가능.
"""

from __future__ import annotations

from datetime import datetime


class IncidentDurationResult:
    """인시던트 지속 시간 계산 결과."""

    def __init__(
        self,
        started_at: str | None,
        resolved_at: str | None,
        duration_seconds: float | None,
        downtime_seconds: float | None = None,
        validation_seconds: float | None = None,
    ):
        """
        Args:
            started_at: 인시던트 시작 시각 (ISO 형식)
            resolved_at: 인시던트 종료 시각 (ISO 형식)
            duration_seconds: 전체 지속 시간 (OPEN → CLOSED)
            downtime_seconds: 실제 서비스 중단 시간 (OPEN → HALF_OPEN)
            validation_seconds: 복구 검증 시간 (HALF_OPEN → CLOSED)
        """
        self.started_at = started_at
        self.resolved_at = resolved_at
        self.duration_seconds = duration_seconds
        self.downtime_seconds = downtime_seconds
        self.validation_seconds = validation_seconds


def parse_iso_timestamp(timestamp: str | None) -> datetime | None:
    """
    ISO 형식 타임스탬프를 datetime으로 파싱.

    Args:
        timestamp: ISO 8601 형식 문자열 (예: "2026-01-27T14:00:00+09:00")

    Returns:
        파싱된 datetime 객체, 실패시 None
    """
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def calculate_time_diff_seconds(
    start_ts: str | None, end_ts: str | None
) -> float | None:
    """
    두 타임스탬프 간 시간 차이를 초 단위로 계산.

    Args:
        start_ts: 시작 시각 (ISO 형식)
        end_ts: 종료 시각 (ISO 형식)

    Returns:
        시간 차이 (초), 역순이거나 파싱 실패시 None
    """
    start_dt = parse_iso_timestamp(start_ts)
    end_dt = parse_iso_timestamp(end_ts)
    if start_dt and end_dt:
        try:
            diff = (end_dt - start_dt).total_seconds()
        except TypeError:
            # One timestamp is offset-naive and the other offset-aware; the two
            # cannot be compared, so the difference is undefined.
            return None
        return diff if diff >= 0 else None
    return None


def find_first_event_by_type(timeline: list, event_keywords: list[str]) -> dict | None:
    """
    타임라인에서 특정 키워드를 포함하는 첫 번째 이벤트 찾기.

    Args:
        timeline: 이벤트 리스트
        event_keywords: 검색할 키워드 목록 (소문자)

    Returns:
        일치하는 첫 번째 이벤트, 없으면 None
    """
    for event in timeline:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event_type", "")).lower()
        for keyword in event_keywords:
            if keyword in event_type:
                return event
    return None


def find_last_event_by_type(timeline: list, event_keywords: list[str]) -> dict | None:
    """
    타임라인에서 특정 키워드를 포함하는 마지막 이벤트 찾기.

    Args:
        timeline: 이벤트 리스트
        event_keywords: 검색할 키워드 목록 (소문자)

    Returns:
        일치하는 마지막 이벤트, 없으면 None
    """
    for event in reversed(timeline):
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event_type", "")).lower()
        for keyword in event_keywords:
            if keyword in event_type:
                return event
    return None


def calculate_incident_duration(
    timeline: list,
    current_time_iso: str | None = None,
) -> IncidentDurationResult:
    """
    타임라인에서 인시던트 지속 시간 세부 정보 계산.

    CB 상태별 시간 세분화:
    - duration_seconds: 전체 소요 시간 (OPEN → CLOSED)
    - downtime_seconds: 실제 서비스 중단 시간 (OPEN → HALF_OPEN)
    - validation_seconds: 복구 검증 시간 (HALF_OPEN → CLOSED)

    Args:
        timeline: 타임라인 이벤트 리스트
        current_time_iso: 현재 시각 (ISO 형식), CLOSED 이벤트 없을 때 사용

    Returns:
        IncidentDurationResult: 시작/종료 시각 및 세분화된 duration 정보
    """
    if not timeline:
        return IncidentDurationResult(
            started_at=None,
            resolved_at=current_time_iso,
            duration_seconds=None,
            downtime_seconds=None,
            validation_seconds=None,
        )

    # 첫 번째 CB OPEN 이벤트 찾기
    open_event = find_first_event_by_type(timeline, ["opened", "open"])
    started_at = open_event.get("timestamp") if open_event else None

    # OPEN 이벤트가 없으면 첫 번째 이벤트 사용
    if not started_at and timeline and isinstance(timeline[0], dict):
        started_at = timeline[0].get("timestamp")

    # 첫 번째 HALF_OPEN 이벤트 찾기
    half_open_event = find_first_event_by_type(timeline, ["half_open", "half-open"])
    half_open_at = half_open_event.get("timestamp") if half_open_event else None

    # 마지막 CB CLOSED 이벤트 찾기
    closed_event = find_last_event_by_type(timeline, ["closed"])
    resolved_at = closed_event.get("timestamp") if closed_event else None

    # CLOSED 이벤트가 없으면 현재 시각 사용 (진행 중 인시던트)
    if not resolved_at:
        resolved_at = current_time_iso

    # 전체 duration 계산
    duration_seconds = calculate_time_diff_seconds(started_at, resolved_at)

    # 세분화된 duration 계산
    downtime_seconds: float | None = None
    validation_seconds: float | None = None

    if half_open_at:
        # HALF_OPEN 이벤트가 있는 경우
        downtime_seconds = calculate_time_diff_seconds(started_at, half_open_at)
        validation_seconds = calculate_time_diff_seconds(half_open_at, resolved_at)
    elif duration_seconds is not None:
        # HALF_OPEN 없이 직접 CLOSED된 경우
        downtime_seconds = duration_seconds
        validation_seconds = 0.0

    return IncidentDurationResult(
        started_at=started_at,
        resolved_at=resolved_at,
        duration_seconds=duration_seconds,
        downtime_seconds=downtime_seconds,
        validation_seconds=validation_seconds,
    )
