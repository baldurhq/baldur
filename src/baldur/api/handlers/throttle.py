"""
Framework-agnostic adaptive-throttle status handler.

Mirrors the ``bulkhead`` sibling: a pure ``RequestContext -> ResponseContext``
function with no Django/DRF imports. Surfaces the live v1.0 throttle runtime
state (``AdaptiveThrottle.get_stats()``) for the admin-console "Throttle" panel.
"""

from __future__ import annotations

from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.utils.time import utc_now

__all__ = ["throttle_status"]


def throttle_status(ctx: RequestContext) -> ResponseContext:
    """Get the live adaptive-throttle runtime state.

    Returns ``AdaptiveThrottle.get_stats()`` (current/min/max limit, request
    counters, gradient, and the nested emergency/governance/recovery maps),
    stamped with a freshness ``timestamp``. ``404`` when the PRO provider is
    absent (OSS-only checkout).
    """
    from baldur.factory.registry import ProviderRegistry

    throttle = ProviderRegistry.adaptive_throttle.safe_get()
    if throttle is None:
        return ResponseContext.not_found(
            "Adaptive throttle is unavailable (baldur_pro required)"
        )
    stats = throttle.get_stats()
    stats["timestamp"] = utc_now().isoformat()
    return ResponseContext.json(stats)
