"""
Postmortem dynamic action item generation utilities.

Pure functions that build dynamic actions and recommendations from timeline
events. Usable without a Django dependency.
"""

from __future__ import annotations

# Action message mapping per event type
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
    """Extract automatically performed actions from a timeline."""
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
    """Build recommendations from the analysis result."""
    recommendations = []

    # Recovery time criteria
    if duration_seconds is not None:
        if duration_seconds > 120:
            recommendations.append(
                f"Recovery time {duration_seconds:.0f}s exceeded 2 minutes - SLA review required"
            )
        elif duration_seconds > 60:
            recommendations.append(
                f"Recovery time {duration_seconds:.0f}s exceeded target (60s) - improvement review recommended"
            )

    # Multi-service failure criteria
    if len(affected_services) > 3:
        recommendations.append(
            f"Multi-service failure ({len(affected_services)} services) - common cause analysis required"
        )

    # Check whether fast fail did not occur
    has_cb_open = any("circuit_breaker_opened" in (key, "") for key, _ in seen_actions)
    has_cb_recovery = any(
        key in ("circuit_breaker_half_opened", "circuit_breaker_closed")
        for key, _ in seen_actions
    )
    if has_cb_open and not has_cb_recovery:
        recommendations.append(
            "Fast fail not triggered - circuit breaker configuration review required"
        )

    # Default recommendation
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
    Build dynamic action items and recommendations from the timeline and the
    analysis result.

    Args:
        timeline: Event timeline list
        affected_services: List of affected services
        duration_seconds: Incident duration (seconds)
        current_timestamp: Current time (ISO format), used for the default
            message

    Returns:
        tuple: (auto_actions, recommendations)
            - auto_actions: List of automatically performed actions
              (Google SRE standard structure)
            - recommendations: List of recommendation strings
    """
    # Event tracking for de-duplication (event key, service name)
    seen_actions: set[tuple[str, str]] = set()

    # Extract the automatic actions
    auto_actions = _extract_auto_actions(timeline, seen_actions)

    # Default message when there is no action
    if not auto_actions:
        auto_actions.append(
            {
                "action": "Incident recorded",
                "status": "completed",
                "timestamp": current_timestamp or "",
                "service": None,
            }
        )

    # Build the recommendations
    recommendations = _generate_recommendations(
        duration_seconds, affected_services, seen_actions
    )

    return auto_actions, recommendations
