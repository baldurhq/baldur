"""
Degradation broadcast protocol and unified status query.

Provides inter-manager state synchronization via a lightweight Observer pattern.

DR-2 (Exception isolation): notify() isolates each observer's exception so that
one observer's failure does not interrupt the caller's execution flow.

DR-3 (Broadcast purpose): Broadcast is limited to state notification + unified
read-only query. Cascading state changes are not included in this scope.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import structlog

logger = structlog.get_logger()


@runtime_checkable
class DegradationObserver(Protocol):
    def on_degradation_changed(
        self,
        source: str,
        is_degraded: bool,
        level: str | None,
        reason: str,
    ) -> None: ...


class DegradationBroadcaster:
    """Degradation state change broadcaster.

    DR-2: notify() loop isolates each observer's exception via try-except,
    preventing one observer's failure from interrupting the caller's flow
    (Fail-safe Observer pattern).
    """

    _observers: list[DegradationObserver] = []

    @classmethod
    def register(cls, observer: DegradationObserver) -> None:
        if observer not in cls._observers:
            cls._observers.append(observer)

    @classmethod
    def unregister(cls, observer: DegradationObserver) -> None:
        try:
            cls._observers.remove(observer)
        except ValueError:
            pass

    @classmethod
    def notify(
        cls,
        source: str,
        is_degraded: bool,
        level: str | None,
        reason: str,
    ) -> None:
        for observer in cls._observers:
            try:
                observer.on_degradation_changed(source, is_degraded, level, reason)
            except Exception:
                logger.warning(
                    "degradation.observer_notify_failed",
                    observer=type(observer).__name__,
                    source=source,
                    exc_info=True,
                )

    @classmethod
    def reset(cls) -> None:
        """Clear all observers (for testing)."""
        cls._observers = []


class DegradationStatus:
    """Unified degradation status query across all managers."""

    @classmethod
    def get_unified_status(cls) -> dict[str, Any]:
        """Get consolidated degradation status from all managers."""
        from baldur.audit.graceful_degradation.degradation_manager import (
            HashChainDegradationManager,
        )

        from .degraded_mode import DegradedModeManager

        external_status = DegradedModeManager.get_instance().get_status()
        redis_status = HashChainDegradationManager._instance
        redis_status_dict = (
            redis_status.get_status()
            if redis_status
            else {
                "level": "normal",
                "is_degraded": False,
            }
        )

        external_degraded = external_status.get("degraded", False)
        redis_degraded = redis_status_dict.get("is_degraded", False)

        # Determine worst level
        worst_level = "normal"
        redis_level = redis_status_dict.get("level", "normal")
        level_severity = {"normal": 0, "degraded": 1, "emergency": 2, "readonly": 3}

        if external_degraded:
            worst_level = "degraded"

        if level_severity.get(redis_level, 0) > level_severity.get(worst_level, 0):
            worst_level = redis_level

        return {
            "external_backends": external_status,
            "redis_hashchain": redis_status_dict,
            "overall_degraded": external_degraded or redis_degraded,
            "worst_level": worst_level,
        }


__all__ = [
    "DegradationObserver",
    "DegradationBroadcaster",
    "DegradationStatus",
]
