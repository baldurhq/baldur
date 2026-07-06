"""Emergency Manager Interface (519 PR 2 / D-c1).

OSS-side Protocol for the PRO EmergencyModeManager singleton. PRO ships
the realized backend behind ``baldur_pro.services.emergency_mode``; OSS
callers resolve via ``ProviderRegistry.emergency_manager.safe_get()`` and
use the returned instance with a None-guard.

Methods are Interface Segregation — only those OSS code currently calls
are declared. ``get_current_level()``'s return is kept ``Any`` to avoid
coupling to PRO-owned ``EmergencyLevel`` (relocation tracked in PR 3 /
(d) track).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["EmergencyManager"]


@runtime_checkable
class EmergencyManager(Protocol):
    """Protocol for the PRO emergency mode manager."""

    def get_current_level(self) -> Any: ...

    def get_state(self) -> Any: ...

    def activate_auto(self, *args: Any, **kwargs: Any) -> Any: ...
