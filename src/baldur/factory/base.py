"""
GenericProviderRegistry[T] — type-safe generic registry for adapters.

Replaces the repetitive register_*/get_* pattern with a single generic class.
Each adapter type gets its own GenericProviderRegistry instance.

D3: auto_discover callback unifies 3 DCL variants.
D4: type[T] | Callable[..., T] unified provider signature.
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any, Generic, TypeVar

import structlog

from baldur.core.exceptions import AdapterNotFoundError

logger = structlog.get_logger()

T = TypeVar("T")

RegistrySnapshot = dict[str, Any]

__all__ = ["GenericProviderRegistry", "RegistrySnapshot"]


class GenericProviderRegistry(Generic[T]):
    """Type-safe generic registry for a single adapter type.

    Thread-safe via Double-Checked Locking. Supports lazy auto-discovery
    via callback (D3).

    Threading note: read-only dict operations (``in``, ``len``) rely on
    CPython GIL atomicity and intentionally omit locking for performance.
    If migrating to free-threaded Python (PEP 703), audit these paths.
    """

    def __init__(
        self,
        adapter_type: str,
        interface: type[T] | None = None,
        auto_discover: Callable[[], None] | None = None,
    ) -> None:
        self._providers: dict[str, type[T] | Callable[..., T]] = {}
        self._default: str | None = None
        self._adapter_type = adapter_type
        self._interface = interface
        self._auto_discover = auto_discover
        self._lock = threading.Lock()
        # Cached instances are stored in a ContextVar so test fixtures can
        # swap them per Context (450 D5). The ContextVar default is a
        # process-shared dict so threads that did not inherit the parent
        # Context (plain ``threading.Thread``) still see the same instances
        # — preserving the singleton-across-threads guarantee. Tests that
        # want isolation call ``self._instances_var.set(new_dict)`` (e.g.
        # via ``isolated_context``) to bind a fresh dict in their Context.
        self._shared_instances: dict[str, T] = {}
        self._instances_var: contextvars.ContextVar[dict[str, T]] = (
            contextvars.ContextVar(
                f"baldur.registry.{adapter_type}.instances",
                default=self._shared_instances,
            )
        )

    @property
    def _instances(self) -> dict[str, T]:
        """Cached-provider dict for the current Context.

        Returns the ContextVar value — the shared default dict in
        production, or whichever isolated dict a test fixture installed via
        ``ContextVar.set``. Reads/in-place mutations operate on the dict
        the current Context is bound to.
        """
        return self._instances_var.get()

    @_instances.setter
    def _instances(self, value: dict[str, T]) -> None:
        """Replace the cached-instance dict for the current Context.

        Used by ``restore_state`` and ``override``'s finally-block to swap
        in a previously captured snapshot. The new dict becomes the
        ContextVar value in the current Context only.
        """
        self._instances_var.set(value)

    def register(self, name: str, provider: type[T] | Callable[..., T]) -> None:
        """Register a provider class or factory function."""
        # No lock needed: CPython GIL makes dict/attr assignment atomic.
        # Registration is idempotent (dict overwrite). _default race is benign
        # (first-registered-wins semantics preserved by check-then-set).
        self._providers[name] = provider
        if self._default is None:
            self._default = name
        logger.debug(
            "registry.provider_registered",
            adapter_type=self._adapter_type,
            name=name,
        )

    def get(self, name: str | None = None) -> T:
        """Get or create a provider instance (DCL pattern).

        If name is None, uses the default provider.
        auto_discover callback is invoked if provider not found (D3).
        """
        name = name or self._default

        # Auto-discover if no name resolved or provider not found
        if (name is None or name not in self._providers) and self._auto_discover:
            self._auto_discover()
            # Re-resolve default after discovery (first registered becomes default)
            if name is None:
                name = self._default

        if name is None:
            raise AdapterNotFoundError(
                adapter_type=self._adapter_type,
                adapter_name="(no default set)",
            )

        # Fast path (no lock)
        if name in self._instances:
            return self._instances[name]

        with self._lock:
            # Double check
            if name in self._instances:
                return self._instances[name]

            if name not in self._providers:
                raise AdapterNotFoundError(
                    adapter_type=self._adapter_type,
                    adapter_name=name,
                )

            provider = self._providers[name]
            instance = provider() if callable(provider) else provider

            self._instances[name] = instance
            return instance

    def safe_get(self, name: str | None = None) -> T | None:
        """Same as :meth:`get`, but returns ``None`` when no provider is registered.

        Used by OSS->PRO boundary slots (519 D-c2) where the OSS install
        leaves the slot empty until PRO concretely registers a singleton.
        Callsites consume the result with a None-guard::

            if (svc := ProviderRegistry.<slot>.safe_get()) is not None:
                svc.method(...)

        Prefer :meth:`get` for slots that pre-register a NoOp default
        (e.g., ``governance``, ``pool_monitor``) — those never return None.
        """
        try:
            return self.get(name)
        except AdapterNotFoundError:
            return None

    def set_default(self, name: str) -> None:
        """Set the default provider name."""
        self._default = name

    def list_providers(self) -> list[str]:
        """List all registered provider names."""
        return list(self._providers.keys())

    def clear_instances(self) -> None:
        """Clear cached instances (providers remain registered)."""
        with self._lock:
            self._instances.clear()

    def reset(self) -> None:
        """Full reset — clear providers and instances."""
        with self._lock:
            self._providers.clear()
            self._instances.clear()
            self._default = None

    def has_provider(self, name: str) -> bool:
        """Check if a provider is registered."""
        return name in self._providers

    def invalidate_instance(self, name: str) -> None:
        """Invalidate cached instance (forces fresh creation on next get)."""
        with self._lock:
            self._instances.pop(name, None)

    def create_new(self, name: str | None = None) -> T:
        """Create a fresh provider instance without caching.

        Follows the same name resolution logic as get() — see Design Note.
        If name is None, uses the default provider.
        Raises AdapterNotFoundError if provider is not registered.
        """
        name = name or self._default
        if name is None or name not in self._providers:
            if self._auto_discover:
                self._auto_discover()
                if name is None:
                    name = self._default
            if name is None or name not in self._providers:
                raise AdapterNotFoundError(
                    adapter_type=self._adapter_type,
                    adapter_name=name or "(no default set)",
                )

        provider = self._providers[name]
        if callable(provider):
            return provider()
        return provider

    def get_provider(self, name: str | None = None) -> type[T] | Callable[..., T]:
        """Get registered provider without instantiation.

        Triggers auto-discover if provider not found.
        Raises AdapterNotFoundError if provider is not registered.
        """
        name = name or self._default
        if (name is None or name not in self._providers) and self._auto_discover:
            self._auto_discover()
            if name is None:
                name = self._default
        if name is None or name not in self._providers:
            raise AdapterNotFoundError(
                adapter_type=self._adapter_type,
                adapter_name=name or "(no default set)",
            )
        return self._providers[name]

    def has_instance(self, name: str) -> bool:
        """Check if a cached instance exists for the given name."""
        # No lock: single dict lookup is atomic under GIL (see class docstring).
        return name in self._instances

    def set_instance(self, name: str, instance: T) -> None:
        """Inject or replace a cached instance (thread-safe)."""
        with self._lock:
            self._instances[name] = instance

    def instance_count(self) -> int:
        """Return the number of cached instances."""
        # No lock: len(dict) is atomic under GIL (see class docstring).
        return len(self._instances)

    def get_default_name(self) -> str | None:
        """Return the current default provider name."""
        return self._default

    def has_any_providers(self) -> bool:
        """Check if any providers are registered."""
        return bool(self._providers)

    def get_cached_instances(self) -> dict[str, T]:
        """Return a snapshot of cached instances (name → instance).

        Returns a shallow copy — safe from concurrent modification.
        """
        with self._lock:
            return self._instances.copy()

    def save_state(self) -> RegistrySnapshot:
        """Snapshot current state for later restoration (test utility).

        Returns an opaque dict — pass to restore_state() without modification.
        Captures: _providers, _instances, _default, _auto_discover.

        Note: restores name-to-instance *mapping* only; does not revert
        internal state of cached instances. Use override() or mocking for
        full instance isolation.
        """
        with self._lock:
            return {
                "providers": self._providers.copy(),
                "instances": self._instances.copy(),
                "default": self._default,
                "auto_discover": self._auto_discover,
            }

    def restore_state(self, snapshot: RegistrySnapshot) -> None:
        """Restore state from a previous save_state() snapshot (test utility).

        Copies mutable containers so the snapshot remains reusable after
        the registry is mutated post-restore.
        """
        with self._lock:
            self._providers = snapshot["providers"].copy()
            self._instances = snapshot["instances"].copy()
            self._default = snapshot["default"]
            self._auto_discover = snapshot["auto_discover"]

    @contextmanager
    def snapshot(self) -> Generator[None, None, None]:
        """Save and auto-restore state on exit (test utility)."""
        state = self.save_state()
        try:
            yield
        finally:
            self.restore_state(state)

    def health_check(self) -> dict[str, bool]:
        """Check health of all instantiated providers."""
        results = {}
        for name, instance in self._instances.items():
            try:
                if hasattr(instance, "health_check"):
                    results[name] = instance.health_check()
                else:
                    results[name] = True
            except Exception:
                results[name] = False
        return results

    @contextmanager
    def override(self, mock_instance: T) -> Generator[None, None, None]:
        """Temporarily replace default provider with mock (test utility, D1)."""
        with self._lock:
            original_default = self._default
            original_instances = self._instances.copy()
            override_name = "__test_override__"

        try:
            with self._lock:
                self._instances[override_name] = mock_instance
                self._default = override_name
            yield
        finally:
            with self._lock:
                self._default = original_default
                self._instances = original_instances

    @contextmanager
    def isolated_context(self) -> Generator[GenericProviderRegistry[T], None, None]:
        """Fully isolated test context (D1)."""
        isolated = GenericProviderRegistry[T](
            adapter_type=self._adapter_type,
            interface=self._interface,
            auto_discover=self._auto_discover,
        )
        # Copy registrations but not instances
        isolated._providers = self._providers.copy()
        isolated._default = self._default
        yield isolated
