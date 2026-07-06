"""
Cascade Event audit notification templates.

Notification message builder functions for Cascade Events.

Templates:
- cascade_integrity_alert: Hash Chain integrity violation alert
- cascade_depth_alert: chain depth exceeded alert
- cascade_load_shedding_alert: Load Shedding activation alert
- cascade_summary: daily Cascade Event summary

Reference:
    docs/baldur/middleware_system/76_CASCADE_EVENT_AUDIT.md
"""

from __future__ import annotations

from typing import Any

from baldur.utils.time import utc_now


def cascade_integrity_alert(
    namespace: str,
    errors: list[dict[str, Any]],
    verified_count: int,
) -> dict[str, Any]:
    """
    Build a Hash Chain integrity violation alert message.

    Args:
        namespace: namespace
        errors: list of integrity errors
        verified_count: number of verified events

    Returns:
        Alert message dictionary:
        - title: title
        - severity: severity (critical, warning, info)
        - message: body
        - details: detailed information
    """
    error_count = len(errors)

    return {
        "title": f"🔴 [CRITICAL] Cascade Hash Chain integrity violation - {namespace}",
        "severity": "critical",
        "message": (
            f"Hash Chain integrity violation detected in namespace '{namespace}'.\n"
            f"• Verified events: {verified_count}\n"
            f"• Error count: {error_count}\n\n"
            "Immediate investigation required. Cascade Event logs may have been tampered with."
        ),
        "details": {
            "namespace": namespace,
            "verified_count": verified_count,
            "error_count": error_count,
            "errors": errors[:5],  # include at most 5
            "timestamp": utc_now().isoformat(),
        },
        "actions": [
            {
                "label": "Investigate details",
                "url": f"/api/baldur/cascade/events/?namespace={namespace}",
            },
            {
                "label": "Restore checkpoint",
                "url": f"/api/baldur/cascade/checkpoint/?namespace={namespace}",
            },
        ],
    }


def cascade_depth_alert(
    namespace: str,
    cascade_id: str,
    current_depth: int,
    max_depth: int,
) -> dict[str, Any]:
    """
    Build a chain depth exceeded alert message.

    Args:
        namespace: namespace
        cascade_id: Cascade Event ID
        current_depth: current chain depth
        max_depth: maximum allowed depth

    Returns:
        Alert message dictionary
    """
    severity = "critical" if current_depth >= max_depth else "warning"
    emoji = "🔴" if severity == "critical" else "🟡"

    return {
        "title": f"{emoji} [{severity.upper()}] Cascade chain depth threshold reached - {namespace}",
        "severity": severity,
        "message": (
            f"Cascade chain depth reached the threshold in namespace '{namespace}'.\n"
            f"• Cascade ID: {cascade_id}\n"
            f"• Current depth: {current_depth}\n"
            f"• Maximum allowed: {max_depth}\n\n"
            "Possible circular reference or infinite loop."
        ),
        "details": {
            "namespace": namespace,
            "cascade_id": cascade_id,
            "current_depth": current_depth,
            "max_depth": max_depth,
            "timestamp": utc_now().isoformat(),
        },
        "actions": [
            {
                "label": "View Cascade details",
                "url": f"/api/baldur/cascade/events/{cascade_id}/?namespace={namespace}",
            },
        ],
    }


def cascade_load_shedding_alert(
    enabled: bool,
    current_load: float,
    threshold: float,
    dropped_count: int,
) -> dict[str, Any]:
    """
    Build a Load Shedding activation/deactivation alert message.

    Args:
        enabled: whether Load Shedding is active
        current_load: current load ratio (0.0~1.0)
        threshold: activation threshold
        dropped_count: number of dropped events

    Returns:
        Alert message dictionary
    """
    if enabled:
        return {
            "title": "🟡 [WARNING] Cascade Audit Load Shedding activated",
            "severity": "warning",
            "message": (
                f"Cascade Audit Load Shedding was activated due to system load.\n"
                f"• Current load: {current_load * 100:.1f}%\n"
                f"• Activation threshold: {threshold * 100:.1f}%\n"
                f"• Dropped events: {dropped_count}\n\n"
                "Low-priority events are being dropped."
            ),
            "details": {
                "enabled": enabled,
                "current_load": current_load,
                "threshold": threshold,
                "dropped_count": dropped_count,
                "timestamp": utc_now().isoformat(),
            },
        }
    return {
        "title": "🟢 [INFO] Cascade Audit Load Shedding deactivated",
        "severity": "info",
        "message": (
            f"System load returned to normal, so Load Shedding was deactivated.\n"
            f"• Current load: {current_load * 100:.1f}%\n"
            f"• Total dropped events: {dropped_count}"
        ),
        "details": {
            "enabled": enabled,
            "current_load": current_load,
            "dropped_count": dropped_count,
            "timestamp": utc_now().isoformat(),
        },
    }


def cascade_summary(
    namespace: str,
    date: str,
    total_events: int,
    events_by_trigger: dict[str, int],
    effects_by_action: dict[str, dict[str, int]],
    integrity_valid: bool,
    max_chain_depth: int,
) -> dict[str, Any]:
    """
    Build a daily Cascade Event summary alert message.

    Args:
        namespace: namespace
        date: summary date (YYYY-MM-DD)
        total_events: total number of Cascade Events
        events_by_trigger: event count per trigger
        effects_by_action: effect count per action (success/failure)
        integrity_valid: Hash Chain integrity status
        max_chain_depth: maximum chain depth

    Returns:
        Alert message dictionary
    """
    integrity_status = "✅ Valid" if integrity_valid else "❌ Violated"

    # per-trigger summary lines
    trigger_lines = []
    for trigger, count in sorted(events_by_trigger.items(), key=lambda x: -x[1]):
        trigger_lines.append(f"  • {trigger}: {count}")

    # per-action summary lines
    action_lines = []
    for action, stats in sorted(effects_by_action.items()):
        success = stats.get("success", 0)
        failure = stats.get("failure", 0)
        total = success + failure
        success_rate = (success / total * 100) if total > 0 else 0
        action_lines.append(f"  • {action}: {total} (success rate {success_rate:.1f}%)")

    return {
        "title": f"📊 [DAILY] Cascade Event daily summary - {namespace} ({date})",
        "severity": "info",
        "message": (
            f"Cascade Event summary for {date} in namespace '{namespace}'.\n\n"
            f"📈 **Total events**: {total_events}\n"
            f"🔗 **Max chain depth**: {max_chain_depth}\n"
            f"🔒 **Hash Chain integrity**: {integrity_status}\n\n"
            f"**Distribution by trigger**:\n" + "\n".join(trigger_lines) + "\n\n"
            "**Results by action**:\n" + "\n".join(action_lines)
        ),
        "details": {
            "namespace": namespace,
            "date": date,
            "total_events": total_events,
            "events_by_trigger": events_by_trigger,
            "effects_by_action": effects_by_action,
            "integrity_valid": integrity_valid,
            "max_chain_depth": max_chain_depth,
            "timestamp": utc_now().isoformat(),
        },
    }


def cascade_fallback_recovery_alert(
    recovered_count: int,
    failed_count: int,
    fallback_path: str,
) -> dict[str, Any]:
    """
    Build a local fallback recovery completion alert message.

    Args:
        recovered_count: number of recovered events
        failed_count: number of events that failed to recover
        fallback_path: fallback file path

    Returns:
        Alert message dictionary
    """
    if failed_count == 0:
        severity = "info"
        emoji = "🟢"
        status = "success"
    else:
        severity = "warning"
        emoji = "🟡"
        status = "partial success"

    return {
        "title": f"{emoji} [{severity.upper()}] Cascade local fallback recovery {status}",
        "severity": severity,
        "message": (
            f"Cascade Events saved to the local fallback were recovered.\n"
            f"• Recovered: {recovered_count}\n"
            f"• Failed: {failed_count}\n"
            f"• Fallback path: {fallback_path}"
        ),
        "details": {
            "recovered_count": recovered_count,
            "failed_count": failed_count,
            "fallback_path": fallback_path,
            "timestamp": utc_now().isoformat(),
        },
    }
