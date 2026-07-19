"""DLQ Service / Repository Interfaces (519 PR 2 / D-c1).

OSS-side Protocols for the PRO DLQ singletons. PRO ships the realized
implementations behind ``baldur_pro.services.dlq``; OSS callers resolve
via ``ProviderRegistry.dlq_service.safe_get()`` /
``ProviderRegistry.dlq_repository.safe_get()`` and use the returned
instance with a None-guard.

Methods are Interface Segregation — only those OSS code currently calls
are declared. Entry types use ``Any`` to avoid coupling to PRO-owned
``DLQEntry`` (relocation tracked in PR 3 / (d) track).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["DLQRepository", "DLQService"]


@runtime_checkable
class DLQService(Protocol):
    """Protocol for the PRO DLQ orchestrator."""

    def push(self, *args: Any, **kwargs: Any) -> Any: ...

    def store(self, *args: Any, **kwargs: Any) -> Any: ...

    def store_failure(self, *args: Any, **kwargs: Any) -> Any: ...

    # ``entry_id`` accepts both the PRO ``int`` PK and any string identifier;
    # widened so OSS callers (admin / xtest) don't need per-call casts.
    def get_entry(self, entry_id: Any) -> Any: ...

    def get_stats(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    def get_pending_entries(self, *args: Any, **kwargs: Any) -> list[Any]: ...

    def list_pending_entries(self, *args: Any, **kwargs: Any) -> list[Any]: ...

    def list_entries(self, *args: Any, **kwargs: Any) -> Any: ...

    def query_entries(self, *args: Any, **kwargs: Any) -> list[Any]: ...

    def get_sla_breached_entries(self, *args: Any, **kwargs: Any) -> list[Any]: ...

    def resolve_entry(self, *args: Any, **kwargs: Any) -> Any: ...

    def delete_entry(self, entry_id: Any) -> Any: ...

    def delete_by_source(self, *args: Any, **kwargs: Any) -> int: ...

    def archive_old_entries(self, *args: Any, **kwargs: Any) -> int: ...

    def count_archived_older_than(self, *args: Any, **kwargs: Any) -> int: ...

    def cleanup_old_entries(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    def purge_archived(self, *args: Any, **kwargs: Any) -> int: ...

    # PRO ``DLQService`` exposes the underlying repository as ``self.repository``
    # for admin/xtest direct-update flows. ``Any`` keeps the Protocol decoupled
    # from the PRO repository type.
    @property
    def repository(self) -> Any: ...


@runtime_checkable
class DLQRepository(Protocol):
    """Protocol for the PRO DLQ persistence layer."""

    def get(self, *args: Any, **kwargs: Any) -> Any: ...

    def list_pending(self, *args: Any, **kwargs: Any) -> list[Any]: ...

    def delete(self, *args: Any, **kwargs: Any) -> Any: ...

    def get_compressed_entries(self, *args: Any, **kwargs: Any) -> list[Any]: ...

    def get_compressed_entries_before(self, *args: Any, **kwargs: Any) -> list[Any]: ...

    def update_compressed_status(self, *args: Any, **kwargs: Any) -> Any: ...

    def release_stale_replaying(self, *args: Any, **kwargs: Any) -> int: ...
