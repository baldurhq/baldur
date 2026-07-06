"""Event filtering utilities for namespace-aware event handling."""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

__all__ = ["should_handle_emergency_event"]


def should_handle_emergency_event(event: Any) -> bool:
    """Filter Regional Emergency events by current region.

    Returns True if:
    - Global emergency (all pods should handle)
    - Regional emergency matching current pod's region
    - Event structure unknown (fail-open)

    Fail-open policy: filter failure -> process event (safe default).

    Args:
        event: Event object (must have `data` attribute or be a dict)

    Returns:
        True if this pod should handle the event, False otherwise
    """
    data = event.data if hasattr(event, "data") else event
    if not isinstance(data, dict):
        return True  # Unknown structure -> fail-open

    namespace = data.get("namespace", "global")
    if namespace == "global":
        return True  # Global -> all pods

    try:
        from baldur.core.cluster_identity import get_cluster_identity

        my_region = get_cluster_identity().region
        return my_region is None or my_region == namespace
    except Exception:
        return True  # ClusterIdentity failure -> fail-open
