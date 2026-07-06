"""Selfhealer Watchdog Interface (519 PR 2 / D-c1).

OSS-side Protocol for the PRO SelfhealerWatchdog singleton. PRO ships the
realized backend behind ``baldur_pro.services.meta_watchdog``; OSS callers
resolve via ``ProviderRegistry.selfhealer_watchdog.safe_get()`` and use
the returned instance with a None-guard.

Methods are Interface Segregation — only those OSS code currently calls
are declared. ``get_state()`` return is kept ``Any`` to avoid coupling to
PRO-owned ``WatchdogState`` (relocation tracked in PR 3 / (d) track).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["SelfhealerWatchdog"]


@runtime_checkable
class SelfhealerWatchdog(Protocol):
    """Protocol for the PRO selfhealer watchdog singleton."""

    def start(self, *args: Any, **kwargs: Any) -> Any: ...

    def stop(self, *args: Any, **kwargs: Any) -> Any: ...

    def get_state(self) -> Any: ...

    # ``_worker`` is intentionally public on the Protocol (despite the
    # leading underscore) because the OSS ``WatchdogShutdownHandler``
    # inspects it to determine drain completion. Declared ``Any`` to
    # avoid coupling to the PRO worker-thread type.
    _worker: Any
