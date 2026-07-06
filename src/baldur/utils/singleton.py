"""
Thread-safe singleton factory helper.

Replaces hand-rolled DCL (Double-Check Locking) singleton patterns with a
generic factory that delegates to :class:`baldur.runtime.BaldurRuntime`.

The triple ``(get_fn, configure_fn, reset_fn)`` returned by
:func:`make_singleton_factory` is the same shape as before — but each function
now reads/writes through the active ``BaldurRuntime``'s singleton store, so
test isolation, ``copy_context()`` scoping, and runtime swap-in just work.
"""

# Reference: 450 (D2 — Phase 2 delegation).

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

import structlog

T = TypeVar("T")

logger = structlog.get_logger()

_REGISTRY: dict[
    str, tuple[Callable[[], Any], Callable[[Any], None], Callable[..., None]]
] = {}


def CLEANUP_CLOSE(x: Any) -> None:
    x.close()


def CLEANUP_STOP(x: Any) -> None:
    x.stop()


def make_singleton_factory(
    name: str,
    create_fn: Callable[[], T],
    *,
    cleanup_fn: Callable[[T], None] | None = None,
) -> tuple[Callable[[], T], Callable[[T], None], Callable[..., None]]:
    """Create a runtime-scoped singleton factory.

    All three functions delegate to the active :class:`BaldurRuntime`:

    - ``get_fn()`` → ``runtime.get_singleton(name, create_fn)``
    - ``configure_fn(value)`` → ``runtime.set_singleton(name, value)``
    - ``reset_fn(*, cleanup=True)`` → ``runtime.reset_singleton(name)`` then
      ``cleanup_fn(old)`` outside the runtime lock.

    Returns:
        (get_fn, configure_fn, reset_fn) tuple.
        reset_fn accepts optional cleanup: bool = True keyword argument.
    """
    # get_runtime is re-resolved on every call rather than captured once in the
    # closure. A captured binding is permanently poisoned if this module's body
    # runs while baldur.runtime.get_runtime is patched (e.g. the first import of
    # a singleton-defining module happens inside a test's
    # ``patch("baldur.runtime.get_runtime")`` window): unittest.mock restores the
    # module attribute on exit but cannot restore an already-captured closure
    # cell, so the factory would keep calling the mock forever. A per-call lookup
    # always reflects the live attribute. The import stays function-local to
    # preserve the circular-dependency break: baldur.runtime imports nothing from
    # utils.singleton, but several baldur subpackages exercise this module at
    # import time before baldur.runtime has finished defining its accessors.

    def get_fn() -> T:
        from baldur.runtime import get_runtime

        return get_runtime().get_singleton(name, create_fn)  # type: ignore[no-any-return]

    def configure_fn(value: T) -> None:
        from baldur.runtime import get_runtime

        get_runtime().set_singleton(name, value)

    def reset_fn(*, cleanup: bool = True) -> None:
        from baldur.runtime import get_runtime

        was_present, old = get_runtime().reset_singleton(name)
        if was_present and cleanup and cleanup_fn is not None:
            try:
                cleanup_fn(old)
            except Exception:
                logger.warning("singleton.cleanup_failed", name=name, exc_info=True)

    triple = (get_fn, configure_fn, reset_fn)
    _REGISTRY[name] = triple
    return triple


__all__ = [
    "CLEANUP_CLOSE",
    "CLEANUP_STOP",
    "make_singleton_factory",
]
