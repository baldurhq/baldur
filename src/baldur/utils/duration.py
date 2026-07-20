"""
Postmortem duration calculation utilities.

Pure functions that compute incident duration from timeline events.
Usable without a Django dependency.
"""

from __future__ import annotations

from datetime import datetime


class IncidentDurationResult:
    """Result of an incident duration calculation."""

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
            started_at: Incident start time (ISO format)
            resolved_at: Incident end time (ISO format)
            duration_seconds: Total duration (OPEN -> CLOSED)
            downtime_seconds: Actual service outage time (OPEN -> HALF_OPEN)
            validation_seconds: Recovery validation time (HALF_OPEN -> CLOSED)
        """
        self.started_at = started_at
        self.resolved_at = resolved_at
        self.duration_seconds = duration_seconds
        self.downtime_seconds = downtime_seconds
        self.validation_seconds = validation_seconds


def parse_iso_timestamp(timestamp: str | None) -> datetime | None:
    """
    Parse an ISO-format timestamp into a datetime.

    Args:
        timestamp: ISO 8601 string (e.g. "2026-01-27T14:00:00+09:00")

    Returns:
        Parsed datetime object, or None on failure
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
    Compute the difference between two timestamps in seconds.

    Args:
        start_ts: Start time (ISO format)
        end_ts: End time (ISO format)

    Returns:
        Difference in seconds, or None when reversed or unparseable
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
    Find the first event in a timeline whose type contains a keyword.

    Args:
        timeline: Event list
        event_keywords: Keywords to search for (lowercase)

    Returns:
        First matching event, or None when there is none
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
    Find the last event in a timeline whose type contains a keyword.

    Args:
        timeline: Event list
        event_keywords: Keywords to search for (lowercase)

    Returns:
        Last matching event, or None when there is none
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
    Compute detailed incident duration information from a timeline.

    Time broken down by CB state:
    - duration_seconds: Total elapsed time (OPEN -> CLOSED)
    - downtime_seconds: Actual service outage time (OPEN -> HALF_OPEN)
    - validation_seconds: Recovery validation time (HALF_OPEN -> CLOSED)

    Args:
        timeline: Timeline event list
        current_time_iso: Current time (ISO format), used when there is
            no CLOSED event

    Returns:
        IncidentDurationResult: start/end times and the duration breakdown
    """
    if not timeline:
        return IncidentDurationResult(
            started_at=None,
            resolved_at=current_time_iso,
            duration_seconds=None,
            downtime_seconds=None,
            validation_seconds=None,
        )

    # Find the first CB OPEN event
    open_event = find_first_event_by_type(timeline, ["opened", "open"])
    started_at = open_event.get("timestamp") if open_event else None

    # Fall back to the first event when there is no OPEN event
    if not started_at and timeline and isinstance(timeline[0], dict):
        started_at = timeline[0].get("timestamp")

    # Find the first HALF_OPEN event
    half_open_event = find_first_event_by_type(timeline, ["half_open", "half-open"])
    half_open_at = half_open_event.get("timestamp") if half_open_event else None

    # Find the last CB CLOSED event
    closed_event = find_last_event_by_type(timeline, ["closed"])
    resolved_at = closed_event.get("timestamp") if closed_event else None

    # Use the current time when there is no CLOSED event (ongoing incident)
    if not resolved_at:
        resolved_at = current_time_iso

    # Compute the total duration
    duration_seconds = calculate_time_diff_seconds(started_at, resolved_at)

    # Compute the duration breakdown
    downtime_seconds: float | None = None
    validation_seconds: float | None = None

    if half_open_at:
        # A HALF_OPEN event is present
        downtime_seconds = calculate_time_diff_seconds(started_at, half_open_at)
        validation_seconds = calculate_time_diff_seconds(half_open_at, resolved_at)
    elif duration_seconds is not None:
        # Closed directly without passing through HALF_OPEN
        downtime_seconds = duration_seconds
        validation_seconds = 0.0

    return IncidentDurationResult(
        started_at=started_at,
        resolved_at=resolved_at,
        duration_seconds=duration_seconds,
        downtime_seconds=downtime_seconds,
        validation_seconds=validation_seconds,
    )
