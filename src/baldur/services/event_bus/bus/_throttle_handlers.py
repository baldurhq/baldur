"""
Throttle EventBus integration handlers.

This module is an internal implementation of the baldur.services.event_bus.bus package.
"""

from __future__ import annotations

from typing import Any, cast

import structlog

from . import BaldurEvent

logger = structlog.get_logger()


def _as_any(throttle: object) -> Any:
    """Boundary helper — exposes PRO-impl introspection fields.

    The OSS ``AdaptiveThrottle`` Protocol intentionally omits internal state
    (``current_limit``, ``config``, etc.) because Protocol surface should
    stay minimal. EventBus handlers do impl-specific reads/writes by
    design — cast at the boundary so the Protocol stays tight.
    """
    return cast(Any, throttle)


def _emergency_severity(value: object) -> int:
    """Coerce an event ``level`` field to a numeric 0-3 severity.

    The EmergencyManager publishes ``level``/``previous_level`` as the
    EmergencyLevel ``.value`` strings ("normal", "level_2", ...).
    ``adjust_for_emergency`` is typed ``level: int`` and compares ``level >= 2``,
    so passing the raw string raised ``TypeError: '>=' not supported between
    instances of 'str' and 'int'`` — silently failing every throttle adjustment.
    Accepts int severity, EmergencyLevel, or the ``.value`` string; returns 0
    (NORMAL) on anything unparseable so the handler fails safe.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    try:
        from baldur.models.emergency import EmergencyLevel

        if isinstance(value, EmergencyLevel):
            return value.severity
        if isinstance(value, str):
            return EmergencyLevel(value).severity
    except Exception:
        pass
    return 0


def _on_emergency_level_changed_throttle(event: BaldurEvent) -> None:
    """
    Auto-adjust throttle limit on emergency level change.

    Calls AdaptiveThrottle.adjust_for_emergency() to apply
    limit multiplier and gradient freeze based on emergency level.

    Limit multiplier per emergency level:
    - NORMAL (0): 1.0 (full capacity, recovery)
    - LEVEL_1 (1): 0.8 (80% capacity)
    - LEVEL_2 (2): 0.5 (50% capacity)
    - LEVEL_3 (3): fixed at min_limit + gradient freeze
    """
    # Prevent circular reference: ignore self-sourced events
    if event.source == "throttle":
        return

    level = _emergency_severity(event.data.get("level", 0))
    previous_level = _emergency_severity(event.data.get("previous_level", 0))

    try:
        from baldur.factory.registry import ProviderRegistry

        throttle = ProviderRegistry.adaptive_throttle.safe_get()
        if throttle is None:
            raise RuntimeError("baldur_pro AdaptiveThrottle not registered")
        impl = _as_any(throttle)
        previous_limit = impl.current_limit

        # Unified handling via adjust_for_emergency method
        throttle.adjust_for_emergency(level)

        logger.info(
            "throttle.emergency_level_limit",
            previous_level=previous_level,
            throttle_level=level,
            previous_limit=previous_limit,
            throttle=impl.current_limit,
        )
    except ImportError:
        logger.debug("event_handler.throttle_module_unavailable")
    except Exception as e:
        logger.warning(
            "event_handler.adjust_throttle_emergency_failed",
            error=e,
        )


def _on_kill_switch_activated_throttle(event: BaldurEvent) -> None:
    """
    Pause throttle on kill switch activation.

    When the kill switch is activated, all automation must stop,
    so throttle is also fixed at min_limit.
    """
    # Prevent circular reference: ignore self-sourced events
    if event.source == "throttle":
        return

    try:
        from baldur.factory.registry import ProviderRegistry

        throttle = ProviderRegistry.adaptive_throttle.safe_get()
        if throttle is None:
            raise RuntimeError("baldur_pro AdaptiveThrottle not registered")
        impl = _as_any(throttle)
        previous_limit = impl.current_limit

        # Fix at min_limit
        impl.current_limit = impl.config.min_limit

        logger.warning(
            "throttle.kill_switch_activated_limit",
            previous_limit=previous_limit,
            throttle=impl.current_limit,
        )
    except ImportError:
        logger.debug("event_handler.throttle_module_unavailable")
    except Exception as e:
        logger.warning(
            "event_handler.adjust_throttle_kill_failed",
            error=e,
        )
