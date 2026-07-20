"""
Postmortem root cause analysis utilities.

Pure functions that build the trigger, detection, resolution, and
root_cause_hypothesis fields per the Google SRE standard.
Usable without a Django dependency.
"""

from __future__ import annotations


def extract_trigger_info(timeline: list) -> dict | None:
    """
    Extract failure trigger information from a timeline.

    Collects trigger information from the first CB OPEN event.

    Args:
        timeline: Event timeline list

    Returns:
        Trigger information dict, or None when there is no data
    """
    if not timeline:
        return None

    # Find the first CB OPEN event (half_open excluded)
    for event in timeline:
        event_type = event.get("event_type", "").lower()
        # Skip half_open/half-open and look for opened/open
        if "half_open" in event_type or "half-open" in event_type:
            continue
        if "opened" in event_type or "open" in event_type:
            details = event.get("details", {})
            error_context = details.get("error_context") or {}

            return {
                "event_type": event.get("event_type"),
                "service": details.get("service_name") or details.get("service"),
                "timestamp": event.get("timestamp"),
                "error_context": (
                    {
                        "error_type": error_context.get("error_type"),
                        "message": error_context.get("message"),
                    }
                    if error_context
                    else None
                ),
            }

    return None


def extract_detection_info(
    timeline: list, threshold_config: dict | None = None
) -> dict | None:
    """
    Extract failure detection information from a timeline.

    Collects the CB threshold breach time and the detection method.

    Args:
        timeline: Event timeline list
        threshold_config: CB threshold configuration (optional)

    Returns:
        Detection information dict, or None when there is no data
    """
    if not timeline:
        return None

    # Find the first CB OPEN event (OPEN = detection time, half_open excluded)
    for event in timeline:
        event_type = event.get("event_type", "").lower()
        # Exclude half_open/half-open
        if "half_open" in event_type or "half-open" in event_type:
            continue
        if "opened" in event_type or "open" in event_type:
            details = event.get("details", {})

            # Try to extract failure_count and threshold
            failure_count = details.get("failure_count")
            threshold = details.get("threshold") or details.get("failure_threshold")

            # Fall back to threshold_config when details omits it
            if threshold is None and threshold_config:
                threshold = threshold_config.get("failure_threshold")

            result = {
                "method": "circuit_breaker_threshold",
                "detected_at": event.get("timestamp"),
                "detector": "CircuitBreakerService",
            }

            # Add threshold information when present
            if failure_count is not None or threshold is not None:
                result["threshold_exceeded"] = {}
                if failure_count is not None:
                    result["threshold_exceeded"]["failure_count"] = failure_count
                if threshold is not None:
                    result["threshold_exceeded"]["threshold"] = threshold

            return result

    return None


def extract_resolution_info(timeline: list) -> dict | None:
    """
    Extract resolution information from a timeline.

    Collects recovery information from the CB CLOSED event.

    Args:
        timeline: Event timeline list

    Returns:
        Resolution information dict, or None when there is no data
    """
    if not timeline:
        return None

    # Track the order of CB state changes
    state_changes = []
    resolved_at = None

    for event in timeline:
        event_type = event.get("event_type", "").lower()

        # Check half_open/half-open first (takes precedence over opened)
        if "half_open" in event_type or "half-open" in event_type:
            state_changes.append("HALF_OPEN")
        elif "opened" in event_type:
            state_changes.append("OPEN")
        elif "closed" in event_type:
            state_changes.append("CLOSED")
            resolved_at = event.get("timestamp")

    if not resolved_at:
        return None

    # Build the recovery path
    recovery_path = " → ".join(state_changes) if state_changes else None

    return {
        "method": "automatic_recovery",
        "resolved_at": resolved_at,
        "recovery_path": recovery_path,
        "manual_intervention": False,
    }


def _extract_error_context_from_timeline(
    timeline: list,
) -> tuple[str | None, str | None, str | None]:
    """
    Extract the error context of the first OPEN event in a timeline.

    Returns:
        (error_type, error_message, first_service) tuple
    """
    for event in timeline:
        event_type = event.get("event_type", "").lower()
        if "opened" in event_type or "open" in event_type:
            details = event.get("details", {})
            error_context = details.get("error_context") or {}
            return (
                error_context.get("error_type", ""),
                error_context.get("message", ""),
                details.get("service_name") or details.get("service"),
            )
    return None, None, None


def _match_error_pattern(
    error_type: str | None,
    error_message: str | None,
    keywords: list[str],
) -> bool:
    """Match keyword patterns against the error type/message."""
    error_type_lower = (error_type or "").lower()
    error_message_lower = (error_message or "").lower()
    return any(kw in error_type_lower or kw in error_message_lower for kw in keywords)


def _build_hypothesis_from_error_pattern(
    error_type: str | None,
    error_message: str | None,
    first_service: str | None,
) -> str | None:
    """Build a hypothesis from the error pattern."""
    # DB-related error patterns
    db_keywords = [
        "database",
        "db",
        "connection",
        "sql",
        "postgresql",
        "mysql",
        "redis",
    ]
    if _match_error_pattern(error_type, error_message, db_keywords):
        service_info = f": {first_service}" if first_service else ""
        return f"Database connection issue{service_info}"

    # Timeout error patterns
    timeout_keywords = ["timeout", "timed out", "timeouterror"]
    if _match_error_pattern(error_type, error_message, timeout_keywords):
        service_info = f": {first_service}" if first_service else ""
        return f"Network latency or service overload{service_info}"

    return None


def generate_root_cause_hypothesis(
    timeline: list,
    affected_services: list,
) -> str | None:
    """
    Build a root cause hypothesis from the timeline and the affected services.

    Pattern-based classification:
    - Single service OPEN -> "Single service failure: {service}"
    - Multiple services OPEN -> "Possible infrastructure-wide failure"
    - DB-related error -> "Database connection issue"
    - Timeout error -> "Network latency or service overload"

    Args:
        timeline: Event timeline list
        affected_services: List of affected services

    Returns:
        Root cause hypothesis string, or None when no hypothesis can be built
    """
    if not timeline and not affected_services:
        return None

    # Collect the error context
    error_type, error_message, first_service = _extract_error_context_from_timeline(
        timeline
    )

    # Multi-service failure decision
    if len(affected_services) > 1:
        return "Possible infrastructure-wide failure - common cause analysis required"

    # Build the hypothesis from the error pattern
    pattern_hypothesis = _build_hypothesis_from_error_pattern(
        error_type, error_message, first_service
    )
    if pattern_hypothesis:
        return pattern_hypothesis

    # Single service failure
    service_name = first_service or (
        affected_services[0] if affected_services else "unknown"
    )
    error_info = f" - {error_type} detected" if error_type else ""

    return f"Single service failure: {service_name}{error_info}"


def build_postmortem_root_cause_fields(
    timeline: list,
    affected_services: list,
    threshold_config: dict | None = None,
) -> dict:
    """
    Build the root cause fields to add to a post-mortem.

    Args:
        timeline: Event timeline list
        affected_services: List of affected services
        threshold_config: CB threshold configuration (optional)

    Returns:
        Dict of the root cause fields
    """
    return {
        "trigger": extract_trigger_info(timeline),
        "detection": extract_detection_info(timeline, threshold_config),
        "resolution": extract_resolution_info(timeline),
        "root_cause_hypothesis": generate_root_cause_hypothesis(
            timeline, affected_services
        ),
    }
