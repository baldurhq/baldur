"""
EventEmitterMixin — fail-safe event emission with TTL-based backoff.

Provides lazy EventBus initialization and non-critical event emission
for services that emit observability events. EventBus unavailability
does NOT affect business logic; events are silently dropped.

When EventBus initialization fails (e.g. Redis-backed bus with network
issues), the mixin applies TTL-based negative caching to prevent
repeated initialization attempts and log storms at enterprise scale.

Usage:
    class MyService(EventEmitterMixin):
        _event_source = "my_service"

        def do_work(self):
            self._emit_event(EventType.SOME_EVENT, data={"key": "value"})
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import structlog

logger = structlog.get_logger()

__all__ = [
    "EventEmitterMixin",
]

_UNAVAILABLE = object()

# Default retry interval when EventBus initialization fails (seconds).
_DEFAULT_RETRY_INTERVAL: float = 60.0


class EventEmitterMixin:
    """Fail-safe EventBus lazy initialization and event emission mixin.

    Subclasses MUST set ``_event_source`` to a string identifying the
    service in emitted events (e.g. ``"replay_service"``).

    Thread-safety: benign races on ``_event_bus`` assignment are
    acceptable — worst case is a redundant initialization, which the
    underlying ``get_event_bus()`` singleton handles safely.
    """

    _event_source: ClassVar[str] = ""
    """Override in subclass. Used as ``source`` parameter in bus.emit()."""

    _event_bus_retry_interval: ClassVar[float] = _DEFAULT_RETRY_INTERVAL
    """Seconds to wait before retrying EventBus initialization after failure."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Ensure each subclass gets its own instance-level slot
        # without interfering with other mixins' __init__.

    def _get_event_bus(self) -> Any:
        """Lazy EventBus getter with TTL-based negative caching.

        Returns the EventBus instance, or None if unavailable.
        After a failed initialization, retries are suppressed for
        ``_event_bus_retry_interval`` seconds to prevent log storms.
        """
        bus = getattr(self, "_event_bus", None)

        if bus is _UNAVAILABLE:
            fail_time = getattr(self, "_event_bus_fail_time", 0.0)
            if time.monotonic() - fail_time < self._event_bus_retry_interval:
                return None
            # TTL expired — allow retry
            bus = None

        if bus is not None:
            return bus

        try:
            from baldur.services.event_bus import get_event_bus

            self._event_bus = get_event_bus()
        except Exception as exc:
            logger.warning(
                "event_emitter.event_bus_initialization_failed",
                source=self._event_source or "event_emitter",
                error=str(exc),
            )
            self._event_bus = _UNAVAILABLE  # type: ignore[assignment]
            self._event_bus_fail_time = time.monotonic()
            return None

        return self._event_bus

    def _emit_event(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        priority: Any = None,
    ) -> None:
        """Emit an event via EventBus. Failure is non-critical.

        If EventBus is unavailable or ``bus.emit()`` raises, the error
        is logged at WARNING level and silently suppressed.
        Metrics are recorded for observability (emit skipped/failed).
        """
        bus = self._get_event_bus()
        if bus is None:
            self._record_emit_skipped()
            return
        try:
            kwargs: dict[str, Any] = {"data": data, "source": self._event_source}
            if priority is not None:
                kwargs["priority"] = priority
            bus.emit(event_type, **kwargs)
        except Exception as exc:
            self._record_emit_failed(event_type)
            logger.warning(
                "event_emitter.event_emit_failed",
                source=self._event_source or "event_emitter",
                event_type=event_type,
                error=str(exc),
            )

    def _record_emit_skipped(self) -> None:
        """Record metric for skipped emit (EventBus unavailable)."""
        try:
            from baldur.metrics.recorders.event_bus import record_emit_skipped

            record_emit_skipped(self._event_source or "event_emitter")
        except Exception:
            pass  # Metrics unavailable — silent fail

    def _record_emit_failed(self, event_type: str) -> None:
        """Record metric for failed emit (exception raised)."""
        try:
            from baldur.metrics.recorders.event_bus import record_emit_failed

            record_emit_failed(self._event_source or "event_emitter", event_type)
        except Exception:
            pass  # Metrics unavailable — silent fail
