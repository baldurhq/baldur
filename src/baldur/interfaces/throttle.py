"""Adaptive Throttle Interface (519 PR 2 / D-c1).

OSS-side Protocol for the PRO AdaptiveThrottle singleton. PRO ships the
realized backend behind ``baldur_pro.services.throttle``; OSS callers
resolve via ``ProviderRegistry.adaptive_throttle.safe_get()`` and use the
returned instance with a None-guard.

Methods are Interface Segregation — only those OSS code currently calls
are declared.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["AdaptiveThrottle"]


@runtime_checkable
class AdaptiveThrottle(Protocol):
    """Protocol for the PRO adaptive throttle singleton."""

    def adjust_for_emergency(self, *args: Any, **kwargs: Any) -> Any: ...

    def get_emergency_level(self) -> Any: ...

    def get_stats(self) -> dict[str, Any]: ...

    # OSS-side callers (DRF AdaptiveDRFThrottle bridge) use these three.
    # Returned objects expose `.allowed`, `.limit`, `.current_count`,
    # `.reset_at` per `baldur_pro.services.throttle.config.ThrottleResult`;
    # typed Any here keeps the OSS Protocol free of a PRO type dependency.
    def allow_request(self, ident: str) -> Any: ...

    def get_status(self, key: str) -> Any: ...

    def record_response(self, rtt_ms: float) -> None: ...
