"""Bulkhead Interfaces.

OSS-side Protocols for the PRO Bulkhead types. PRO ships the realized
backends behind ``baldur_pro.services.bulkhead``; OSS callers resolve
``BulkheadRegistry`` via ``ProviderRegistry.bulkhead_registry.safe_get()``
and consume the individual ``Bulkhead`` instances it returns by duck typing.
There is no remaining OSS static-annotation consumer of the ``Bulkhead``
Protocol — it is the structural type contract the PRO backends satisfy,
enforced by the PRO contract test rather than by an OSS annotation.

Methods are Interface Segregation — only those OSS code currently calls
are declared.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol, runtime_checkable

__all__ = ["Bulkhead", "BulkheadRegistry"]


@runtime_checkable
class BulkheadRegistry(Protocol):
    """Protocol for the PRO bulkhead registry singleton."""

    def get(self, *args: Any, **kwargs: Any) -> Any: ...

    def get_or_create(self, *args: Any, **kwargs: Any) -> Any: ...

    def list_names(self) -> list[str]: ...

    def get_all_states(self) -> dict[str, Any]: ...


@runtime_checkable
class Bulkhead(Protocol):
    """Protocol for an individual PRO bulkhead instance.

    The structural return-type contract that PRO's bulkhead backends
    (``SemaphoreBulkhead`` / ``ThreadPoolBulkhead``) satisfy. OSS callers
    receive these instances from ``BulkheadRegistry.get()`` /
    ``get_or_create()`` and use them by duck typing; there is no OSS
    static-annotation consumer. ``@runtime_checkable`` plus the PRO
    contract test enforce conformance.
    """

    @property
    def name(self) -> str: ...

    def try_acquire(self, timeout: float | None = None) -> bool: ...

    def acquire(self, timeout: float | None = None) -> AbstractContextManager[None]: ...

    def release(self) -> None: ...

    def get_state(self) -> Any: ...
