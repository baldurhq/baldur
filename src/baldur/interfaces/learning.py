"""Learning Service Interface.

OSS-side Protocol for the private-tier LearningService singleton. The
realized backend ships in the private distribution; OSS callers resolve
via ``ProviderRegistry.learning_service.safe_get()`` and use the
returned instance with a None-guard.

Methods are Interface Segregation — only those OSS code currently calls
are declared (constraint-engine blacklist gates). The full service
surface (pattern learning, sessions, suggestions) stays private.

Status: Internal
"""

# Introduced per 599 D11 (TYPE_CHECKING retarget off the
# relocated service module).

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["LearningServiceProtocol"]


# OSS consumers of this Protocol: core/constraint_engine.py,
# services/settings_recommendation/service.py (DI annotations).
@runtime_checkable
class LearningServiceProtocol(Protocol):
    """Protocol for the private-tier self-learning service singleton."""

    def is_manual_only_mode(self, module: str) -> bool: ...

    def is_parameter_blocked(
        self,
        module: str,
        parameter: str,
        value: str,
    ) -> tuple[bool, Any]: ...
