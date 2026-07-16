"""
Framework-agnostic bulkhead status handler.

Extracted from api/django/views/bulkhead.py — pure function with
no Django/DRF imports.
"""

from __future__ import annotations

from typing import Any

from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.utils.time import utc_now

__all__ = ["bulkhead_status"]


def bulkhead_status(ctx: RequestContext) -> ResponseContext:
    """Get all bulkhead states with optional name filter."""
    from baldur.services.bulkhead.registry import get_bulkhead_registry

    registry = get_bulkhead_registry()
    name_filter = ctx.get_query("name")

    states = registry.get_all_states()

    if name_filter:
        states = {k: v for k, v in states.items() if k == name_filter}
        if not states:
            return ResponseContext.not_found(f"Bulkhead '{name_filter}' not found")

    response_data = _build_response(states)
    return ResponseContext.json(response_data)


def _build_response(states: dict[str, Any]) -> dict[str, Any]:
    """Build bulkhead status response data."""
    bulkheads_data: dict[str, Any] = {}
    total_active = 0
    total_rejected = 0
    high_utilization: list[str] = []

    for name, state in states.items():
        bulkheads_data[name] = {
            "type": state.bulkhead_type.value,
            "max_concurrent": state.max_concurrent,
            "active_count": state.active_count,
            "waiting_count": state.waiting_count,
            "rejected_count": state.rejected_count,
            "available_permits": state.available_permits,
            "utilization_percent": round(state.utilization_percent, 2),
            "last_rejection_time": (
                state.last_rejection_time.isoformat()
                if state.last_rejection_time
                else None
            ),
        }

        total_active += state.active_count
        total_rejected += state.rejected_count

        if state.utilization_percent > 80:
            high_utilization.append(name)

    return {
        "bulkheads": bulkheads_data,
        "summary": {
            "total_bulkheads": len(states),
            "total_active": total_active,
            "total_rejected": total_rejected,
            "high_utilization": high_utilization,
        },
        "timestamp": utc_now().isoformat(),
    }
