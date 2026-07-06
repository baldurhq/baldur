"""
BaldurRuntime — scoped runtime context object.

Centralizes mutable state (settings caches, singleton instances, framework
flags) into a single object whose lifetime is governed by a module-level
``ContextVar``. Tests get strong isolation by running inside an isolated
``copy_context()`` or by swapping the runtime via :func:`set_runtime`; production
keeps the same ``BaldurRuntime`` instance for the whole process.

Reference: 450 (D1, D3, D4, D6).
"""

from __future__ import annotations

import contextvars
import os
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

import structlog

if TYPE_CHECKING:
    from pydantic_settings import BaseSettings

logger = structlog.get_logger()

S = TypeVar("S", bound="BaseSettings")

__all__ = [
    "BaldurRuntime",
    "current_runtime",
    "get_runtime",
    "is_production",
    "reset_runtime",
    "set_runtime",
]


class BaldurRuntime:
    """Process- or test-scoped runtime container.

    Holds the per-context state that used to live in ~13 ``_xxx_settings``
    module-level singletons and (after Phase 2) the make_singleton_factory
    instances. Each entry is created lazily through DCL (D6) so callers see
    the same object identity for the lifetime of the runtime instance.

    Threading: ``_lock`` guards both ``_settings`` and ``_singletons``. The
    ``get_settings`` fast path is a single dict lookup that relies on the
    GIL, matching :class:`baldur.factory.base.GenericProviderRegistry`.

    ``_lock`` is an ``RLock`` so a singleton's ``create_fn`` may transitively
    request another singleton (e.g. ``constraint_engine``'s create_fn calls
    ``get_dependency_graph()``) without deadlocking the per-runtime mutex.
    """

    __slots__ = (
        "_lock",
        "_settings",
        "_singletons",
        "is_production",
        "is_test_mode",
    )

    # Sentinel for "key not present" — distinguishes a legitimately cached
    # ``None``/``False`` singleton (e.g. ``create_fn`` that returned ``None``)
    # from a missing entry.
    _UNSET: Any = object()

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._settings: dict[type, Any] = {}
        self._singletons: dict[str, Any] = {}
        # Eager-read of BALDUR_TEST_MODE at runtime construction time (453 D5a).
        # The framework's "in-test" signal is read once here so production-init
        # code paths cannot couple a transient env read with cross-test global
        # state mutation. Tests must seed BALDUR_TEST_MODE *before* the runtime
        # is constructed (tests/testapp/settings.py module-level setdefault).
        self.is_test_mode: bool = (
            os.environ.get("BALDUR_TEST_MODE", "").lower() == "true"
        )
        # Eager-read of BALDUR_ENVIRONMENT at runtime construction time (463 D1).
        # Strict equality with "production" — no aliases, no DJANGO_SETTINGS_MODULE
        # substring fallback. Framework-agnostic detection. The four
        # legacy alias precedents are converged into this slot via D10; D15 hard-fails
        # known legacy values at startup so silent regressions cannot ship.
        self.is_production: bool = (
            os.environ.get("BALDUR_ENVIRONMENT", "").strip().lower() == "production"
        )

    # -- Settings -------------------------------------------------------------

    def get_settings(self, cls: type[S]) -> S:
        """Return a per-runtime cached instance of ``cls``.

        Pydantic ``BaseSettings`` subclasses read environment variables at
        construction time, so caching one instance per runtime preserves the
        previous "constructed once at first import" semantics while allowing
        tests to drop the cache by swapping the runtime.
        """
        cached = self._settings.get(cls)
        if cached is not None:
            return cached  # type: ignore[no-any-return]

        with self._lock:
            cached = self._settings.get(cls)
            if cached is not None:
                return cached  # type: ignore[no-any-return]
            instance = cls()
            self._settings[cls] = instance
            return instance

    def reset_settings(self, cls: type[S] | None = None) -> None:
        """Drop the cached settings instance(s).

        Passing ``None`` clears every cached settings class — used by the
        test-isolation runtime swap. Passing a specific class is the
        per-settings ``reset_xxx_settings()`` analogue.
        """
        with self._lock:
            if cls is None:
                self._settings.clear()
            else:
                self._settings.pop(cls, None)

    def set_settings(self, cls: type[S], value: S) -> None:
        """Override the cached settings instance — for tests that build a
        custom ``XxxSettings(field=value)`` and want subsequent ``get_xxx``
        callers to see the override."""
        with self._lock:
            self._settings[cls] = value

    # -- Singleton registry (Phase 2 surface) ---------------------------------

    def get_singleton(self, name: str, create_fn: Callable[[], Any]) -> Any:
        """Return a runtime-scoped singleton, creating it on first call.

        Used by ``make_singleton_factory`` after Phase 2 so all 70+
        ``get_xxx()`` accessors read from this dict instead of module-level
        closures.

        ``None`` and ``False`` are valid cached values — the lookup uses an
        ``_UNSET`` sentinel so a ``create_fn`` that returns ``None`` is still
        cached and not re-invoked on subsequent calls.
        """
        cached = self._singletons.get(name, self._UNSET)
        if cached is not self._UNSET:
            return cached

        with self._lock:
            cached = self._singletons.get(name, self._UNSET)
            if cached is not self._UNSET:
                return cached
            instance = create_fn()
            self._singletons[name] = instance
            return instance

    def set_singleton(self, name: str, value: Any) -> None:
        """Inject or replace a runtime-scoped singleton (configure_fn surface)."""
        with self._lock:
            self._singletons[name] = value

    def reset_singleton(self, name: str) -> tuple[bool, Any]:
        """Drop the cached singleton.

        Returns ``(was_present, old_value)`` — the boolean is required so
        callers can distinguish "never created" from "value was ``None``"
        when deciding whether to invoke the cleanup hook. Caller is
        responsible for invoking the cleanup (close/stop) outside the lock
        to avoid re-entrancy if the cleanup itself touches the runtime.
        """
        with self._lock:
            if name in self._singletons:
                return True, self._singletons.pop(name)
            return False, None

    def has_singleton(self, name: str) -> bool:
        return name in self._singletons


# =============================================================================
# Context-scoped runtime accessor
# =============================================================================


_runtime_var: contextvars.ContextVar[BaldurRuntime | None] = contextvars.ContextVar(
    "baldur.runtime", default=None
)

# Process-global default runtime. Plain ``threading.Thread`` workers do not
# inherit the parent's ContextVar values (PEP 567), so a Phase 2 singleton
# accessed from such a thread would otherwise lazy-create a *separate*
# runtime per thread and break the "exactly one instance" contract. Falling
# back to this slot keeps singletons process-global by default; tests opt
# into per-test isolation by calling :func:`set_runtime` (the ContextVar
# override takes precedence over this default).
_default_runtime: BaldurRuntime | None = None
_default_runtime_lock = threading.Lock()


def current_runtime() -> BaldurRuntime | None:
    """Return the active runtime, or ``None`` if none is set.

    Prefer :func:`get_runtime` for callers that need a runtime — this helper
    exists for diagnostics and test-isolation fixtures that want to detect
    "no runtime yet" without paying the lazy-create cost. Looks at the
    ContextVar slot only; the process-global default is intentionally
    invisible here.
    """
    return _runtime_var.get()


def get_runtime() -> BaldurRuntime:
    """Return the active runtime, lazily creating one if missing.

    Resolution order:

    1. The current Context's :data:`_runtime_var` (set by tests via
       :func:`set_runtime` or by ``copy_context()`` propagation).
    2. The process-global :data:`_default_runtime`, lazily created on first
       call.

    The two-tier design preserves the "singletons are process-global" guarantee
    relied on by background threads (which do not inherit ContextVar values)
    while keeping the test-isolation surface (``set_runtime`` + ContextVar
    override) intact.
    """
    global _default_runtime
    runtime = _runtime_var.get()
    if runtime is not None:
        return runtime

    cached = _default_runtime
    if cached is not None:
        return cached

    with _default_runtime_lock:
        if _default_runtime is None:
            _default_runtime = BaldurRuntime()
        return _default_runtime


def set_runtime(runtime: BaldurRuntime | None) -> contextvars.Token:
    """Install ``runtime`` as the active runtime in the current Context.

    Returns the ContextVar ``Token`` so callers can restore the previous
    value with :func:`contextvars.ContextVar.reset` — this is how test
    fixtures swap in an isolated runtime and restore it on teardown.
    """
    return _runtime_var.set(runtime)


def is_production() -> bool:
    """Return True iff ``BALDUR_ENVIRONMENT == "production"``.

    Single canonical production signal. Framework-agnostic — strict
    equality, no aliases, no ``DJANGO_SETTINGS_MODULE`` substring fallback.
    Read once at :class:`BaldurRuntime` construction (eager-read), so test
    fixtures that swap the runtime via :func:`set_runtime` get a fresh
    per-test signal.
    """
    return get_runtime().is_production


def reset_runtime() -> None:
    """Clear both the ContextVar slot and the process-global default runtime.

    Used by :func:`baldur.bootstrap.reset_init_state` so the next ``init()``
    call rebuilds the runtime from scratch. Tests that need scoped isolation
    should prefer :func:`set_runtime` with a fresh :class:`BaldurRuntime` —
    this entry point is the heavier "wipe everything" lever for full reset.
    """
    global _default_runtime
    _runtime_var.set(None)
    with _default_runtime_lock:
        _default_runtime = None
