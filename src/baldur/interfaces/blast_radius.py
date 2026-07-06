"""Blast Radius Manager Interface (519 PR 2 / D-c1).

OSS-side Protocol for the PRO BlastRadiusManager singleton. PRO ships the
realized backend behind
``baldur_pro.services.regional_emergency.blast_radius``; OSS callers
resolve via ``ProviderRegistry.blast_radius_manager.safe_get()`` and use
the returned instance with a None-guard.

Methods are Interface Segregation — only those OSS code currently calls
are declared. The OSS-side ``baldur.models.blast_radius`` already
provides the value types this manager exchanges with callers.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["BlastRadiusManager"]


@runtime_checkable
class BlastRadiusManager(Protocol):
    """Protocol for the PRO blast-radius manager singleton."""

    def check(self, *args: Any, **kwargs: Any) -> Any: ...

    def get_pending_approvals(self) -> list[Any]: ...

    def expire_pending_approvals(self, *args: Any, **kwargs: Any) -> int: ...
