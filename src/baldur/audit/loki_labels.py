"""
Loki/ELK standard label definitions.

Helper functions for log-system-friendly labeling.

Loki Label Best Practice:
- Prefer low cardinality
- Prefer fixed values (cluster, env, component)
- Put high-cardinality data in the log body, not in labels
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

# CASCADE_EVENT target action set (imported by throttle/audit.py)
CASCADE_EVENT_ACTIONS: set[str] = {
    "throttle_emergency_sync",
    "throttle_cb_sync",
    "throttle_sla_critical",
    "throttle_full_stop_activated",
    "throttle_full_stop_deactivated",
}


def get_standard_labels(audit_data: dict[str, Any]) -> dict[str, str]:
    """
    Extract standard labels from audit data.

    Loki Label Best Practice:
    - Low cardinality
    - Prefer fixed values (cluster, env, component)
    - Put high-cardinality data in the log body, not in labels

    Args:
        audit_data: Audit data dictionary

    Returns:
        Dictionary of Loki/ELK labels
    """
    cluster_info = audit_data.get("cluster", {})
    action = audit_data.get("action", "unknown")

    return {
        # Low-cardinality labels
        "job": "baldur-audit",
        "component": "throttle",
        "env": cluster_info.get("environment", "production"),
        "region": cluster_info.get("region", "unknown"),
        "cluster": cluster_info.get("cluster_id", "unknown"),
        # Event classification
        "audit_action": action,
        "severity": audit_data.get("severity", "info"),
        # Whether this is a CASCADE_EVENT
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
    Build standard labels for a throttle audit event.

    Args:
        action: Audit event type
        severity: Severity (debug, info, warning, critical)
        region: Region info
        cluster_id: Cluster ID
        environment: Environment (production, staging, development)

    Returns:
        Loki/ELK label dictionary
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
    Merge base labels with user-defined labels.

    User-defined labels override base labels.

    Args:
        base_labels: Base label dictionary
        custom_labels: User-defined labels (optional)

    Returns:
        Merged label dictionary
    """
    if custom_labels is None:
        return base_labels.copy()

    result = base_labels.copy()
    result.update(custom_labels)
    return result


def validate_labels(labels: dict[str, str]) -> tuple[bool, list[str]]:
    """
    Validate labels.

    Loki label rules:
    - Label name: only letters, digits and _ allowed
    - Label value: empty string not allowed

    Args:
        labels: Label dictionary to validate

    Returns:
        Tuple of (is_valid, list of errors)
    """
    import re

    errors = []
    label_name_pattern = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

    for name, value in labels.items():
        # Validate label name
        if not label_name_pattern.match(name):
            errors.append(f"Invalid label name: {name}")

        # Validate label value
        if not value or not isinstance(value, str):
            errors.append(f"Invalid label value for {name}: {value}")

    return len(errors) == 0, errors


def sanitize_label_value(value: str, max_length: int = 128) -> str:
    """
    Sanitize a label value.

    Strips special characters and applies a length limit.

    Args:
        value: Original label value
        max_length: Maximum length (default 128)

    Returns:
        Sanitized label value
    """
    if not value:
        return "unknown"

    # Replace special characters with underscore
    import re

    sanitized = re.sub(r"[^a-zA-Z0-9_\-]", "_", value)

    # Apply length limit
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length]

    return sanitized
