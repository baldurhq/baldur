"""Degraded Mode Protocol — shared contract for degraded mode managers."""

from __future__ import annotations

from typing import Any, Protocol

__all__ = ["DegradedModeProtocol"]


class DegradedModeProtocol(Protocol):
    """Common contract for degraded mode state management.

    Two implementations exist with different domains:
    - DegradedModeHandler (core): Runtime config fallback when Command Center is down
    - DegradedModeManager (audit): Audit backend resilience when circuit breakers open

    Each implementation may return different key structures from get_status()
    since the domains are fundamentally different. The protocol defines
    get_status() -> dict[str, Any] as a loose contract.
    """

    @property
    def is_degraded(self) -> bool: ...

    def enter_degraded_mode(self, reason: str = "") -> None: ...

    def exit_degraded_mode(self) -> None: ...

    def get_status(self) -> dict[str, Any]: ...
