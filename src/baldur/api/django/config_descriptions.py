"""
Semantic Configuration Descriptions.

Provides business-friendly names for configuration fields
to enhance audit log readability and governance clarity.

Usage:
    from baldur.api.django.config_descriptions import (
        get_field_description,
        format_value_change,
    )

    # Get human-readable name
    label = get_field_description("jitter_enabled")
    # Returns: "Infrastructure Protection (Jitter)"

    # Format a value change for logging
    log_entry = format_value_change("jitter_max_delay_seconds", 60.0, 5.0)
    # Returns: "Jitter Delay Threshold: 60.0s → 5.0s"
"""

from typing import Any

# =============================================================================
# Configuration Field Descriptions
# =============================================================================
# Format: "field_name": ("Business-Friendly Label", "unit")
# Units: "bool", "seconds", "hours", "percent", "count", "multiplier", "dict", "list", "text"

CONFIG_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    # =========================================================================
    # Circuit Breaker - Fault Isolation
    # =========================================================================
    "enabled": ("Feature Toggle", "bool"),
    "failure_threshold": ("Circuit Breaker Failure Threshold", "count"),
    "recovery_timeout": ("Circuit Breaker Recovery Timeout", "seconds"),
    "success_threshold": ("Circuit Breaker Success Threshold", "count"),
    "half_open_max_calls": ("Half-Open Max Probe Calls", "count"),
    "rate_limit_cascade_threshold": ("Rate Limit Cascade Detection Threshold", "count"),
    "rate_limit_cascade_window_seconds": (
        "Rate Limit Cascade Detection Window",
        "seconds",
    ),
    "rate_limit_cascade_rate": ("Rate Limit Cascade Rate Threshold", "percent"),
    "rate_limit_cascade_minimum_calls": ("Rate Limit Cascade Minimum Calls", "count"),
    "self_ddos_protection_enabled": ("Self-DDoS Protection Toggle", "bool"),
    "self_ddos_rps_limit": ("Self-DDoS RPS Limit", "count"),
    "self_ddos_window_seconds": ("Self-DDoS Detection Window", "seconds"),
    "self_ddos_backoff_multiplier": ("Self-DDoS Backoff Multiplier", "multiplier"),
    # =========================================================================
    # DLQ - Dead Letter Queue
    # =========================================================================
    "max_retries": ("Maximum Retry Attempts", "count"),
    "retry_delay": ("Retry Delay Interval", "seconds"),
    "expiry_hours": ("Message Expiry Time", "hours"),
    "retention_days": ("Data Retention Period", "days"),
    "batch_size": ("Batch Processing Size", "count"),
    "max_replay_attempts": ("Maximum Replay Attempts", "count"),
    # =========================================================================
    # Retry - Backoff Strategy
    # =========================================================================
    "max_attempts": ("Maximum Retry Attempts", "count"),
    "backoff_strategy": ("Backoff Strategy Type", "text"),
    "backoff_base": ("Exponential Backoff Base", "multiplier"),
    "base_delay": ("Base Delay Duration", "seconds"),
    "max_delay": ("Maximum Delay Cap", "seconds"),
    "min_delay": ("Minimum Delay Floor", "seconds"),
    "jitter": ("Jitter Randomization Toggle", "bool"),
    "jitter_percent": ("Jitter Randomization Range", "percent"),
    # =========================================================================
    # SLA - Service Level Agreement
    # =========================================================================
    "default_hours": ("Default SLA Resolution Time", "hours"),
    "thresholds_by_domain": ("Domain-Specific SLA Thresholds", "dict"),
    # =========================================================================
    # Rate Limit Coordination
    # =========================================================================
    "default_retry_after": ("Default Retry-After Duration", "seconds"),
    "backoff_multiplier": ("Backoff Multiplier", "multiplier"),
    # =========================================================================
    # Idempotency
    # =========================================================================
    "default_cache_ttl": ("Default Cache TTL", "seconds"),
    "extended_cache_ttl": ("Extended Cache TTL", "seconds"),
    "clock_skew_tolerance_seconds": ("Clock Skew Tolerance", "seconds"),
    # =========================================================================
    # Security Thresholds
    # =========================================================================
    "temporary_ban_hours": ("Temporary Ban Duration", "hours"),
    "permanent_ban_threshold": ("Permanent Ban Threshold", "count"),
    "suspicious_ip_cache_timeout": ("Suspicious IP Cache Timeout", "seconds"),
    "injection_ban_hours": ("Injection Attack Ban Duration", "hours"),
    "suspicious_ip_cache_prefix": ("Suspicious IP Cache Key Prefix", "text"),
    "banned_ip_cache_prefix": ("Banned IP Cache Key Prefix", "text"),
    # =========================================================================
    # Forensic Context Limits
    # =========================================================================
    "error_message_max_length": ("Error Message Max Length", "count"),
    "response_body_max_length": ("Response Body Max Length", "count"),
    "user_agent_max_length": ("User-Agent Max Length", "count"),
    # =========================================================================
    # Metrics Collection (Infrastructure Protection)
    # =========================================================================
    "prefix": ("Metrics Prefix", "text"),
    "jitter_enabled": ("Infrastructure Protection (Jitter)", "bool"),
    "jitter_max_delay_seconds": ("Jitter Delay Threshold", "seconds"),
    # =========================================================================
    # Notifications
    # =========================================================================
    "channels": ("Notification Channels", "list"),
    "critical_threshold": ("Critical Alert Threshold", "count"),
    "warning_threshold": ("Warning Alert Threshold", "count"),
    "slack_block_text_limit": ("Slack Block Text Limit", "count"),
    "description_max_length": ("Description Max Length", "count"),
    "action_taken_max_length": ("Action Taken Max Length", "count"),
    "title_max_length": ("Title Max Length", "count"),
    "notification_timeout_seconds": ("Notification Timeout", "seconds"),
    "critical_channel": ("Critical Alert Channel", "text"),
    "high_channel": ("High Priority Alert Channel", "text"),
    "medium_channel": ("Medium Priority Alert Channel", "text"),
    # =========================================================================
    # Error Budget - SRE Governance
    # =========================================================================
    "threshold_healthy": ("Error Budget Healthy Level", "percent"),
    "threshold_caution": ("Error Budget Caution Level", "percent"),
    "threshold_warning": ("Error Budget Warning Level", "percent"),
    "threshold_critical": ("Error Budget Critical Level", "percent"),
    "burn_rate_fast_critical": ("Fast Burn Rate Critical Threshold", "multiplier"),
    "burn_rate_fast_warning": ("Fast Burn Rate Warning Threshold", "multiplier"),
    "burn_rate_slow_warning": ("Slow Burn Rate Warning Threshold", "multiplier"),
    "burn_rate_slow_info": ("Normal Burn Rate Baseline", "multiplier"),
    "failsafe_alert_enabled": ("Fail-Safe Alert Toggle", "bool"),
    "failsafe_cooldown_seconds": ("Fail-Safe Alert Cooldown", "seconds"),
    "heartbeat_enabled": ("Heartbeat (Dead Man's Snitch) Toggle", "bool"),
    "heartbeat_interval_seconds": ("Heartbeat Interval", "seconds"),
    "heartbeat_timeout_seconds": ("Heartbeat Timeout", "seconds"),
    "recovery_alert_enabled": ("Recovery Alert Toggle", "bool"),
    "recovery_alert_include_downtime": ("Include Downtime in Recovery Alert", "bool"),
    "escalation_enabled": ("Override Escalation Toggle", "bool"),
    "escalation_channel": ("Escalation Channel", "text"),
    "escalation_mention": ("Escalation Mention Targets", "text"),
    # =========================================================================
    # SLO Runtime Configuration
    # =========================================================================
    "default_window_days": ("Default SLO Window", "days"),
    "default_target": ("Default SLO Target", "percent"),
    "default_fast_burn_rate": ("Default Fast Burn Rate", "multiplier"),
    "default_slow_burn_rate": ("Default Slow Burn Rate", "multiplier"),
    "slos": ("SLO Definitions", "list"),
}


# =============================================================================
# Unit Formatters
# =============================================================================

# Simple suffix formatters: unit -> suffix
_UNIT_SUFFIXES: dict[str, str] = {
    "seconds": "s",
    "hours": "h",
    "days": "d",
    "percent": "%",
    "multiplier": "x",
}


def _format_collection(value: Any, empty_str: str, max_items: int = 3) -> str:
    """Format a dict or list with item count for large collections."""
    if not value:
        return empty_str
    if len(value) > max_items:
        bracket = "{" if isinstance(value, dict) else "["
        close = "}" if isinstance(value, dict) else "]"
        return f"{bracket}...{len(value)} items{close}"
    return str(value)


def _format_value_with_unit(value: Any, unit: str) -> str:
    """Format a value with its unit suffix."""
    if value is None:
        return "None"

    # Boolean formatting
    if unit == "bool":
        return "enabled" if value else "disabled"

    # Simple suffix formatting
    if unit in _UNIT_SUFFIXES:
        return f"{value}{_UNIT_SUFFIXES[unit]}"

    # Count (no suffix)
    if unit == "count":
        return str(value)

    # Collection formatting
    if unit == "dict":
        return (
            _format_collection(value, "{}") if isinstance(value, dict) else str(value)
        )

    if unit == "list":
        return (
            _format_collection(value, "[]") if isinstance(value, list) else str(value)
        )

    # Text formatting
    if unit == "text":
        return f'"{value}"' if value else '""'

    # Default
    return str(value)


# =============================================================================
# Public API
# =============================================================================


def get_field_description(field_name: str) -> str:
    """
    Get the business-friendly description for a config field.

    Args:
        field_name: The technical field name (e.g., "jitter_enabled")

    Returns:
        Human-readable label (e.g., "Infrastructure Protection (Jitter)")
    """
    if field_name in CONFIG_DESCRIPTIONS:
        return CONFIG_DESCRIPTIONS[field_name][0]
    # Fallback: Convert snake_case to Title Case
    return field_name.replace("_", " ").title()


def get_field_unit(field_name: str) -> str:
    """
    Get the unit type for a config field.

    Args:
        field_name: The technical field name

    Returns:
        Unit type (e.g., "seconds", "bool", "percent")
    """
    if field_name in CONFIG_DESCRIPTIONS:
        return CONFIG_DESCRIPTIONS[field_name][1]
    return "text"


def format_value_change(
    field_name: str,
    old_value: Any,
    new_value: Any,
) -> str:
    """
    Format a configuration value change for audit logging.

    Args:
        field_name: The technical field name
        old_value: Previous value
        new_value: New value

    Returns:
        Formatted string like "Jitter Delay Threshold: 60.0s → 5.0s"

    Note:
        This function is defensive - it will never raise an exception.
        If formatting fails, it falls back to basic string representation.
    """
    try:
        label = get_field_description(field_name)
        unit = get_field_unit(field_name)

        old_formatted = _format_value_with_unit(old_value, unit)
        new_formatted = _format_value_with_unit(new_value, unit)

        return f"{label}: {old_formatted} → {new_formatted}"
    except Exception:
        # Fallback: basic formatting that can't fail
        return f"{field_name}: {old_value} → {new_value}"


def format_changes_log(
    changes: dict[str, Any],
    previous_config: dict[str, Any] | None = None,
) -> list[str]:
    """
    Format multiple configuration changes for audit logging.

    Args:
        changes: Dictionary of field_name -> new_value
        previous_config: Dictionary of field_name -> old_value (optional)

    Returns:
        List of formatted change strings
    """
    formatted_lines = []

    for field_name, new_value in changes.items():
        old_value = previous_config.get(field_name) if previous_config else None
        formatted_lines.append(format_value_change(field_name, old_value, new_value))

    return formatted_lines


def format_changes_summary(
    changes: dict[str, Any],
    previous_config: dict[str, Any] | None = None,
) -> str:
    """
    Format changes as a single summary string for logging.

    Args:
        changes: Dictionary of field_name -> new_value
        previous_config: Dictionary of field_name -> old_value (optional)

    Returns:
        Multi-line formatted string

    Note:
        This function is defensive - it will never raise an exception.
        If formatting fails completely, it falls back to raw dict representation.
    """
    try:
        lines = format_changes_log(changes, previous_config)
        if not lines:
            return "No changes"
        return "\n  • " + "\n  • ".join(lines)
    except Exception:
        # Ultimate fallback: just stringify the dict
        return f"\n  changes={changes}"


__all__ = [
    "CONFIG_DESCRIPTIONS",
    "get_field_description",
    "get_field_unit",
    "format_value_change",
    "format_changes_log",
    "format_changes_summary",
]
