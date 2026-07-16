"""
Bulkhead Registry - per-domain bulkhead management.

A registry that manages bulkheads per domain (ConnectionType or custom).

Usage:
    registry = get_bulkhead_registry()

    # Look up by ConnectionType
    db_bulkhead = registry.get(ConnectionType.DATABASE)

    # Look up by custom domain
    custom_bulkhead = registry.get_or_create("my_custom_domain")

    # Use
    with db_bulkhead.acquire():
        do_database_work()
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import structlog

from baldur.core.connection_health import ConnectionType
from baldur.services.bulkhead.async_semaphore import AsyncSemaphoreBulkhead
from baldur.services.bulkhead.base import Bulkhead, BulkheadState
from baldur.services.bulkhead.exceptions import BulkheadNotFoundError
from baldur.services.bulkhead.semaphore import SemaphoreBulkhead

if TYPE_CHECKING:
    from baldur.settings.bulkhead import BulkheadSettings

logger = structlog.get_logger()

__all__ = ["BulkheadRegistry", "get_bulkhead_registry", "reset_bulkhead_registry"]

# Settings-owned built-in compartments: the four ConnectionType values.
# These are protection compartments constructed from settings at registry
# creation — unregister is blocked, overwrite is warned.
_BUILTIN_BULKHEAD_NAMES = frozenset(ct.value for ct in ConnectionType)


class BulkheadRegistry:
    """
    Per-domain bulkhead registry.

    Integrates with the existing ConnectionType and also supports custom domains.

    Features:
    - Automatic registration of default bulkheads per ConnectionType
    - Custom domain support
    - Automatic creation of asynchronous bulkheads
    - Fine-grained bulkheads per DB alias / cache instance
    """

    def __init__(self, settings: BulkheadSettings | None = None):
        """
        Args:
            settings: Bulkhead settings. Uses defaults if None.
        """
        from baldur.settings.bulkhead import get_bulkhead_settings

        self._settings = settings or get_bulkhead_settings()
        self._bulkheads: dict[str, Bulkhead] = {}
        self._async_bulkheads: dict[str, AsyncSemaphoreBulkhead] = {}
        self._lock = threading.Lock()

        # Register default bulkheads based on ConnectionType
        self._register_default_bulkheads()

    def _build_thread_pool_bulkhead(
        self,
        name: str,
        max_workers: int,
        queue_size: int | None = None,
    ) -> Bulkhead:
        """
        Build a bulkhead for a thread-pool request — the tier seam.

        This base registry has no worker-pool implementation, so it falls
        back to semaphore isolation: capacity = ``max_workers`` (the
        conservative bound — a semaphore has no waiting-slot distinction, so
        ``queue_size`` is inert), no worker-pool offload, and any ``timeout``
        passed at execution bounds the admission wait only — not execution
        time. A registry overlay that ships a worker pool overrides this
        method to build the real thread-pool compartment.

        The WARNING fires once per name by construction: this builder runs
        only at compartment creation, and created compartments are cached.

        Args:
            name: Domain name
            max_workers: Requested worker count (mapped to semaphore capacity)
            queue_size: Requested wait-queue size (inert on the semaphore
                fallback; None lets a pool implementation use its own default)

        Returns:
            Bulkhead instance
        """
        logger.warning(
            "bulkhead_registry.thread_pool_unavailable",
            bulkhead_name=name,
            max_concurrent=max_workers,
            fallback="semaphore",
            semantics=(
                "semaphore isolation - no worker-pool offload; "
                "timeout bounds admission wait only, not execution"
            ),
        )
        return SemaphoreBulkhead(name=name, max_concurrent=max_workers)

    def _build_builtin_bulkheads(
        self, settings: BulkheadSettings
    ) -> dict[str, Bulkhead]:
        """Construct the settings-owned built-in compartments (single builder)."""
        return {
            ConnectionType.DATABASE.value: SemaphoreBulkhead(
                name=ConnectionType.DATABASE.value,
                max_concurrent=settings.database_max_concurrent,
            ),
            ConnectionType.CACHE.value: SemaphoreBulkhead(
                name=ConnectionType.CACHE.value,
                max_concurrent=settings.cache_max_concurrent,
            ),
            ConnectionType.EXTERNAL_API.value: self._build_thread_pool_bulkhead(
                name=ConnectionType.EXTERNAL_API.value,
                max_workers=settings.external_api_max_workers,
                queue_size=settings.external_api_queue_size,
            ),
            ConnectionType.MESSAGE_QUEUE.value: SemaphoreBulkhead(
                name=ConnectionType.MESSAGE_QUEUE.value,
                max_concurrent=settings.message_queue_max_concurrent,
            ),
        }

    def _register_default_bulkheads(self) -> None:
        """Register default bulkheads based on ConnectionType."""
        for name, bulkhead in self._build_builtin_bulkheads(self._settings).items():
            self._bulkheads[name] = bulkhead
            logger.debug(
                "bulkhead_registry.bulkhead_registered",
                bulkhead_name=name,
            )

    def get(self, name: str | ConnectionType) -> Bulkhead:
        """
        Look up a bulkhead.

        Args:
            name: Domain name or ConnectionType

        Returns:
            Bulkhead instance

        Raises:
            BulkheadNotFoundError: Unregistered domain. Subclasses ``KeyError``,
                so existing ``except KeyError`` consumers keep working; the
                message names the missing domain and lists the registered
                compartments.
        """
        key = name.value if isinstance(name, ConnectionType) else name

        with self._lock:
            if key not in self._bulkheads:
                # Read names directly under the held lock — list_names() would
                # re-enter the non-reentrant lock and deadlock.
                raise BulkheadNotFoundError(key, list(self._bulkheads.keys()))
            return self._bulkheads[key]

    def get_or_create(
        self,
        name: str,
        max_concurrent: int | None = None,
        bulkhead_type: str = "semaphore",
    ) -> Bulkhead:
        """
        Look up or create a bulkhead.

        Args:
            name: Domain name
            max_concurrent: Maximum concurrent execution count (default if None)
            bulkhead_type: "semaphore" or "thread_pool". A thread-pool request
                routes through the overridable builder seam — on the base
                registry it falls back to semaphore isolation (see
                ``_build_thread_pool_bulkhead``).

        Returns:
            Bulkhead instance
        """
        with self._lock:
            if name not in self._bulkheads:
                concurrent = max_concurrent or self._settings.default_max_concurrent
                if bulkhead_type == "thread_pool":
                    self._bulkheads[name] = self._build_thread_pool_bulkhead(
                        name=name,
                        max_workers=concurrent,
                    )
                else:
                    self._bulkheads[name] = SemaphoreBulkhead(
                        name=name,
                        max_concurrent=concurrent,
                    )
                logger.info(
                    "bulkhead_registry.created",
                    bulkhead_name=name,
                    bulkhead_type=bulkhead_type,
                )

            return self._bulkheads[name]

    def get_async(self, name: str | ConnectionType) -> AsyncSemaphoreBulkhead:
        """
        Look up an asynchronous bulkhead.

        Creates/returns the asynchronous version deriving its capacity from the
        synchronous twin. Strict: a domain with no synchronous twin is treated
        identically to ``get()`` — provisioning must precede the async lookup, so
        the async twin can never be a registry-invisible, default-capacity mint.

        Args:
            name: Domain name or ConnectionType

        Returns:
            AsyncSemaphoreBulkhead instance

        Raises:
            BulkheadNotFoundError: No synchronous twin for ``name``. Subclasses
                ``KeyError``; message lists the registered compartments.
        """
        key = name.value if isinstance(name, ConnectionType) else name

        with self._lock:
            if key not in self._async_bulkheads:
                sync_bh = self._bulkheads.get(key)
                if sync_bh is None:
                    # Read names directly under the held lock (lock-symmetry).
                    raise BulkheadNotFoundError(key, list(self._bulkheads.keys()))
                # Derive capacity from the synchronous twin.
                self._async_bulkheads[key] = AsyncSemaphoreBulkhead(
                    name=key,
                    max_concurrent=sync_bh.get_state().max_concurrent,
                )
            return self._async_bulkheads[key]

    def get_for_database(self, alias: str = "default") -> Bulkhead:
        """
        Return the bulkhead for a DB alias.

        Args:
            alias: Django DB alias (default, replica, analytics, etc.)

        Returns:
            The bulkhead for the given alias
        """
        key = f"database:{alias}"
        max_concurrent = self._settings.database_aliases.get(
            alias, self._settings.database_max_concurrent
        )
        return self.get_or_create(
            name=key,
            max_concurrent=max_concurrent,
            bulkhead_type="semaphore",
        )

    def get_for_cache(self, name: str = "default") -> Bulkhead:
        """
        Return the bulkhead for a cache instance.

        Args:
            name: Cache name (default, session, etc.)

        Returns:
            The bulkhead for the given cache
        """
        key = f"cache:{name}"
        max_concurrent = self._settings.cache_instances.get(
            name, self._settings.cache_max_concurrent
        )
        return self.get_or_create(
            name=key,
            max_concurrent=max_concurrent,
            bulkhead_type="semaphore",
        )

    def register(self, bulkhead: Bulkhead) -> None:
        """
        Register a custom bulkhead.

        Overwriting a built-in name (one of the four ConnectionType values) is
        allowed — it is the only path for swapping a built-in's implementation
        type and is load-bearing for the re-register capacity flow — but it
        permanently replaces a settings-owned protection compartment, so a
        WARNING is emitted to flag the footgun.

        The async twin for the same name is invalidated so async callees pick up
        the new capacity on next access.

        Args:
            bulkhead: Bulkhead to register
        """
        with self._lock:
            if bulkhead.name in _BUILTIN_BULKHEAD_NAMES:
                logger.warning(
                    "bulkhead_registry.builtin_overwritten",
                    bulkhead_name=bulkhead.name,
                )
            self._bulkheads[bulkhead.name] = bulkhead
            self._async_bulkheads.pop(bulkhead.name, None)
            logger.info(
                "bulkhead_registry.bulkhead_registered",
                bulkhead=bulkhead.name,
            )

    def unregister(self, name: str) -> bool:
        """
        Unregister a bulkhead.

        The async twin for the same name is invalidated alongside the sync entry.

        Args:
            name: Domain name

        Returns:
            True if unregistration succeeded, False if the name was not registered

        Raises:
            ValueError: ``name`` is a built-in compartment (one of the four
                ConnectionType values). Built-ins are settings-owned protection
                compartments — removing one silently degrades isolation, so the
                mutation is blocked rather than allowed.
        """
        with self._lock:
            if name in _BUILTIN_BULKHEAD_NAMES:
                raise ValueError(
                    f"Cannot unregister built-in bulkhead '{name}'. "
                    f"Built-in compartments "
                    f"{sorted(_BUILTIN_BULKHEAD_NAMES)} are settings-owned "
                    f"protection compartments."
                )
            if name in self._bulkheads:
                del self._bulkheads[name]
                self._async_bulkheads.pop(name, None)
                return True
            return False

    def get_all_states(self) -> dict[str, BulkheadState]:
        """Return all bulkhead states."""
        with self._lock:
            return {name: bh.get_state() for name, bh in self._bulkheads.items()}

    def list_names(self) -> list[str]:
        """Return all registered domain names."""
        with self._lock:
            return list(self._bulkheads.keys())


# =============================================================================
# Singleton — resolution chain
# =============================================================================

_registry: BulkheadRegistry | None = None
_registry_lock = threading.Lock()


def get_bulkhead_registry() -> BulkheadRegistry:
    """Return the active BulkheadRegistry — the resolution chain.

    Chain: the ``ProviderRegistry.bulkhead_registry`` slot when populated (a
    registry overlay registered by an extension package), else the lazily-built
    base singleton. The registry is the user-facing provisioning API
    (``get_or_create`` before ``@bulkhead`` on custom domains), so a single
    getter structurally prevents provisioning into an inactive registry.

    A provider registered into the slot MUST NOT itself consult the slot
    (its factory runs while the slot's non-reentrant lock is held — a
    re-entrant resolution fails loud with RuntimeError).
    """
    from baldur.factory.registry import ProviderRegistry

    slot_registry = ProviderRegistry.bulkhead_registry.safe_get()
    if slot_registry is not None:
        return slot_registry

    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = BulkheadRegistry()
    return _registry


def reset_bulkhead_registry() -> None:
    """Reset the base singleton (for testing).

    Clears only the chain's fallback leg; a populated provider slot is
    owned (and reset) by whoever registered it.
    """
    global _registry
    with _registry_lock:
        _registry = None
